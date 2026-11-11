import * as duckdb from "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.29.0/+esm";

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
const TABLES = ["trips", "zones"];
const PAGE = 20;

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const ident = (s) => '"' + String(s).replace(/"/g, '""') + '"';

let conn, presets, pyodide;
const columnsOf = {}; // table -> [column names]
const state = { custom: false, preset: null, traced: null, result: null, selected: null, sorts: {}, pages: {} };

// ---------- DuckDB ----------

async function startDuckDB() {
  const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
  const workerUrl = URL.createObjectURL(new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" }));
  const db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING), new Worker(workerUrl));
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  URL.revokeObjectURL(workerUrl);
  conn = await db.connect();

  status("Downloading 9 MB of taxi trips…");
  const [trips, zones] = await Promise.all([
    fetch("trips.parquet").then((r) => r.arrayBuffer()),
    fetch("zones.csv").then((r) => r.text()),
  ]);
  await db.registerFileBuffer("trips.parquet", new Uint8Array(trips));
  await db.registerFileText("zones.csv", zones);
  await conn.query("CREATE TABLE trips AS SELECT * FROM read_parquet('trips.parquet')");
  await conn.query("CREATE TABLE zones AS SELECT * FROM read_csv('zones.csv', header = true)");
  for (const t of TABLES) {
    columnsOf[t] = (await query(`DESCRIBE ${t}`)).rows.map((r) => r[0]);
  }
}

function cell(v, type) {
  if (v === null || v === undefined) return null;
  if (typeof v === "bigint") return Number(v);
  if (/^Timestamp/.test(type)) return new Date(Number(v)).toISOString().replace("T", " ").slice(0, 19);
  if (/^Date/.test(type)) return new Date(Number(v)).toISOString().slice(0, 10);
  if (typeof v === "object" && v.toJSON) return JSON.stringify(v.toJSON());
  return v;
}

/** Run SQL; columns are read by position so duplicate names survive. */
async function query(sql) {
  const t = await conn.query(sql);
  const fields = t.schema.fields;
  const types = fields.map((f) => String(f.type));
  const rows = [];
  for (let i = 0; i < t.numRows; i++) {
    rows.push(fields.map((_, j) => cell(t.getChildAt(j).get(i), types[j])));
  }
  return { columns: fields.map((f) => f.name), types, rows };
}

// ---------- Rewriting (precomputed for presets, Pyodide for your own SQL) ----------

async function rewriteInBrowser(sql) {
  if (!pyodide) {
    status("Loading Python and sqlglot to rewrite your query (once, about 15 MB)…");
    const { loadPyodide } = await import(PYODIDE + "pyodide.mjs");
    pyodide = await loadPyodide({ indexURL: PYODIDE });
    await pyodide.loadPackage("micropip");
    await pyodide.runPythonAsync("import micropip\nawait micropip.install('sqlglot>=30,<31')");
    pyodide.FS.writeFile("/home/pyodide/perche_rewrite.py", await fetch("rewrite.py").then((r) => r.text()));
    pyodide.runPython("import sys; sys.path.insert(0, '/home/pyodide')");
  }
  pyodide.globals.set("user_sql", sql);
  try {
    return JSON.parse(
      pyodide.runPython(`import json, perche_rewrite\njson.dumps(perche_rewrite.rewrite(user_sql, ${JSON.stringify(TABLES).replace(/\[/, "{").replace(/\]/, "}")}))`),
    );
  } catch (e) {
    const last = String(e.message).trim().split("\n").pop();
    throw new Error(last.replace(/^perche_rewrite\.UnsupportedQuery: /, ""));
  }
}

// ---------- Tracing ----------

/** One SQL expression per source: the distinct ids of all its occurrences. */
function mergedLineage(traced) {
  const groups = new Map();
  for (const l of traced.lineage) {
    if (!groups.has(l.source)) groups.set(l.source, { table: l.table, cols: [] });
    groups.get(l.source).cols.push(l.column);
  }
  return [...groups].map(([source, g]) => ({
    source,
    table: g.table,
    expr: `list_distinct(flatten([${g.cols.map((c) => `coalesce(${c}, []::BIGINT[])`).join(", ")}]))`,
  }));
}

async function runTrace(traced) {
  await conn.query(`CREATE OR REPLACE TEMP TABLE perche_result AS ${traced.sql}`);
  const sources = mergedLineage(traced);
  const hidden = traced.lineage.map((l) => l.column);
  const exclude = hidden.length ? ` EXCLUDE (${hidden.join(", ")})` : "";
  const counts = sources.map((s, i) => `len(${s.expr}) AS __perche_n${i}`);
  const r = await query(`SELECT *${exclude}${counts.map((c) => ", " + c).join("")} FROM perche_result ORDER BY rowid`);
  const k = r.columns.length - sources.length;
  return {
    columns: r.columns.slice(0, k),
    types: r.types.slice(0, k),
    rows: r.rows.map((row) => row.slice(0, k)),
    counts: r.rows.map((row) => row.slice(k)),
    sources,
  };
}

// ---------- UI ----------

function status(text, error = false) {
  const el = $("#status");
  el.textContent = text;
  el.classList.toggle("error", error);
}

const fmt = (v) =>
  v === null ? "NULL" : typeof v === "number" ? v.toLocaleString("en-US", { maximumFractionDigits: 2 }) : String(v);
const isNum = (type) => /Int|Float|Decimal|Double/.test(type);

function renderTabs() {
  $("#tabs").innerHTML = presets
    .map((p) => `<button data-id="${p.id}" aria-pressed="${!state.custom && state.preset?.id === p.id}">${esc(p.title)}</button>`)
    .join("");
  for (const b of $("#tabs").querySelectorAll("button")) {
    b.onclick = () => choose(presets.find((p) => p.id === b.dataset.id));
  }
}

async function choose(preset) {
  state.preset = preset;
  state.custom = false;
  history.replaceState(null, "", "#" + preset.id);
  renderTabs();
  $("#blurb").textContent = preset.blurb;
  $("#sql").value = preset.sql;
  await run();
}

async function run() {
  const sql = $("#sql").value.trim();
  const preset = state.preset;
  $("#run").disabled = true;
  try {
    const traced = preset && sql === preset.sql.trim() ? preset.traced : await rewriteInBrowser(sql);
    state.custom = !(preset && sql === preset.sql.trim());
    if (state.custom) {
      $("#blurb").textContent = "Your query. Click a bar or a row to see where it comes from.";
    }
    renderTabs();
    status("Running…");
    const t0 = performance.now();
    state.traced = traced;
    state.result = await runTrace(traced);
    state.selected = null;
    state.sorts = {};
    state.pages = {};
    $("#traced").textContent = traced.sql;
    const n = state.result.rows.length;
    status(`${n.toLocaleString()} row${n === 1 ? "" : "s"} with lineage in ${Math.round(performance.now() - t0)} ms`);
    renderChart();
    renderResult();
    renderPanel();
    if (n) select(0);
  } catch (e) {
    status(String(e.message || e), true);
  } finally {
    $("#run").disabled = false;
  }
}

function chartColumns(r) {
  const value = r.types.map((t, i) => (isNum(t) ? i : -1)).filter((i) => i >= 0).pop();
  if (value === undefined) return null;
  const label = r.types.findIndex((t, i) => i !== value && !isNum(t));
  return { label: label >= 0 ? label : r.columns.findIndex((_, i) => i !== value), value };
}

function renderChart() {
  const r = state.result;
  const cols = chartColumns(r);
  const box = $("#chart");
  if (!cols || !r.rows.length || r.rows.length > 40) {
    box.innerHTML = "";
    return;
  }
  const rows = r.rows.map((row, i) => ({ i, label: cols.label >= 0 ? fmt(row[cols.label]) : `row ${i}`, value: row[cols.value] ?? 0 }));
  const width = Math.max(320, box.clientWidth || 600);
  const labelW = Math.min(170, Math.max(...rows.map((d) => d.label.length)) * 7 + 8);
  const valueW = 80;
  const band = 30, bar = 20, top = 4;
  const lo = Math.min(0, ...rows.map((d) => d.value));
  const hi = Math.max(0, ...rows.map((d) => d.value));
  const x = (v) => labelW + ((v - lo) / (hi - lo || 1)) * (width - labelW - valueW);
  const h = top + rows.length * band;
  const truncate = (s) => (s.length * 7 > labelW - 8 ? s.slice(0, Math.floor((labelW - 8) / 7) - 1) + "…" : s);

  const marks = rows
    .map((d) => {
      const y = top + d.i * band;
      const x0 = x(Math.min(0, d.value)), x1 = x(Math.max(0, d.value));
      const w = Math.max(1, x1 - x0);
      const r4 = Math.min(4, w);
      // Rounded at the data end, square at the baseline.
      const path = d.value >= 0
        ? `M${x0},${y + (band - bar) / 2}h${w - r4}a${r4},${r4} 0 0 1 ${r4},${r4}v${bar - 2 * r4}a${r4},${r4} 0 0 1 -${r4},${r4}h-${w - r4}z`
        : `M${x1},${y + (band - bar) / 2}h-${w - r4}a${r4},${r4} 0 0 0 -${r4},${r4}v${bar - 2 * r4}a${r4},${r4} 0 0 0 ${r4},${r4}h${w - r4}z`;
      const sel = state.selected === d.i;
      const dim = state.selected !== null && !sel;
      return `<g>
        <text class="lbl${sel ? " sel" : ""}" x="${labelW - 8}" y="${y + band / 2 + 4}" text-anchor="end">${esc(truncate(d.label))}</text>
        <path class="bar${dim ? " dim" : ""}" d="${path}"></path>
        <text class="val" x="${x1 + 6}" y="${y + band / 2 + 4}">${esc(fmt(d.value))}</text>
        <rect class="hit" data-i="${d.i}" tabindex="0" role="button" aria-label="${esc(d.label)}: ${esc(fmt(d.value))}"
              x="0" y="${y}" width="${width}" height="${band}"></rect>
      </g>`;
    })
    .join("");
  box.innerHTML = `<p class="chart-title">${esc(r.columns[cols.value])} by ${esc(cols.label >= 0 ? r.columns[cols.label] : "row")}</p>
    <svg viewBox="0 0 ${width} ${h}" height="${h}" role="img" aria-label="Bar chart">
      <line class="axis" x1="${x(0)}" x2="${x(0)}" y1="0" y2="${h}"></line>${marks}
    </svg>`;

  const tip = $("#tip");
  for (const el of box.querySelectorAll(".hit")) {
    const i = Number(el.dataset.i);
    el.onclick = () => select(i);
    el.onkeydown = (e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), select(i));
    el.onmousemove = (e) => {
      const counts = state.result.sources.map((s, k) => `${state.result.counts[i][k].toLocaleString()} ${s.source}`).join(", ");
      tip.innerHTML = `<b>${esc(rows[i].label)}</b>: ${esc(fmt(rows[i].value))}<br>from ${esc(counts || "no rows")}`;
      tip.style.display = "block";
      tip.style.left = Math.min(e.clientX + 12, innerWidth - 330) + "px";
      tip.style.top = e.clientY + 12 + "px";
    };
    el.onmouseleave = () => (tip.style.display = "none");
  }
}

function renderResult() {
  const r = state.result;
  const shown = r.rows.slice(0, 200);
  const head = r.columns.map((c, j) => `<th class="${isNum(r.types[j]) ? "num" : ""}">${esc(c)}</th>`).join("");
  const srcHead = r.sources.map((s) => `<th class="num">← ${esc(s.source)}</th>`).join("");
  const body = shown
    .map(
      (row, i) =>
        `<tr data-i="${i}" class="${state.selected === i ? "sel" : ""}">${row
          .map((v, j) => `<td class="${isNum(r.types[j]) ? "num" : ""}">${esc(fmt(v))}</td>`)
          .join("")}${r.counts[i].map((n) => `<td class="num">${n.toLocaleString()}</td>`).join("")}</tr>`,
    )
    .join("");
  $("#result").innerHTML = r.rows.length
    ? `<div class="table-scroll result-table"><table><thead><tr>${head}${srcHead}</tr></thead><tbody>${body}</tbody></table></div>
       ${r.rows.length > shown.length ? `<p class="hint">First ${shown.length} of ${r.rows.length.toLocaleString()} rows.</p>` : ""}`
    : `<p class="empty">The query returned no rows.</p>`;
  for (const tr of $("#result").querySelectorAll("tr[data-i]")) tr.onclick = () => select(Number(tr.dataset.i));
}

function select(i) {
  state.selected = i;
  state.pages = {};
  renderChart();
  for (const tr of $("#result").querySelectorAll("tr[data-i]")) tr.classList.toggle("sel", Number(tr.dataset.i) === i);
  renderPanel();
}

function defaultSort(table) {
  const [col, dir] = (state.preset?.sort || "").split(" ");
  if (col && columnsOf[table]?.includes(col) && $("#sql").value.trim() === state.preset.sql.trim()) {
    return { col, desc: dir === "DESC" };
  }
  return { col: "rowid", desc: false };
}

async function renderPanel() {
  const panel = $("#panel");
  const r = state.result;
  const i = state.selected;
  if (i === null || !r) {
    panel.innerHTML = `<h2>Where does it come from?</h2><p class="empty">Click a bar or a result row.</p>`;
    return;
  }
  const summary = r.columns.map((c, j) => `${esc(c)} = <b>${esc(fmt(r.rows[i][j]))}</b>`).join(", ");
  const parts = [];
  for (const [k, s] of r.sources.entries()) {
    const total = r.counts[i][k];
    if (!total) {
      parts.push(`<div class="source"><h3>← ${esc(s.source)} <span class="n">no rows</span></h3></div>`);
      continue;
    }
    const sort = state.sorts[s.source] || defaultSort(s.table);
    const page = state.pages[s.source] || 0;
    const order = `${sort.col === "rowid" ? "rowid" : ident(sort.col)} ${sort.desc ? "DESC" : "ASC"} NULLS LAST, rowid`;
    const src = await query(
      `SELECT rowid AS "rowid", * FROM ${s.table}
       WHERE rowid IN (SELECT unnest(${s.expr}) FROM perche_result WHERE rowid = ${i})
       ORDER BY ${order} LIMIT ${PAGE} OFFSET ${page * PAGE}`,
    );
    const head = src.columns
      .map((c, j) => {
        const arrow = sort.col === c ? (sort.desc ? " ↓" : " ↑") : "";
        return `<th class="${isNum(src.types[j]) ? "num" : ""}" data-col="${esc(c)}" title="Sort by ${esc(c)}">${esc(c)}<span class="arrow">${arrow}</span></th>`;
      })
      .join("");
    const body = src.rows
      .map((row) => `<tr>${row.map((v, j) => `<td class="${isNum(src.types[j]) ? "num" : ""}">${esc(fmt(v))}</td>`).join("")}</tr>`)
      .join("");
    const last = Math.ceil(total / PAGE) - 1;
    parts.push(`<div class="source" data-source="${esc(s.source)}">
      <h3>← ${esc(s.source)} <span class="n">${total.toLocaleString()} row${total === 1 ? "" : "s"}${sort.col !== "rowid" ? `, sorted by ${esc(sort.col)}` : ""}</span></h3>
      <div class="table-scroll"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
      ${last > 0 ? `<div class="pager"><button data-step="-1" ${page === 0 ? "disabled" : ""}>←</button>
        rows ${(page * PAGE + 1).toLocaleString()}–${Math.min(total, (page + 1) * PAGE).toLocaleString()} of ${total.toLocaleString()}
        <button data-step="1" ${page >= last ? "disabled" : ""}>→</button></div>` : ""}
    </div>`);
  }
  if (state.selected !== i) return; // a newer click won
  panel.innerHTML = `<h2>Where does it come from?</h2><p class="row-summary">Row ${i}: ${summary}</p>${
    parts.join("") || `<p class="empty">No source rows: a constant, or an aggregate over nothing.</p>`
  }`;
  for (const div of panel.querySelectorAll(".source[data-source]")) {
    const name = div.dataset.source;
    const table = r.sources.find((s) => s.source === name).table;
    for (const th of div.querySelectorAll("th")) {
      th.onclick = () => {
        const cur = state.sorts[name] || defaultSort(table);
        const col = th.dataset.col;
        state.sorts[name] = { col, desc: cur.col === col ? !cur.desc : isNum(src_type(div, th)) };
        state.pages[name] = 0;
        renderPanel();
      };
    }
    for (const b of div.querySelectorAll(".pager button")) {
      b.onclick = () => {
        state.pages[name] = (state.pages[name] || 0) + Number(b.dataset.step);
        renderPanel();
      };
    }
  }
}

// Numbers sort biggest first on the first click; text sorts A→Z.
function src_type(div, th) {
  return th.classList.contains("num") ? "Double" : "Utf8";
}

// ---------- Start ----------

async function main() {
  $("#run").onclick = run;
  $("#sql").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) run();
  });
  try {
    const p = fetch("presets.json").then((r) => r.json());
    await startDuckDB();
    const data = await p;
    presets = data.presets;
    $("#ntrips").textContent = data.trips.toLocaleString("en-US");
    const wanted = presets.find((x) => x.id === location.hash.slice(1)) || presets[0];
    await choose(wanted);
  } catch (e) {
    status(`Could not start: ${e.message || e}`, true);
  }
  let resizeTimer;
  addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => state.result && renderChart(), 150);
  });
}

main();
