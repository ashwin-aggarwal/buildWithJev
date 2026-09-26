/* Detective Jev run viewer.
 *
 * ONE piece of state drives the Run tab: the selected timestep t (chunk index).
 * The bar chart, line chart, and text panel are all views of that t; every
 * control (scrubber, buttons, play, keyboard, clicking/dragging the chart)
 * goes through setT().
 *
 * Spoiler rule: nothing here knows or shows which character is the culprit.
 * Candidates are ordered by first appearance in the text; colours go to the
 * characters Jev rated highest at some point, in first-appearance order.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const SLOTS = 8;                 // categorical colour slots (fixed order, never cycled)
const BASE_STEPS_PER_SEC = 2;    // 1x speed
const LOG_FLOOR = 0.01;          // Jev reports probabilities to 2 decimals

const state = {
  runs: [],
  run: null,          // payload from /api/run/<file>
  cost: null,         // payload from /api/cost/<file>
  t: 1,
  playing: false,
  timer: null,
  speed: 1,
  hover: null,        // candidate index or null
  hidden: new Set(),  // candidate indices whose line is hidden
  fullRun: false,
  log: false,
  tab: "run",
};

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function pct(p) {
  // Jev's probabilities come to 2 decimals, so whole percents are the honest precision.
  if (p == null) return "–";
  if (p >= 0.005) return `${Math.round(p * 100)}%`;
  return p > 0 ? "<1%" : "0%";
}
function money(x) {
  if (x == null) return "–";
  if (x === 0) return "$0";
  if (x < 0.01) return `$${x.toFixed(4)}`;
  if (x < 1) return `$${x.toFixed(3)}`;
  if (x < 100) return `$${x.toFixed(2)}`;
  return `$${Math.round(x).toLocaleString()}`;
}
function tokens(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return String(Math.round(n));
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function label(c) { return c.is_none ? "None of these" : c.name; }

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

async function getJSON(url) {
  const r = await fetch(url);
  const body = await r.json();
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

async function init() {
  try {
    state.runs = (await getJSON("/api/runs")).runs;
  } catch (e) {
    $("bookline").textContent = `Could not load runs: ${e.message}`;
    return;
  }
  if (!state.runs.length) {
    $("bookline").textContent = "No saved runs";
    $("empty").hidden = false;
    return;
  }
  const sel = $("runsel");
  sel.innerHTML = state.runs.map((r) =>
    `<option value="${esc(r.file)}">${esc(r.title || r.book_id)} · ${esc(r.candidate_set)} · ${esc(r.condition)}${r.mock ? " · MOCK" : ""}</option>`
  ).join("");
  $("runpick").hidden = state.runs.length < 2;
  sel.addEventListener("change", () => loadRun(sel.value));
  wireControls();
  showTab(location.hash === "#cost" ? "cost" : "run");
  await loadRun(state.runs[0].file);
}

async function loadRun(file) {
  pause();
  const run = await getJSON(`/api/run/${encodeURIComponent(file)}`);
  prepareRun(run);
  state.run = run;
  state.cost = null;
  state.hidden = new Set();
  state.hover = null;
  $("bookline").textContent =
    `${run.title || run.book_id}${run.author ? " · " + run.author : ""} · ${run.candidates.length} candidates · ${run.candidate_set}`;
  $("mockbanner").hidden = !run.mock;
  $("scrub").max = run.steps.length;
  buildBars();
  buildLegend();
  setT(1, { instant: true });
  drawChart();
  if (state.tab === "cost") loadCost();
}

/* Decide each candidate's colour once per run. Colour follows the entity: the
 * SLOTS characters with the highest peak probability get the categorical
 * slots, assigned in first-appearance order (the candidates' order); everyone
 * else is a muted "other" line. none_of_these is a dashed neutral line. */
function prepareRun(run) {
  const n = run.candidates.length;
  const peak = new Array(n).fill(0);
  for (const s of run.steps) s.p.forEach((p, i) => { if (p > peak[i]) peak[i] = p; });
  const ranked = run.candidates.map((c, i) => i).filter((i) => !run.candidates[i].is_none)
    .sort((a, b) => peak[b] - peak[a]).slice(0, SLOTS);
  const coloured = new Set(ranked);
  let slot = 0;
  run.candidates.forEach((c, i) => {
    c.i = i;
    c.peak = peak[i];
    if (c.is_none) { c.kind = "none"; c.color = "var(--none)"; }
    else if (coloured.has(i)) { c.kind = "series"; c.color = `var(--s${++slot})`; }
    else { c.kind = "other"; c.color = "var(--other)"; }
  });
  run.byT = new Map(run.steps.map((s) => [s.t, s]));
  run.chunkByT = new Map(run.chunks.map((c) => [c.t, c]));
  run.maxT = run.steps.length;
}

// ---------------------------------------------------------------------------
// The one state change
// ---------------------------------------------------------------------------

function setT(t, opts = {}) {
  const run = state.run;
  if (!run) return;
  state.t = Math.max(1, Math.min(run.maxT, Math.round(t)));
  $("scrub").value = state.t;
  $("readout").textContent = `${state.t} / ${run.maxT}`;
  $("bars-t").textContent = state.t;
  updateBars(opts.instant);
  updateChartT();
  updateText();
}

function setHover(i) {
  if (state.hover === i) return;
  state.hover = i;
  updateHover();
}

// ---------------------------------------------------------------------------
// Left: bar chart of the distribution at t
// ---------------------------------------------------------------------------

function buildBars() {
  const box = $("bars");
  box.innerHTML = state.run.candidates.map((c) => `
    <div class="bar-row" data-i="${c.i}">
      <span class="name" title="${esc(label(c))}">${esc(label(c))}</span>
      <span class="track"><span class="fill" style="background:${c.color}${c.kind === "none" ? ";opacity:.7" : ""}"></span></span>
      <span class="val"></span>
    </div>`).join("");
  for (const row of box.children) {
    const i = Number(row.dataset.i);
    row.addEventListener("mouseenter", () => setHover(i));
    row.addEventListener("mouseleave", () => setHover(null));
  }
}

function updateBars(instant) {
  const run = state.run;
  const step = run.byT.get(state.t);
  const box = $("bars");
  const rows = [...box.children];
  const order = run.candidates.map((c) => c.i)
    .sort((a, b) => (step.p[b] - step.p[a]) || (a - b));
  const rowH = Math.max(20, Math.min(30, Math.floor(box.clientHeight / run.candidates.length) || 24));
  if (instant) box.style.setProperty("--dur", "0ms");
  order.forEach((ci, rank) => {
    const row = rows[ci];
    row.style.height = `${rowH}px`;
    row.style.transform = `translateY(${rank * rowH}px)`;
    row.querySelector(".fill").style.width = `${(step.p[ci] * 100).toFixed(2)}%`;
    row.querySelector(".val").textContent = pct(step.p[ci]);
  });
  // Rows are absolutely positioned; a spacer gives the box its scroll height.
  const spacer = box.querySelector(".spacer") || box.appendChild(Object.assign(document.createElement("div"), { className: "spacer" }));
  spacer.style.height = `${order.length * rowH}px`;
  if (instant) requestAnimationFrame(() => box.style.removeProperty("--dur"));
}

// ---------------------------------------------------------------------------
// Center: line chart
// ---------------------------------------------------------------------------

const M = { l: 46, r: 16, t: 14, b: 34 };
let geom = null;   // {w, h, x(t), y(p), tAt(px)}

function yFor(p) {
  const { h } = geom;
  const top = M.t, bottom = h - M.b;
  if (state.log) {
    const v = Math.log10(Math.max(p, LOG_FLOOR));
    const lo = Math.log10(LOG_FLOOR);
    return bottom - ((v - lo) / (0 - lo)) * (bottom - top);
  }
  return bottom - p * (bottom - top);
}

function drawChart() {
  const run = state.run;
  if (!run) return;
  const svg = $("chart");
  const w = svg.clientWidth, h = svg.clientHeight;
  if (!w || !h) return;
  const n = run.n_chunks;
  const x = (t) => M.l + ((t - 1) / Math.max(1, n - 1)) * (w - M.l - M.r);
  const tAt = (px) => 1 + ((px - M.l) / (w - M.l - M.r)) * (n - 1);
  geom = { w, h, x, tAt };

  const ticksY = state.log
    ? [[0.01, "≤1%"], [0.03, "3%"], [0.1, "10%"], [0.3, "30%"], [1, "100%"]]
    : [0, 0.25, 0.5, 0.75, 1].map((v) => [v, `${v * 100}%`]);
  const stepX = n > 120 ? 20 : n > 60 ? 10 : 5;
  const ticksX = [1];
  for (let t = stepX; t <= n; t += stepX) ticksX.push(t);

  let out = `<g class="grid">`;
  for (const [v] of ticksY) out += `<line x1="${M.l}" x2="${w - M.r}" y1="${yFor(v)}" y2="${yFor(v)}"/>`;
  out += `</g>`;
  for (const [v, lab] of ticksY) out += `<text class="tick" x="${M.l - 8}" y="${yFor(v) + 4}" text-anchor="end">${lab}</text>`;
  for (const t of ticksX) out += `<text class="tick" x="${x(t)}" y="${h - M.b + 16}" text-anchor="middle">${t}</text>`;
  out += `<text class="axis-title" x="${w - M.r}" y="${h - 4}" text-anchor="end">chunk →</text>`;
  // Draw muted lines first so coloured ones sit on top.
  const drawOrder = [...run.candidates].sort((a, b) => (a.kind === "series") - (b.kind === "series"));
  out += `<g class="lines">`;
  for (const c of drawOrder) {
    out += `<path class="series ${c.kind}" data-i="${c.i}" style="stroke:${c.kind === "series" ? c.color : ""}"/>`;
  }
  out += `</g>`;
  out += `<line class="hover-x" id="hover-x" y1="${M.t}" y2="${h - M.b}" visibility="hidden"/>`;
  out += `<line class="marker" id="marker" y1="${M.t - 4}" y2="${h - M.b}"/>`;
  out += `<g id="dots"></g>`;
  out += `<rect class="overlay" id="overlay" x="${M.l}" y="0" width="${w - M.l - M.r}" height="${h}"/>`;
  svg.innerHTML = out;
  bindChartPointer();
  updateChartT();
}

function pathFor(c, upTo) {
  const run = state.run;
  let d = "";
  for (const s of run.steps) {
    if (s.t > upTo) break;
    d += `${d ? "L" : "M"}${geom.x(s.t).toFixed(1)},${yFor(s.p[c.i]).toFixed(1)}`;
  }
  return d;
}

function updateChartT() {
  const run = state.run;
  if (!run || !geom) return;
  const upTo = state.fullRun ? run.maxT : state.t;
  for (const el of $("chart").querySelectorAll(".series")) {
    const c = run.candidates[Number(el.dataset.i)];
    el.setAttribute("d", state.hidden.has(c.i) ? "" : pathFor(c, upTo));
  }
  const mx = geom.x(state.t);
  const marker = $("marker");
  marker.setAttribute("x1", mx);
  marker.setAttribute("x2", mx);
  const step = run.byT.get(state.t);
  $("dots").innerHTML = run.candidates
    .filter((c) => c.kind === "series" && !state.hidden.has(c.i))
    .map((c) => `<circle class="dot" data-i="${c.i}" cx="${mx}" cy="${yFor(step.p[c.i])}" r="4.5" fill="${c.color}"/>`)
    .join("");
  updateHover();
}

function bindChartPointer() {
  const overlay = $("overlay");
  const svg = $("chart");
  let dragging = false;
  const local = (ev) => {
    const r = svg.getBoundingClientRect();
    return [ev.clientX - r.left, ev.clientY - r.top];
  };
  overlay.addEventListener("pointerdown", (ev) => {
    dragging = true;
    overlay.setPointerCapture(ev.pointerId);
    pause();
    setT(geom.tAt(local(ev)[0]));
    hideTip();
  });
  overlay.addEventListener("pointermove", (ev) => {
    const [px, py] = local(ev);
    if (dragging) { setT(geom.tAt(px)); return; }
    hoverAt(px, py);
  });
  const end = (ev) => {
    if (!dragging) return;
    dragging = false;
    try { overlay.releasePointerCapture(ev.pointerId); } catch (_) {}
  };
  overlay.addEventListener("pointerup", end);
  overlay.addEventListener("pointercancel", end);
  overlay.addEventListener("pointerleave", () => { if (!dragging) { setHover(null); hideTip(); } });
}

/* Hovering: nearest visible line within 14px highlights that character; the
 * tooltip lists the top values at the hovered chunk. */
function hoverAt(px, py) {
  const run = state.run;
  const upTo = state.fullRun ? run.maxT : state.t;
  const t = Math.max(1, Math.min(run.maxT, Math.round(geom.tAt(px))));
  const hx = $("hover-x");
  if (t > upTo) { setHover(null); hideTip(); hx.setAttribute("visibility", "hidden"); return; }
  const step = run.byT.get(t);
  let best = null, bestD = 14;
  for (const c of run.candidates) {
    if (state.hidden.has(c.i)) continue;
    const d = Math.abs(yFor(step.p[c.i]) - py);
    if (d < bestD || (d === bestD && c.kind === "series")) { best = c.i; bestD = d; }
  }
  setHover(best);
  hx.setAttribute("x1", geom.x(t));
  hx.setAttribute("x2", geom.x(t));
  hx.setAttribute("visibility", "visible");
  showTip(t, px, py);
}

function showTip(t, px, py) {
  const run = state.run;
  const step = run.byT.get(t);
  const top = run.candidates.filter((c) => !state.hidden.has(c.i))
    .sort((a, b) => step.p[b.i] - step.p[a.i]).slice(0, 5);
  if (state.hover != null && !top.some((c) => c.i === state.hover)) top.push(run.candidates[state.hover]);
  const tip = $("tip");
  tip.innerHTML = `<div class="tip-h">Chunk ${t} · click to jump here</div>` + top.map((c) => {
    const name = c.i === state.hover ? `<b>${esc(label(c))}</b>` : esc(label(c));
    return `<div class="tip-r"><span class="key ${c.kind === "none" ? "none" : ""}" style="border-color:${c.color}"></span>${name}<span class="v">${pct(step.p[c.i])}</span></div>`;
  }).join("");
  tip.hidden = false;
  const wrap = $("chartwrap");
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let left = px + 14, topPx = py + 14;
  if (left + tw > wrap.clientWidth) left = px - tw - 14;
  if (topPx + th > wrap.clientHeight) topPx = Math.max(0, py - th - 14);
  tip.style.left = `${left}px`;
  tip.style.top = `${topPx}px`;
}
function hideTip() {
  $("tip").hidden = true;
  const hx = $("hover-x");
  if (hx) hx.setAttribute("visibility", "hidden");
}

// ---------------------------------------------------------------------------
// Legend (toggle lines) and cross-panel highlight
// ---------------------------------------------------------------------------

function buildLegend() {
  const run = state.run;
  const series = run.candidates.filter((c) => c.kind === "series");
  const others = run.candidates.filter((c) => c.kind === "other");
  const none = run.candidates.filter((c) => c.kind === "none");
  let html = series.map((c) =>
    `<button data-i="${c.i}" aria-pressed="true"><span class="key" style="border-color:${c.color}"></span>${esc(c.name)}</button>`).join("");
  if (others.length) {
    html += `<button data-group="other" aria-pressed="true" title="${esc(others.map((c) => c.name).join(", "))}"><span class="key" style="border-color:var(--other)"></span>Other characters (${others.length})</button>`;
  }
  for (const c of none) {
    html += `<button data-i="${c.i}" aria-pressed="true"><span class="key none" style="border-color:var(--none)"></span>None of these</button>`;
  }
  const box = $("legend");
  box.innerHTML = html;
  for (const b of box.children) {
    b.addEventListener("click", () => {
      const ids = b.dataset.group ? others.map((c) => c.i) : [Number(b.dataset.i)];
      const hide = b.getAttribute("aria-pressed") === "true";
      ids.forEach((i) => (hide ? state.hidden.add(i) : state.hidden.delete(i)));
      b.setAttribute("aria-pressed", String(!hide));
      updateChartT();
    });
    if (!b.dataset.group) {
      b.addEventListener("mouseenter", () => setHover(Number(b.dataset.i)));
      b.addEventListener("mouseleave", () => setHover(null));
    }
  }
}

function updateHover() {
  const h = state.hover;
  for (const el of $("chart").querySelectorAll(".series")) {
    const i = Number(el.dataset.i);
    el.classList.toggle("hot", h === i);
    el.classList.toggle("dim", h != null && h !== i);
  }
  for (const row of $("bars").querySelectorAll(".bar-row")) {
    const i = Number(row.dataset.i);
    row.classList.toggle("hot", h === i);
    row.classList.toggle("dim", h != null && h !== i);
  }
  // Bring the hovered line to the front.
  if (h != null) {
    const el = $("chart").querySelector(`.series[data-i="${h}"]`);
    if (el) el.parentNode.appendChild(el);
  }
  updateText();
}

// ---------------------------------------------------------------------------
// Right: text of chunk t
// ---------------------------------------------------------------------------

let shownT = null;
function updateText() {
  const run = state.run;
  const chunk = run.chunkByT.get(state.t);
  $("chunk-title").textContent = `Chunk ${state.t} of ${run.n_chunks}`;
  $("chunk-sub").textContent =
    `${chunk.chapter ? chunk.chapter + " · " : ""}${Math.round(chunk.fraction * 100)}% through the book`;
  let html = chunk.text.split("\n\n").map((p) => `<p>${esc(p)}</p>`).join("");
  const c = state.hover != null ? run.candidates[state.hover] : null;
  if (c && c.names.length) {
    const alts = c.names.map((n) => esc(n).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).sort((a, b) => b.length - a.length);
    const rx = new RegExp(`(?<![\\w])(${alts.join("|")})(?![\\w])`, "g");
    html = html.replace(rx, `<mark style="--c:${c.color}">$1</mark>`);
  }
  const box = $("chunktext");
  box.innerHTML = html;
  if (shownT !== state.t) { box.scrollTop = 0; shownT = state.t; }
}

// ---------------------------------------------------------------------------
// Playback + controls
// ---------------------------------------------------------------------------

function interval() { return 1000 / (BASE_STEPS_PER_SEC * state.speed); }

function play() {
  const run = state.run;
  if (!run) return;
  if (state.t >= run.maxT) setT(1);
  state.playing = true;
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
    // Bars glide for most of each step, so fast playback still reads as motion.
    document.documentElement.style.setProperty("--dur", `${Math.min(350, interval() * 0.85)}ms`);
  }
  renderPlayButton();
  const tick = () => {
    if (!state.playing) return;
    if (state.t >= run.maxT) { pause(); return; }
    setT(state.t + 1);
    state.timer = setTimeout(tick, interval());
  };
  state.timer = setTimeout(tick, interval());
}
function pause() {
  state.playing = false;
  clearTimeout(state.timer);
  document.documentElement.style.removeProperty("--dur");
  renderPlayButton();
}
function renderPlayButton() {
  $("play-icon").innerHTML = state.playing
    ? `<rect x="4" y="3.5" width="2.8" height="9" rx=".8" class="fill"/><rect x="9.2" y="3.5" width="2.8" height="9" rx=".8" class="fill"/>`
    : `<path d="M5 3.5v9l7.5-4.5z" class="fill"/>`;
  $("play").setAttribute("aria-label", state.playing ? "Pause" : "Play");
}
function step(delta) { pause(); setT(state.t + delta); }

function showTab(tab) {
  state.tab = tab;
  $("tab-run").setAttribute("aria-selected", String(tab === "run"));
  $("tab-cost").setAttribute("aria-selected", String(tab === "cost"));
  $("view-run").hidden = tab !== "run";
  $("view-cost").hidden = tab !== "cost";
  history.replaceState(null, "", tab === "cost" ? "#cost" : "#");
  if (tab === "run") { pause(); requestAnimationFrame(() => { drawChart(); state.run && updateBars(true); }); }
  else { pause(); loadCost(); }
}

function wireControls() {
  $("prev").addEventListener("click", () => step(-1));
  $("next").addEventListener("click", () => step(1));
  $("play").addEventListener("click", () => (state.playing ? pause() : play()));
  $("scrub").addEventListener("input", (e) => { pause(); setT(Number(e.target.value)); });
  $("speed").addEventListener("change", (e) => {
    state.speed = Number(e.target.value);
    if (state.playing) { pause(); play(); }
  });
  $("fullrun").addEventListener("change", (e) => { state.fullRun = e.target.checked; updateChartT(); });
  $("logscale").addEventListener("change", (e) => { state.log = e.target.checked; drawChart(); });
  $("tab-run").addEventListener("click", () => showTab("run"));
  $("tab-cost").addEventListener("click", () => showTab("cost"));
  for (const id of ["c-compress", "c-output", "c-reason"]) $(id).addEventListener("input", renderCost);

  document.addEventListener("keydown", (e) => {
    if (state.tab !== "run" || !state.run) return;
    const tag = e.target.tagName;
    if (tag === "SELECT" || (tag === "INPUT" && e.target.type !== "range" && e.target.type !== "checkbox")) return;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      step((e.key === "ArrowRight" ? 1 : -1) * (e.shiftKey ? 10 : 1));
    } else if (e.key === " " && tag !== "BUTTON") {
      e.preventDefault();
      state.playing ? pause() : play();
    } else if (e.key === "Home") { e.preventDefault(); step(-Infinity); }
    else if (e.key === "End") { e.preventDefault(); step(Infinity); }
  });

  new ResizeObserver(() => { if (state.tab === "run" && state.run) { drawChart(); updateBars(true); } })
    .observe($("chartwrap"));
}

// ---------------------------------------------------------------------------
// Cost analysis tab (pure arithmetic; no model is called)
// ---------------------------------------------------------------------------

async function loadCost() {
  if (!state.run) return;
  if (!state.cost || state.cost.file !== state.run.file) {
    $("costbars").innerHTML = `<p class="muted">Calculating…</p>`;
    state.cost = await getJSON(`/api/cost/${encodeURIComponent(state.run.file)}`);
  }
  renderCost();
}

function costModel() {
  const c = state.cost;
  const withComp = $("c-compress").checked;
  const full = $("c-output").value === "full";
  const reason = Math.max(0, Number($("c-reason").value) || 0);
  const inf = c.inference, comp = c.compression;
  const calls = inf.calls + (withComp ? comp.calls : 0);
  const input = inf.input_tokens + (withComp ? comp.input_tokens : 0);
  const output =
    inf.calls * ((full ? inf.llm_output_full_per_call : inf.llm_output_top_per_call) + reason) +
    (withComp ? (full ? comp.llm_output_full_total : comp.llm_output_top_total) + comp.calls * reason : 0);
  const jev = input / 1e6 * c.jev_price_per_m;
  const rows = [{ name: "Jev", provider: "TypeSafe", jev: true, inCost: jev, outCost: 0, total: jev,
                  pin: c.jev_price_per_m, pout: 0 }];
  for (const m of c.prices.models) {
    const inCost = input / 1e6 * m.input_per_m;
    const outCost = output / 1e6 * m.output_per_m;
    rows.push({ name: m.name, provider: m.provider, inCost, outCost, total: inCost + outCost,
                pin: m.input_per_m, pout: m.output_per_m });
  }
  rows.sort((a, b) => a.total - b.total);
  return { rows, calls, input, output, jev, withComp, full, reason };
}

function renderCost() {
  const c = state.cost;
  if (!c) return;
  const inf = c.inference, comp = c.compression;
  const m = costModel();

  $("tiles").innerHTML = [
    ["Jev spend, this run", money(inf.cost), `${inf.calls} inference calls, measured`],
    ["Jev time, this run", `${(inf.latency_total_ms / 1000).toFixed(0)} s`, `median ${Math.round(inf.latency_median_ms)} ms per call`],
    ["Ledger building (estimate)", money(comp.cost), `${comp.calls} compression calls, ~${tokens(comp.input_tokens)} tokens`],
    ["Tokens per full run", tokens(m.input), `${m.calls} calls · ${c.n_words ? c.n_words.toLocaleString() + " words" : ""}`],
  ].map(([k, v, d]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("");

  const max = Math.max(...m.rows.map((r) => r.total));
  $("costbars").innerHTML = m.rows.map((r) => {
    const times = r.jev ? "" : ` · ${r.total / m.jev >= 10 ? Math.round(r.total / m.jev) : (r.total / m.jev).toFixed(1)}× Jev`;
    return `<div class="cbar${r.jev ? " jev" : ""}">
      <span class="name">${esc(r.name)} <small>${esc(r.provider)}</small></span>
      <span class="track"><span class="fill" style="width:${(r.total / max * 88).toFixed(2)}%"></span><span class="val">${money(r.total)}${times}</span></span>
    </div>`;
  }).join("");

  $("costtable").innerHTML =
    `<thead><tr><th>Model</th><th>Input $/1M</th><th>Output $/1M</th><th>Input cost</th><th>Output cost</th><th>Total</th><th>vs Jev</th></tr></thead><tbody>` +
    m.rows.map((r) => `<tr class="${r.jev ? "jev" : ""}"><td>${esc(r.name)}</td><td>$${r.pin.toFixed(3)}</td><td>${r.jev ? "free" : "$" + r.pout.toFixed(2)}</td>
      <td>${money(r.inCost)}</td><td>${money(r.outCost)}</td><td>${money(r.total)}</td><td>${r.jev ? "–" : (r.total / m.jev).toFixed(1) + "×"}</td></tr>`).join("") +
    `</tbody>`;

  $("costnotes").innerHTML = `
    <p><b>How this is calculated.</b> Every model is priced for the same ${m.calls} calls on the same ${tokens(m.input)} input tokens
      Jev used${m.withComp ? " (inference measured, ledger building estimated from the chunk text and questions)" : " (inference only, measured)"}.
      A text model must also <i>write</i> its answers: ~${tokens(m.output)} output tokens here
      (${m.full ? "a probability for every option, the same information Jev returns" : "only the top answer per question"}${m.reason ? `, plus ${m.reason} thinking tokens per call` : ""}).
      Jev's output is free.</p>
    <p>Prices: ${esc(c.prices.source)}, ${esc(c.prices.fetched)} (edit <code>config/model_prices.yaml</code>). Token counts use one shared tokenizer as an
      approximation; each provider counts slightly differently. No other model was called: this compares cost only, not speed or accuracy,
      and text models don't return calibrated probabilities the way Jev does.</p>
    ${c.mock ? "<p><b>This is a mock run</b>: its Jev cost is zero and not meaningful.</p>" : ""}`;
}

init();
