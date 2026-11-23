"""Portable exports of returned query data and charts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from html import escape
from io import BytesIO
from numbers import Integral
import re
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from plotly.graph_objects import Figure


def _csv_value(value):
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def to_csv(frame: pd.DataFrame) -> bytes:
    """Encode UTF-8 CSV and neutralize formula-like text for spreadsheet readers."""
    safe = frame.map(_csv_value)
    safe.columns = [_csv_value(label) for label in safe.columns]
    return safe.to_csv(index=False).encode("utf-8-sig")


def _excel_value(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.isoformat()
    if isinstance(value, Integral) and abs(value) >= 10**15:
        return str(value)
    if isinstance(value, (dict, list, tuple, bytes, Decimal)):
        return str(value)
    return value


def to_excel(frame: pd.DataFrame) -> bytes:
    """Write an XLSX workbook with formulas and automatic hyperlinks disabled."""
    buffer = BytesIO()
    safe = frame.map(_excel_value)
    with pd.ExcelWriter(
        buffer, engine="xlsxwriter",
        engine_kwargs={"options": {"strings_to_formulas": False, "strings_to_urls": False}},
    ) as writer:
        safe.to_excel(writer, sheet_name="Results", index=False, freeze_panes=(1, 0))
        if len(safe.columns):
            writer.sheets["Results"].autofilter(0, 0, len(safe), len(safe.columns) - 1)
    return buffer.getvalue()


def chart_html(figure: Figure) -> bytes:
    """Bundle the Plotly runtime for an interactive chart that opens offline."""
    from plotly.graph_objects import Figure

    exported = Figure(figure)
    exported.update_layout(paper_bgcolor="#111f31", plot_bgcolor="#111f31")
    return exported.to_html(
        full_html=True, include_plotlyjs=True, config={"responsive": True, "displaylogo": False}
    ).encode("utf-8")


def _insights_html(insights: str | Sequence[str] | None) -> str:
    def emphasize(text: str) -> str:
        return re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escape(text))

    if isinstance(insights, str):
        lines = [line.strip() for line in insights.splitlines() if line.strip()]
        if lines and all(line.startswith("- ") for line in lines):
            insights = [line[2:] for line in lines]
        else:
            return f"<p>{emphasize(insights)}</p>" if lines else ""
    if insights:
        items = "".join(f"<li>{emphasize(str(item))}</li>" for item in insights)
        return f"<ul>{items}</ul>"
    return ""


def report_html(
    *, question: str, sql: str, frame: pd.DataFrame, insights: str | Sequence[str] | None,
    elapsed_ms: float, truncated: bool, figure: Figure | None = None,
) -> bytes:
    """Create a self-contained report with query, returned-row preview, and chart."""
    chart = ""
    if figure is not None:
        embedded = figure.to_html(full_html=False, include_plotlyjs=True, config={"responsive": True, "displaylogo": False})
        chart = f'<section aria-labelledby="chart"><h2 id="chart">Visualization</h2>{embedded}</section>'

    explanation = _insights_html(insights)
    analysis = f"<section><h2>Insights</h2>{explanation}</section>" if explanation else ""

    row_limit = " · Query results reached the row limit" if truncated else ""
    if len(frame) > 100:
        preview = "<p>The table shows the first 100 returned rows.</p>"
    elif frame.empty:
        preview = "<p>The query returned no rows.</p>"
    else:
        preview = ""
    table = frame.head(100).to_html(index=False, border=0, escape=True)
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI SQL Agent report</title>
<style>
  :root {{ color-scheme: dark; font: 16px/1.55 system-ui, sans-serif; background: #0b1523; color: #e9f1fc; }}
  body {{ margin: 0; padding: clamp(1rem, 4vw, 3.5rem); }}
  main {{ max-width: 68rem; margin: 0 auto; }}
  h1 {{ font-size: clamp(1.6rem, 4vw, 2.5rem); line-height: 1.2; margin: .45em 0 .3em; }}
  h2 {{ font-size: 1.12rem; margin: 0 0 1rem; }}
  header p {{ color: #a4b6cc; margin: 0 0 1rem; }}
  .product {{ color: #50de8b; font-weight: 700; }}
  section {{ background: #111f31; border: 1px solid #27384e; border-radius: .8rem; margin: 1rem 0; padding: 1.2rem; }}
  section p {{ white-space: pre-wrap; }}
  pre, .table {{ overflow: auto; }}
  pre {{ margin-bottom: 0; color: #a6eec4; }}
  table {{ width: 100%; border-collapse: collapse; text-align: left; }}
  th, td {{ padding: .6rem .75rem; border-bottom: 1px solid #27384e; text-align: left; }}
  th {{ color: #a4b6cc; }}
</style>
</head>
<body>
<main>
  <header><div class="product">AI SQL Agent</div><h1>{escape(question)}</h1>
  <p>{len(frame):,} returned rows · {elapsed_ms:,.1f} ms{row_limit}</p></header>
  {analysis}
  {chart}
  <section><h2>Results</h2>{preview}<div class="table">{table}</div></section>
  <section><h2>SQL</h2><pre><code>{escape(sql)}</code></pre></section>
</main>
</body>
</html>"""
    return html.encode("utf-8")
