"""Database connection, scoped schema discovery, and bounded read-only queries."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import ssl
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import pandas as pd
from sqlalchemy import URL, create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from .sql_safety import DIALECTS, prepare_query


class DatabaseError(RuntimeError):
    """Database failure with a user-facing message that omits credentials."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ConnectionConfig:
    dialect: str
    username: str = ""
    password: str = field(default="", repr=False)
    host: str = ""
    port: int | None = None
    database: str = ""
    schema: str = ""
    ssl_mode: str = ""


@dataclass(frozen=True)
class QueryResult:
    frame: pd.DataFrame
    truncated: bool
    elapsed_ms: float


_DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306}
_ALLOWED_SQLITE_ACTIONS = {
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}
_BLOCKED_SQLITE_FUNCTIONS = {"load_extension", "readfile", "writefile"}


class Database:
    """Manage one selected database and scope queries to its discovered schema."""

    def __init__(
        self, config: ConnectionConfig, *, timeout_seconds: int = 10, max_rows: int = 1000
    ) -> None:
        self.dialect = config.dialect.strip().lower()
        if self.dialect not in DIALECTS:
            raise DatabaseError("Choose SQLite, MySQL, or PostgreSQL.")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise DatabaseError("The query timeout must be between 1 and 300 seconds.")
        if type(max_rows) is not int or not 1 <= max_rows <= 100_000:
            raise DatabaseError("The query row limit must be between 1 and 100,000.")

        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows
        self._tables: frozenset[str] | None = None
        try:
            if self.dialect == "sqlite":
                self.schema, self._engine, identity = self._sqlite_engine(config)
            else:
                self.schema, self._engine, identity = self._server_engine(config)
        except (SQLAlchemyError, OSError, ImportError, ValueError):
            raise DatabaseError("Could not initialize the selected database connection.") from None
        self.namespace = hashlib.sha256(
            json.dumps((self.dialect, *identity, self.schema), ensure_ascii=False).encode()
        ).hexdigest()

    def _sqlite_engine(self, config: ConnectionConfig) -> tuple[str, Engine, tuple]:
        if config.schema and config.schema.lower() != "main":
            raise DatabaseError("SQLite uses the main schema of the selected file.")
        if not config.database or config.database == ":memory:":
            raise DatabaseError("Choose an existing SQLite database file.")
        path = Path(config.database).expanduser().resolve()
        if not path.is_file():
            raise DatabaseError("The selected SQLite database file does not exist.")

        def open_read_only() -> sqlite3.Connection:
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro", uri=True, timeout=self.timeout_seconds
            )
            try:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA trusted_schema = OFF")
            except sqlite3.Error:
                connection.close()
                raise
            return connection

        engine = create_engine("sqlite+pysqlite://", creator=open_read_only, poolclass=NullPool)
        return "main", engine, (str(path),)

    def _server_engine(self, config: ConnectionConfig) -> tuple[str, Engine, tuple]:
        host = config.host.strip()
        if not host or any(char.isspace() or char in "/\\,?#" for char in host):
            raise DatabaseError("Enter a single database host name or IP address.")
        if not config.username.strip() or not config.database.strip():
            raise DatabaseError("Enter a database name and username.")
        if any("\x00" in value for value in (config.username, config.database, config.schema)):
            raise DatabaseError("Database names, usernames, and schemas cannot contain NUL characters.")
        ssl_mode = config.ssl_mode.strip().lower() or "verify-full"
        if ssl_mode not in {"disable", "require", "verify-full"}:
            raise DatabaseError("Choose verify-full, require, or disable for TLS.")
        port = config.port if config.port is not None else _DEFAULT_PORTS[self.dialect]
        if type(port) is not int or not 1 <= port <= 65535:
            raise DatabaseError("Enter a TCP port between 1 and 65535.")

        connect_args: dict = {"connect_timeout": self.timeout_seconds}
        if self.dialect == "postgresql":
            driver = "postgresql+psycopg"
            schema = config.schema or "public"
            connect_args.update(sslmode=ssl_mode, gssencmode="disable")
            if ssl_mode == "verify-full":
                connect_args["sslrootcert"] = os.getenv("PGSSLROOTCERT") or "system"
        else:
            driver = "mysql+pymysql"
            schema = config.database
            if config.schema and config.schema != schema:
                raise DatabaseError("On MySQL the schema is the selected database.")
            # Match the SQL parser's string-literal rules and keep LOCAL INFILE disabled.
            connect_args.update(
                read_timeout=self.timeout_seconds + 2,
                write_timeout=self.timeout_seconds + 2,
                charset="utf8mb4",
                local_infile=False,
                sql_mode="ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION",
            )
            if ssl_mode == "disable":
                connect_args["ssl_disabled"] = True
            else:
                context = ssl.create_default_context(cafile=os.getenv("AI_SQL_MYSQL_SSL_CA") or None)
                if ssl_mode == "require":
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                connect_args["ssl"] = context

        url = URL.create(
            driver, username=config.username, password=config.password,
            host=host, port=port, database=config.database,
        )
        engine = create_engine(url, connect_args=connect_args, poolclass=NullPool, hide_parameters=True)
        return schema, engine, (host.lower(), port, config.database, config.username)

    @contextmanager
    def _read_connection(self, *, querying: bool = False) -> Iterator[Connection]:
        with self._engine.connect() as conn:
            if self.dialect == "sqlite":
                native = conn.connection.driver_connection
                deadline = time.monotonic() + self.timeout_seconds
                native.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                if querying:
                    native.set_authorizer(_sqlite_authorizer)
                try:
                    yield conn
                finally:
                    native.set_authorizer(None)
                    native.set_progress_handler(None, 0)
            elif self.dialect == "postgresql":
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                conn.exec_driver_sql(f"SET LOCAL statement_timeout = {self.timeout_seconds * 1000}")
                conn.exec_driver_sql("SET LOCAL standard_conforming_strings = on")
                conn.exec_driver_sql("SET LOCAL search_path TO pg_catalog, pg_temp")
                yield conn
            else:
                if conn.dialect.is_mariadb:
                    conn.exec_driver_sql(f"SET SESSION max_statement_time = {self.timeout_seconds}")
                else:
                    conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {self.timeout_seconds * 1000}")
                conn.exec_driver_sql("START TRANSACTION READ ONLY")
                yield conn

    def discover(self) -> dict:
        """Inspect table and view columns, primary keys, and foreign keys."""
        tables = []
        try:
            with self._read_connection() as conn:
                inspector = inspect(conn)
                readable_columns = None
                mysql_columns = None
                if self.dialect == "postgresql":
                    readable_columns = _postgres_selectable_columns(conn, self.schema)
                elif self.dialect == "mysql":
                    mysql_columns = _mysql_selectable_columns(conn)
                    readable_columns = {
                        name: {column["name"] for column in columns}
                        for name, columns in mysql_columns.items()
                    }
                table_names = set(inspector.get_table_names(schema=self.schema))
                view_names = set(inspector.get_view_names(schema=self.schema)) - table_names
                if self.dialect == "postgresql":
                    view_names.update(inspector.get_materialized_view_names(schema=self.schema))
                    table_names.update(inspector.get_foreign_table_names(schema=self.schema))
                relation_names = table_names | view_names
                if readable_columns is not None:
                    relation_names.intersection_update(readable_columns)
                for name in sorted(relation_names):
                    is_view = name in view_names
                    columns = (
                        mysql_columns[name] if mysql_columns is not None
                        else inspector.get_columns(name, schema=self.schema)
                    )
                    primary_key, foreign_keys = [], []
                    if not is_view:
                        try:
                            primary_key = (
                                inspector.get_pk_constraint(name, schema=self.schema)
                                .get("constrained_columns") or []
                            )
                            foreign_keys = inspector.get_foreign_keys(name, schema=self.schema)
                        except DBAPIError as exc:
                            # Column-only MySQL grants allow reading data but may hide table DDL.
                            if self.dialect != "mysql" or not exc.orig.args or exc.orig.args[0] not in {1142, 1143}:
                                raise
                    if readable_columns is not None:
                        allowed = readable_columns[name]
                        columns = [column for column in columns if column["name"] in allowed]
                        if not allowed.issuperset(primary_key):
                            primary_key = []
                        foreign_keys = [
                            fk for fk in foreign_keys
                            if fk.get("referred_schema") in {None, self.schema}
                            and fk["referred_table"] in relation_names
                            and allowed.issuperset(fk.get("constrained_columns") or ())
                            and readable_columns[fk["referred_table"]].issuperset(fk.get("referred_columns") or ())
                        ]
                    tables.append({
                        "name": name,
                        "kind": "view" if is_view else "table",
                        "columns": [
                            {"name": col["name"], "type": str(col["type"]), "nullable": col.get("nullable", True)}
                            for col in columns
                        ],
                        "primary_key": primary_key,
                        "foreign_keys": [
                            {
                                "columns": fk.get("constrained_columns") or [],
                                "referred_schema": fk.get("referred_schema"),
                                "referred_table": fk["referred_table"],
                                "referred_columns": fk.get("referred_columns") or [],
                            }
                            for fk in foreign_keys
                        ],
                    })
        except (SQLAlchemyError, OSError) as exc:
            raise _database_error(exc, self.dialect) from None
        self._tables = frozenset(table["name"] for table in tables)
        return {"dialect": self.dialect, "schema": self.schema, "tables": tables}

    def execute(self, sql: str) -> QueryResult:
        """Validate a generated statement and return at most max_rows result rows."""
        if self._tables is None:
            self.discover()
        prepared = prepare_query(
            sql, self.dialect, schema=self.schema, tables=self._tables or (), row_limit=self.max_rows,
        )
        start = time.perf_counter()
        reading_query = False
        try:
            with self._read_connection(querying=True) as conn:
                reading_query = True
                # A raw SQL statement may contain literal percent signs in LIKE and date formats.
                with conn.exec_driver_sql(prepared, execution_options={"no_parameters": True}) as result:
                    columns = _unique_column_names(list(result.keys()))
                    rows = result.fetchmany(self.max_rows + 1)
                reading_query = False
        except (SQLAlchemyError, OSError) as exc:
            raise _database_error(exc, self.dialect, querying=reading_query) from None
        truncated = len(rows) > self.max_rows
        frame = pd.DataFrame.from_records(rows[:self.max_rows], columns=columns)
        return QueryResult(frame, truncated, (time.perf_counter() - start) * 1000)

    def close(self) -> None:
        self._engine.dispose()


def _postgres_selectable_columns(conn: Connection, schema: str) -> dict[str, set[str]]:
    """Filter SQLAlchemy catalog metadata to relations and columns with SELECT grants."""
    schema_allowed = conn.execute(text("""
        SELECT pg_catalog.has_schema_privilege(oid, 'USAGE')
        FROM pg_catalog.pg_namespace WHERE nspname = :schema
    """), {"schema": schema}).scalar()
    if schema_allowed is None:
        raise DatabaseError("The selected PostgreSQL schema does not exist.")
    if not schema_allowed:
        raise DatabaseError("The database account lacks USAGE permission on the selected schema.")

    rows = conn.execute(text("""
        SELECT relation.relname, column_def.attname
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        LEFT JOIN pg_catalog.pg_attribute AS column_def
          ON column_def.attrelid = relation.oid AND column_def.attnum > 0 AND NOT column_def.attisdropped
        WHERE namespace.nspname = :schema AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (
            pg_catalog.has_table_privilege(relation.oid, 'SELECT')
            OR pg_catalog.has_column_privilege(relation.oid, column_def.attnum, 'SELECT')
          )
    """), {"schema": schema})
    allowed: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        allowed.setdefault(table_name, set())
        if column_name is not None:
            allowed[table_name].add(column_name)
    return allowed


def _mysql_selectable_columns(conn: Connection) -> dict[str, list[dict]]:
    """Read accessible columns, including views that the account cannot inspect as DDL."""
    rows = conn.exec_driver_sql("""
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND CAST(TABLE_SCHEMA AS BINARY) = CAST(DATABASE() AS BINARY)
          AND FIND_IN_SET('select', PRIVILEGES) > 0
        ORDER BY TABLE_NAME, ORDINAL_POSITION
    """)
    tables: dict[str, list[dict]] = {}
    for table_name, name, column_type, nullable in rows:
        tables.setdefault(table_name, []).append({
            "name": name, "type": column_type, "nullable": nullable == "YES",
        })
    return tables


def _sqlite_authorizer(action: int, name: str | None, detail: str | None,
                       schema: str | None, _source: str | None) -> int:
    if action not in _ALLOWED_SQLITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and (
        schema not in {None, "main"} or (name or "").lower().startswith("sqlite_")
    ):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (detail or "").lower() in _BLOCKED_SQLITE_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _unique_column_names(columns: list[str]) -> list[str]:
    reserved = set(columns)
    output = []
    seen = set()
    for name in columns:
        unique = name
        suffix = 2
        if unique in seen:
            unique = f"{name}_{suffix}"
            while unique in reserved or unique in seen:
                suffix += 1
                unique = f"{name}_{suffix}"
        seen.add(unique)
        output.append(unique)
    return output


def _database_error(exc: Exception, dialect: str, *, querying: bool = False) -> DatabaseError:
    if isinstance(exc, DBAPIError):
        original = exc.orig
        if dialect == "postgresql":
            state = getattr(original, "sqlstate", "") or ""
            if state in {"57014", "55P03"}:
                return DatabaseError("The query timed out. Narrow its date range or aggregate fewer rows.")
            if state in {"25006", "42501"} or state.startswith("28"):
                return DatabaseError("The database account lacks permission for this operation.")
            if querying and (state.startswith("22") or state.startswith("42")):
                return DatabaseError(f"PostgreSQL rejected the query (SQLSTATE {state}).", retryable=True)
        elif dialect == "mysql":
            code = original.args[0] if original.args and isinstance(original.args[0], int) else None
            if code in {1205, 1317, 1969, 3024}:
                return DatabaseError("The query timed out. Narrow its date range or aggregate fewer rows.")
            if code in {1044, 1045, 1142, 1143, 1792}:
                return DatabaseError("The database account lacks permission for this operation.")
            if code == 2026:
                return DatabaseError("Could not establish MySQL TLS. Check the server certificate and TLS mode.")
            if querying and code in {1054, 1055, 1064, 1111, 1146, 1241, 1242, 1305, 1582, 1690}:
                message = {
                    1054: "MySQL could not resolve a column reference.",
                    1055: "MySQL requires non-aggregate selected columns to appear in GROUP BY.",
                    1064: "MySQL rejected the query syntax.",
                    1146: "MySQL could not resolve a table reference.",
                }.get(code, f"MySQL rejected the query (error {code}).")
                return DatabaseError(message, retryable=True)
        else:
            code = getattr(original, "sqlite_errorcode", 0) & 255
            if code in {sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT}:
                return DatabaseError("Could not read the SQLite file. Choose an intact database snapshot.")
            if code == sqlite3.SQLITE_INTERRUPT:
                return DatabaseError("The query timed out. Narrow its date range or aggregate fewer rows.")
            if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                return DatabaseError("The SQLite database is busy or locked. Try again when it is available.")
            if code in {sqlite3.SQLITE_AUTH, sqlite3.SQLITE_READONLY}:
                return DatabaseError("SQLite rejected an operation that requires additional permissions.")
            if querying and code in {sqlite3.SQLITE_ERROR, sqlite3.SQLITE_MISMATCH}:
                original_text = str(original).lower()
                if original_text.startswith("no such column:"):
                    return DatabaseError("SQLite could not resolve a column reference.", retryable=True)
                if original_text.startswith("no such table:"):
                    return DatabaseError("SQLite could not resolve a table reference.", retryable=True)
                return DatabaseError("SQLite rejected the query syntax or expressions.", retryable=True)
    if querying:
        return DatabaseError("The database query could not complete. Check the connection and try again.")
    if dialect == "sqlite":
        return DatabaseError("Could not open the SQLite file or read its schema.")
    return DatabaseError(
        "Could not access the database or read its schema. Check the host, credentials, TLS, and schema permissions."
    )
