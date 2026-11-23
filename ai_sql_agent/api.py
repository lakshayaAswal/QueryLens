"""Optional FastAPI factory for a single server-configured database."""

from __future__ import annotations

import json
from base64 import b64encode
from contextlib import ExitStack
from decimal import Decimal
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, StringConstraints

from .ai import AI_PROVIDERS, ProviderError, create_provider
from .analyst import Analyst
from .database import ConnectionConfig, Database, DatabaseError
from .sql_safety import UnsafeQueryError


class AskRequest(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


_bearer_scheme = HTTPBearer(auto_error=False)


def _json_cell(value: object) -> object:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"encoding": "base64", "data": b64encode(value).decode("ascii")}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_json_cell(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_cell(item) for key, item in value.items()}
    return value


def create_app(
    *,
    database_config: ConnectionConfig,
    gemini_api_key: str | None = None,
    api_token: str,
    gemini_model: str = "gemini-2.5-flash",
    provider: str = "gemini",
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: int = 10,
    max_rows: int = 1000,
) -> FastAPI:
    """Create an API configured with credentials supplied by its host process."""
    if len(api_token) < 32 or not all("!" <= character <= "~" for character in api_token):
        raise ValueError("The API token must contain at least 32 printable ASCII characters without spaces.")
    provider_id = provider.strip().lower()
    if provider_id not in AI_PROVIDERS:
        raise ValueError("Choose a supported AI provider.")
    if provider_id == "gemini":
        api_key = gemini_api_key if api_key is None else api_key
        model = gemini_model if model is None else model
    if not api_key or not api_key.strip():
        if provider_id == "gemini":
            raise ValueError("A Gemini API key is required.")
        raise ValueError("An API key is required for the selected AI provider.")
    expected_token = api_token.encode("ascii")

    app = FastAPI(title="AI SQL Agent API", version="0.1.0")

    def require_token(credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)]) -> None:
        if credentials is None or not compare_digest(credentials.credentials.encode("utf-8"), expected_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A valid access token is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/ask", dependencies=[Depends(require_token)])
    def ask(request: AskRequest) -> dict[str, object]:
        try:
            with ExitStack() as resources:
                database = Database(database_config, timeout_seconds=timeout_seconds, max_rows=max_rows)
                resources.callback(database.close)
                schema = database.discover()
                ai_provider = create_provider(provider_id, api_key, model)
                resources.callback(ai_provider.close)
                analysis = Analyst(database, schema, ai_provider).ask(request.question)
        except UnsafeQueryError:
            raise HTTPException(422, "The generated query could not be confirmed as read-only.") from None
        except DatabaseError:
            raise HTTPException(502, "The database could not complete the analysis.") from None
        except ProviderError:
            raise HTTPException(502, "The AI provider could not complete the analysis.") from None
        except ValueError:
            raise HTTPException(422, "The analysis could not be prepared for this question.") from None

        result = analysis.frame.map(_json_cell).to_json(
            orient="split", date_format="iso", index=False, double_precision=15, default_handler=str
        )
        return {
            "question": analysis.question,
            "sql": analysis.sql,
            "result": json.loads(result),
            "truncated": analysis.truncated,
            "elapsed_ms": analysis.elapsed_ms,
            "insights": analysis.insights,
            "chart": analysis.chart,
            "attempts": analysis.attempts,
            "notices": analysis.notices,
            "repair_notes": analysis.repair_notes,
        }

    return app
