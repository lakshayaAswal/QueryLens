# QueryLens

QueryLens is a Streamlit workspace for exploring SQLite, PostgreSQL, and MySQL databases in plain English. Pick an AI provider, ask a question about your data, and inspect the generated SQL, query results, interactive chart, and summary. QueryLens discovers the database schema and validates each read-only query before running it.

- Use Google Gemini, OpenAI GPT, xAI Grok, or a model from OpenRouter with your own API key.
- Try the included synthetic retail database and built-in questions without an API key.
- Explore table schemas, ask follow-up questions, and rerun queries from your history.
- Export results to CSV or Excel, and download charts and reports as HTML.

## Run locally

Install Python 3.11 or 3.12. Clone this repository or extract the entire source ZIP, then open the folder containing `app.py`.

### Windows

Double-click `start-windows.cmd` to create the virtual environment, install dependencies, and start the app. To do the same from PowerShell, run these commands in the project folder.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Use `py -3.11` in the first command if Python 3.11 is installed. If your terminal already shows `(.venv)`, you can use `python` for the install and launch commands.

### macOS or Linux

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Use `python3.11` in the first command if Python 3.11 is installed.

Keep the terminal open while using QueryLens. Open the workspace in your browser. [http://127.0.0.1:8501](http://127.0.0.1:8501)

## Try the retail demo

1. The workspace opens with **Sample Commerce**, a synthetic retail database.
2. Select **Sales by region**, or ask “Show revenue by region.” Follow up with “Now show only the top three regions.”
3. Review the generated SQL and result table. Hover or zoom on the chart, or use the chart controls to choose another view.
4. Open **Query history** to revisit a successful query. Rerunning it executes the saved SQL against the current data.

The sample covers January 2025 through October 2026 and counts revenue from completed orders. Enable the selected AI provider for the sample in **Settings** to ask questions beyond the built-in examples.

## Set up an AI provider

In **Settings**, choose a provider, enter its API key, choose a model ID or use the default, and save. Keys entered in the app are scoped to the current session. Use **Clear API key** to remove the active provider's key from that session.

| Provider | Default model | API key | Model override |
| --- | --- | --- | --- |
| Google Gemini | `gemini-2.5-flash` | `GEMINI_API_KEY` | `GEMINI_MODEL` |
| OpenAI | `gpt-4.1-mini` | `OPENAI_API_KEY` | `OPENAI_MODEL` |
| xAI | `grok-4.7` | `XAI_API_KEY` | `XAI_MODEL` |
| OpenRouter | `openai/gpt-4.1-mini` | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` |

Create an API key for the service you select. With OpenRouter, use the model ID from its catalog, including the provider prefix. Choose a text model that supports JSON object responses. Availability and usage charges depend on the provider and your API account. [Google AI Studio](https://aistudio.google.com/apikey) · [OpenAI Platform](https://platform.openai.com/api-keys) · [xAI Console](https://console.x.ai/) · [OpenRouter keys](https://openrouter.ai/settings/keys) · [OpenRouter models](https://openrouter.ai/models)

To set a startup provider, set `QUERYLENS_AI_PROVIDER` to `gemini`, `openai`, `xai`, or `openrouter` and set the corresponding key and optional model in the process environment before launching Streamlit. The default provider is Gemini. The `.env.example` file lists the supported variables.

## Connect your database

1. Select **Connect database** in the sidebar.
2. For **SQLite**, upload a standalone `.db`, `.sqlite`, or `.sqlite3` backup of up to 50 MB. For **PostgreSQL** or **MySQL**, enter the hostname, port, database name, username, password, and TLS mode. PostgreSQL also accepts a schema name.
3. Give your database login `SELECT` access to the tables and views you want to explore. For a remote database, use the option that verifies the TLS certificate and hostname.
4. Open **Schema explorer** to inspect discovered tables, column types, primary keys, and foreign keys.
5. Return to **Workspace** and ask a question such as “Which product categories generated the most revenue last month?”

A remote database must be reachable from the machine running QueryLens. If the database uses a private root CA, set `PGSSLROOTCERT` for PostgreSQL or `AI_SQL_MYSQL_SSL_CA` for MySQL. Use MySQL 5.7.8 or later for statement time limits.

## Configuration

| Environment variable | Purpose |
| --- | --- |
| `QUERYLENS_AI_PROVIDER` | Initial AI provider. Defaults to `gemini`. |
| `QUERYLENS_DATA_DIR` | Application data directory. Defaults to `.querylens` inside the project. |
| `QUERYLENS_SQLITE_PATH` | Optional path to an operator-provided SQLite database. |
| `QUERYLENS_TIMEOUT_SECONDS` | Database query timeout. Defaults to 10 seconds. |
| `QUERYLENS_MAX_ROWS` | Maximum rows per query. Defaults to 1,000. |
| `QUERYLENS_REQUIRE_LOGIN` | Set to `1` to require Streamlit OIDC sign-in. |
| `QUERYLENS_DB_HOSTS` | Comma-separated allowlist for user-entered remote database hosts. |

To offer a server-configured PostgreSQL or MySQL connection, set `QUERYLENS_DB_DIALECT` and the applicable `QUERYLENS_DB_HOST`, `QUERYLENS_DB_PORT`, `QUERYLENS_DB_NAME`, `QUERYLENS_DB_SCHEMA`, `QUERYLENS_DB_USER`, `QUERYLENS_DB_PASSWORD`, and `QUERYLENS_DB_SSL_MODE` values. Grant workspace access only to users authorized to query that database.

## Data handling

- SQL validation permits a single read-only query. Database controls enforce read-only access, timeouts, and row limits. The analyst can ask the selected model to repair a query once after an eligible database error.
- The selected provider receives your question, relevant schema details, recent questions and SQL, and computed result summaries. Sharing a small sample of returned rows is off by default. When you use OpenRouter, its service routes requests to a model provider.
- Results stay in the active workspace. Query history is stored in SQLite and scoped to the user and database connection. Chroma computes embeddings locally from schema text and successful questions and SQL. Signed-in users can reuse indexed examples across sessions.

## Host the workspace

Use a host that can run a long-lived Streamlit Python process. The proxy needs to support WebSockets, and the host needs a writable data directory for SQLite history, uploaded databases, and the Chroma index. Set `QUERYLENS_DATA_DIR` to a mounted persistent volume for data that should survive restarts.

Streamlit ties a user session to a server process. Vercel Functions close WebSocket connections at the function's maximum duration, and a reconnect can reach a different instance. This workspace requires a Streamlit-compatible runtime that preserves session affinity. [Streamlit architecture](https://docs.streamlit.io/develop/concepts/architecture/architecture) · [Vercel WebSockets](https://vercel.com/docs/functions/websockets)

For a shared deployment, configure Streamlit OIDC in `.streamlit/secrets.toml`, set `QUERYLENS_REQUIRE_LOGIN=1`, and serve the workspace over HTTPS. Set `QUERYLENS_DB_HOSTS` and restrict outbound database access to approved hosts. Store API keys and database passwords in your hosting environment's secret manager. [Streamlit authentication documentation](https://docs.streamlit.io/develop/concepts/connections/authentication)

When your hosting platform needs the app to listen on all container interfaces, override the local loopback address in the start command.

```bash
python -m streamlit run app.py --server.address 0.0.0.0 --server.headless true
```

## Optional HTTP API

The FastAPI factory uses the same analyst with one server-configured database. Pass a `ConnectionConfig`, the provider ID and API key, and a separate API token containing at least 32 printable ASCII characters without spaces. You can supply a model ID with `model`. Existing Gemini integrations can still use `gemini_api_key` and `gemini_model`.

Serve the resulting ASGI app with Uvicorn. Send an authenticated `POST /v1/ask` request with a JSON body such as `{"question": "Show sales by region"}` and an `Authorization: Bearer <token>` header. `GET /health` reports service health.

## Run tests

After installing the dependencies, run the test suite from the project folder.

```bash
python -m unittest discover -s tests -v
```

## Project structure

| Component | Responsibility |
| --- | --- |
| `app.py`, `style.css`, `ai_sql_agent/workspace.py` | Streamlit interface, session settings, and database connections |
| `ai_sql_agent/database.py`, `ai_sql_agent/sql_safety.py` | Schema discovery, SQL validation, and bounded query execution |
| `ai_sql_agent/ai.py`, `ai_sql_agent/analyst.py`, `ai_sql_agent/retrieval.py` | Model providers, SQL planning and repair, and local retrieval |
| `ai_sql_agent/analytics.py`, `ai_sql_agent/exports.py` | Result summaries, charts, and downloads |
| `ai_sql_agent/history.py`, `ai_sql_agent/demo.py` | Query history and synthetic retail data |
| `ai_sql_agent/api.py` | Optional FastAPI factory |
| `tests/` | Automated tests |
