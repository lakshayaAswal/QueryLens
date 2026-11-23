"""Connection and session configuration shared by the Streamlit views."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from .ai import AI_PROVIDERS, Provider, SampleProvider, create_provider
from .analyst import Analyst
from .database import ConnectionConfig, Database
from .demo import DEFAULT_QUESTION, EXAMPLE_QUERIES, ensure_demo_database
from .retrieval import SemanticIndex

PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("QUERYLENS_DATA_DIR", str(PROJECT_DIR / ".querylens"))).expanduser()
REQUIRE_LOGIN = os.environ.get("QUERYLENS_REQUIRE_LOGIN", "").lower() in {"1", "true", "yes"}
DIALECT_LABELS = {"sqlite": "SQLite", "postgresql": "PostgreSQL", "mysql": "MySQL"}
PORTS = {"postgresql": 5432, "mysql": 3306}
SQLITE_HEADER = b"SQLite format 3\x00"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@dataclass
class ActiveConnection:
    database: Database
    label: str
    schema: dict
    sample: bool = False
    upload_dir: tempfile.TemporaryDirectory | None = field(default=None, repr=False)
    index: SemanticIndex | None = field(default=None, repr=False)
    snapshot_id: str | None = field(default=None, repr=False)

    @property
    def tables(self) -> list[dict]:
        return self.schema["tables"]

    @property
    def dialect(self) -> str:
        return self.database.dialect

    @property
    def namespace(self) -> str:
        return self.snapshot_id or self.database.namespace

    def close(self) -> None:
        if self.index is not None:
            self.index.close()
        self.database.close()
        if self.upload_dir is not None:
            self.upload_dir.cleanup()


def _db_options() -> dict[str, int]:
    return {
        "timeout_seconds": int(os.environ.get("QUERYLENS_TIMEOUT_SECONDS", "10")),
        "max_rows": int(os.environ.get("QUERYLENS_MAX_ROWS", "1000")),
    }


def open_connection(
    config: ConnectionConfig,
    label: str,
    *,
    sample: bool = False,
    upload_dir: tempfile.TemporaryDirectory | None = None,
) -> ActiveConnection:
    database = Database(config, **_db_options())
    try:
        schema = database.discover()
    except Exception:
        database.close()
        raise
    return ActiveConnection(database, label, schema, sample, upload_dir)


def open_demo() -> ActiveConnection:
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = ensure_demo_database(DATA_DIR / "demo" / "sample-commerce.sqlite3")
    return open_connection(ConnectionConfig(dialect="sqlite", database=str(path)), "Sample Commerce", sample=True)


def open_uploaded(upload: BinaryIO, name: str, size: int) -> ActiveConnection:
    upload.seek(0)
    if not 0 < size <= MAX_UPLOAD_BYTES or upload.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
        raise ValueError("Upload a valid SQLite database file of up to 50 MB.")
    upload.seek(0)
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = tempfile.TemporaryDirectory(prefix="querylens-", dir=DATA_DIR)
    path = Path(temporary.name) / "database.sqlite3"
    digest = hashlib.sha256(b"querylens-sqlite-snapshot\0")
    try:
        with path.open("wb") as output:
            while chunk := upload.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        config = ConnectionConfig(dialect="sqlite", database=str(path))
        connection = open_connection(config, Path(name).name, upload_dir=temporary)
        connection.snapshot_id = digest.hexdigest()
        return connection
    except Exception:
        temporary.cleanup()
        raise


def allowed_manual_host(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    permitted = {
        value.strip().lower().rstrip(".")
        for value in os.environ.get("QUERYLENS_DB_HOSTS", "").split(",")
        if value.strip()
    }
    if not host or any(character.isspace() or character in "/\\?@%#" for character in host):
        raise ValueError("Enter a database hostname or IP address.")
    if (permitted or REQUIRE_LOGIN) and host not in permitted:
        raise ValueError("That database host is outside this deployment’s configured allowlist.")
    return host


def configured_connection() -> tuple[ConnectionConfig, str] | None:
    sqlite_path = os.environ.get("QUERYLENS_SQLITE_PATH", "").strip()
    dialect = os.environ.get("QUERYLENS_DB_DIALECT", "").strip().lower()
    if dialect == "sqlite" or (not dialect and sqlite_path):
        path = Path(sqlite_path or os.environ.get("QUERYLENS_DB_NAME", "")).expanduser()
        return ConnectionConfig(dialect="sqlite", database=str(path)), path.name or "Configured SQLite"
    if dialect in PORTS:
        database_name = os.environ.get("QUERYLENS_DB_NAME", "")
        config = ConnectionConfig(
            dialect=dialect,
            host=os.environ.get("QUERYLENS_DB_HOST", ""),
            port=int(os.environ.get("QUERYLENS_DB_PORT", str(PORTS[dialect]))),
            database=database_name,
            schema=os.environ.get("QUERYLENS_DB_SCHEMA", ""),
            username=os.environ.get("QUERYLENS_DB_USER", ""),
            password=os.environ.get("QUERYLENS_DB_PASSWORD", ""),
            ssl_mode=os.environ.get("QUERYLENS_DB_SSL_MODE", "verify-full"),
        )
        return config, database_name or "Configured database"
    return None


def identity_key(user: Any, anonymous_id: str) -> tuple[str, bool]:
    if user.get("is_logged_in", False) and user.get("sub"):
        identity = f"{user.get('iss', '')}\0{user['sub']}"
        return hashlib.sha256(identity.encode()).hexdigest(), True
    return anonymous_id, False


def provider_for(connection: ActiveConnection, settings: dict) -> Provider:
    use_ai_for_sample = settings.get("use_ai_for_sample", settings.get("use_gemini_for_sample", False))
    if connection.sample and not (use_ai_for_sample and settings["api_key"]):
        return SampleProvider()
    return create_provider(settings.get("provider", "gemini"), settings["api_key"], settings["model"])


def build_analyst(
    connection: ActiveConnection,
    settings: dict,
    owner: str,
    persistent: bool,
    *,
    index: bool = True,
    default_sample: bool = False,
) -> Analyst:
    if index and connection.index is None:
        connection.index = SemanticIndex(
            connection.namespace,
            connection.schema,
            owner=owner,
            persist_path=DATA_DIR / "vectors" if persistent else None,
            examples=EXAMPLE_QUERIES if connection.sample else None,
        )
    provider = SampleProvider() if default_sample else provider_for(connection, settings)
    return Analyst(
        connection.database,
        connection.schema,
        provider,
        connection.index if index else None,
        share_result_samples=settings["share_result_samples"],
    )


def initial_settings() -> dict[str, Any]:
    provider = os.environ.get("QUERYLENS_AI_PROVIDER", "").strip().lower() or "gemini"
    if provider not in AI_PROVIDERS:
        raise ValueError("Set QUERYLENS_AI_PROVIDER to gemini, openai, xai, or openrouter.")
    prefix = provider.upper()
    return {
        "provider": provider,
        "api_key": os.environ.get(f"{prefix}_API_KEY", "").strip(),
        "model": os.environ.get(f"{prefix}_MODEL", "").strip() or AI_PROVIDERS[provider].default_model,
        "share_result_samples": False,
        "use_ai_for_sample": False,
    }


def demo_analysis(connection: ActiveConnection, settings: dict, owner: str):
    analyst = build_analyst(connection, settings, owner, False, index=False, default_sample=True)
    return analyst.rerun(EXAMPLE_QUERIES[DEFAULT_QUESTION], DEFAULT_QUESTION)
