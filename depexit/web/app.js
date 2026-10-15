"use strict";
// Depexit workbench. Every value rendered here comes from the backend pipeline's own
// index.json / capsule.json; nothing is simulated or computed client-side.

const $ = (s) => document.querySelector(s);
const LINKWATCH = "https://github.com/rishrek/LinkWatch";
const STAGES = ["inspect", "baseline", "risk", "plan", "review", "verify", "combine", "done"];
const PROV_LABEL = { observed: "observed", executed: "executed", ai_generated: "AI · Gemma", rule_based: "rule" };
const STATUS_LABEL = { verified: "Verified", verified_with_gaps: "Verified · gaps", incomplete: "Incomplete",
  failed: "Failed", planned: "Planned", not_executed: "Kept (no exit)", tool_error: "Tool error", partial: "Partial",
  running: "Running", awaiting_review: "Plans ready · review", done: "Done", error: "Error", queued: "Queued",
  met: "met", not_checked: "not checked", pending: "pending", applied: "applied" };
const TONE = { verified: "ok", verified_with_gaps: "warn", partial: "warn", failed: "bad", tool_error: "bad",
  incomplete: "idle", not_executed: "idle", planned: "info" };
const ACTION_ICON = { upgrade: "↑", replace: "⇄", stdlib: "≡", remove: "✕", declare: "+", keep: "•" };
const SEV_W = { critical: 100, high: 78, medium: 52, low: 28, info: 14, none: 4 };
let job = null, events = 0, pollTimer = null, current = null, selected = new Set(), tab = "Overview", capData = null;

// ------------------------------------------------------------------ helpers
function h(tag, attrs, ...kids) {
  const svg = ["svg", "g", "rect", "text", "path", "tspan", "title", "circle"].includes(tag);
  const el = svg ? document.createElementNS("http://www.w3.org/2000/svg", tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const prov = (p) => p ? String(p).split("+").map((x) => h("span", { class: "prov " + x }, PROV_LABEL[x] || x)) : null;
const status = (s, label) => h("span", { class: "status st-" + (s || "none") }, label || STATUS_LABEL[s] || s || "—");
const code = (t) => h("code", {}, t ?? "");
const empty = (t) => h("p", { class: "empty" }, t || "None.");
const clip = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body && body.error ? body.error : `HTTP ${r.status}`);
  return body;
}
const fileUrl = (rel, dl) => `/api/jobs/${job.id}/files/${rel.split("/").map(encodeURIComponent).join("/")}${dl ? "?download=1" : ""}`;

// ------------------------------------------------------------------ theme
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("depexit-theme"); } catch (e) { /* storage unavailable */ }
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme-btn").onclick = () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("depexit-theme", document.documentElement.dataset.theme); } catch (e) { /* ignore */ }
  };
})();

// ------------------------------------------------------------------ Gemma status
async function loadLLM() {
  const pill = $("#llm-pill"), sel = $("#model"), note = $("#llm-note");
  try {
    const s = await api("/api/llm");
    sel.replaceChildren();
    const models = s.gemma_models && s.gemma_models.length ? s.gemma_models : s.models;
    for (const m of models) sel.append(h("option", { value: m }, m));
    const pick = s.model_available ? s.configured_model : s.suggested_model;
    if (pick) sel.value = pick;
    if (s.reachable && pick) {
      pill.className = "pill ok"; pill.querySelector(".txt").textContent = `${pick} · local · connected`;
      note.classList.add("hidden");
    } else {
      pill.className = "pill bad"; pill.querySelector(".txt").textContent = "Gemma unavailable";
      $("#use-llm").checked = false;
      note.classList.remove("hidden");
      note.replaceChildren(h("b", {}, "Model unavailable: "), s.detail || "", h("br"),
        h("b", {}, "Disabled: "), (s.unavailable_features || []).join(", "), ".", h("br"), s.fallback || "");
    }
  } catch (e) { pill.className = "pill bad"; pill.querySelector(".txt").textContent = "Gemma status error"; }
}

// ------------------------------------------------------------------ start
$("#fill-linkwatch").onclick = () => { $("#target").value = LINKWATCH; $("#target").focus(); };
$("#start-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const err = $("#start-error"); err.classList.add("hidden");
  const pythons = [...document.querySelectorAll("input[name=py]:checked")].map((i) => i.value);
  const body = { target: $("#target").value.trim(), pythons, use_llm: $("#use-llm").checked,
    model: $("#use-llm").checked ? $("#model").value || null : null,
    include_low: $("#include-low").checked, max_fix_attempts: +$("#fix").value,
    acknowledge_untrusted_execution: $("#ack").checked };
  $("#start-btn").disabled = true;
  try {
    job = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    attach(job);
  } catch (e) { err.textContent = e.message; err.classList.remove("hidden"); }
  finally { $("#start-btn").disabled = false; }
};

function attach(j) {
  job = j; events = 0; selected.clear(); current = null; closeDrawer();
  $("#log").textContent = "";
  $("#run-card").classList.remove("hidden");
  $("#run-id").textContent = job.id;
  $("#run-target").textContent = job.target;
  try { history.replaceState(null, "", "#run=" + job.id); } catch (e) { /* ignore */ }
  poll();
}

// ------------------------------------------------------------------ polling
async function poll() {
  clearTimeout(pollTimer);
  try {
    const v = await api(`/api/jobs/${job.id}?since=${events}`);
    for (const e of v.events) $("#log").textContent += `${new Date(e.t).toLocaleTimeString()}  ${e.stage.padEnd(8)}  ${e.msg}\n`;
    events = v.event_count;
    $("#ev-count").textContent = `(${events})`;
    $("#log").scrollTop = $("#log").scrollHeight;
    job = { ...job, ...v };
    renderRun(v);
    if (v.state === "running" || v.state === "queued") pollTimer = setTimeout(poll, 2000);
  } catch (e) { const b = $("#run-error"); b.textContent = e.message; b.classList.remove("hidden"); }
}

function renderRun(v) {
  $("#run-state").replaceWith(Object.assign(status(v.state), { id: "run-state" }));
  const stage = v.state === "awaiting_review" ? "review" : v.state === "done" ? "done" : v.stage;
  const idx = STAGES.indexOf(stage);
  $("#stepper").replaceChildren(...STAGES.map((s, i) => h("li", {
    class: v.state === "done" || i < idx ? "done" : i === idx ? (v.state === "error" ? "failed" : "active") : "" }, s)));
  const err = $("#run-error");
  if (v.error) { err.textContent = v.error; err.classList.remove("hidden"); } else err.classList.add("hidden");
  if (!v.index) return;
  renderTiles(v.index);
  renderBaseline(v.index, v.execution_boundary);
  renderGraph(v.index, v.state);
  renderDeps(v.index, v.state);
  renderCombined(v.index, v.state);
  if (current) openDep(current, true);
}

function tile(k, v, s, tone) {
  return h("div", { class: "tile " + (tone || "") }, h("div", { class: "k" }, k), h("div", { class: "v" }, v), s ? h("div", { class: "s" }, s) : null);
}

function renderTiles(ix) {
  const caps = ix.dependencies.filter((d) => d.capsule);
  const count = (...st) => caps.filter((d) => st.includes(d.status)).length;
  const executed = count("verified", "verified_with_gaps", "failed", "incomplete", "tool_error");
  const base = Object.entries(ix.baseline || {})[0];
  const bc = base ? base[1].tests.counts : null;
  $("#tiles").replaceChildren(...[
    tile("Dependencies", ix.dependencies.length, "declared, imported or installed"),
    tile("At risk", caps.length, `${ix.dependencies.filter((d) => d.max_severity === "critical").length} critical`, caps.length ? "bad" : "ok"),
    tile("Planned", count("planned") + executed, `${count("not_executed")} kept as-is`, "info"),
    tile("Verified", count("verified", "verified_with_gaps"), `${count("verified_with_gaps")} with gaps`, "ok"),
    tile("Failed", count("failed", "tool_error"), "a required check failed", count("failed", "tool_error") ? "bad" : ""),
    tile("Incomplete", count("incomplete"), "required check not run", count("incomplete") ? "warn" : ""),
    bc ? tile(`Baseline py${base[0]}`, `${bc.passed}/${bc.passed + bc.failed + bc.error}`, "tests passing before changes", bc.failed + bc.error ? "warn" : "ok") : null,
    ix.combined ? tile("Combined", STATUS_LABEL[ix.combined.status] || ix.combined.status, `${ix.combined.dependencies.length} exits merged`, TONE[ix.combined.status]) : null,
  ].filter(Boolean));
}

function renderBaseline(ix, boundary) {
  const rows = Object.entries(ix.baseline || {}).map(([py, b]) => {
    const c = b.tests.counts;
    return h("tr", {}, h("td", {}, h("b", {}, py)), h("td", {}, status(b.env.ok ? "met" : "failed", b.env.ok ? "installed" : "install failed")),
      h("td", { class: "num" }, c.passed), h("td", { class: "num" }, c.failed), h("td", { class: "num" }, c.error),
      h("td", {}, b.tests.collection_error ? code(clip(b.tests.collection_error.split("\n").pop(), 60)) : h("span", { class: "muted" }, "–")));
  });
  const ai = ix.ai || {};
  $("#baseline").replaceChildren(
    h("h3", {}, "Baseline · unmodified repository ", prov("executed")),
    h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("thead", {}, h("tr", {}, ...["Python", "Install", "Pass", "Fail", "Err", "Collection"].map((t) => h("th", {}, t)))), h("tbody", {}, rows))),
    h("p", { class: "muted small" }, `${ix.target.source} @ ${(ix.target.commit || "n/a").slice(0, 10)} · AI: `,
      ai.reachable ? `${ai.model} reachable` : `not used (${ai.detail || "disabled"})`),
    h("div", { class: "callout info small" }, h("b", {}, "Execution boundary · "), boundary.detail));
}

// ------------------------------------------------------------------ migration graph
const TONE_VARS = {
  ok: ["--ok-bg", "--ok-line", "--ok"], warn: ["--warn-bg", "--warn-line", "--warn"], bad: ["--bad-bg", "--bad-line", "--bad"],
  idle: ["--idle-bg", "--idle-line", "--idle"], info: ["--info-bg", "--info-line", "--info"], plain: ["--surface", "--line", "--ink-2"],
};
function node(x, y, w, hgt, tone, title, sub, onclick, tip) {
  const [bg, line] = TONE_VARS[tone] || TONE_VARS.plain;
  return h("g", { class: "g-node", transform: `translate(${x},${y})`, onclick, role: "button", tabindex: "0",
    onkeydown: (e) => { if (e.key === "Enter") onclick && onclick(); } },
    tip ? h("title", {}, tip) : null,
    h("rect", { width: w, height: hgt, rx: 10, style: `fill:var(${bg});stroke:var(${line})` }),
    h("text", { x: 12, y: sub ? 20 : hgt / 2 + 4, class: "t-strong" }, title),
    sub ? h("text", { x: 12, y: 37, class: "sub" }, sub) : null);
}
function edge(x1, y1, x2, y2, tone, dashed) {
  const mx = (x1 + x2) / 2;
  const color = (TONE_VARS[tone] || TONE_VARS.idle)[tone === "plain" ? 1 : 2];
  return h("path", { class: "g-edge", d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`,
    style: `stroke:var(${color});${dashed ? "stroke-dasharray:5 5;opacity:.7" : ""}` });
}

function renderGraph(ix, state) {
  const deps = ix.dependencies.filter((d) => d.capsule);
  const card = $("#graph-card");
  if (!deps.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const W = 980, top = 46, rowH = 58, nodeH = 46;
  const C = { dep: [16, 200], act: [266, 212], st: [530, 200], comb: [790, 174] };
  const H = Math.max(top + deps.length * rowH + 12, 240);
  const cb = ix.combined;
  const inCombined = new Set(cb ? cb.dependencies : []);
  const conflicted = new Set(cb ? (cb.skipped || []).map((s) => s.dependency) : []);
  const svg = h("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Migration graph" });
  [["Dependency", C.dep], ["Strategy", C.act], ["Individual verification", C.st], ["Combined", C.comb]].forEach(([t, [x]]) =>
    svg.append(h("text", { x: x + 2, y: 24, class: "g-col" }, t)));
  const combY = top + Math.max(0, (deps.length * rowH - 120) / 2), combH = 112;
  const edges = h("g"), nodes = h("g");
  deps.forEach((d, i) => {
    const y = top + i * rowH, cy = y + nodeH / 2, open = () => openDep(d.name);
    const sevTone = { critical: "bad", high: "bad", medium: "warn" }[d.max_severity] || "idle";
    nodes.append(node(C.dep[0], y, C.dep[1], nodeH, "plain", clip(d.name, 24), `${d.max_severity} risk · score ${d.score}`, open,
      d.indicators.join(", ")));
    nodes.append(h("rect", { x: C.dep[0], y: y + 8, width: 4, height: nodeH - 16, rx: 2, style: `fill:var(${TONE_VARS[sevTone][2]})` }));
    const tgt = d.action === "keep" ? "no safe change" : (d.action_target || "");
    nodes.append(node(C.act[0], y, C.act[1], nodeH, d.action === "keep" ? "idle" : "plain",
      `${ACTION_ICON[d.action] || "?"}  ${d.action || "–"}`, clip(tgt || (d.action ? "" : "not planned"), 30), open));
    const tone = TONE[d.status] || "idle";
    nodes.append(node(C.st[0], y, C.st[1], nodeH, tone, STATUS_LABEL[d.status] || d.status || "–",
      clip(d.status === "planned" ? "awaiting approval" : d.status === "not_executed" ? "nothing executed" : (d.status_reason || "all required checks met"), 30),
      open, d.status_reason || ""));
    edges.append(edge(C.dep[0] + C.dep[1], cy, C.act[0], cy, "plain"));
    if (d.action !== "keep") edges.append(edge(C.act[0] + C.act[1], cy, C.st[0], cy, tone, d.status === "planned"));
    if (inCombined.has(d.name)) edges.append(edge(C.st[0] + C.st[1], cy, C.comb[0], combY + combH / 2, TONE[cb.status] || "idle"));
    else if (conflicted.has(d.name)) edges.append(edge(C.st[0] + C.st[1], cy, C.comb[0], combY + combH / 2, "bad", true));
  });
  let ctone = "idle", ctitle = "Not run yet", csub = state === "done" ? "no verified exit to combine" : "runs after verification";
  if (cb) {
    ctone = TONE[cb.status] || "idle"; ctitle = STATUS_LABEL[cb.status] || cb.status;
    csub = `${cb.dependencies.length} merged · ${(cb.skipped || []).length} conflicts`;
  }
  const comb = node(C.comb[0], combY, C.comb[1], combH, ctone, ctitle, csub, () => $("#combined-card").scrollIntoView({ behavior: "smooth" }));
  if (cb) Object.entries(cb.comparison || {}).forEach(([py, c], k) => {
    const t = c.tests ? `py${py}: ${c.tests.replacement.passed} pass · ${c.tests.regressed.length} regressed` : `py${py}: install failed`;
    comb.append(h("text", { x: 12, y: 60 + k * 18, class: "sub" }, t));
  });
  nodes.append(comb);
  svg.append(edges, nodes);
  $("#graph").replaceChildren(svg);
  const seen = [...new Set(deps.map((d) => d.status).concat(cb ? [cb.status] : []))];
  $("#graph-legend").replaceChildren(...seen.map((s) => status(s)));
}

// ------------------------------------------------------------------ dependencies table
function renderDeps(ix, state) {
  $("#deps-card").classList.remove("hidden");
  const canRun = state === "awaiting_review" || state === "done";
  const rows = ix.dependencies.filter((d) => d.at_risk || d.indicators.length).map((d) => {
    const plannable = d.status === "planned" && canRun;
    const cb = h("input", { type: "checkbox", disabled: !plannable, checked: plannable && selected.has(d.name),
      title: plannable ? "Select for migration" : "Only planned dependencies can be executed",
      onclick: (e) => { e.stopPropagation(); e.target.checked ? selected.add(d.name) : selected.delete(d.name); updateExec(state); } });
    return h("tr", { class: (d.capsule ? "clickable" : "") + (current === d.name ? " selected" : ""), onclick: () => d.capsule && openDep(d.name) },
      h("td", {}, cb),
      h("td", {}, h("div", { class: "dep-name" }, d.name), d.status_reason && d.status !== "planned" ? h("div", { class: "dep-sub" }, clip(d.status_reason, 70)) : null),
      h("td", {}, h("div", { class: "risk sev-" + d.max_severity }, h("span", { class: "risk-bar" }, h("i", { style: `width:${SEV_W[d.max_severity] || 4}%` })),
        h("span", { class: "sev-label" }, d.max_severity), h("span", { class: "muted small" }, d.score))),
      h("td", { class: "tags" }, d.indicators.map((i) => h("span", {}, i))),
      h("td", {}, d.action ? h("span", { class: "action" }, h("span", { class: "ic" }, ACTION_ICON[d.action] || "?"), d.action,
        d.action_target ? h("span", { class: "muted small" }, d.action_target) : null) : h("span", { class: "muted" }, "–")),
      h("td", {}, d.status ? status(d.status) : h("span", { class: "muted" }, "healthy")));
  });
  $("#deps-table tbody").replaceChildren(...rows);
  for (const n of [...selected]) if (!ix.dependencies.some((d) => d.name === n && d.status === "planned")) selected.delete(n);
  updateExec(state);
}
function updateExec(state) {
  const b = $("#exec-btn");
  b.disabled = !selected.size || !(state === "awaiting_review" || state === "done");
  b.textContent = selected.size ? `Migrate & verify ${selected.size} selected` : "Select planned dependencies to migrate";
}
$("#exec-btn").onclick = async () => {
  try {
    await api(`/api/jobs/${job.id}/execute`, { method: "POST", body: JSON.stringify({ deps: [...selected] }) });
    selected.clear(); job.state = "running"; poll();
  } catch (e) { alert(e.message); }
};

// ------------------------------------------------------------------ combined
function renderCombined(ix, state) {
  const cb = ix.combined, card = $("#combined-card");
  if (!cb) {
    if (state === "done") { card.classList.remove("hidden");
      $("#combined").replaceChildren(empty("No individually verified migration, so nothing was combined.")); }
    else card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");
  const rows = Object.entries(cb.comparison || {}).map(([py, c]) => {
    if (!c.install_ok) return h("tr", {}, h("td", {}, h("b", {}, py)), h("td", { colspan: 5 }, status("failed", "install failed")));
    const n = c.tests.replacement;
    return h("tr", {}, h("td", {}, h("b", {}, py)), h("td", { class: "num" }, n.passed), h("td", { class: "num" }, n.failed), h("td", { class: "num" }, n.error),
      h("td", { class: "num" }, c.tests.fixed.length), h("td", { class: "num" }, c.tests.regressed.length + (c.tests.vanished_passing || []).length));
  });
  $("#combined").replaceChildren(
    h("div", { class: "row-flex" }, status(cb.status), " ", h("span", { class: "muted" }, " Re-verified result of applying ",
      h("b", {}, cb.dependencies.join(", ") || "nothing"), " together. Individual results are in each capsule.")),
    h("div", { class: "split", style: "margin-top:12px" },
      h("div", {}, h("h3", {}, "Tests with all exits applied"),
        h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("thead", {}, h("tr", {}, ...["Python", "Pass", "Fail", "Err", "Fixed", "Regressed"].map((t) => h("th", {}, t)))), h("tbody", {}, rows)))),
      h("div", {}, h("h3", {}, "Merge"),
        h("ul", { class: "plain" },
          h("li", {}, "Conflicts: ", (cb.conflicts || []).join(", ") || "none"),
          h("li", {}, "Re-applied by Gemma: ", (cb.rebased_by_ai || []).map((r) => r.dependency).join(", ") || "none"),
          h("li", {}, "Excluded (conflict): ", (cb.skipped || []).map((s) => `${s.dependency}: ${s.reason}`).join("; ") || "none"),
          h("li", {}, "Not combined (not verified individually): ", (cb.excluded_unverified || []).map((s) => `${s.dependency} (${STATUS_LABEL[s.status] || s.status})`).join(", ") || "none")))),
    h("h3", {}, "Requirements"), requirementsTable(cb.verification && cb.verification.requirements),
    h("div", { class: "action-bar" },
      h("a", { href: fileUrl("combined/all-migrations.patch"), target: "_blank" }, "all-migrations.patch"),
      h("button", { class: "secondary", onclick: () => applyPatch("combined") }, "Apply combined patch to a fresh working copy")),
    h("div", { id: "apply-combined" }));
}

// ------------------------------------------------------------------ drawer
const TABS = ["Overview", "Usage", "Risk", "Plan", "Verification", "Gaps", "Proof"];
function closeDrawer() { current = null; $("#drawer").classList.add("hidden"); $("#scrim").classList.add("hidden"); }
$("#detail-close").onclick = closeDrawer;
$("#scrim").onclick = closeDrawer;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

async function openDep(name, quiet) {
  try {
    const d = await api(`/api/jobs/${job.id}/capsules/${encodeURIComponent(name)}`);
    if (quiet && current !== name) return;
    current = name; capData = d;
    $("#drawer").classList.remove("hidden"); $("#scrim").classList.remove("hidden");
    $("#detail-title").replaceChildren(name, status(d.capsule.status));
    $("#tabs").replaceChildren(...TABS.map((t) => h("button", { class: t === tab ? "on" : "", onclick: () => { tab = t; openDep(name, true); } }, t)));
    const body = $("#detail-body"), keep = body.scrollTop;
    const fn = { Overview: tOverview, Usage: tUsage, Risk: tRisk, Plan: tPlan, Verification: tVerify, Gaps: tGaps, Proof: tProof }[tab];
    body.replaceChildren(...[].concat(fn(d.capsule)).filter(Boolean));
    if (quiet) body.scrollTop = keep;
  } catch (e) { if (!quiet) alert(e.message); }
}

function kv(pairs) {
  return h("dl", { class: "kv" }, pairs.filter(Boolean).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v ?? "–")]));
}

function tOverview(c) {
  const d = c.dependency, p = d.pypi || {}, pr = c.proposed_replacement;
  return [
    h("div", { class: "callout " + ({ ok: "ok", warn: "warn", bad: "bad" }[TONE[c.status]] || "info") },
      h("b", {}, STATUS_LABEL[c.status] || c.status), c.status_reason ? ` — ${c.status_reason}` : ""),
    h("h3", {}, "Proposed exit ", prov(pr.provenance)),
    h("div", { class: "box" }, h("div", { class: "box-title" }, h("span", { class: "action" }, h("span", { class: "ic" }, ACTION_ICON[pr.action] || "?"), pr.action || "–"),
      pr.package ? code(pr.package + (pr.specifier || "")) : null, pr.stdlib_module ? code("stdlib: " + pr.stdlib_module) : null),
      h("div", {}, pr.rationale || "")),
    h("h3", {}, "Declarations & versions ", prov("observed")),
    kv([["Declared in", d.declared.length ? h("ul", { class: "plain" }, d.declared.map((x) =>
          h("li", {}, code(`${x.file}:${x.line}`), " ", code(x.raw), x.commented ? " (commented out)" : "", h("span", { class: "muted small" }, ` ${x.group}`)))) : h("b", {}, "not declared")],
        ["Import names", d.import_names.join(", ") || "–"],
        ["Installed", Object.entries(d.installed_versions).map(([py, v]) => `py${py}: ${v}`).join(" · ") || "not installed"],
        ["Latest on PyPI", `${p.latest_version || "?"} (${p.latest_release_date || "?"}) · compatible with targets: ${p.latest_compatible || "?"}`],
        ["Summary", p.summary], ["Source", p.source ? h("a", { href: p.source, target: "_blank", rel: "noopener" }, p.source) : null],
        d.github ? ["GitHub", d.github.available ? `${d.github.repo}${d.github.archived ? " (ARCHIVED)" : ""}${d.github.days_since_push != null ? ` · last push ${d.github.days_since_push} days ago` : ""}`
          : `${d.github.repo}: metadata unavailable (HTTP ${d.github.status ?? "error"})`] : null]),
  ];
}

function tUsage(c) {
  const u = c.usage_locations;
  if (!u.sites.length) return [h("h3", {}, "Usage ", prov("observed")), empty("No usage found in Python code or build/CI files.")];
  return [h("h3", {}, `${u.count} usage site(s) in ${u.files.length} file(s) `, prov("observed")),
    h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("thead", {}, h("tr", {}, ...["Location", "Kind", "Symbol", "Code"].map((t) => h("th", {}, t)))),
      h("tbody", {}, u.sites.map((s) => h("tr", {}, h("td", {}, code(`${s.file}:${s.line}`)),
        h("td", {}, s.kind, s.private ? h("div", { class: "sev-medium sev-label" }, "private API") : ""), h("td", {}, code(s.symbol)), h("td", {}, code(s.code)))))))];
}

function tRisk(c) {
  const r = c.risk_indicators;
  const src = (s) => /^https?:/.test(s || "") ? h("a", { href: s, target: "_blank", rel: "noopener" }, "source")
    : h("span", { class: "muted small" }, String(s || ""));
  const row = (i) => h("tr", {},
    h("td", {}, h("span", { class: "sev-label sev-" + i.severity }, i.severity)),
    h("td", {}, h("b", {}, i.title)),
    h("td", {}, i.detail),
    h("td", {}, prov(i.evidence.type), " ", src(i.evidence.source)));
  const head = h("thead", {}, h("tr", {}, ...["Severity", "Indicator", "Detail", "Evidence"].map((t) => h("th", {}, t))));
  return [h("h3", {}, `Risk indicators · score ${r.score} · max ${r.max_severity} `, prov("observed"), " ", prov("executed")),
    r.items.length ? h("div", { class: "table-wrap" }, h("table", { class: "data" }, head, h("tbody", {}, r.items.map(row)))) : empty()];
}

function checksList(checks) {
  if (!checks || !checks.length) return empty("No checks recorded.");
  return h("ul", { class: "checks" }, checks.map((x) => h("li", {}, h("span", {}, x.ok ? "✅" : x.blocking === false ? "⚠️" : "❌"),
    h("span", {}, x.check, x.blocking === false ? h("span", { class: "muted small" }, " · advisory") : "",
      Array.isArray(x.detail) && x.detail.length ? h("div", { class: "muted small" }, x.detail.join(", "))
        : x.detail && typeof x.detail !== "object" ? h("div", { class: "muted small" }, String(x.detail)) : ""))));
}

function requirementsTable(reqs) {
  if (!reqs || !reqs.length) return empty("No requirements (nothing to verify).");
  return h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("thead", {}, h("tr", {}, ...["Result", "Requirement", "Level", "Evidence"].map((t) => h("th", {}, t)))),
    h("tbody", {}, reqs.map((r) => h("tr", {}, h("td", {}, status(r.result)), h("td", {}, r.description),
      h("td", {}, r.required ? h("b", {}, "required") : h("span", { class: "muted" }, "advisory")), h("td", { class: "small" }, (r.evidence || []).join("; ") || "–"))))));
}

function tPlan(c) {
  const p = c.proposed_replacement, m = c.migration_steps, rv = p.api_mapping_review;
  const out = [h("h3", {}, "Strategy ", prov(p.provenance)),
    kv([["Action", h("span", { class: "action" }, h("span", { class: "ic" }, ACTION_ICON[p.action] || "?"), p.action)],
      ["Package", p.package ? code(p.package + (p.specifier || "")) : "–"], ["Stdlib module", p.stdlib_module || "–"],
      ["Code changes expected", p.needs_code_changes ? "yes" : "no"], ["Rationale", p.rationale]])];
  if (p.rejected_ai_plan) out.push(h("div", { class: "callout warn" }, "Gemma's proposal ", code(`${p.rejected_ai_plan.action} ${p.rejected_ai_plan.package || ""}`),
    " was rejected by deterministic validation; the rule-based plan is shown instead."));
  out.push(h("h3", {}, "Migration steps ", prov(m.provenance)), m.steps.length ? h("ol", {}, m.steps.map((s) => h("li", {}, s))) : empty());
  out.push(h("h3", {}, "API mapping ", prov(p.provenance), " · checked against usage ", prov("observed")));
  if (rv && (rv.entries.length || rv.unmapped_symbols.length)) {
    out.push(h("p", { class: "muted small" }, `${rv.mapped_symbol_count} of ${rv.observed_symbol_count} observed symbols covered`));
    if (rv.entries.length) out.push(h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("thead", {}, h("tr", {}, ...["Old", "New", "Note", "Observed at", "Check"].map((t) => h("th", {}, t)))),
      h("tbody", {}, rv.entries.map((e) => h("tr", {}, h("td", {}, code(e.old)), h("td", {}, code(e.new)), h("td", { class: "small" }, e.note || ""),
        h("td", { class: "small" }, e.observed_sites.length ? e.observed_sites.join(", ") : h("span", { class: "sev-label sev-medium" }, "not observed")),
        h("td", {}, [!e.valid_shape && "incomplete", e.new_still_uses_old && "still uses old dep"].filter(Boolean).join("; ") || "✅")))))));
    if (rv.unmapped_symbols.length) out.push(h("div", { class: "callout warn" }, h("b", {}, "Unmapped symbols in use: "),
      rv.unmapped_symbols.map((u) => `${u.symbol} (${u.sites.join(", ")})`).join("; ")));
  } else out.push(empty(p.action === "replace" || p.action === "stdlib" ? "No API mapping was produced." : "Not needed: this action does not change any API."));
  out.push(h("h3", {}, "Validation of proposal & AI output ", prov("observed")), checksList(c.validation_of_ai_output.checks));
  out.push(h("h3", {}, "Verification requirements · decided before execution"), requirementsTable(c.verification.requirements));
  if (c.suggested_tests.behaviour_probes) out.push(h("p", {}, "Behaviour probes ", prov("ai_generated"), " ",
    h("a", { href: fileUrl(`capsules/${c.dependency.name}/probes.py`), target: "_blank" }, "probes.py"), " run on original and migrated code; results are diffed."));
  return out;
}

function tVerify(c) {
  const ex = c.executed_test_results, v = c.verification;
  if (!Object.keys(ex.per_python || {}).length)
    return [h("div", { class: "callout info" }, c.status === "planned" ? "Not executed yet. Select it in the workbench and run “Migrate & verify”." : "Nothing was executed for this dependency.")];
  const out = [h("h3", {}, "Requirements ", prov("executed")), requirementsTable(v.requirements), h("h3", {}, "Per Python ", prov("executed"))];
  for (const [py, e] of Object.entries(ex.per_python)) {
    const t = e.tests || {}, o = t.original || {}, n = t.replacement || {};
    out.push(h("div", { class: "box" }, h("div", { class: "box-title" }, `Python ${py}`, e.install_ok ? status("met", "installed") : status("failed", "install failed")),
      kv([["Install commands", (e.install_commands || []).map((x) => h("div", {}, code(x)))],
        e.installed ? ["Versions", `${e.installed.original || "–"} → ${e.installed.replacement_package || "(removed)"} ${e.installed.replacement_version || ""}`
          + (e.installed.original_still_installed ? " · old package still installed transitively" : "")] : null,
        ["Tests pass/fail/err", `${o.passed ?? "-"}/${o.failed ?? "-"}/${o.error ?? "-"} → ${n.passed ?? "-"}/${n.failed ?? "-"}/${n.error ?? "-"}`],
        ["Fixed", (t.fixed || []).join(", ") || "–"],
        ["Regressed", (t.regressed || []).map((r) => r.test).join(", ") || "–"],
        t.replacement_collection_error ? ["Collection error", code(t.replacement_collection_error.split("\n").pop())] : null,
        ["Replacement import", e.replacement_import ? (e.replacement_import.ok ? `✅ ${e.replacement_import.module}` : `❌ ${e.replacement_import.module}: ${e.replacement_import.error}`) : "not checked"],
        ["New warnings", (e.warnings && e.warnings.new || []).map((w) => `${w.category}: ${w.message}`).join(" | ") || "–"],
        ["Probes", e.probes && e.probes.executed ? `${e.probes.identical}/${e.probes.total} identical${e.probes.infrastructure_error ? " (runner error)" : ""}` : "not executed"],
        e.log ? ["Log", h("a", { href: fileUrl(`capsules/${c.dependency.name}/logs/replacement-py${py}.log`), target: "_blank" }, `replacement-py${py}.log`)] : null]),
      e.probes && e.probes.different && e.probes.different.length ? h("div", { class: "callout warn" }, h("b", {}, "Probe differences"),
        h("ul", { class: "plain" }, e.probes.different.map((d) => h("li", {}, code(d.probe), ": original ", code(JSON.stringify(d.original)), " vs migrated ", code(JSON.stringify(d.replacement)))))) : null));
  }
  if (ex.attempts.length > 1) out.push(h("h3", {}, "Fix loop"), h("ul", { class: "plain" },
    ex.attempts.map((a, i) => h("li", {}, `Attempt ${i + 1}: `, status(a.status || (a.clean ? "verified" : "failed")), a.edited_files.length ? ` edited ${a.edited_files.join(", ")}` : ""))));
  return out;
}

function tGaps(c) {
  const out = [h("h3", {}, "Compatibility gaps ", prov("executed"))];
  out.push(c.compatibility_gaps.length ? h("ul", { class: "checks" }, c.compatibility_gaps.map((g) =>
    h("li", {}, prov(g.provenance), h("span", {}, h("b", {}, g.kind), h("span", { class: "muted small" }, ` py ${g.pythons.join(", ")}`), h("div", {}, g.detail))))) : empty("No gaps observed in what was executed."));
  if (c.ai_review) out.push(h("h3", {}, "Interpretation ", prov("ai_generated")), h("p", {}, c.ai_review.summary || ""),
    h("ul", { class: "plain" }, (c.ai_review.gap_explanations || []).map((g) => h("li", {}, h("i", {}, g.gap), ": ", g.likely_cause, " → ", g.suggested_fix))));
  out.push(h("h3", {}, "Open concerns, uncertainties & limitations"),
    c.open_concerns_and_limitations.length ? h("ul", { class: "checks" }, c.open_concerns_and_limitations.map((x) => h("li", {}, prov(x.provenance), h("span", {}, x.text)))) : empty());
  return out;
}

function tProof(c) {
  const name = c.dependency.name, files = capData.files;
  const out = [h("h3", {}, "Capsule artifacts (written by the backend)"),
    h("div", { class: "files" }, files.map((f) => h("a", { href: fileUrl(f), target: "_blank", title: f }, f.replace(`capsules/${name}/`, ""))))];
  if (c.proposed_replacement.model_calls && c.proposed_replacement.model_calls.length)
    out.push(h("h3", {}, "Model calls ", prov("ai_generated")), h("ul", { class: "plain" }, c.proposed_replacement.model_calls.map((m) =>
      h("li", {}, m.model, " · sha256 ", code((m.prompt_sha256 || "").slice(0, 12)), " · ", h("a", { href: fileUrl(m.log), target: "_blank" }, m.log)))));
  if (files.includes(`capsules/${name}/migration.patch`)) {
    const pre = h("pre", { class: "diff" }, "loading…");
    out.push(h("h3", {}, "Migration patch · review before applying"), pre,
      h("div", { class: "action-bar" }, h("button", { class: "secondary", onclick: () => applyPatch(name) }, "Apply to a fresh working copy"),
        h("span", { class: "muted small" }, "The analysed repository is never modified.")), h("div", { id: "apply-" + name }));
    fetch(fileUrl(`capsules/${name}/migration.patch`)).then((r) => r.text()).then((t) => {
      pre.replaceChildren(...t.split("\n").map((l) => h("div", { class: /^(\+\+\+|---|@@)/.test(l) ? "h" : l.startsWith("+") ? "a" : l.startsWith("-") ? "d" : "" }, l || " ")));
    });
  }
  return out;
}

async function applyPatch(which) {
  const box = document.getElementById("apply-" + which);
  try {
    const r = await api(`/api/jobs/${job.id}/apply`, { method: "POST", body: JSON.stringify({ which }) });
    if (box) box.replaceChildren(h("div", { class: "callout " + (r.status === "applied" ? "ok" : "bad") }, status(r.status === "applied" ? "applied" : "failed"), " ",
      r.working_copy ? ["Working copy: ", code(r.working_copy)] : "", h("br"), r.note, h("pre", { class: "log" }, r.output)));
  } catch (e) { if (box) box.replaceChildren(h("div", { class: "callout bad" }, e.message)); }
}

// ------------------------------------------------------------------ boot
loadLLM();
(async () => {   // re-attach to a run after reload (#run=<id>) or to the latest run
  try {
    const want = (location.hash.match(/run=([\w-]+)/) || [])[1];
    const runs = await api("/api/jobs");
    const r = runs.find((x) => x.id === want) || runs[0];
    if (r) attach(r);
  } catch (e) { /* no runs yet */ }
})();
