const byId = (id) => document.getElementById(id);
const viewNames = {workspace: "Workspace", schema: "Schema explorer", history: "Query history", settings: "Settings"};
const dialectNames = {sqlite: "SQLite", postgresql: "PostgreSQL", mysql: "MySQL"};
const promptLabels = {
  "How has monthly revenue changed in 2026?": "Monthly revenue",
  "Show the top 10 customers by revenue": "Top customers",
  "Show revenue by region": "Sales by region",
  "Which products generated the most revenue?": "Top products",
  "Compare monthly sales for the last two years": "Compare monthly sales",
};
const state = {
  config: null,
  connection: null,
  providers: new Map(),
  ai: {provider: "", useForSample: false, shareSamples: false},
  busy: "",
  view: "workspace",
  result: null,
  history: [],
  turns: [],
};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function request(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 155000);
  try {
    const options = {cache: "no-store", redirect: "error", signal: controller.signal};
    if (body !== undefined) {
      options.method = "POST";
      options.headers = {"Content-Type": "application/json"};
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload) {
      const detail = payload?.detail ?? payload?.error;
      if (typeof detail === "string") throw new Error(detail);
      if (response.status === 413) throw new Error("This database or request exceeds the upload limit.");
      throw new Error("QueryLens returned an error (" + response.status + "). Please try again.");
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The request timed out. Please try again.");
    if (error instanceof TypeError) throw new Error("Could not reach QueryLens. Check your connection and try again.");
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function setMessage(id, text = "") {
  const node = byId(id);
  node.textContent = text;
  node.hidden = text.length === 0;
}

function refreshControls() {
  const ready = Boolean(state.connection);
  byId("open-connection").disabled = !state.config || Boolean(state.busy);
  byId("run-query").disabled = !ready || Boolean(state.busy);
  byId("new-query").disabled = !ready || Boolean(state.busy);
  byId("connect-database").disabled = Boolean(state.busy);
  byId("close-connection").disabled = state.busy === "connect";
  byId("retry-startup").disabled = Boolean(state.busy);
  for (const button of byId("prompt-list").querySelectorAll("button")) button.disabled = !ready || Boolean(state.busy);
  byId("question-form").setAttribute("aria-busy", String(state.busy === "query"));
  const provider = state.providers.get(state.ai.provider);
  const sample = state.connection?.database.kind === "sample";
  byId("mode-label").textContent = !ready ? "Connecting" :
    sample && !state.ai.useForSample ? "Sample mode" :
    provider?.key ? provider.label + " configured" : "AI setup needed";
}

function showView(view, {focus = true} = {}) {
  state.view = view;
  byId("breadcrumb-view").textContent = viewNames[view];
  for (const button of document.querySelectorAll("[data-view]")) {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  for (const panel of document.querySelectorAll("[data-panel]")) panel.hidden = panel.dataset.panel !== view;
  if (view === "schema") renderSchema();
  if (view === "history") renderHistory();
  if (view === "workspace") drawChart();
  if (focus) byId(view + "-view").querySelector("h1").focus();
}

function renderConnection() {
  const connection = state.connection;
  byId("connection-name").textContent = connection.name;
  byId("connection-name").title = connection.name;
  const count = connection.tables.length;
  byId("connection-meta").textContent = dialectNames[connection.dialect] + " · " + count + " " + (count === 1 ? "table" : "tables");
  byId("connection-dot").hidden = false;
  const sidebar = byId("sidebar-tables");
  sidebar.replaceChildren();
  byId("sidebar-schema").hidden = count === 0;
  for (const table of connection.tables.slice(0, 5)) {
    const item = element("li");
    const button = element("button", "▤  " + table.name);
    button.type = "button";
    button.onclick = () => {
      byId("schema-search").value = table.name;
      showView("schema");
    };
    item.append(button);
    sidebar.append(item);
  }

  const prompts = byId("prompt-list");
  prompts.replaceChildren();
  const isSample = connection.database.kind === "sample";
  if (isSample) {
    for (const question of state.config.suggested_questions) {
      const button = element("button", promptLabels[question] ?? question, "secondary");
      button.type = "button";
      button.onclick = () => {
        byId("question").value = question;
        runQuery(question);
      };
      prompts.append(button);
    }
  }
  byId("sample-prompts").hidden = !isSample;
  renderResult();
  if (state.view === "schema") renderSchema();
  if (state.view === "history") renderHistory();
}

function activateConnection(database, name, schema) {
  state.connection = {database, name, dialect: schema.dialect, tables: schema.tables, truncated: schema.truncated};
  state.turns = [];
  state.history = [];
  state.result = null;
  byId("question").value = "";
  byId("schema-search").value = "";
  setMessage("query-error");
  byId("query-status").textContent = "";
  renderConnection();
}

function populateConfig(config) {
  const select = byId("provider");
  select.replaceChildren();
  state.providers.clear();
  for (const provider of config.providers) {
    state.providers.set(provider.id, {...provider, key: "", model: ""});
    select.append(new Option(provider.label, provider.id));
  }
  state.ai.provider = select.value;
  select.disabled = false;
  byId("save-settings").disabled = false;

  const databaseSelect = byId("database-kind");
  for (const option of Array.from(databaseSelect.options).slice(2)) option.remove();
  if (config.remote_enabled) {
    databaseSelect.append(new Option("PostgreSQL", "postgresql"), new Option("MySQL", "mysql"));
  }
  byId("sqlite-help").textContent = "Upload a standalone database of up to " +
    (config.max_sqlite_bytes / 1024 / 1024).toLocaleString() + " MB.";
  renderProviderDraft();
}

async function start() {
  state.busy = "start";
  byId("connection-meta").textContent = "Connecting…";
  byId("retry-startup").hidden = true;
  setMessage("startup-error");
  refreshControls();
  const database = {kind: "sample"};
  try {
    const [config, schema] = await Promise.all([
      request("/api/config"),
      request("/api/schema", {database}),
    ]);
    state.config = config;
    populateConfig(config);
    activateConnection(database, "Sample Commerce", schema);
  } catch (error) {
    setMessage("startup-error", error.message);
    byId("connection-meta").textContent = "Disconnected";
    byId("retry-startup").hidden = false;
  } finally {
    state.busy = "";
    refreshControls();
  }
  const first = state.config?.suggested_questions[0];
  if (state.connection && first) {
    byId("question").value = first;
    await runQuery(first);
  }
}

function renderProviderDraft() {
  const provider = state.providers.get(byId("provider").value);
  if (!provider) return;
  byId("model").value = provider.model;
  byId("model").placeholder = provider.default_model;
  byId("api-key").value = "";
  byId("api-key").placeholder = provider.key ? "Enter a replacement key" : "Enter your " + provider.label + " API key";
  byId("key-status").textContent = provider.key ?
    "A key is set for " + provider.label + " in this session." :
    "Keys are kept in this page during your session.";
  byId("clear-api-key").disabled = !provider.key;
  byId("settings-status").textContent = "";
}

function saveSettings(event) {
  event.preventDefault();
  const provider = state.providers.get(byId("provider").value);
  if (!provider) return;
  const key = byId("api-key").value.trim();
  if (key) provider.key = key;
  provider.model = byId("model").value.trim();
  state.ai = {
    provider: provider.id,
    useForSample: byId("use-ai-sample").checked,
    shareSamples: byId("share-samples").checked,
  };
  renderProviderDraft();
  refreshControls();
  byId("settings-status").textContent = "Settings saved for this session.";
}

function clearKey() {
  const provider = state.providers.get(byId("provider").value);
  if (!provider?.key) return;
  provider.key = "";
  renderProviderDraft();
  refreshControls();
  byId("settings-status").textContent = provider.label + " API key cleared.";
}

function updateConnectionFields() {
  const kind = byId("database-kind").value;
  const remote = kind === "postgresql" || kind === "mysql";
  byId("sqlite-fields").hidden = kind !== "sqlite";
  byId("sqlite-file").required = kind === "sqlite";
  byId("remote-fields").hidden = !remote;
  byId("db-schema-field").hidden = kind !== "postgresql";
  for (const field of ["db-host", "db-port", "db-name", "db-username"]) byId(field).required = remote;
  byId("db-port").value = kind === "postgresql" ? "5432" : kind === "mysql" ? "3306" : "";
}

function readBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",", 2)[1]);
    reader.onerror = () => reject(new Error("The selected file could not be read."));
    reader.readAsDataURL(file);
  });
}

async function connectionDraft() {
  const kind = byId("database-kind").value;
  if (kind === "sample") return {database: {kind}, name: "Sample Commerce"};
  if (kind === "sqlite") {
    const [file] = byId("sqlite-file").files;
    if (!file || !file.size) throw new Error("Choose a SQLite database file.");
    if (file.size > state.config.max_sqlite_bytes) throw new Error("The SQLite file exceeds the upload limit for this deployment.");
    return {database: {kind, file_b64: await readBase64(file)}, name: file.name};
  }
  const database = {
    kind,
    host: byId("db-host").value.trim(),
    port: byId("db-port").valueAsNumber,
    database: byId("db-name").value.trim(),
    schema: kind === "postgresql" ? byId("db-schema").value.trim() : "",
    username: byId("db-username").value.trim(),
    password: byId("db-password").value,
  };
  return {database, name: database.database};
}

async function connectDatabase(event) {
  event.preventDefault();
  if (state.busy) return;
  state.busy = "connect";
  refreshControls();
  byId("connect-database").textContent = "Connecting…";
  setMessage("connection-error");
  try {
    const {database, name} = await connectionDraft();
    const schema = await request("/api/schema", {database});
    activateConnection(database, name, schema);
    byId("connection-dialog").close();
    showView("workspace");
  } catch (error) {
    setMessage("connection-error", error.message);
  } finally {
    state.busy = "";
    byId("connect-database").textContent = "Connect";
    refreshControls();
  }
}

async function runQuery(rawQuestion) {
  const question = rawQuestion.trim();
  if (!question || state.busy || !state.connection) return;
  setMessage("query-error");
  const database = state.connection.database;
  const sample = database.kind === "sample";
  const useAI = !sample || state.ai.useForSample;
  const provider = state.providers.get(state.ai.provider);
  if (useAI && !provider?.key) {
    setMessage("query-error", "Add an AI provider API key in Settings, then run your question again.");
    return;
  }
  const payload = {database, question, history: state.turns.slice(-5)};
  if (useAI) {
    payload.ai = {provider: provider.id, api_key: provider.key, model: provider.model || provider.default_model};
    payload.share_result_samples = state.ai.shareSamples;
  }

  state.busy = "query";
  refreshControls();
  byId("query-status").textContent = "Running a read-only analysis…";
  try {
    const analysis = await request("/api/query", payload);
    state.result = analysis;
    state.turns = [...state.turns.slice(-4), {question: analysis.question, sql: analysis.sql.slice(0, 8000)}];
    state.history = [{analysis, at: new Date()}, ...state.history.slice(0, 14)];
    selectTab("overview-tab");
    renderResult();
    if (state.view === "history") renderHistory();
    byId("query-status").textContent = "Analysis complete.";
  } catch (error) {
    setMessage("query-error", error.message);
    byId("query-status").textContent = "";
  } finally {
    state.busy = "";
    refreshControls();
  }
}

function displayValue(value) {
  if (value === null || value === undefined) return "–";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function dataTable(columns, rows) {
  const table = element("table");
  const header = table.createTHead().insertRow();
  for (const column of columns) {
    const cell = element("th", column);
    cell.scope = "col";
    header.append(cell);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const line = body.insertRow();
    for (const [index] of columns.entries()) line.insertCell().textContent = displayValue(row[index]);
  }
  return table;
}

function renderResult() {
  const analysis = state.result;
  byId("results").hidden = !analysis;
  byId("result-empty").hidden = !state.connection || Boolean(analysis);
  if (!analysis) {
    for (const id of ["table-result", "sql-question", "sql-code", "sql-meta", "insights", "result-notices"]) byId(id).replaceChildren();
    return;
  }
  const {columns, data} = analysis.result;
  byId("row-count").textContent = data.length.toLocaleString();
  byId("column-count").textContent = columns.length.toLocaleString();
  byId("query-time").textContent = Number(analysis.elapsed_ms).toLocaleString(undefined, {maximumFractionDigits: 1}) + " ms";
  byId("table-result").replaceChildren(data.length ?
    dataTable(columns, data) : element("p", "This query returned no rows.", "muted"));
  byId("sql-question").textContent = analysis.question;
  byId("sql-code").textContent = analysis.sql;
  byId("sql-meta").textContent = dialectNames[state.connection.dialect] + " · " + analysis.attempts +
    " execution " + (analysis.attempts === 1 ? "attempt" : "attempts");

  const insights = byId("insights");
  insights.replaceChildren();
  const observations = (analysis.insights || "").split("\n").map((line) => line.replace(/^\s*[-*]\s+/, "").trim()).filter(Boolean);
  if (observations.length > 1) {
    const list = element("ul");
    for (const line of observations) list.append(element("li", line));
    insights.append(list);
  } else {
    insights.append(element("p", observations[0] || "The result table contains the returned data."));
  }

  const notices = [...(analysis.notices ?? [])];
  if (analysis.truncated) notices.unshift("The result was shortened. Narrow the question to see more detail.");
  byId("result-notices").replaceChildren(...notices.map((notice) => element("li", notice)));
  byId("result-notices").hidden = notices.length === 0;
  drawChart();
}

function renderSchema() {
  const connection = state.connection;
  const result = byId("schema-results");
  result.replaceChildren();
  if (!connection) {
    result.append(element("p", "Connect a database to explore its schema.", "panel empty-state"));
    return;
  }
  byId("schema-caption").textContent = "Explore the tables and relationships in " + connection.name + ".";
  if (connection.truncated) result.append(element("p", "The schema preview was shortened to fit this deployment’s size limits.", "muted"));
  const needle = byId("schema-search").value.trim().toLowerCase();
  const tables = connection.tables.filter((table) => table.name.toLowerCase().includes(needle) ||
    table.columns.some((column) => column.name.toLowerCase().includes(needle)));
  if (!tables.length) {
    result.append(element("p", "No tables or views match this search.", "panel empty-state"));
    return;
  }
  result.append(element("p", tables.length + " of " + connection.tables.length + " tables and views", "muted"));
  for (const table of tables) {
    const details = element("details", undefined, "panel schema-table");
    details.open = Boolean(needle) && tables.length === 1;
    details.append(element("summary", table.name + " · " + table.columns.length + " columns"));
    const primary = new Set(table.primary_key);
    const foreign = new Set(table.foreign_keys.flatMap((key) => key.columns));
    const rows = table.columns.map((column) => [
      column.name,
      column.type,
      column.nullable ? "Yes" : "No",
      [primary.has(column.name) ? "PK" : "", foreign.has(column.name) ? "FK" : ""].filter(Boolean).join(" / "),
    ]);
    const wrap = element("div", undefined, "table-wrap");
    wrap.append(dataTable(["Column", "Type", "Nullable", "Key"], rows));
    details.append(wrap);
    for (const key of table.foreign_keys) {
      const target = [key.referred_schema, key.referred_table].filter(Boolean).join(".");
      details.append(element("p", key.columns.join(", ") + " → " + target + " (" + key.referred_columns.join(", ") + ")", "muted"));
    }
    result.append(details);
  }
}

function renderHistory() {
  const list = byId("history-list");
  list.replaceChildren();
  byId("history-empty").hidden = state.history.length > 0;
  list.hidden = state.history.length === 0;
  for (const {analysis, at} of state.history) {
    const item = element("li");
    const button = element("button", analysis.question, "secondary");
    button.type = "button";
    button.append(element("small", at.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"}) +
      " · " + analysis.result.data.length.toLocaleString() + " rows"));
    button.onclick = () => {
      if (state.busy) return;
      byId("question").value = analysis.question;
      byId("query-status").textContent = "";
      setMessage("query-error");
      state.result = analysis;
      selectTab("overview-tab");
      renderResult();
      showView("workspace");
    };
    item.append(button);
    list.append(item);
  }
}

function selectTab(id, focus = false) {
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    const active = tab.id === id;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    byId(tab.getAttribute("aria-controls")).hidden = !active;
    if (active && focus) tab.focus();
  }
  if (id === "overview-tab") drawChart();
}

function csvCell(value) {
  if (value === null || value === undefined) return '""';
  let text = displayValue(value);
  if (typeof value !== "number" && /^\s*[=+@-]|^[\t\r\n]/.test(text)) text = "'" + text;
  return '"' + text.replaceAll('"', '""') + '"';
}

function downloadCsv() {
  if (!state.result) return;
  const {columns, data} = state.result.result;
  const csv = [columns, ...data].map((row) => row.map(csvCell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob(["\ufeff", csv], {type: "text/csv;charset=utf-8"}));
  const link = element("a");
  link.href = url;
  link.download = "querylens-results.csv";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function numeric(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string" || !/^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim())) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function chartData(analysis) {
  const {columns, data} = analysis.result;
  const suggestion = analysis.chart || {};
  const rows = data.slice(0, 36);
  const xIndex = columns.indexOf(suggestion.x);
  const yIndex = columns.indexOf(suggestion.y);
  const measures = columns.map((_, index) => index).filter((index) => {
    const present = rows.filter((row) => row[index] !== null && row[index] !== "");
    return present.length > 0 && present.every((row) => numeric(row[index]) !== null);
  });
  const measure = measures.includes(yIndex) ? yIndex : measures.find((i) => i !== xIndex) ?? measures[0];
  if (measure === undefined) return null;
  const category = xIndex >= 0 && xIndex !== measure ? xIndex :
    columns.findIndex((_, index) => index !== measure && !measures.includes(index));
  const points = rows.map((row, index) => ({
    label: category < 0 ? String(index + 1) : displayValue(row[category]),
    value: numeric(row[measure]),
  })).filter((point) => point.value !== null);
  if (!points.length) return null;
  return {
    title: suggestion.title || columns[measure] + (category < 0 ? " by row" : " by " + columns[category]),
    line: (suggestion.type === "line" || points.every((point) => /^\d{4}-\d{2}/.test(point.label))) && points.length > 1,
    points,
  };
}

function drawChart() {
  if (!state.result || state.view !== "workspace" || byId("overview-result").hidden) return;
  const chart = chartData(state.result);
  const canvas = byId("chart");
  canvas.hidden = !chart;
  byId("chart-empty").hidden = Boolean(chart);
  byId("chart-title").textContent = chart?.title || "Query visualization";
  setMessage("chart-caption", chart && state.result.result.data.length > 36 ? "Chart uses the first 36 returned rows." : "");
  if (!chart) return;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.scale(ratio, ratio);
  const points = chart.points;
  const x0 = width < 350 ? 47 : 58;
  const y0 = 13;
  const plotWidth = width - x0 - 15;
  const plotHeight = height - y0 - 29;
  if (plotWidth < 50) return;
  const values = points.map((point) => point.value);
  const high = Math.max(0, ...values);
  const low = Math.min(0, ...values);
  const range = high - low || 1;
  const formatAxis = new Intl.NumberFormat(undefined, {notation: "compact", maximumFractionDigits: 2});
  // Normalize first to keep the chart usable for finite values close to Number.MAX_VALUE.
  const scale = Number.isFinite(range) ? 1 : Math.max(Math.abs(low), Math.abs(high));
  const span = high / scale - low / scale || 1;
  const y = (number) => y0 + (high / scale - number / scale) / span * plotHeight;
  const steps = 4;
  ctx.font = '11px system-ui, sans-serif';
  for (let step = 0; step <= steps; step++) {
    const row = y0 + (step / steps) * plotHeight;
    const fraction = step / steps;
    ctx.beginPath();
    ctx.strokeStyle = "#29394f";
    ctx.moveTo(x0, row);
    ctx.lineTo(x0 + plotWidth, row);
    ctx.stroke();
    ctx.fillStyle = "#a6b5c9";
    ctx.textAlign = "right";
    ctx.fillText(formatAxis.format(high * (1 - fraction) + low * fraction), x0 - 7, row + 4);
  }
  const slot = plotWidth / points.length;
  const x = (index) => x0 + slot * (index + .5);
  ctx.fillStyle = "#57e99b";
  if (chart.line) {
    ctx.beginPath();
    ctx.strokeStyle = "#57e99b";
    ctx.lineWidth = 2.2;
    points.forEach((point, index) => {
      if (index === 0) ctx.moveTo(x(index), y(point.value));
      else ctx.lineTo(x(index), y(point.value));
    });
    ctx.stroke();
    points.forEach((point, index) => {
      ctx.beginPath();
      ctx.arc(x(index), y(point.value), 3.2, 0, Math.PI * 2);
      ctx.fill();
    });
  } else {
    const bar = Math.min(slot * .62, 48);
    const baseline = y(0);
    points.forEach((point, index) => {
      const top = Math.min(baseline, y(point.value));
      ctx.fillRect(x(index) - bar / 2, top, bar, Math.abs(baseline - y(point.value)));
    });
  }
  const stride = Math.max(1, Math.ceil(67 / slot));
  ctx.textAlign = "center";
  ctx.fillStyle = "#a6b5c9";
  points.forEach((point, index) => {
    if (index % stride === 0) {
      const label = point.label.length > 15 ? point.label.slice(0, 14) + "…" : point.label;
      ctx.fillText(label, x(index), height - 8, Math.min(94, slot * stride - 7));
    }
  });
}

for (const button of document.querySelectorAll("[data-view]")) button.onclick = () => showView(button.dataset.view);
for (const tab of document.querySelectorAll('[role="tab"]')) {
  tab.onclick = () => selectTab(tab.id);
  tab.onkeydown = (event) => {
    const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
    const index = tabs.indexOf(tab);
    const next = {
      ArrowLeft: (index + tabs.length - 1) % tabs.length,
      ArrowRight: (index + 1) % tabs.length,
      Home: 0,
      End: tabs.length - 1,
    }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    selectTab(tabs[next].id, true);
  };
}
byId("question-form").onsubmit = (event) => {
  event.preventDefault();
  runQuery(byId("question").value);
};
byId("new-query").onclick = () => {
  state.result = null;
  state.turns = [];
  renderResult();
  byId("question").value = "";
  byId("query-status").textContent = "";
  setMessage("query-error");
  showView("workspace", {focus: false});
  byId("question").focus();
};
byId("retry-startup").onclick = start;
byId("schema-search").oninput = renderSchema;
byId("provider").onchange = renderProviderDraft;
byId("settings-form").onsubmit = saveSettings;
byId("clear-api-key").onclick = clearKey;
byId("download-csv").onclick = downloadCsv;
byId("open-connection").onclick = () => {
  byId("connection-form").reset();
  updateConnectionFields();
  setMessage("connection-error");
  byId("connection-dialog").showModal();
};
byId("close-connection").onclick = () => byId("connection-dialog").close();
byId("connection-dialog").addEventListener("cancel", (event) => {
  if (state.busy === "connect") event.preventDefault();
});
byId("connection-dialog").addEventListener("close", () => byId("connection-form").reset());
byId("database-kind").onchange = updateConnectionFields;
byId("connection-form").onsubmit = connectDatabase;
new ResizeObserver(drawChart).observe(byId("chart-area"));
start();
