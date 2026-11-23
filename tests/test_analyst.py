from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ai_sql_agent.ai import Report
from ai_sql_agent.analyst import Analyst
from ai_sql_agent.database import ConnectionConfig, Database
from ai_sql_agent.demo import ensure_demo_database
from ai_sql_agent.retrieval import SemanticIndex
from ai_sql_agent.sql_safety import UnsafeQueryError


class RecordingProvider:
    def __init__(self, sql):
        self.sql = sql
        self.calls = []
        self.evidence = None

    def plan(self, question, schema, history, examples, *, previous_sql="", error=""):
        self.calls.append((previous_sql, error))
        return "SELECT name FROM products ORDER BY id LIMIT 2" if error else self.sql

    def analyze(self, question, sql, evidence):
        self.evidence = evidence
        return Report("The query returned two products.")


class AnalystTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = ensure_demo_database(Path(self.directory.name) / "sample.sqlite")
        self.database = Database(ConnectionConfig(dialect="sqlite", database=str(path)))
        self.addCleanup(self.database.close)
        self.schema = self.database.discover()

    def test_repairs_unknown_column_and_sends_aggregate_evidence_by_default(self):
        provider = RecordingProvider("SELECT missing_column FROM products")
        result = Analyst(self.database, self.schema, provider).ask("Show two products")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(list(result.frame.columns), ["name"])
        self.assertEqual(len(result.frame), 2)
        self.assertEqual(len(result.repair_notes), 1)
        self.assertNotIn("sample", provider.evidence)

    def test_rejects_mutating_query_without_repair(self):
        provider = RecordingProvider("UPDATE products SET unit_price = 0")
        with self.assertRaises(UnsafeQueryError):
            Analyst(self.database, self.schema, provider).ask("Show prices")
        self.assertEqual(provider.calls, [("", "")])
        self.assertIsNone(provider.evidence)

    def test_concurrent_indexes_keep_examples_separate_and_deduplicated(self):
        path = Path(self.directory.name) / "vectors"

        def round_trip(owner):
            index = SemanticIndex(self.database.namespace, self.schema, owner=owner, persist_path=path)
            try:
                self.assertTrue(index.available, index.last_error)
                index.remember(owner, "select 1")
                index.remember(owner, "SELECT 1")
                self.assertEqual(index.query(owner).examples, ({"question": owner, "sql": "SELECT 1"},))
                return index.collection_name
            finally:
                index.close()

        with ThreadPoolExecutor() as workers:
            collections = list(workers.map(round_trip, ("analyst one", "analyst two")))
        self.assertEqual(len(set(collections)), 2)
