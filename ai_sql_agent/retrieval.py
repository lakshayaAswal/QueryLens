"""Connection-scoped schema and successful-query retrieval."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any


logger = logging.getLogger(__name__)
_DIMENSIONS = 384
_SCHEMA_VERSION = "2"
_CLIENT_STARTUP = Lock()
_TERMS = (
    {"customer", "client", "buyer", "account", "user", "member", "subscriber"},
    {"revenue", "sale", "total", "amount", "price", "spend", "income", "booking"},
    {"order", "purchase", "transaction", "invoice", "payment", "checkout"},
    {"product", "item", "sku", "catalog", "merchandise"},
    {"region", "territory", "market", "country", "state", "city", "location", "geography"},
    {"year", "month", "week", "day", "date", "time", "period", "quarter", "annual", "calendar"},
    {"quantity", "count", "volume", "unit", "number"},
    {"status", "stage", "state", "completed", "open", "pending", "canceled", "cancelled"},
    {"employee", "staff", "worker", "team", "department"},
    {"cost", "expense", "fee", "tax", "overhead"},
    {"growth", "change", "trend", "increase", "decrease", "compare"},
)
_STOP_WORDS = {
    "a", "an", "the", "any", "and", "or", "from", "by", "to", "of", "please", "show", "me",
    "their", "this", "that", "these", "were", "with", "for", "did", "in", "on", "is", "was",
    "how", "what", "when", "where", "which", "has", "have", "had", "table", "column",
}


@dataclass(frozen=True)
class RetrievedContext:
    table_names: tuple[str, ...]
    examples: tuple[dict[str, str], ...]


def _word_tokens(value: str) -> list[str]:
    split_case = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return [token for token in re.findall(r"[^\W_]+", split_case.lower()) if token not in _STOP_WORDS]


def _lemma(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith(("ss", "us", "is")) and len(token) > 3:
        return token[:-1]
    return token


def _embed(value: str) -> list[float]:
    """Embed identifiers and business concepts locally without transmitting schemas."""
    tokens = [_lemma(token) for token in _word_tokens(value)]
    values = [0.0] * _DIMENSIONS
    weights = {f"term:{token}": 1.0 for token in tokens}
    for first, second in zip(tokens, tokens[1:]):
        weights[f"bigram:{first}:{second}"] = 0.5
    for group_index, group in enumerate(_TERMS):
        if group.intersection(tokens):
            weights[f"concept:{group_index}"] = 0.8
    for feature, weight in weights.items():
        digest = hashlib.blake2b(feature.encode(), digest_size=4).digest()
        bucket = int.from_bytes(digest[:3], "big") % _DIMENSIONS
        values[bucket] += weight if digest[3] % 2 else -weight
    norm = math.sqrt(math.fsum(value * value for value in values))
    if norm:
        return [value / norm for value in values]
    values[-1] = 1.0
    return values


def _table_text(table: Mapping[str, Any]) -> str:
    columns = ", ".join(f"{col['name']} {col['type']}" for col in table.get("columns", ()))
    related = ", ".join(key["referred_table"] for key in table.get("foreign_keys", ()))
    return f"{table['name']} {table['name']}\n{columns}\nRelated tables: {related}"


def _rank_embeddings(embedding: list[float], tables: Mapping[str, list[float]], limit: int) -> tuple[str, ...]:
    ordered = sorted(
        tables,
        key=lambda name: (-math.fsum(a * b for a, b in zip(embedding, tables[name])), name.casefold()),
    )
    return tuple(ordered[:limit])


def rank_tables(schema: Mapping[str, Any], question: str, limit: int = 10) -> tuple[str, ...]:
    tables = {table["name"]: _embed(_table_text(table)) for table in schema.get("tables", ())}
    return _rank_embeddings(_embed(question), tables, limit)


def _normalized_sql(sql: str, dialect: str) -> str:
    from sqlglot import parse_one
    from sqlglot.errors import SqlglotError

    try:
        expression = parse_one(sql, read={"postgresql": "postgres"}.get(dialect, dialect))
        return expression.sql(normalize=True, comments=False)
    except (SqlglotError, ValueError):
        return sql.strip().rstrip(";")


class SemanticIndex:
    """Use a private Chroma collection for one owner, connection and schema version."""

    def __init__(
        self,
        namespace: str,
        schema: Mapping[str, Any],
        *,
        owner: str,
        persist_path: str | Path | None = None,
        examples: Mapping[str, str] | None = None,
    ) -> None:
        if not owner or not namespace:
            raise ValueError("Semantic retrieval requires an owner and a database namespace.")
        canonical_schema = json.dumps(schema, sort_keys=True, ensure_ascii=False, default=str)
        scope = f"{_SCHEMA_VERSION}\x00{owner}\x00{namespace}\x00{canonical_schema}"
        self.collection_name = "sql_agent_" + hashlib.sha256(scope.encode()).hexdigest()[:48]
        self._dialect = str(schema.get("dialect", ""))
        self._persist_path = persist_path
        self._tables = {table["name"]: _embed(_table_text(table)) for table in schema.get("tables", ())}
        self._client = None
        self._collection = None
        self.last_error: str | None = None
        try:
            import chromadb
            from chromadb.config import Settings

            settings = Settings(anonymized_telemetry=False)
            # Chroma's shared client registers its runtime before startup is complete.
            with _CLIENT_STARTUP:
                if persist_path is None:
                    self._client = chromadb.EphemeralClient(settings=settings)
                else:
                    path = Path(persist_path).expanduser().resolve()
                    path.mkdir(parents=True, mode=0o700, exist_ok=True)
                    self._client = chromadb.PersistentClient(path=str(path), settings=settings)
            self._collection = self._client.get_or_create_collection(
                self.collection_name, embedding_function=None, metadata={"hnsw:space": "cosine"}
            )
            records = list(schema.get("tables", ()))
            for offset in range(0, len(records), 256):
                batch = records[offset : offset + 256]
                self._collection.upsert(
                    ids=["table-" + hashlib.sha256(table["name"].encode()).hexdigest() for table in batch],
                    embeddings=[self._tables[table["name"]] for table in batch],
                    documents=[_table_text(table) for table in batch],
                    metadatas=[{"kind": "table", "name": table["name"]} for table in batch],
                )
            for question, sql in (examples or {}).items():
                self.remember(question, sql)
        except Exception as exc:
            self._degrade(exc)

    @property
    def available(self) -> bool:
        return self._collection is not None

    def _degrade(self, error: Exception) -> None:
        logger.warning("Semantic index unavailable (%s)", type(error).__name__)
        self.last_error = "ChromaDB is unavailable. Schema selection is using local matching."
        self._collection = None

    def remember(self, question: str, sql: str) -> None:
        if self._collection is None or len(sql) > 12_000:
            return
        canonical = _normalized_sql(sql, self._dialect)
        identity = "query-" + hashlib.sha256(canonical.encode()).hexdigest()
        try:
            self._collection.upsert(
                ids=[identity],
                embeddings=[_embed(f"{question}\n{sql[:5000]}")],
                documents=[question[:3000]],
                metadatas=[{"kind": "example", "question": question[:3000], "sql": sql[:12000]}],
            )
        except Exception as exc:
            self._degrade(exc)

    def query(self, question: str, *, table_limit: int = 10, example_limit: int = 3) -> RetrievedContext:
        embedding = _embed(question)
        if self._collection is not None:
            try:
                tables = self._query_records(embedding, "table", table_limit)
                examples = self._query_records(embedding, "example", example_limit)
                return RetrievedContext(
                    tuple(item["name"] for item in tables),
                    tuple({"question": item["question"], "sql": item["sql"]} for item in examples),
                )
            except Exception as exc:
                self._degrade(exc)
        return RetrievedContext(_rank_embeddings(embedding, self._tables, table_limit), ())

    def _query_records(self, embedding: list[float], kind: str, limit: int) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        limit = min(limit, self._collection.count())
        if not limit:
            return []
        result = self._collection.query(
            query_embeddings=[embedding], n_results=limit, where={"kind": kind}, include=["metadatas"]
        )
        return [item for item in (result.get("metadatas") or [[]])[0] if item]

    def close(self) -> None:
        """Release the local Chroma handle and any ephemeral collection."""
        client = self._client
        if client is not None:
            if self._persist_path is None:
                try:
                    client.delete_collection(self.collection_name)
                except Exception as exc:
                    logger.debug("Could not release semantic collection (%s)", type(exc).__name__)
            close_client = getattr(client, "close", None)
            if close_client is not None:
                try:
                    with _CLIENT_STARTUP:
                        close_client()
                except Exception as exc:
                    logger.debug("Could not close semantic client (%s)", type(exc).__name__)
        self._collection = None
        self._client = None
