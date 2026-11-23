"""Streamlit entry point for QueryLens."""

from __future__ import annotations

import html
import logging
import re
import sqlite3
import uuid
from datetime import timezone
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import streamlit as st

from ai_sql_agent.ai import AI_PROVIDERS, ProviderError
from ai_sql_agent.analytics import build_chart, choose_chart
from ai_sql_agent.database import ConnectionConfig, DatabaseError
from ai_sql_agent.demo import DEFAULT_QUESTION, REVENUE_BY_REGION_QUESTION, TOP_CUSTOMERS_QUESTION
from ai_sql_agent.exports import chart_html, report_html, to_csv, to_excel
from ai_sql_agent.history import HistoryStore
from ai_sql_agent.sql_safety import UnsafeQueryError
from ai_sql_agent.workspace import (
    DATA_DIR,
    DIALECT_LABELS,
    PORTS,
    REQUIRE_LOGIN,
    allowed_manual_host,
    build_analyst,
    configured_connection,
    demo_analysis,
    identity_key,
    initial_settings,
    open_connection,
    open_demo,
    open_uploaded,
)

logger = logging.getLogger(__name__)
CSS = Path(__file__).with_name("style.css")
MENU = {
    "Workspace": ":material/dashboard:",
    "Schema explorer": ":material/account_tree:",
    "Query history": ":material/history:",
    "Settings": ":material/settings:",
}
PROMPT_CHIPS = (
    ("Monthly revenue", DEFAULT_QUESTION),
    ("Top customers", TOP_CUSTOMERS_QUESTION),
    ("Sales by region", REVENUE_BY_REGION_QUESTION),
)
CHART_TYPES = ("Auto", "Line", "Bar", "Pie", "Scatter", "Histogram")

st.set_page_config(page_title="QueryLens · AI SQL Agent", page_icon="📊", layout="wide", initial_sidebar_state="auto")
st.html(f"<style>{CSS.read_text(encoding='utf-8')}</style>")
ss = st.session_state


@st.cache_resource(show_spinner=False)
def history_store() -> HistoryStore:
    return HistoryStore(DATA_DIR / "history.sqlite3")


def icon(name: str) -> str:
    return f'<span class="ql-icon" aria-hidden="true">{name}</span>'


def plain_links(text: str) -> str:
    """Keep the report's Markdown emphasis and prevent embedded remote content."""
    return re.sub(r"([\[\]<>])", r"\\\1", text)


def network_label() -> str:
    if ss.connection.sample and not (ss.settings["api_key"] and ss.settings["use_ai_for_sample"]):
        return "Sample mode"
    label = AI_PROVIDERS[ss.settings["provider"]].label
    return f"{label} configured" if ss.settings["api_key"] else "AI setup needed"


def initialize() -> None:
    if REQUIRE_LOGIN and not st.user.get("is_logged_in", False):
        st.html('<div class="ql-page-title">Sign in to QueryLens</div>')
        st.write("Sign in to open your analytics workspace.")
        if st.button("Sign in", type="primary"):
            st.login()
        st.stop()
    if "session_id" not in ss:
        ss.session_id = uuid.uuid4().hex
    ss.owner, ss.persistent_identity = identity_key(st.user, ss.session_id)
    if "connection" in ss:
        ss.settings.setdefault("provider", "gemini")
        ss.settings.setdefault("use_ai_for_sample", ss.settings.pop("use_gemini_for_sample", False))
        return
    ss.settings = initial_settings()
    ss.page = "Workspace"
    ss.turns = []
    ss.result_token = 0
    ss.connection = open_demo()
    ss.result = demo_analysis(ss.connection, ss.settings, ss.owner)
    ss.turns = [ss.result]


def set_page(page: str) -> None:
    ss.page = page


def new_query() -> None:
    ss.page = "Workspace"
    ss.result = None
    ss.turns = []
    ss.question_input = ""
    ss.result_token += 1
    ss.pop("query_error", None)


def select_prompt(question: str) -> None:
    ss.question_input = question
    ss.pending_query = (question, None)


def queue_rerun(question: str, sql: str) -> None:
    ss.page = "Workspace"
    ss.question_input = question
    ss.pending_query = (question, sql)


def activate(connection) -> None:
    previous = ss.connection
    ss.connection = connection
    previous.close()
    ss.pop("connection_password", None)
    ss.pop("draft_api_key", None)
    new_query()
    if connection.sample:
        ss.result = demo_analysis(connection, ss.settings, ss.owner)
        ss.turns = [ss.result]
    st.rerun()


@st.dialog("Connect a database", width="medium")
def connection_dialog() -> None:
    source = st.selectbox("Database source", ("SQLite", "PostgreSQL", "MySQL"), key="connection_source")
    uploaded = None
    settings = None
    if source == "SQLite":
        uploaded = st.file_uploader("SQLite database file", type=["db", "sqlite", "sqlite3"], key="sqlite_upload")
        st.caption("Upload a standalone SQLite snapshot up to 50 MB.")
    else:
        dialect = source.lower()
        a, b = st.columns([3, 1])
        host = a.text_input("Hostname")
        port = b.number_input("Port", min_value=1, max_value=65535, value=PORTS[dialect], step=1, key=f"port_{dialect}")
        database_name = st.text_input("Database name")
        login, secret = st.columns(2)
        username = login.text_input("Username")
        password = secret.text_input("Password", type="password", key="connection_password")
        schema = st.text_input("Schema", placeholder="public") if dialect == "postgresql" else ""
        tls = st.selectbox("TLS", ("Verify certificate and hostname", "Require TLS", "Disable TLS"))
        ssl_mode = {"Verify certificate and hostname": "verify-full", "Require TLS": "require", "Disable TLS": "disable"}[tls]
        settings = dict(dialect=dialect, port=int(port), database=database_name.strip(), schema=schema.strip(),
                        username=username.strip(), password=password, ssl_mode=ssl_mode)
        st.caption("Use a dedicated account with SELECT access to the tables you want to explore.")
    manual, sample = st.columns([1, 1])
    if manual.button("Connect", type="primary", width="stretch", disabled=source == "SQLite" and uploaded is None):
        try:
            with st.spinner("Connecting and discovering the schema…"):
                if uploaded is not None:
                    connection = open_uploaded(uploaded, uploaded.name, uploaded.size)
                else:
                    assert settings is not None
                    if not settings["username"] or not settings["database"]:
                        raise ValueError("Enter a database name and username.")
                    settings["host"] = allowed_manual_host(host)
                    connection = open_connection(ConnectionConfig(**settings), settings["database"])
            activate(connection)
        except (DatabaseError, ValueError, OSError) as error:
            st.error(str(error) if isinstance(error, (DatabaseError, ValueError)) else "The uploaded database could not be opened.")
    if sample.button("Use sample database", width="stretch"):
        if ss.connection.sample:
            st.rerun()
        activate(open_demo())
    try:
        configured = configured_connection()
    except ValueError:
        st.info("The server-configured database has an invalid setting.")
        configured = None
    if configured and st.button("Open server-configured database", width="stretch", key="configured_connection"):
        try:
            with st.spinner("Connecting and discovering the schema…"):
                connection = open_connection(*configured)
            activate(connection)
        except (DatabaseError, ValueError) as error:
            st.error(str(error))


def sidebar() -> None:
    connection = ss.connection
    with st.sidebar:
        st.html(f'<div class="ql-brand"><span class="ql-brandmark">{icon("database")}</span>'
                '<div><div class="ql-brandname">QueryLens</div><div class="ql-brandline">AI SQL AGENT</div></div></div>')
        st.html('<div class="ql-side-label">WORKSPACE</div>')
        for page, symbol in MENU.items():
            with st.container(key="active-navigation" if ss.page == page else f"nav-{page.replace(' ', '-').lower()}"):
                st.button(page, icon=symbol, key=f"nav-{page}", on_click=set_page, args=(page,), width="stretch")
        st.html('<div class="ql-side-label ql-data-label">DATABASE</div>')
        name = html.escape(connection.label)
        dialect = DIALECT_LABELS[connection.dialect]
        st.html(f'<div class="ql-connection"><div class="ql-connection-icon">{icon("storage")}</div>'
                f'<div class="ql-connection-text"><div class="ql-connection-name" title="{name}">{name}</div>'
                f'<div class="ql-connection-meta">{dialect} · {len(connection.tables)} tables</div></div>'
                '<span class="ql-dot" aria-label="Connected"></span></div>')
        with st.container(key="connect-button"):
            if st.button("Connect database", icon=":material/add:", width="stretch"):
                connection_dialog()
        if connection.tables:
            st.html('<div class="ql-side-label ql-schema-label">SCHEMA</div>')
            names = connection.tables[:5]
            for table in names:
                st.html(f'<div class="ql-schema-row">{icon("table_chart")}<span>{html.escape(table["name"])}</span></div>')
            if len(connection.tables) > len(names):
                st.caption(f"+ {len(connection.tables) - len(names)} more · Open Schema explorer")
        st.html(f'<div class="ql-sidebar-foot">{icon("shield")}<span>Read-only access</span></div>')


def topbar() -> None:
    bread, action = st.columns([6, 1.4], vertical_alignment="center")
    with bread:
        page = html.escape(ss.page)
        st.html(f'<div class="ql-breadcrumb">{icon("space_dashboard")}<span>QueryLens</span>'
                f'{icon("chevron_right")}<strong>{page}</strong></div>')
    with action, st.container(horizontal=True, horizontal_alignment="right"):
        st.button("New query", icon=":material/add:", on_click=new_query, key="new-query")
    st.html('<div class="ql-header-rule"></div>')


def heading(title: str, subtitle: str, *, show_status: bool = False) -> None:
    status = f'<div class="ql-mode"><span class="ql-dot"></span>{network_label()}</div>' if show_status else ""
    st.html(f'<div class="ql-page-heading"><div><h1>{title}</h1><p>{subtitle}</p></div>{status}</div>')


def ask(question: str, sql: str | None) -> None:
    question = question.strip()
    if not question:
        ss.query_error = "Enter a question to start an analysis."
        return
    ss.pop("query_error", None)
    try:
        with st.spinner("Running and analyzing a read-only query…"):
            analyst = build_analyst(ss.connection, ss.settings, ss.owner, ss.persistent_identity, index=sql is None)
            try:
                history = tuple(ss.turns[-5:])
                result = analyst.ask(question, history=history) if sql is None else analyst.rerun(sql, question, history=history)
            finally:
                analyst.provider.close()
    except (DatabaseError, ProviderError, UnsafeQueryError, ValueError) as error:
        ss.query_error = str(error)
        return
    ss.result = result
    ss.result_token += 1
    ss.turns = [*ss.turns[-9:], result]
    try:
        history_store().add(ss.owner, ss.connection.namespace, question=result.question, sql=result.sql,
                            row_count=len(result.frame), elapsed_ms=result.elapsed_ms, truncated=result.truncated,
                            insights=result.insights, chart=result.chart, attempts=result.attempts)
    except (sqlite3.Error, OSError):
        logger.warning("Could not write query history")
        st.warning("The query succeeded. History storage is temporarily unavailable.")


def composer() -> None:
    with st.container(border=True, key="composer"):
        st.html(f'<div class="ql-composer-label">{icon("auto_awesome")}<span>Ask a question</span></div>')
        with st.form("question-form", border=False, enter_to_submit=True):
            entry, send = st.columns([5, 1.11], vertical_alignment="center")
            question = entry.text_input("Ask a question", placeholder="e.g. How has monthly revenue changed in 2026?",
                                        key="question_input", label_visibility="collapsed", max_chars=2000)
            submitted = send.form_submit_button("Run analysis", icon=":material/arrow_forward:", type="primary", width="stretch")
        if ss.connection.sample:
            st.html('<div class="ql-prompt-label">TRY AN EXAMPLE</div>')
            chips = st.columns(3, gap="small")
            for column, (label, prompt) in zip(chips, PROMPT_CHIPS):
                with column:
                    st.button(label, icon=":material/north_east:", on_click=select_prompt, args=(prompt,), width="stretch",
                              key=f"example-{label}")
        elif len(ss.turns) > 0:
            st.caption("Follow-up questions use the queries from this conversation.")
        if submitted:
            ss.pending_query = (question, None)
    if pending := ss.pop("pending_query", None):
        ask(*pending)
    if error := ss.get("query_error"):
        st.error(error)


def metric(label: str, value: str, description: str, symbol: str) -> None:
    st.html(f'<div class="ql-metric"><div><div class="ql-metric-label">{label}</div>'
            f'<div class="ql-metric-value">{value}</div><div class="ql-metric-detail">{description}</div>'
            f'</div><div class="ql-metric-icon">{icon(symbol)}</div></div>')


def render_metrics(result) -> None:
    frame = result.frame
    columns = st.columns(3, gap="medium")
    sample_dashboard = ss.connection.sample and result.question == DEFAULT_QUESTION and {"revenue", "orders", "month"} <= set(frame)
    if sample_dashboard and not frame.empty:
        revenue = float(frame["revenue"].sum())
        orders = int(frame["orders"].sum())
        average = revenue / orders if orders else 0
        first, last = (pd.Period(frame["month"].iloc[index], freq="M").strftime("%b %Y") for index in (0, -1))
        metrics = (
            ("Total revenue", f"${revenue:,.2f}", f"{first} – {last}", "payments"),
            ("Completed orders", f"{orders:,}", "Across the selected period", "shopping_bag"),
            ("Average order value", f"${average:,.2f}", "Revenue per completed order", "monitoring"),
        )
    else:
        metrics = (
            ("Rows returned", f"{len(frame):,}", "Result capped" if result.truncated else "Query results", "table_rows"),
            ("Columns", f"{len(frame.columns):,}", "Fields in this result", "view_column"),
            ("Query duration", f"{result.elapsed_ms:,.1f} ms", "Database execution", "schedule"),
        )
    for col, values in zip(columns, metrics):
        with col:
            metric(*values)


def filter_frame(frame: pd.DataFrame, token: int) -> pd.DataFrame:
    choices = {}
    for column, series in frame.items():
        try:
            values = series.dropna().unique()
        except TypeError:
            continue
        if 1 < len(values) <= 100:
            choices[column] = values.tolist()
    with st.popover("Filter", icon=":material/filter_list:", disabled=not choices, width="stretch"):
        column = st.selectbox("Filter column", list(choices), index=None, key=f"filter-column-{token}")
        if column is None:
            return frame
        selected = st.multiselect("Values to include", choices[column], key=f"filter-values-{token}-{column}", max_selections=100)
        if selected:
            return frame[frame[column].isin(selected)]
    return frame


def show_result(result) -> None:
    result_token = ss.result_token
    title_col, filter_col, export_col = st.columns([5.2, 0.9, 1.1], vertical_alignment="center")
    title_col.html('<div class="ql-section-title">Analysis results</div>')
    with filter_col:
        visible = filter_frame(result.frame, result_token)
    with export_col:
        export_control = st.popover("Export", icon=":material/download:", width="stretch")
    render_metrics(result)
    overview, table, sql_tab = st.tabs(["Overview", "Data table", "Generated SQL"])
    sample_currency = ss.connection.sample
    recommendation = dict(result.chart or {})
    if sample_currency:
        recommendation["currency"] = "$"
    chart = None
    with overview:
        chart_col, insight_col = st.columns([2.2, 1.1], gap="medium")
        with chart_col, st.container(border=True, key="chart-panel"):
            label, control = st.columns([2.7, 1], vertical_alignment="center")
            kind = control.selectbox("Chart type", CHART_TYPES, key=f"chart-kind-{result_token}", label_visibility="collapsed",
                                     disabled=result.frame.empty)
            selected_kind = None if kind == "Auto" else kind.lower()
            spec = choose_chart(visible, recommendation, kind=selected_kind)
            chart_title = (spec.title if spec else None) or "Query visualization"
            label.html(f'<div class="ql-panel-title">{html.escape(chart_title)}</div>')
            chart = build_chart(visible, recommendation, title="", kind=selected_kind)
            if chart is not None:
                chart.update_layout(title=None, height=262, margin=dict(l=9, r=14, t=12, b=9))
                if ss.connection.sample and result.question == DEFAULT_QUESTION and spec.kind == "line":
                    low, high = visible["revenue"].min(), visible["revenue"].max()
                    padding = max((high - low) * 0.12, 500)
                    chart.update_yaxes(range=[low - padding, high + padding])
                    chart.update_traces(
                        fill="tozeroy",
                        fillgradient={
                            "type": "vertical",
                            "colorscale": [[0, "rgba(72, 223, 137, 0.015)"], [1, "rgba(72, 223, 137, 0.19)"]],
                        },
                    )
                st.plotly_chart(chart, width="stretch", theme=None, key=f"result-chart-{result_token}",
                                config={"displaylogo": False, "responsive": True, "scrollZoom": True})
            else:
                st.info("No chart is available for this result and chart type. Open Data table to inspect the rows.")
            if len(visible) != len(result.frame):
                st.caption(f"Chart displays {len(visible):,} of {len(result.frame):,} returned rows.")
        with insight_col, st.container(border=True, key="insight-panel"):
            st.html(f'<div class="ql-insight-title"><span>{icon("auto_awesome")}</span>Analyst insights</div>')
            st.markdown(plain_links(result.insights) if result.insights else "No further patterns were identified in the returned rows.")
            st.html(f'<div class="ql-insight-foot">{icon("check_circle")}Based on {len(result.frame):,} returned rows</div>')
    with table:
        count = f"{len(visible):,} of {len(result.frame):,}" if len(visible) != len(result.frame) else f"{len(visible):,}"
        st.caption(f"{count} rows · {len(result.frame.columns)} columns")
        st.dataframe(visible, width="stretch", hide_index=True, height=370)
    with sql_tab:
        st.caption(plain_links(result.question))
        st.code(result.sql, language="sql", wrap_lines=True)
        st.caption(f"{DIALECT_LABELS[ss.connection.dialect]} · {result.attempts} execution attempt{'s' if result.attempts != 1 else ''}")
        for repair in result.repair_notes:
            st.caption(repair)
    with export_control:
        st.download_button("Results CSV", data=partial(to_csv, result.frame), file_name="query-results.csv", mime="text/csv", width="stretch", on_click="ignore")
        st.download_button("Results Excel", data=partial(to_excel, result.frame), file_name="query-results.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch", on_click="ignore")
        if chart is not None:
            st.download_button("Interactive chart", data=partial(chart_html, chart), file_name="query-chart.html", mime="text/html", width="stretch", on_click="ignore")
        report_figure = chart if len(visible) == len(result.frame) else build_chart(result.frame, result.chart)
        report = partial(report_html, question=result.question, sql=result.sql, frame=result.frame, insights=result.insights,
                         elapsed_ms=result.elapsed_ms, truncated=result.truncated, figure=report_figure)
        st.download_button("Analysis report", data=report, file_name="analysis-report.html", mime="text/html", width="stretch", on_click="ignore")
    if result.truncated:
        st.warning("The query reached the row limit. Narrow the question for a more complete analysis.")
    for notice in result.notices:
        st.info(notice)


def workspace_view() -> None:
    heading("Ask your data", "Turn everyday questions into SQL, charts, and insights.", show_status=True)
    composer()
    if ss.result is not None:
        show_result(ss.result)
    elif not ss.get("query_error"):
        st.info("Your query results and analyst insights will appear here.")


def schema_view() -> None:
    connection = ss.connection
    heading("Schema explorer", f"Explore the tables and relationships in {html.escape(connection.label)}.")
    search = st.text_input("Find a table or column", placeholder="Search tables or columns…", key="schema-search")
    needle = search.casefold().strip()
    tables = [table for table in connection.tables if not needle or needle in table["name"].casefold()
              or any(needle in column["name"].casefold() for column in table["columns"])]
    st.caption(f"{len(tables)} of {len(connection.tables)} tables and views · {DIALECT_LABELS[connection.dialect]}")
    for item in tables:
        with st.expander(f"{item['name']} · {len(item['columns'])} columns", icon=":material/table_chart:"):
            primary = set(item["primary_key"])
            foreign = {column for key in item["foreign_keys"] for column in key["columns"]}
            rows = [{"Column": col["name"], "Type": col["type"], "Nullable": "Yes" if col["nullable"] else "No",
                     "Key": " / ".join(name for name, keys in (("PK", primary), ("FK", foreign)) if col["name"] in keys)}
                    for col in item["columns"]]
            st.dataframe(rows, width="stretch", hide_index=True)
            for key in item["foreign_keys"]:
                target = ".".join(filter(None, (key["referred_schema"], key["referred_table"])))
                st.text(f"{', '.join(key['columns'])} → {target} ({', '.join(key['referred_columns'])})")
    if not tables:
        st.info("No visible tables or views match the search.")


def history_view() -> None:
    heading("Query history", "Reopen a previous question or rerun its SQL on the active database.")
    try:
        items = history_store().list(ss.owner, ss.connection.namespace, limit=50)
    except (sqlite3.Error, OSError):
        st.error("Query history is temporarily unavailable.")
        return
    if not items:
        st.info("Questions you run on this database will appear here.")
        return
    search = st.text_input("Search past questions", placeholder="Search by question or SQL…")
    needle = search.casefold().strip()
    matches = [entry for entry in items if not needle or needle in entry.question.casefold() or needle in entry.sql.casefold()]
    try:
        browser_timezone = ZoneInfo(st.context.timezone) if st.context.timezone else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        browser_timezone = timezone.utc
    for entry in matches:
        label = plain_links(entry.question.replace("\n", " "))
        with st.expander(label, icon=":material/history:"):
            local_date = entry.created_at.astimezone(browser_timezone).strftime("%d %b %Y, %H:%M %Z")
            st.caption(f"{local_date} · {entry.row_count:,} rows · {entry.elapsed_ms:,.1f} ms")
            st.code(entry.sql, language="sql", wrap_lines=True)
            if entry.insights:
                st.markdown(plain_links(entry.insights))
            st.button("Rerun query", type="primary", icon=":material/replay:", key=f"rerun-{entry.id}",
                      on_click=queue_rerun, args=(entry.question, entry.sql))
    if not matches:
        st.info("No previous queries match the search.")


def save_settings(provider: str) -> None:
    config = AI_PROVIDERS[provider]
    api_key = ss.pop(f"draft_api_key_{provider}", "").strip()
    if api_key or provider != ss.settings["provider"]:
        ss.settings["api_key"] = api_key
    ss.settings["provider"] = provider
    ss.settings["model"] = ss[f"draft_model_{provider}"].strip() or config.default_model
    ss.settings["share_result_samples"] = ss[f"draft_share_{provider}"]
    ss.settings["use_ai_for_sample"] = ss[f"draft_sample_{provider}"]
    ss.settings_notice = "Settings saved for this session."


def clear_key() -> None:
    ss.settings["api_key"] = ""
    ss.pop(f"draft_api_key_{ss.settings['provider']}", None)
    ss.settings_notice = "API key cleared for this session."


def settings_view() -> None:
    heading("Settings", "Choose an AI provider and control what information is shared for analysis.")
    with st.container(border=True, key="settings-card"):
        active_provider = ss.settings["provider"]
        provider = st.selectbox(
            "AI provider", list(AI_PROVIDERS), index=list(AI_PROVIDERS).index(active_provider),
            format_func=lambda value: AI_PROVIDERS[value].label, key="draft_provider",
        )
        config = AI_PROVIDERS[provider]
        has_key = provider == active_provider and bool(ss.settings["api_key"])
        with st.form(f"ai-settings-{provider}", border=False):
            st.text_input(
                f"{config.label} API key", type="password", key=f"draft_api_key_{provider}",
                placeholder="Key saved for this session" if has_key else "Enter a key for this session",
            )
            st.text_input(
                "Model ID", value=ss.settings["model"] if provider == active_provider else config.default_model,
                key=f"draft_model_{provider}", help="Use a text model that supports structured JSON output.",
            )
            st.checkbox("Use AI with the sample database", value=ss.settings["use_ai_for_sample"], key=f"draft_sample_{provider}")
            st.checkbox(
                "Share a small sample of returned rows", value=ss.settings["share_result_samples"],
                key=f"draft_share_{provider}",
            )
            st.caption("The selected provider receives your question, relevant schema, recent questions and SQL, and aggregate statistics. Raw result samples are opt-in.")
            st.form_submit_button("Save settings", type="primary", on_click=save_settings, args=(provider,))
        if has_key:
            st.button("Clear API key", on_click=clear_key)
        if message := ss.pop("settings_notice", None):
            st.success(message)
    if st.user.get("is_logged_in", False):
        if st.button("Log out", icon=":material/logout:"):
            ss.connection.close()
            st.logout()


initialize()
sidebar()
topbar()
views = {"Workspace": workspace_view, "Schema explorer": schema_view, "Query history": history_view, "Settings": settings_view}
views[ss.page]()
