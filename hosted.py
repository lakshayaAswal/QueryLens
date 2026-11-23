"""Stateless HTTP API for the hosted QueryLens workspace."""

from __future__ import annotations

import binascii
import ipaddress
import json
import logging
import math
import os
import re
import tempfile
from base64 import b64decode
from contextlib import ExitStack, contextmanager
from datetime import date, time
from decimal import Decimal
from numbers import Integral, Real
from pathlib import Path
from typing import Annotated, Iterator, Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from ai_sql_agent.ai import AI_PROVIDERS, Provider, ProviderError, SampleProvider, create_provider
from ai_sql_agent.analyst import Analysis, Analyst
from ai_sql_agent.database import ConnectionConfig, Database, DatabaseError
from ai_sql_agent.demo import DEFAULT_QUESTION, SUGGESTED_QUESTIONS, ensure_demo_database
from ai_sql_agent.sql_safety import UnsafeQueryError

MAX_SQLITE_BYTES = 3 * 1024 * 1024
MAX_BASE64_LENGTH = 4 * ((MAX_SQLITE_BYTES + 2) // 3)
MAX_REQUEST_BYTES = 4_300_000
MAX_RESULT_ROWS = 250
MAX_SQLITE_VALUE_BYTES = 128 * 1024
MAX_RESULT_COLUMNS = 40
MAX_CELL_LENGTH = 800
MAX_DISPLAY_BYTES = 512 * 1024
MAX_SCHEMA_TABLES = 120
MAX_SCHEMA_COLUMNS = 100
SQLITE_HEADER = b"SQLite format 3\x00"
DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306}
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

logger = logging.getLogger(__name__)
app = FastAPI(title="QueryLens Hosted API", docs_url=None, redoc_url=None, openapi_url=None)

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, max_length=256)]


class DatabaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["sample", "sqlite", "postgresql", "mysql"]
    file_b64: str | None = Field(default=None, max_length=MAX_BASE64_LENGTH, repr=False)
    host: Identifier = ""
    port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    database: Identifier = ""
    schema_name: Identifier = Field(default="", alias="schema")
    username: Identifier = ""
    password: SecretStr = Field(default=SecretStr(""), max_length=4096)


class AIInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Annotated[str, StringConstraints(strip_whitespace=True, max_length=32)]
    api_key: SecretStr = Field(max_length=4096)
    model: Identifier | None = None


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Question
    sql: str = Field(min_length=1, max_length=8_000)


class SchemaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: DatabaseInput


class QueryInput(SchemaInput):
    question: Question
    ai: AIInput | None = None
    history: list[TurnInput] = Field(default_factory=list, max_length=6)
    share_result_samples: bool = False


@app.middleware("http")
async def protect_response(request: Request, call_next):
    length = request.headers.get("content-length", "")
    if length.isascii() and length.isdecimal() and (len(length) > 8 or int(length) > MAX_REQUEST_BYTES):
        response = JSONResponse({"detail": "The request is too large. Upload a SQLite file up to 3 MiB."}, status_code=413)
    else:
        try:
            response = await call_next(request)
        except Exception as error:
            # Driver exceptions can carry connection or query details.
            logger.error("Hosted request failed (%s)", type(error).__name__)
            response = JSONResponse({"detail": "QueryLens could not complete the request."}, status_code=500)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
    return JSONResponse({"detail": "Check the request fields and try again."}, status_code=422)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _host_name(host: str) -> str:
    host = host.strip().lower()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        host = host.removesuffix(".")
        if len(host) <= 253 and all(DNS_LABEL.fullmatch(part) for part in host.split(".")):
            return host
        raise ValueError("Enter a valid database hostname or IP address.") from None


def _remote_endpoints() -> set[tuple[str, int]]:
    endpoints = set()
    for address in os.environ.get("QUERYLENS_HOSTED_DB_ENDPOINTS", "").split(","):
        host, separator, port = address.strip().rpartition(":")
        if not separator or not port.isascii() or not port.isdecimal() or len(port) > 5:
            continue
        number = int(port)
        if not 1 <= number <= 65535:
            continue
        try:
            endpoints.add((_host_name(host.removeprefix("[").removesuffix("]")), number))
        except ValueError:
            continue
    return endpoints


@app.get("/api/config")
def config() -> dict:
    return {
        "providers": [
            {"id": provider, "label": settings.label, "default_model": settings.default_model}
            for provider, settings in AI_PROVIDERS.items()
        ],
        "suggested_questions": [DEFAULT_QUESTION, *SUGGESTED_QUESTIONS],
        "max_sqlite_bytes": MAX_SQLITE_BYTES,
        "remote_enabled": bool(_remote_endpoints()),
    }


def _remote_config(request: DatabaseInput) -> ConnectionConfig:
    port = request.port or DEFAULT_PORTS[request.kind]
    try:
        host = _host_name(request.host)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if (host, port) not in _remote_endpoints():
        raise HTTPException(403, "This database host and port are outside the hosted deployment’s allowlist.")
    if not request.database or not request.username:
        raise HTTPException(422, "Enter a database name and username.")
    return ConnectionConfig(
        dialect=request.kind,
        host=host,
        port=port,
        database=request.database,
        schema=request.schema_name,
        username=request.username,
        password=request.password.get_secret_value(),
        ssl_mode="verify-full",
    )


def _uploaded_config(request: DatabaseInput, resources: ExitStack) -> ConnectionConfig:
    try:
        payload = b64decode(request.file_b64 or "", validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, "Upload a valid SQLite database file.") from None
    if not payload.startswith(SQLITE_HEADER) or len(payload) > MAX_SQLITE_BYTES:
        raise HTTPException(422, "Upload a valid SQLite database file up to 3 MiB.")
    temporary = resources.enter_context(tempfile.TemporaryDirectory(prefix="querylens-upload-"))
    path = Path(temporary) / "database.sqlite3"
    path.write_bytes(payload)
    return ConnectionConfig(dialect="sqlite", database=str(path))


@contextmanager
def _database(request: DatabaseInput) -> Iterator[tuple[Database, dict]]:
    with ExitStack() as resources:
        if request.kind == "sample":
            path = Path(tempfile.gettempdir()) / "querylens-sample" / "sample-commerce.sqlite3"
            settings = ConnectionConfig(dialect="sqlite", database=str(ensure_demo_database(path)))
        elif request.kind == "sqlite":
            settings = _uploaded_config(request, resources)
        else:
            settings = _remote_config(request)
        try:
            database = Database(settings, max_rows=MAX_RESULT_ROWS, sqlite_max_value_bytes=MAX_SQLITE_VALUE_BYTES)
            resources.callback(database.close)
            schema = database.discover()
        except DatabaseError:
            raise HTTPException(422, "Could not open the database. Check the file or connection settings and permissions.") from None
        yield database, schema


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii"))


@app.post("/api/schema")
def inspect_schema(request: SchemaInput) -> dict:
    with _database(request.database) as (_, schema):
        tables = []
        remaining = MAX_DISPLAY_BYTES
        truncated = len(schema["tables"]) > MAX_SCHEMA_TABLES
        for table in schema["tables"][:MAX_SCHEMA_TABLES]:
            columns = table["columns"][:MAX_SCHEMA_COLUMNS]
            truncated |= len(columns) < len(table["columns"])
            displayed = {**table, "columns": columns}
            size = _json_size(displayed) + 1
            if size > remaining:
                truncated = True
                continue
            tables.append(displayed)
            remaining -= size
        return {"dialect": schema["dialect"], "tables": tables, "truncated": truncated}


def _provider(request: QueryInput) -> Provider:
    ai = request.ai
    if ai is None or not ai.api_key.get_secret_value().strip():
        if request.database.kind == "sample":
            return SampleProvider()
        raise HTTPException(422, "Add an AI provider API key to query your database.")
    try:
        return create_provider(ai.provider, ai.api_key.get_secret_value(), ai.model)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def _display_text(text: str) -> str:
    if len(text) > MAX_CELL_LENGTH:
        text = text[:MAX_CELL_LENGTH - 1] + "…"
    return text.encode("utf-8", errors="replace").decode("utf-8")


def _display_cell(value: object) -> tuple[object, bool]:
    if value is None or value is pd.NA or value is pd.NaT:
        return None, False
    if isinstance(value, bool):
        return value, False
    if isinstance(value, Integral):
        integer = int(value)
        return (str(integer) if abs(integer) >= 2 ** 53 else integer), False
    if isinstance(value, Real):
        number = float(value)
        return (number if math.isfinite(number) else None), False
    if isinstance(value, (date, time)):
        return value.isoformat(), False
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"Binary value ({len(value):,} bytes)", True
    text = format(value, "f") if isinstance(value, Decimal) else str(value)
    displayed = _display_text(text)
    return displayed, displayed != text


def _display_result(analysis: Analysis) -> tuple[dict, bool]:
    frame = analysis.frame
    displayed = frame.iloc[:MAX_RESULT_ROWS, :MAX_RESULT_COLUMNS]
    columns = [_display_text(str(column)) for column in displayed.columns]
    truncated = displayed.shape != frame.shape or list(displayed.columns) != columns
    remaining = MAX_DISPLAY_BYTES - _json_size(columns)
    rows = []
    for row in displayed.itertuples(index=False, name=None):
        cells = []
        row_truncated = False
        for value in row:
            cell, cell_truncated = _display_cell(value)
            cells.append(cell)
            row_truncated |= cell_truncated
        size = _json_size(cells) + 1
        if size > remaining:
            truncated = True
            break
        remaining -= size
        rows.append(cells)
        truncated |= row_truncated
    return {"columns": columns, "data": rows}, truncated


@app.post("/api/query")
def query(request: QueryInput) -> dict:
    try:
        with ExitStack() as resources:
            provider = _provider(request)
            resources.callback(provider.close)
            database, schema = resources.enter_context(_database(request.database))
            analysis = Analyst(
                database,
                schema,
                provider,
                share_result_samples=request.share_result_samples,
            ).ask(request.question, history=[turn.model_dump() for turn in request.history])
    except UnsafeQueryError:
        raise HTTPException(422, "The generated SQL could not be confirmed as read-only. Try a narrower question.") from None
    except DatabaseError:
        raise HTTPException(502, "The database could not complete this query. Check permissions or try a narrower question.") from None
    except ProviderError as error:
        raise HTTPException(422, _display_text(str(error))) from None

    result, display_truncated = _display_result(analysis)
    notices = list(analysis.notices)
    if display_truncated:
        notices.append("The hosted result display was shortened to fit its size limit.")
    return {
        "question": analysis.question,
        "sql": analysis.sql,
        "result": result,
        "truncated": analysis.truncated or display_truncated,
        "elapsed_ms": analysis.elapsed_ms,
        "insights": analysis.insights,
        "chart": analysis.chart,
        "attempts": analysis.attempts,
        "notices": notices,
    }
