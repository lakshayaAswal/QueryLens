import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_sql_agent.database import ConnectionConfig, Database, DatabaseError
from ai_sql_agent.sql_safety import UnsafeQueryError, prepare_query


class SqlSafetyTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "sales & regions?.sqlite"
        with sqlite3.connect(path) as connection:
            connection.executescript("""
                CREATE TABLE regions (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY,
                    region_id INTEGER REFERENCES regions(id),
                    amount REAL
                );
                INSERT INTO regions VALUES (1, 'West'), (2, 'South');
                INSERT INTO orders VALUES (1, 1, 25), (2, 2, 100), (3, 1, 50);
                CREATE VIEW totals AS SELECT region_id, SUM(amount) AS revenue
                FROM orders GROUP BY region_id;
            """)
        connection.close()
        self.database = Database(ConnectionConfig("sqlite", database=str(path)), max_rows=2)
        self.addCleanup(self.database.close)
        self.schema = self.database.discover()

    def test_schema_reports_keys_and_views(self):
        tables = {entry["name"]: entry for entry in self.schema["tables"]}
        self.assertEqual(tables["orders"]["primary_key"], ["id"])
        self.assertEqual(tables["orders"]["foreign_keys"][0]["referred_table"], "regions")
        self.assertEqual(tables["totals"]["kind"], "view")
        self.assertEqual(self.database.execute("SELECT revenue FROM totals ORDER BY revenue").frame.values.tolist(),
                         [[75.0], [100.0]])

    def test_limit_preserves_order_offset_and_smaller_explicit_caps(self):
        results = self.database.execute("SELECT id FROM orders ORDER BY id DESC")
        self.assertTrue(results.truncated)
        self.assertEqual(results.frame["id"].tolist(), [3, 2])
        results = self.database.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 1 OFFSET 1")
        self.assertFalse(results.truncated)
        self.assertEqual(results.frame["id"].tolist(), [2])

    def test_supports_common_ctes_and_case_expressions(self):
        results = self.database.execute(
            "WITH Totals AS (SELECT SUM(CASE WHEN amount > 25 OR id = 1 THEN amount ELSE 0 END) AS Amount "
            "FROM orders) SELECT Amount FROM totals; -- end"
        )
        self.assertEqual(results.frame["Amount"].tolist(), [175.0])

    def test_rejects_operations_outside_read_policy(self):
        for sql in (
            "UPDATE orders SET amount = 0",
            "SELECT id FROM orders; DELETE FROM orders",
            "SELECT * FROM other.orders",
            "SELECT * FROM pragma_database_list()",
            "SELECT load_extension('test')",
            "SELECT * FROM orders FOR UPDATE",
            "SELECT * FROM orders INTO archive",
        ):
            with self.subTest(sql=sql), self.assertRaises(UnsafeQueryError):
                self.database.execute(sql)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM orders").frame.iat[0, 0], 3)

    def test_missing_tables_and_columns_are_repairable(self):
        with self.assertRaises(UnsafeQueryError) as missing_table:
            self.database.execute("SELECT * FROM order")
        self.assertTrue(missing_table.exception.retryable)
        with self.assertRaises(DatabaseError) as missing_column:
            self.database.execute("SELECT quantity FROM orders")
        self.assertTrue(missing_column.exception.retryable)

    def test_repairable_parse_error_excludes_mutations_and_extra_statements(self):
        cases = {
            "SELECT id FROM orders WHERE = 1": True,
            "SELECT id FROM orders; DELETE FROM": False,
            "WITH changed AS (UPDATE orders SET) SELECT * FROM changed": False,
            "SELECT * FROM other.orders": False,
        }
        for sql, retryable in cases.items():
            with self.subTest(sql=sql), self.assertRaises(UnsafeQueryError) as rejected:
                self.database.execute(sql)
            self.assertEqual(rejected.exception.retryable, retryable)

    def test_sqlite_statement_time_limit(self):
        self.database.timeout_seconds = 1
        with self.assertRaisesRegex(DatabaseError, "timed out"):
            self.database.execute(
                "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 100000000) "
                "SELECT SUM(n) FROM seq"
            )

    def test_mysql_and_postgresql_dialect_output(self):
        mysql = prepare_query(
            "SELECT DATE_FORMAT(created_at, '%Y-%m') FROM orders LIMIT 4, 100",
            "mysql", schema="analytics", tables=["orders"], row_limit=2,
        )
        self.assertIn("DATE_FORMAT(created_at, '%Y-%m')", mysql)
        self.assertIn("`analytics`.orders LIMIT 3 OFFSET 4", mysql)
        pg = prepare_query(
            "SELECT DATE_TRUNC('month', created_at) FROM orders FETCH FIRST 1 ROW ONLY",
            "postgresql", schema="analytics", tables=["orders"], row_limit=2,
        )
        self.assertIn("DATE_TRUNC('MONTH', created_at)", pg)
        self.assertIn('"analytics".orders LIMIT 1', pg)


if __name__ == "__main__":
    unittest.main()
