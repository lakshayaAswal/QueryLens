"""Plan, run, repair, and explain an analysis against a single database."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from .ai import Provider, ProviderError
from .analytics import build_evidence
from .database import Database, DatabaseError, QueryResult
from .retrieval import SemanticIndex, rank_tables
from .sql_safety import UnsafeQueryError


@dataclass(frozen=True)
class Analysis:
    question: str
    sql: str
    frame: pd.DataFrame
    truncated: bool
    elapsed_ms: float
    insights: str
    chart: dict[str, str] | None
    attempts: int
    notices: tuple[str, ...] = ()
    repair_notes: tuple[str, ...] = ()


class Analyst:
    """Keep every execution on the same validated database path."""

    def __init__(
        self,
        database: Database,
        schema: Mapping[str, Any],
        provider: Provider,
        retrieval: SemanticIndex | None = None,
        *,
        max_retries: int = 1,
        share_result_samples: bool = False,
    ) -> None:
        if not 0 <= max_retries <= 2:
            raise ValueError("SQL correction must be limited to at most two retries.")
        self.database = database
        self.schema = schema
        self.provider = provider
        self.retrieval = retrieval
        self.max_retries = max_retries
        self.share_result_samples = share_result_samples

    def ask(self, question: str, history: Sequence[Any] = ()) -> Analysis:
        question = _question(question)
        turns = _previous_turns(history)
        schema, examples = self._context(question, turns)
        sql = self.provider.plan(question, schema, turns, examples)
        notes: list[str] = []
        for attempt in range(1, self.max_retries + 2):
            try:
                result = self.database.execute(sql)
            except (DatabaseError, UnsafeQueryError) as exc:
                if not getattr(exc, "retryable", False) or attempt > self.max_retries:
                    raise
                error = str(exc)[:1500]
                notes.append(error)
                corrected = self.provider.plan(question, schema, turns, examples, previous_sql=sql, error=error)
                if corrected.strip().rstrip(";") == sql.strip().rstrip(";"):
                    raise
                sql = corrected
            else:
                return self._finish(question, sql, result, attempt, notes)
        raise RuntimeError("SQL attempt limit reached.")

    def rerun(self, sql: str, question: str, history: Sequence[Any] = ()) -> Analysis:
        question = _question(question)
        return self._finish(question, sql, self.database.execute(sql), 1, ())

    def _context(
        self, question: str, history: Sequence[Mapping[str, str]]
    ) -> tuple[dict[str, Any], Sequence[Mapping[str, str]]]:
        search = question
        if history and re.match(r"(?:now|then|also|just|only|instead|same|what about)\b", question, re.IGNORECASE):
            search += f"\n{history[-1]['question']}"
        if self.retrieval:
            context = self.retrieval.query(search)
            names, examples = context.table_names, context.examples
        else:
            names, examples = rank_tables(self.schema, search), ()
        return _schema_context(self.schema, names), examples

    def _finish(
        self, question: str, sql: str, result: QueryResult, attempts: int, repair_notes: Sequence[str]
    ) -> Analysis:
        evidence = build_evidence(
            result.frame,
            truncated=result.truncated,
            sample_rows=8 if self.share_result_samples else 0,
        )
        notices: list[str] = []
        insights, chart = "", None
        if not result.frame.empty:
            try:
                report = self.provider.analyze(question, sql, evidence)
                insights, chart = report.insights, report.chart
            except ProviderError as exc:
                notices.append(f"AI insights unavailable. {exc}")
        if self.retrieval:
            self.retrieval.remember(question, sql)
            if self.retrieval.last_error:
                notices.append(self.retrieval.last_error)
        return Analysis(
            question=question,
            sql=sql,
            frame=result.frame,
            truncated=result.truncated,
            elapsed_ms=result.elapsed_ms,
            insights=insights or _local_insights(evidence),
            chart=chart,
            attempts=attempts,
            notices=tuple(notices),
            repair_notes=tuple(repair_notes),
        )


def _question(question: str) -> str:
    cleaned = question.strip()
    if not cleaned:
        raise ValueError("Enter a question about the connected database.")
    if len(cleaned) > 2000:
        raise ValueError("Limit the question to 2,000 characters.")
    return cleaned


def _previous_turns(history: Sequence[Any]) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for item in history[-6:]:
        if isinstance(item, Mapping):
            question, sql = item.get("question"), item.get("sql")
        else:
            question, sql = getattr(item, "question", None), getattr(item, "sql", None)
        if isinstance(question, str) and isinstance(sql, str) and question and sql:
            turns.append({"question": question[:2000], "sql": sql[:8000]})
    return turns


def _schema_context(schema: Mapping[str, Any], selected_names: Sequence[str]) -> dict[str, Any]:
    tables = {table["name"]: table for table in schema.get("tables", ())}
    selected = dict.fromkeys(name for name in selected_names if name in tables)
    for name in tuple(selected):
        for key in tables[name].get("foreign_keys", ()):
            referenced = key["referred_table"]
            if len(selected) < 16 and referenced in tables and key.get("referred_schema") in (None, schema.get("schema")):
                selected[referenced] = None
    if not selected:
        selected = dict.fromkeys(list(tables)[:10])
    directory = list(tables)[:250]
    detail: list[dict[str, Any]] = []
    remaining_chars = 70_000
    for name in selected:
        table = dict(tables[name])
        columns = table.get("columns", ())
        if len(columns) > 120:
            table["columns"] = columns[:120]
            table["omitted_columns"] = len(columns) - 120
        size = len(json.dumps(table, ensure_ascii=False, default=str))
        if remaining_chars >= size:
            detail.append(table)
            remaining_chars -= size
    return {
        "dialect": schema.get("dialect"),
        "selected_schema": schema.get("schema"),
        "verified_table_names": directory,
        "verified_table_count": len(tables),
        "tables": detail,
        "schema_details_are_partial": len(detail) < len(tables) or any("omitted_columns" in table for table in detail),
    }


def _label(value: str) -> str:
    return value.replace("_", " ").strip() or "measure"


def _number(value: float) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"


def _local_insights(evidence: Mapping[str, Any]) -> str:
    count = evidence["row_count"]
    if not count:
        return "The query returned no rows. Check the date range or filters and try a broader question."
    status = " The result reached the configured row cap." if evidence["truncated"] else ""
    insights = [f"The query returned **{count:,}** {'row' if count == 1 else 'rows'}.{status}"]
    temporal = evidence.get("temporal_summary", {}).get("measures", {})
    if temporal:
        name, trend = next(iter(temporal.items()))
        first_date, last_date = trend["first_date"][:10], trend["last_date"][:10]
        first_value, last_value = trend["first_value"], trend["last_value"]
        percent = trend["percent_change"]
        if first_value is not None and last_value is not None:
            direction = "increased" if last_value > first_value else "decreased"
            change = f", a {abs(percent):,.1f}% change" if percent is not None and percent else ""
            if first_value == last_value:
                insights.append(f"**{_label(name).capitalize()}** was {_number(first_value)} on both {first_date} and {last_date}.")
            else:
                insights.append(
                    f"**{_label(name).capitalize()}** {direction} from {_number(first_value)} on {first_date} "
                    f"to {_number(last_value)} on {last_date}{change}."
                )
    numeric = evidence.get("numeric_summary", {})
    if numeric:
        name, summary = next(iter(numeric.items()))
        minimum, maximum = summary["min"], summary["max"]
        if minimum is not None and maximum is not None:
            if minimum == maximum:
                insights.append(f"**{_label(name).capitalize()}** measured {_number(minimum)} in the returned rows.")
            else:
                insights.append(
                    f"**{_label(name).capitalize()}** ranged from {_number(minimum)} to {_number(maximum)} across returned rows."
                )
    return "\n".join(f"- {line}" for line in insights)
