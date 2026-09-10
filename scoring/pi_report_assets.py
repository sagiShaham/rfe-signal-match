"""
Page assets for the PI Planning report (v2): CSS, JavaScript and the HTML shell.

WHY THIS FILE IS SEPARATE
-------------------------
v1 built the whole report inside one enormous Python f-string, which meant every
brace in the CSS and JavaScript had to be doubled (`{{`). That is how v1 ended
up with a chart whose bars drew on top of each other and dead CSS for panels
that were never rendered: the markup was effectively unreadable, so nobody could
see what it did.

Here the CSS and JS are plain module strings — no escaping, readable as CSS and
as JS — and `render()` injects the data through unique placeholders. The
generator owns the numbers; this file owns the page.

WHAT THE PAGE DOES DIFFERENTLY FROM v1
--------------------------------------
* **The removed chart.** v1's "Portfolio Demand Snapshot" plotted RFE count,
  customer count and ARR as three grouped bar series with ARR on a secondary
  y-axis. Plotly does not group-offset a bar trace bound to a different axis, so
  the ARR bars were drawn *on top of* the count bars — the overlap in the
  screenshot — and even correctly drawn it invited the reader to compare a
  count against a dollar figure on one canvas. It is gone, not repaired: the
  same question is answered better by the domain ranking, which ranks on one
  number and annotates the rest.
* **No dual-axis bar charts anywhere, ever.** That is now a rule of the report,
  not an accident of this one chart.
* **Every chart carries an action.** Each has a one-line "so what" beneath the
  title, and the two exec charts are clickable: they answer "which of these do
  we commit to" rather than describing volume.
* **Charts cannot collide.** Long theme names are wrapped, never ellipsised;
  row height adapts to how many lines a label needs; left margins are computed
  by Plotly's `automargin` instead of being hardcoded.
* **Tables cannot overlap.** Every table is `table-layout: fixed` with an
  explicit `<colgroup>`, breaks long words, and sits in its own horizontal
  scroll container with a sensible `min-width`, so columns never ride over each
  other on a narrow screen.
* **Nothing is truncated with an ellipsis** — not a subject, not a theme name,
  not a PM Summary. Rows grow, labels wrap, containers scroll.
"""

from __future__ import annotations

import json
from typing import Any, Dict

EMPTY_REPORT = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>PI Planning Report</title></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;padding:48px;text-align:center;color:#6b7280;background:#f7f8fa">
<h2 style="color:#111827">No RFE data found</h2>
<p>Upload a Salesforce RFE export on the PI Report tab to generate a report.</p>
</body></html>"""


CSS = """
:root{
  --bg:#f7f8fa; --card:#ffffff; --sunken:#f4f6fa; --border:#e1e5ec;
  --text:#111827; --muted:#6b7280; --faint:#9ca3af;
  --accent:#2563eb; --accent-dark:#1d4ed8; --violet:#7c3aed;
  --green:#16a34a; --orange:#d97706; --red:#dc2626; --slate:#64748b;
  --band-start:#1d4ed8; --band-plan:#7c3aed; --band-keep:#64748b; --band-drop:#94a3b8;
  --shadow:0 1px 3px rgba(17,24,39,.06), 0 0 0 1px var(--border);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:"Segoe UI",Arial,sans-serif;font-size:14px;line-height:1.5}

/* ── Sticky header: title row, control row, tab row ───────────────────────── */
#top-bar{background:var(--card);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:200;box-shadow:0 1px 3px rgba(17,24,39,.05)}
#title-row{display:flex;align-items:baseline;justify-content:space-between;gap:16px;padding:10px 20px 8px;flex-wrap:wrap}
#title-row h1{font-size:15px;font-weight:700;color:var(--accent);letter-spacing:-.1px}
#title-row .sub{font-size:11px;color:var(--muted);font-weight:400;margin-left:8px}
#headline{font-size:12px;color:var(--text);background:#eff6ff;border:1px solid #bfdbfe;border-radius:20px;padding:3px 12px;font-weight:600}

#control-row{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:8px 20px;background:var(--sunken);border-top:1px solid var(--border);border-bottom:1px solid var(--border)}
.ctl{display:flex;align-items:center;gap:6px;min-width:0}
.ctl>label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);white-space:nowrap}
.ctl select{font-family:inherit;font-size:12px;color:var(--text);background:var(--card);border:1px solid var(--border);border-radius:6px;padding:5px 8px;cursor:pointer;max-width:230px}
.ctl select:hover{border-color:var(--accent)}
.ctl select:focus{outline:2px solid #bfdbfe;outline-offset:1px}
.chip-row{display:flex;gap:4px}
.chip{font-size:11px;font-weight:600;padding:4px 9px;border-radius:14px;border:1px solid var(--border);background:var(--card);color:var(--muted);cursor:pointer;user-select:none;white-space:nowrap}
.chip:hover{border-color:var(--accent);color:var(--accent)}
.chip.on{color:#fff;border-color:transparent}
.chip.on.sev-critical{background:#991b1b}
.chip.on.sev-high{background:var(--red)}
.chip.on.sev-medium{background:var(--orange)}
.chip.on.sev-low{background:var(--green)}
.chip.on.sev-unset{background:var(--slate)}
#ctl-status{margin-left:auto;display:flex;align-items:center;gap:10px;font-size:11px;color:var(--muted)}
#ctl-count strong{color:var(--text)}
.link-btn{background:none;border:none;color:var(--accent);font:inherit;font-size:11px;cursor:pointer;text-decoration:underline;padding:0}
.link-btn:hover{color:var(--accent-dark)}

#tab-row{display:flex;overflow-x:auto;padding:0 14px;scrollbar-width:thin}
#tab-row::-webkit-scrollbar{height:3px}
#tab-row::-webkit-scrollbar-thumb{background:var(--border)}
.tab{display:flex;align-items:center;gap:6px;padding:8px 12px;cursor:pointer;color:var(--muted);font-size:12px;font-weight:500;border-bottom:2px solid transparent;white-space:nowrap;flex-shrink:0}
.tab:hover{color:var(--text);background:#fafbfd}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:700}
.tab.empty{color:var(--faint)}
.tab .n{font-size:10px;font-weight:700;background:var(--sunken);color:var(--muted);border-radius:9px;padding:1px 6px;min-width:20px;text-align:center}
.tab.active .n{background:#dbeafe;color:var(--accent-dark)}
.tab .hot{width:6px;height:6px;border-radius:50%;background:var(--band-start)}

/* ── Page frame ───────────────────────────────────────────────────────────── */
.page{display:none;padding:18px 20px 32px;max-width:1680px;margin:0 auto}
.page.active{display:block}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;box-shadow:var(--shadow);margin-bottom:16px}
.card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border);flex-wrap:wrap}
.card-title{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
.card-note{font-size:11px;color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0;margin-top:3px;max-width:820px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.three-col{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
@media(max-width:1080px){.two-col,.three-col{grid-template-columns:1fr}}

/* ── KPI tiles ────────────────────────────────────────────────────────────── */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(185px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px;box-shadow:var(--shadow)}
.kpi .k-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
.kpi .k-value{font-size:23px;font-weight:700;margin:3px 0 2px;letter-spacing:-.5px}
.kpi .k-note{font-size:11px;color:var(--muted);line-height:1.45}
.kpi.accent .k-value{color:var(--accent)}
.kpi.green .k-value{color:var(--green)}
.kpi.red .k-value{color:var(--red)}
.kpi.violet .k-value{color:var(--violet)}
.kpi.slate .k-value{color:var(--slate)}

/* ── Narrative panels ─────────────────────────────────────────────────────── */
.narrative{background:linear-gradient(135deg,#eff6ff,#f5f3ff);border:1px solid #c7d2fe;border-radius:10px;padding:16px 20px;margin-bottom:16px}
.narrative h2{font-size:11px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px}
.narrative p{font-size:13px;color:#374151;line-height:1.75;margin-bottom:9px}
.narrative p:last-child{margin-bottom:0}
.narrative strong{color:var(--text);font-weight:650}
.scope-note{font-size:11px;color:var(--muted);margin-top:10px;padding-top:8px;border-top:1px solid #c7d2fe}

.domain-head{background:linear-gradient(90deg,#eff6ff,#f5f3ff);border:1px solid #c7d2fe;border-radius:10px;padding:14px 18px;margin-bottom:16px}
.domain-head h2{font-size:16px;font-weight:700;color:var(--accent)}
.domain-head .dstats{display:flex;gap:18px;flex-wrap:wrap;margin:7px 0 9px}
.domain-head .dstats span{font-size:12px;color:#374151}
.domain-head p{font-size:13px;color:#374151;line-height:1.75}

/* ── Decision columns ─────────────────────────────────────────────────────── */
.decide-col{border-radius:10px;padding:13px 15px;border:1px solid var(--border);background:var(--card);box-shadow:var(--shadow)}
.decide-col.start{border-left:4px solid var(--band-start)}
.decide-col.plan{border-left:4px solid var(--band-plan)}
.decide-col.drop{border-left:4px solid var(--band-drop)}
.decide-col h3{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.decide-col .cnt{font-size:11px;color:var(--muted);margin-bottom:9px}
.decide-col ol{margin:0;padding-left:18px}
.decide-col li{font-size:12px;line-height:1.5;margin-bottom:7px;color:#374151;cursor:pointer}
.decide-col li:hover{color:var(--accent)}
.decide-col li b{font-weight:650;color:var(--text)}
.decide-col .none{font-size:12px;color:var(--muted);font-style:italic}

/* ── Epic cards ───────────────────────────────────────────────────────────── */
.epics{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:11px}
.epic{background:var(--sunken);border:1px solid var(--border);border-radius:9px;padding:12px 13px;cursor:pointer;display:flex;flex-direction:column;gap:7px}
.epic:hover{border-color:var(--accent);box-shadow:0 2px 10px rgba(37,99,235,.10)}
.epic.open{border-color:var(--accent);background:#eff6ff}
.epic .rank{font-size:10px;font-weight:700;color:var(--accent);display:flex;align-items:center;justify-content:space-between;gap:6px}
.epic .rank .dom{color:var(--muted);font-weight:500;text-transform:none}
.epic .etitle{font-size:12.5px;font-weight:650;line-height:1.45;word-break:break-word}
.epic .pills{display:flex;gap:5px;flex-wrap:wrap}
.epic .why{font-size:11px;color:var(--muted);line-height:1.5;border-top:1px dashed var(--border);padding-top:7px;margin-top:auto}
.pill{font-size:10px;font-weight:700;padding:2px 7px;border-radius:10px;white-space:nowrap}
.pill.req{background:#dbeafe;color:var(--accent-dark)}
.pill.cust{background:#ede9fe;color:#6d28d9}
.pill.arr{background:#dcfce7;color:#15803d}
.pill.flag{background:#fee2e2;color:#991b1b}
.pill.age{background:#f1f5f9;color:var(--slate)}

.band{font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;color:#fff;white-space:nowrap}
.band.start_now{background:var(--band-start)}
.band.plan{background:var(--band-plan)}
.band.backlog{background:var(--band-keep)}
.band.drop{background:var(--band-drop);color:#1f2937}

.sev{font-size:10px;font-weight:700;padding:2px 7px;border-radius:10px;white-space:nowrap}
.sev.critical{background:#7f1d1d;color:#fff}
.sev.high{background:#fee2e2;color:#991b1b}
.sev.medium{background:#fef3c7;color:#92400e}
.sev.low{background:#dcfce7;color:#15803d}
.sev.unset{background:#f1f5f9;color:var(--slate)}

/* ── Tables: fixed layout + own scroll container, so columns cannot overlap ── */
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table.grid{width:100%;min-width:1080px;table-layout:fixed;border-collapse:collapse;font-size:12px}
table.grid th{background:var(--sunken);color:var(--muted);text-align:left;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:bottom;word-break:break-word}
table.grid td{padding:9px 10px;border-bottom:1px solid rgba(225,229,236,.75);vertical-align:top;word-break:break-word;overflow-wrap:anywhere}
table.grid tr.row-main{cursor:pointer}
table.grid tr.row-main:hover{background:#f0f7ff}
table.grid tr.row-open{background:#eff6ff}
table.grid td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.grid td.mid{text-align:center}
.theme-name{font-weight:650;color:var(--text);line-height:1.45}
.theme-sub{font-size:11px;color:var(--muted);margin-top:2px;line-height:1.45}
.score-cell{display:flex;flex-direction:column;gap:3px;align-items:flex-start}
.score-num{font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1}
.expander{display:none}
.expander.open{display:table-row}
.expander>td{padding:0 !important;background:#f8fbff}
.expander-inner{padding:12px 14px;border-top:1px solid #dbeafe}
table.sub{width:100%;min-width:1000px;table-layout:fixed;border-collapse:collapse;font-size:11.5px}
table.sub th{background:#eff6ff;color:#4b5563;text-align:left;font-size:10px;font-weight:700;text-transform:uppercase;padding:6px 8px;border-bottom:1px solid #dbeafe;word-break:break-word}
table.sub td{padding:8px;border-bottom:1px solid #e5efff;vertical-align:top;word-break:break-word;overflow-wrap:anywhere}
table.sub tr:last-child td{border-bottom:none}
/* PM Summaries are shown in full, always. No clamping, no ellipsis, no nowrap. */
.pm{white-space:normal !important;overflow:visible !important;text-overflow:clip !important;line-height:1.6;color:#374151}
.orig{margin-top:7px}
.orig summary{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:var(--muted);cursor:pointer}
.orig summary:hover{color:var(--accent)}
.orig .otext{margin-top:6px;padding:8px 10px;background:var(--sunken);border-left:3px solid var(--border);border-radius:4px;font-size:11px;color:#4b5563;white-space:pre-wrap;line-height:1.6;max-height:260px;overflow-y:auto}

.case{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--accent);cursor:pointer;display:inline-flex;align-items:center;gap:4px;padding:2px 6px;border-radius:4px;border:1px solid #bfdbfe;background:#eff6ff;white-space:nowrap}
.case:hover{background:#dbeafe;border-color:var(--accent)}
.case.copied{color:var(--green);background:#dcfce7;border-color:#86efac}
.flagmark{color:var(--red);font-weight:700}

/* ── Interpretation / actions ─────────────────────────────────────────────── */
.interp{background:var(--sunken);border:1px solid var(--border);border-radius:9px;padding:13px 15px}
.interp-item{margin-bottom:11px}
.interp-item:last-child{margin-bottom:0}
.interp-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.interp-item.accent .interp-label{color:var(--accent)}
.interp-item.green .interp-label{color:var(--green)}
.interp-item.orange .interp-label{color:var(--orange)}
.interp-item.muted .interp-label{color:var(--muted)}
.interp-text{font-size:12px;color:#374151;line-height:1.65}
.actions{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:9px;padding:13px 15px}
.action{display:flex;gap:10px;margin-bottom:10px;align-items:flex-start}
.action:last-child{margin-bottom:0}
.action .n{background:var(--accent);color:#fff;border-radius:50%;width:19px;height:19px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;flex-shrink:0;margin-top:1px}
.action .t{font-size:12px;line-height:1.6;color:#111827}

.chart{width:100%}
.chart-note{font-size:11px;color:var(--muted);margin-top:6px;text-align:right}
.empty-state{padding:26px 16px;text-align:center;color:var(--muted);font-size:12px;background:var(--sunken);border:1px dashed #d1d5db;border-radius:9px}
.dq{font-size:11.5px;color:var(--muted);line-height:1.7}
.dq li{margin-left:16px;margin-bottom:3px}
details.rank-help summary{font-size:11px;color:var(--accent);cursor:pointer;font-weight:600}
details.rank-help .body{margin-top:8px;font-size:11.5px;color:#4b5563;line-height:1.7}
details.rank-help table{border-collapse:collapse;margin-top:6px;font-size:11px}
details.rank-help td{padding:2px 10px 2px 0}
footer{text-align:center;padding:18px;color:var(--faint);font-size:11px;border-top:1px solid var(--border);margin-top:8px}

@media print{
  #top-bar{position:static}
  #control-row,#tab-row{display:none}
  .page{display:block !important;page-break-after:always;max-width:none}
  .card,.kpi,.narrative,.domain-head{break-inside:avoid}
}
"""


JS = r"""
'use strict';
/* ===========================================================================
   PI Planning report — client behaviour.

   The browser never scores anything: `PAYLOAD.tf[<timeframe>]` already carries
   the scores, bands, aggregates and written narratives for that window, all
   computed in Python. The job here is to (a) apply the reader's filters,
   (b) order things by the chosen sort field, and (c) draw.

   Two invariants worth keeping if this is ever extended:
     1. Filters NARROW, they never re-score. A theme's rank is a property of the
        theme; ticking a severity box must not change it, or two people reading
        the same report with different filters would argue about different
        numbers.
     2. No text is ever truncated. Labels wrap, containers scroll, rows grow.
   ========================================================================= */

const M = PAYLOAD;
const SEV_ORDER = {critical: 4, high: 3, medium: 2, low: 1, '': 0};
const BAND_COLOR = {start_now: '#1d4ed8', plan: '#7c3aed', backlog: '#64748b', drop: '#94a3b8'};
const BAND_LABEL = {};
M.meta.bands.forEach(b => { BAND_LABEL[b.key] = b.label; });

const RECORDS = {};
M.records.forEach(r => { RECORDS[r.case] = r; });

const state = {
  tf: M.meta.default_tf,
  sort: 'priority',
  sev: new Set(['critical', 'high', 'medium', 'low', '']),
  bi: 'any',
  arr: 'any',
  rep: 'any',
  page: 'exec',
  drawn: new Set(),
  expandAll: {}
};

/* ── Small utilities ─────────────────────────────────────────────────────── */

function frame() { return M.tf[state.tf]; }

function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fmtArr(v) {
  v = Number(v || 0);
  if (v >= 1e6) return '$' + (v / 1e6).toFixed(1) + 'M';
  if (v >= 1e3) return '$' + Math.round(v / 1e3) + 'K';
  return '$' + Math.round(v);
}

function plural(n, word, suffix) {
  suffix = suffix === undefined ? 's' : suffix;
  return n + ' ' + word + (n === 1 ? '' : suffix);
}

function sevKey(r) { return r.severity_key || ''; }
function sevClass(k) { return k ? k : 'unset'; }
function sevLabel(k) { return k ? k.charAt(0).toUpperCase() + k.slice(1) : 'Unset'; }

/** The scored view of one case for the active timeframe. */
function sc(caseNum) { return frame().scores[caseNum] || {}; }

function ageMonths(days) { return Math.round((days || 0) / 30.4); }

/* ── Filtering ───────────────────────────────────────────────────────────── */

function filtersActive() {
  return state.sev.size !== 5 || state.bi !== 'any' || state.arr !== 'any' || state.rep !== 'any';
}

function passesFilters(r) {
  if (!state.sev.has(sevKey(r))) return false;
  if (state.bi === 'flagged' && !r.bi) return false;
  if (state.bi === 'unflagged' && r.bi) return false;
  if (state.arr !== 'any') {
    const a = Number(r.arr || 0);
    if (state.arr === 'high' && a < 1e6) return false;
    if (state.arr === 'mid' && (a < 250000 || a >= 1e6)) return false;
    if (state.arr === 'low' && (a <= 0 || a >= 250000)) return false;
    if (state.arr === 'none' && a > 0) return false;
  }
  if (state.rep !== 'any') {
    const c = sc(r.case).customers || 1;
    if (state.rep === 'multi' && c < 2) return false;
    if (state.rep === 'three' && c < 3) return false;
    if (state.rep === 'single' && c > 1) return false;
  }
  return true;
}

/** Records inside the active timeframe (all of them, filters not applied). */
function scopeRecords() {
  return M.records.filter(r => r.tfs.indexOf(state.tf) !== -1);
}

/** Records inside the active timeframe that also pass the filters. */
function activeRecords() {
  return scopeRecords().filter(passesFilters);
}

/** Case numbers passing the filters — used to decide which themes to list. */
function activeCaseSet() {
  const s = new Set();
  activeRecords().forEach(r => s.add(r.case));
  return s;
}

/* ── Ordering ────────────────────────────────────────────────────────────── */
/* Mirrors scoring/priority.py `sort_key`, so "Sort by ARR" means the same
   thing on the Executive page, in a domain table and in a chart. */

function cmpValue(item, kind, isRecord) {
  const s = isRecord ? sc(item.case) : item;
  switch (kind) {
    case 'severity':
      return [isRecord ? (SEV_ORDER[sevKey(item)] || 0) : (SEV_ORDER[item.top_severity] || 0),
              s.score || 0];
    case 'arr':
      return [isRecord ? Number(item.arr || 0) : (item.arr || 0), s.score || 0];
    case 'impact':
      return [isRecord ? (item.bi ? 1 : 0) : (item.flagged || 0), s.score || 0];
    case 'repetition':
      return [s.customers || 0, s.requests || 0, s.score || 0];
    case 'recency': {
      const a = s.age_days === null || s.age_days === undefined ? 1e6 : s.age_days;
      return [-a, s.score || 0];
    }
    case 'oldest': {
      const a = s.age_days === null || s.age_days === undefined ? -1 : s.age_days;
      return [a, s.score || 0];
    }
    default:
      return [s.score || 0, (isRecord ? Number(item.arr || 0) : item.arr) || 0, s.customers || 0];
  }
}

function sortItems(list, isRecord) {
  const kind = state.sort;
  return list.slice().sort((a, b) => {
    const va = cmpValue(a, kind, isRecord), vb = cmpValue(b, kind, isRecord);
    for (let i = 0; i < Math.max(va.length, vb.length); i++) {
      const d = (vb[i] || 0) - (va[i] || 0);
      if (d) return d;
    }
    return 0;
  });
}

/* ── Theme (cluster) views ───────────────────────────────────────────────── */

/** Themes for the active timeframe that still contain a passing case.
 *  `matched` records how many of the theme's cases pass, so a filtered table
 *  can say "2 of 12 match" without pretending the theme is smaller. */
function visibleClusters(domainId) {
  const pass = activeCaseSet();
  const out = [];
  frame().clusters.forEach(c => {
    if (domainId && c.domain_id !== domainId) return;
    const matched = c.cases.filter(x => pass.has(x));
    if (!matched.length) return;
    out.push(Object.assign({}, c, {matched: matched, matchedCount: matched.length}));
  });
  return sortItems(out, false);
}

function visibleDomains() {
  const pass = activeCaseSet();
  const counts = {};
  activeRecords().forEach(r => { counts[r.domain_id] = (counts[r.domain_id] || 0) + 1; });
  return frame().domains.map(d => Object.assign({}, d, {matchedCount: counts[d.id] || 0}));
}

/* ── Chart helpers ───────────────────────────────────────────────────────── */

const PLOTLY_CFG = {responsive: true, displayModeBar: false};

function layout(overrides) {
  return Object.assign({
    paper_bgcolor: 'white',
    plot_bgcolor: 'white',
    font: {color: '#374151', family: "'Segoe UI',Arial,sans-serif", size: 11},
    xaxis: {gridcolor: '#f3f4f6', zeroline: false, automargin: true},
    yaxis: {gridcolor: '#f3f4f6', zeroline: false, automargin: true},
    margin: {t: 14, r: 18, b: 44, l: 8},
    showlegend: false,
    autosize: true,
    hoverlabel: {bgcolor: 'white', bordercolor: '#e1e5ec',
                 font: {color: '#111827', size: 11}, align: 'left'}
  }, overrides || {});
}

/** Wrap a label onto as many lines as it needs. Never truncates — a theme name
 *  is the thing the reader has to recognise, so it is shown in full and the row
 *  height is grown to fit instead. */
function wrapLabel(text, width) {
  const words = String(text || '').split(/\s+/);
  const lines = [];
  let line = '';
  words.forEach(w => {
    if (!line.length) { line = w; return; }
    if ((line + ' ' + w).length <= width) { line += ' ' + w; }
    else { lines.push(line); line = w; }
  });
  if (line.length) lines.push(line);
  return {html: lines.map(esc).join('<br>'), lines: Math.max(1, lines.length)};
}

/** Height for a horizontal category chart, from how many lines its labels need. */
function barHeight(wrapped, base) {
  const lines = wrapped.reduce((s, w) => s + w.lines, 0);
  return Math.max(base || 240, lines * 17 + wrapped.length * 16 + 60);
}

/** Disclose a chart's density limit. Called with the number plotted and the
 *  number available; writes nothing when the chart is showing everything. */
function setChartNote(elId, shown, total, what) {
  const el = document.getElementById(elId + '-note');
  if (!el) return;
  el.innerHTML = shown < total
    ? 'Showing the ' + shown + ' highest-ranked of ' + total + ' ' + what +
      '. Use the filters or the table below to reach the rest.'
    : '';
}

/* ── Header, controls, tabs ──────────────────────────────────────────────── */

function renderControls() {
  const tfSel = M.meta.timeframes.map(t =>
    '<option value="' + t.key + '"' + (t.key === state.tf ? ' selected' : '') + '>' +
    esc(t.label) + '</option>').join('');
  const sortSel = M.meta.sort_fields.map(f =>
    '<option value="' + f.key + '"' + (f.key === state.sort ? ' selected' : '') +
    ' title="' + esc(f.help) + '">' + esc(f.label) + '</option>').join('');

  const sevChips = [['critical', 'Critical'], ['high', 'High'], ['medium', 'Medium'],
                    ['low', 'Low'], ['', 'Unset']].map(([k, l]) =>
    '<span class="chip sev-' + sevClass(k) + (state.sev.has(k) ? ' on' : '') +
    '" onclick="toggleSev(\'' + k + '\')">' + l + '</span>').join('');

  const sel = (id, opts, cur) => '<select id="' + id + '" onchange="onSelect(\'' + id + '\',this.value)">' +
    opts.map(o => '<option value="' + o[0] + '"' + (o[0] === cur ? ' selected' : '') + '>' +
    esc(o[1]) + '</option>').join('') + '</select>';

  document.getElementById('control-row').innerHTML =
    '<div class="ctl"><label for="f-sort">Sort by</label>' +
      '<select id="f-sort" onchange="onSelect(\'f-sort\',this.value)">' + sortSel + '</select></div>' +
    '<div class="ctl"><label>Severity</label><div class="chip-row">' + sevChips + '</div></div>' +
    '<div class="ctl"><label for="f-bi">Business impact</label>' +
      sel('f-bi', [['any', 'Any'], ['flagged', 'Flagged only'], ['unflagged', 'Not flagged']], state.bi) + '</div>' +
    '<div class="ctl"><label for="f-arr">ARR</label>' +
      sel('f-arr', [['any', 'Any'], ['high', '$1M and above'], ['mid', '$250K – $1M'],
                    ['low', 'Under $250K'], ['none', 'No ARR recorded']], state.arr) + '</div>' +
    '<div class="ctl"><label for="f-rep">Repetition</label>' +
      sel('f-rep', [['any', 'Any'], ['multi', '2+ customers'], ['three', '3+ customers'],
                    ['single', 'Single customer']], state.rep) + '</div>' +
    '<div class="ctl"><label for="f-tf">Timeframe</label>' +
      '<select id="f-tf" onchange="onSelect(\'f-tf\',this.value)">' + tfSel + '</select></div>' +
    '<div id="ctl-status"><span id="ctl-count"></span>' +
      (filtersActive() ? '<button class="link-btn" onclick="resetFilters()">Clear filters</button>' : '') +
    '</div>';

  const shown = activeRecords().length, scope = scopeRecords().length;
  document.getElementById('ctl-count').innerHTML = filtersActive()
    ? 'Showing <strong>' + shown + '</strong> of ' + scope + ' requests'
    : '<strong>' + scope + '</strong> requests in scope';
}

function renderTabs() {
  const doms = visibleDomains();
  const byId = {};
  doms.forEach(d => { byId[d.id] = d; });
  const pass = activeCaseSet();
  const hot = {};
  frame().clusters.forEach(c => {
    if (c.band.key === 'start_now' && c.cases.some(x => pass.has(x))) hot[c.domain_id] = true;
  });

  let html = '<div class="tab' + (state.page === 'exec' ? ' active' : '') +
             '" onclick="showPage(\'exec\')">Executive</div>';
  M.meta.domains.forEach(d => {
    const n = byId[d.id] ? byId[d.id].matchedCount : 0;
    html += '<div class="tab' + (state.page === d.id ? ' active' : '') + (n ? '' : ' empty') +
            '" onclick="showPage(\'' + d.id + '\')">' +
            (hot[d.id] ? '<span class="hot" title="Contains a Start-now theme"></span>' : '') +
            esc(d.name) + '<span class="n">' + n + '</span></div>';
  });
  document.getElementById('tab-row').innerHTML = html;
}

function onSelect(id, value) {
  if (id === 'f-sort') state.sort = value;
  if (id === 'f-bi') state.bi = value;
  if (id === 'f-arr') state.arr = value;
  if (id === 'f-rep') state.rep = value;
  if (id === 'f-tf') state.tf = value;
  refresh();
}

function toggleSev(key) {
  if (state.sev.has(key)) {
    // Never let the reader filter everything away — the last severity stays on.
    if (state.sev.size > 1) state.sev.delete(key);
  } else {
    state.sev.add(key);
  }
  refresh();
}

function resetFilters() {
  state.sev = new Set(['critical', 'high', 'medium', 'low', '']);
  state.bi = 'any'; state.arr = 'any'; state.rep = 'any';
  refresh();
}

function showPage(id) {
  state.page = id;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  const el = document.getElementById('page-' + id);
  if (el) el.classList.add('active');
  renderTabs();
  if (id === 'exec') renderExec(); else renderDomain(id);
  window.scrollTo({top: 0, behavior: 'smooth'});
}

/** Re-render everything that depends on state. Called on every control change. */
function refresh() {
  renderControls();
  renderTabs();
  state.drawn = new Set();
  if (state.page === 'exec') renderExec(); else renderDomain(state.page);
}

/* ── Copy-to-clipboard case numbers ──────────────────────────────────────── */

function copyCase(ev, caseNum) {
  ev.stopPropagation();          // must not toggle the row it sits in
  ev.preventDefault();
  const el = ev.currentTarget;
  const original = el.innerHTML;
  const done = () => {
    el.classList.add('copied');
    el.innerHTML = '&#10003; Copied';
    setTimeout(() => { el.classList.remove('copied'); el.innerHTML = original; }, 1500);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(caseNum).then(done, done);
  } else {
    const ta = document.createElement('textarea');
    ta.value = caseNum; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) { /* clipboard unavailable */ }
    document.body.removeChild(ta); done();
  }
}

const COPY_ICON = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2.5"><rect x="9" y="9" width="13" height="13" rx="2"/>' +
  '<path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';

function caseChip(caseNum) {
  return '<span class="case" title="Click to copy" onclick="copyCase(event,\'' +
         esc(caseNum) + '\')">' + COPY_ICON + esc(caseNum) + '</span>';
}

/* ── Case sub-table (used by every expander) ─────────────────────────────── */

function caseTable(caseNums, opts) {
  opts = opts || {};
  const recs = sortItems(caseNums.map(c => RECORDS[c]).filter(Boolean), true);
  const rows = recs.map(r => {
    const s = sc(r.case);
    const orig = r.original
      ? '<details class="orig"><summary>Original customer text</summary>' +
        '<div class="otext">' + esc(r.original) + '</div></details>'
      : '';
    return '<tr>' +
      '<td>' + caseChip(r.case) + (r.bi ? ' <span class="flagmark" title="Business impact flagged">&#9873;</span>' : '') + '</td>' +
      '<td>' + esc(r.subject) + '</td>' +
      '<td>' + esc(r.account) + '</td>' +
      '<td class="num">' + fmtArr(r.arr) + '</td>' +
      '<td class="mid">' + esc(r.opened || 'not recorded') + '</td>' +
      '<td class="mid"><span class="sev ' + sevClass(sevKey(r)) + '">' + esc(r.severity) + '</span></td>' +
      '<td class="mid"><span class="score-num">' + (s.score !== undefined ? s.score : '') + '</span>' +
        '<div><span class="band ' + s.band + '">' + esc(BAND_LABEL[s.band] || '') + '</span></div></td>' +
      '<td class="pm">' + esc(r.pm) + orig +
        (r.bi && r.bi_reason ? '<div style="margin-top:6px;font-size:11px;color:#991b1b">' +
          '<strong>Business impact stated:</strong> ' + esc(r.bi_reason) + '</div>' : '') +
      '</td></tr>';
  }).join('');

  return '<div class="tscroll"><table class="sub">' +
    '<colgroup><col style="width:9%"><col style="width:20%"><col style="width:12%">' +
    '<col style="width:7%"><col style="width:8%"><col style="width:7%"><col style="width:8%">' +
    '<col style="width:29%"></colgroup>' +
    '<thead><tr><th>Case #</th><th>Subject</th><th>Account</th><th>ARR</th><th>Opened</th>' +
    '<th>Severity</th><th>Priority</th><th>PM Decision Summary</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table></div>' +
    (opts.note ? '<div style="font-size:11px;color:var(--muted);margin-top:7px">' + opts.note + '</div>' : '');
}

/* ── Theme table (Executive decision queue and every domain tab) ─────────── */

function themeTable(clusters, idPrefix) {
  if (!clusters.length) {
    return '<div class="empty-state">No themes match the current filters.</div>';
  }
  const rows = clusters.map((c, i) => {
    const sev = '<span class="sev ' + sevClass(c.top_severity) + '">' +
                esc(sevLabel(c.top_severity)) + '</span>';
    const matchNote = (c.matchedCount < c.requests)
      ? '<div class="theme-sub">' + c.matchedCount + ' of ' + c.requests + ' match the filters</div>' : '';
    const rowId = idPrefix + '-' + i;
    return '<tr class="row-main" id="row-' + rowId + '" onclick="toggleTheme(\'' + rowId + '\')">' +
      '<td><div class="theme-name">' + esc(c.name) + '</div>' +
        '<div class="theme-sub">' + esc(c.domain) + '</div>' + matchNote + '</td>' +
      '<td><div class="score-cell"><span class="score-num">' + c.score + '</span>' +
        '<span class="band ' + c.band.key + '">' + esc(c.band.label) + '</span></div></td>' +
      '<td class="mid">' + sev + '</td>' +
      '<td class="num">' + fmtArr(c.arr) + '</td>' +
      '<td class="mid">' + c.customers + '</td>' +
      '<td class="mid">' + c.requests + (c.max_repeats > 1 ? ' <span class="theme-sub">(up to ' + c.max_repeats + ' from one)</span>' : '') + '</td>' +
      '<td class="mid">' + (c.flagged ? '<span class="pill flag">' + c.flagged + ' flagged</span>' : '&mdash;') + '</td>' +
      '<td>' + esc(c.rationale) + '</td></tr>' +
      '<tr class="expander" id="exp-' + rowId + '"><td colspan="8"><div class="expander-inner" ' +
        'id="expc-' + rowId + '"></div></td></tr>';
  }).join('');

  window._themeIndex = window._themeIndex || {};
  clusters.forEach((c, i) => { window._themeIndex[idPrefix + '-' + i] = c; });

  return '<div class="tscroll"><table class="grid">' +
    '<colgroup><col style="width:24%"><col style="width:9%"><col style="width:7%">' +
    '<col style="width:7%"><col style="width:7%"><col style="width:9%"><col style="width:8%">' +
    '<col style="width:29%"></colgroup>' +
    '<thead><tr><th>Theme</th><th>PI Priority</th><th>Top severity</th><th>ARR at stake</th>' +
    '<th>Customers</th><th>Requests</th><th>Impact flags</th><th>Why now, and what deferring means</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table></div>';
}

function toggleTheme(rowId) {
  const exp = document.getElementById('exp-' + rowId);
  const main = document.getElementById('row-' + rowId);
  if (!exp) return;
  const open = exp.classList.contains('open');
  if (open) {
    exp.classList.remove('open'); main.classList.remove('row-open');
    return;
  }
  const c = (window._themeIndex || {})[rowId];
  const host = document.getElementById('expc-' + rowId);
  if (c && host && !host.dataset.filled) {
    const note = c.matchedCount < c.requests
      ? 'Showing the ' + c.matchedCount + ' requests in this theme that match the current filters, of ' + c.requests + ' in total.'
      : '';
    host.innerHTML = caseTable(c.matched || c.cases, {note: note});
    host.dataset.filled = '1';
  }
  exp.classList.add('open'); main.classList.add('row-open');
}

/* ── Epic cards ──────────────────────────────────────────────────────────── */

function epicCards(clusters, prefix, showDomain) {
  if (!clusters.length) {
    return '<div class="empty-state">No themes match the current filters.</div>';
  }
  window._epicIndex = window._epicIndex || {};
  const cards = clusters.map((c, i) => {
    const key = prefix + '-' + i;
    window._epicIndex[key] = c;
    return '<div class="epic" id="epic-' + key + '" onclick="toggleEpic(\'' + key + '\',\'' + prefix + '\')">' +
      '<div class="rank">#' + (i + 1) +
        (showDomain ? '<span class="dom">' + esc(c.domain) + '</span>' : '') +
        '<span class="band ' + c.band.key + '">' + esc(c.band.label) + '</span></div>' +
      '<div class="etitle">' + esc(c.name) + '</div>' +
      '<div class="pills">' +
        '<span class="pill req">' + plural(c.requests, 'request') + '</span>' +
        '<span class="pill cust">' + plural(c.customers, 'customer') + '</span>' +
        '<span class="pill arr">' + fmtArr(c.arr) + '</span>' +
        '<span class="sev ' + sevClass(c.top_severity) + '">' + esc(sevLabel(c.top_severity)) + '</span>' +
        (c.flagged ? '<span class="pill flag">' + c.flagged + ' impact ' + (c.flagged === 1 ? 'flag' : 'flags') + '</span>' : '') +
      '</div>' +
      '<div class="why">' + esc(c.rationale) + '</div>' +
    '</div>';
  }).join('');
  return '<div class="epics">' + cards + '</div><div id="epic-detail-' + prefix + '"></div>';
}

function toggleEpic(key, prefix) {
  const host = document.getElementById('epic-detail-' + prefix);
  const card = document.getElementById('epic-' + key);
  const wasOpen = card && card.classList.contains('open');
  document.querySelectorAll('#epic-detail-' + prefix).forEach(h => { h.innerHTML = ''; });
  Array.prototype.forEach.call(document.querySelectorAll('.epic'), e => {
    if (e.id.indexOf('epic-' + prefix + '-') === 0) e.classList.remove('open');
  });
  if (wasOpen || !host) return;
  const c = (window._epicIndex || {})[key];
  if (!c) return;
  card.classList.add('open');
  host.innerHTML = '<div class="card" style="margin-top:12px;margin-bottom:0">' +
    '<div class="card-head"><div><div class="card-title">' + esc(c.name) + '</div>' +
    '<div class="card-note">' + esc(c.domain) + ' &middot; ' + esc(c.rationale) + '</div></div>' +
    '<button class="link-btn" onclick="document.getElementById(\'epic-detail-' + prefix +
      '\').innerHTML=\'\'">Close</button></div>' +
    caseTable(c.matched || c.cases, {}) + '</div>';
  host.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

/* ── Charts ──────────────────────────────────────────────────────────────── */

/** Decision matrix: breadth against priority, sized by ARR, coloured by band.
 *  This is the chart that replaced v1's "Portfolio Demand Snapshot". It answers
 *  a decision ("which of these do we commit to, and which is an account
 *  conversation") instead of describing volume, and clicking a bubble lists the
 *  underlying cases. */
function drawMatrix(elId, clusters, detailId) {
  const el = document.getElementById(elId);
  if (!el) return;
  const items = clusters.slice(0, 40);
  setChartNote(elId, items.length, clusters.length, 'themes');
  if (!items.length) { el.innerHTML = '<div class="empty-state">Nothing to plot.</div>'; return; }
  const maxArr = Math.max.apply(null, items.map(c => c.arr || 0)) || 1;
  const traces = [{
    type: 'scatter', mode: 'markers',
    x: items.map(c => c.customers),
    y: items.map(c => c.score),
    marker: {
      size: items.map(c => 12 + 26 * Math.sqrt((c.arr || 0) / maxArr)),
      color: items.map(c => BAND_COLOR[c.band.key]),
      opacity: 0.72,
      line: {color: '#ffffff', width: 1.5}
    },
    customdata: items.map(c => [c.name, c.domain, c.requests, fmtArr(c.arr), c.band.label]),
    hovertemplate: '<b>%{customdata[0]}</b><br>%{customdata[1]}<br>' +
      'PI Priority %{y} &middot; %{customdata[4]}<br>' +
      '%{x} customers &middot; %{customdata[2]} requests &middot; %{customdata[3]}<extra></extra>'
  }];
  const maxX = Math.max.apply(null, items.map(c => c.customers));
  const lay = layout({
    height: 400,
    margin: {t: 26, r: 24, b: 52, l: 52},
    xaxis: {title: {text: 'Customers asking (breadth)', font: {size: 10}},
            gridcolor: '#f3f4f6', zeroline: false, dtick: 1, range: [0.5, Math.max(2.5, maxX + 0.5)]},
    yaxis: {title: {text: 'PI Priority', font: {size: 10}},
            gridcolor: '#f3f4f6', zeroline: false, range: [0, 100]},
    shapes: [
      {type: 'line', x0: 1.5, x1: 1.5, y0: 0, y1: 100, line: {color: '#d1d5db', width: 1, dash: 'dot'}},
      {type: 'line', x0: 0.5, x1: Math.max(2.5, maxX + 0.5), y0: 54, y1: 54,
       line: {color: '#d1d5db', width: 1, dash: 'dot'}}
    ],
    annotations: [
      {x: 0.02, y: 0.98, xref: 'paper', yref: 'paper', text: 'Urgent, one customer<br>account conversation',
       showarrow: false, font: {size: 9, color: '#9ca3af'}, align: 'left'},
      {x: 0.98, y: 0.98, xref: 'paper', yref: 'paper', text: 'Broad and urgent<br>commit first',
       showarrow: false, font: {size: 9, color: '#1d4ed8'}, align: 'right'},
      {x: 0.98, y: 0.03, xref: 'paper', yref: 'paper', text: 'Broad, lower urgency<br>plan it',
       showarrow: false, font: {size: 9, color: '#9ca3af'}, align: 'right'},
      {x: 0.02, y: 0.03, xref: 'paper', yref: 'paper', text: 'Narrow and quiet<br>candidate to close',
       showarrow: false, font: {size: 9, color: '#9ca3af'}, align: 'left'}
    ]
  });
  Plotly.react(el, traces, lay, PLOTLY_CFG);
  el.removeAllListeners && el.removeAllListeners('plotly_click');
  el.on('plotly_click', ev => {
    const pt = ev.points && ev.points[0];
    if (!pt) return;
    const c = items[pt.pointIndex];
    const host = document.getElementById(detailId);
    if (!host || !c) return;
    host.innerHTML = '<div class="card" style="margin:12px 0 0"><div class="card-head">' +
      '<div><div class="card-title">' + esc(c.name) + '</div>' +
      '<div class="card-note">' + esc(c.domain) + ' &middot; ' + esc(c.rationale) + '</div></div>' +
      '<button class="link-btn" onclick="document.getElementById(\'' + detailId +
        '\').innerHTML=\'\'">Close</button></div>' + caseTable(c.matched || c.cases, {}) + '</div>';
    host.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  });
}

/** Where the work is, by domain: themes stacked by the decision each one needs.
 *
 *  This replaced a first attempt that ranked domains on PI Priority and coloured
 *  the bar by the domain's own band. Calibration killed that idea: at domain
 *  level, breadth and ARR both saturate (74 customers and $11.8M are already
 *  maximum on those scales), so twelve of sixteen domains scored above 56 and
 *  seven came out "Start now" — a ranking that discriminates nothing. Worse, it
 *  is a category error: a team commits to a theme, never to a domain.
 *
 *  Counting themes by the decision they need answers the real question — where
 *  does this PI's capacity have to go — in one unit, on one axis, stacked, so
 *  nothing can overlap. Domains stay ordered by PI Priority, which is what that
 *  score is genuinely good for. */
function drawDomainBands(elId, clusters, domains) {
  const el = document.getElementById(elId);
  if (!el) return;
  const perDomain = {};
  clusters.forEach(c => {
    const slot = perDomain[c.domain_id] ||
      (perDomain[c.domain_id] = {start_now: 0, plan: 0, backlog: 0, drop: 0});
    slot[c.band.key]++;
  });
  const eligible = domains.filter(d => perDomain[d.id]);
  const items = eligible.sort((a, b) => a.score - b.score).slice(-12);
  setChartNote(elId, items.length, eligible.length, 'domains');
  if (!items.length) { el.innerHTML = '<div class="empty-state">Nothing to plot.</div>'; return; }

  const wrapped = items.map(d => wrapLabel(d.domain, 22));
  const y = wrapped.map(w => w.html);
  const series = [['start_now', 'Start now'], ['plan', 'Plan'],
                  ['backlog', 'Keep in backlog'], ['drop', 'Candidate to close']];
  const traces = series.map(([key, label]) => ({
    type: 'bar', orientation: 'h', name: label,
    x: items.map(d => perDomain[d.id][key]),
    y: y,
    marker: {color: BAND_COLOR[key]},
    customdata: items.map(d => [plural(d.requests, 'request'),
                                plural(d.customers, 'customer'), fmtArr(d.arr), d.flagged]),
    hovertemplate: '<b>%{y}</b><br>%{x} themes &mdash; ' + label +
      '<br>Domain total: %{customdata[0]}, %{customdata[1]}, %{customdata[2]}' +
      '<br>%{customdata[3]} business-impact flags<extra></extra>'
  }));

  Plotly.react(el, traces, layout({
    barmode: 'stack',
    height: barHeight(wrapped, 320) + 30,
    margin: {t: 12, r: 24, b: 60, l: 8},
    showlegend: true,
    legend: {orientation: 'h', y: -0.16, x: 0.5, xanchor: 'center', font: {size: 10},
             traceorder: 'normal'},
    xaxis: {title: {text: 'Themes needing each decision', font: {size: 10}},
            gridcolor: '#f3f4f6', zeroline: false, automargin: true, dtick: 5},
    yaxis: {type: 'category', gridcolor: '#ffffff', zeroline: false, automargin: true,
            tickfont: {size: 11}}
  }), PLOTLY_CFG);
}

/** What is heating up: last 90 days against the 90 before, as one diverging
 *  series. Answers "where is new demand arriving" — the question v1's momentum
 *  bubble chart gestured at without ever making legible. */
function drawMomentum(elId, domains) {
  const el = document.getElementById(elId);
  if (!el) return;
  const items = domains.filter(d => d.recent_90 || d.prior_90)
    .map(d => Object.assign({}, d, {delta: d.recent_90 - d.prior_90}))
    .sort((a, b) => Math.abs(a.delta) - Math.abs(b.delta)).slice(-10);
  if (!items.length) {
    el.innerHTML = '<div class="empty-state">No requests were opened in the last 180 days ' +
                   'in this scope, so there is no momentum to show.</div>';
    return;
  }
  const wrapped = items.map(d => wrapLabel(d.domain, 22));
  Plotly.react(el, [{
    type: 'bar', orientation: 'h',
    x: items.map(d => d.delta),
    y: wrapped.map(w => w.html),
    marker: {color: items.map(d => d.delta > 0 ? '#2563eb' : (d.delta < 0 ? '#cbd5e1' : '#e5e7eb'))},
    text: items.map(d => (d.delta > 0 ? '+' : '') + d.delta),
    textposition: 'outside', textfont: {size: 10, color: '#6b7280'},
    cliponaxis: false,
    customdata: items.map(d => [d.recent_90, d.prior_90]),
    hovertemplate: '<b>%{y}</b><br>%{customdata[0]} in the last 90 days, ' +
                   '%{customdata[1]} in the 90 before<extra></extra>'
  }], layout({
    height: barHeight(wrapped, 260),
    margin: {t: 12, r: 60, b: 42, l: 8},
    xaxis: {title: {text: 'Change in requests opened', font: {size: 10}},
            gridcolor: '#f3f4f6', zeroline: true, zerolinecolor: '#d1d5db', automargin: true},
    yaxis: {type: 'category', gridcolor: '#ffffff', zeroline: false, automargin: true,
            tickfont: {size: 11}}
  }), PLOTLY_CFG);
}

/** Themes ranked inside one domain — bars coloured by the decision band, so the
 *  reader sees where the commit line falls without reading a number. */
function drawThemeRanking(elId, clusters) {
  const el = document.getElementById(elId);
  if (!el) return;
  const items = clusters.slice(0, 14).slice().reverse();
  setChartNote(elId, items.length, clusters.length, 'themes');
  if (!items.length) { el.innerHTML = '<div class="empty-state">Nothing to plot.</div>'; return; }
  const wrapped = items.map(c => wrapLabel(c.name, 34));
  Plotly.react(el, [{
    type: 'bar', orientation: 'h',
    x: items.map(c => c.score),
    y: wrapped.map(w => w.html),
    marker: {color: items.map(c => BAND_COLOR[c.band.key])},
    text: items.map(c => c.band.label),
    textposition: 'outside', textfont: {size: 10, color: '#6b7280'},
    cliponaxis: false,
    customdata: items.map(c => [c.requests, c.customers, fmtArr(c.arr)]),
    hovertemplate: '<b>%{y}</b><br>PI Priority %{x}<br>%{customdata[0]} requests &middot; ' +
                   '%{customdata[1]} customers &middot; %{customdata[2]}<extra></extra>'
  }], layout({
    height: barHeight(wrapped, 280),
    margin: {t: 12, r: 120, b: 42, l: 8},
    xaxis: {title: {text: 'PI Priority', font: {size: 10}}, gridcolor: '#f3f4f6',
            zeroline: false, range: [0, 100], automargin: true},
    yaxis: {type: 'category', gridcolor: '#ffffff', zeroline: false, automargin: true,
            tickfont: {size: 10.5}}
  }), PLOTLY_CFG);
}

/** ARR at stake per theme — distinct accounts, so this is exposure, not volume. */
function drawThemeArr(elId, clusters) {
  const el = document.getElementById(elId);
  if (!el) return;
  const items = clusters.slice().sort((a, b) => a.arr - b.arr).slice(-14);
  setChartNote(elId, items.length, clusters.length, 'themes by ARR');
  if (!items.length) { el.innerHTML = '<div class="empty-state">Nothing to plot.</div>'; return; }
  const wrapped = items.map(c => wrapLabel(c.name, 34));
  Plotly.react(el, [{
    type: 'bar', orientation: 'h',
    x: items.map(c => c.arr),
    y: wrapped.map(w => w.html),
    marker: {color: '#16a34a', opacity: 0.85},
    text: items.map(c => fmtArr(c.arr) + ' &middot; ' + plural(c.customers, 'customer')),
    textposition: 'outside', textfont: {size: 10, color: '#6b7280'},
    cliponaxis: false,
    hovertemplate: '<b>%{y}</b><br>%{text}<extra></extra>'
  }], layout({
    height: barHeight(wrapped, 280),
    margin: {t: 12, r: 150, b: 42, l: 8},
    xaxis: {title: {text: 'ARR at stake (each account counted once)', font: {size: 10}},
            gridcolor: '#f3f4f6', zeroline: false, automargin: true},
    yaxis: {type: 'category', gridcolor: '#ffffff', zeroline: false, automargin: true,
            tickfont: {size: 10.5}}
  }), PLOTLY_CFG);
}

/** Requests opened per month. Gap-filled in Python so a quiet month reads as a
 *  zero rather than being skipped and flattering the trend. */
function drawTrend(elId, monthly) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (!monthly || !monthly.length) {
    el.innerHTML = '<div class="empty-state">No dated requests in this scope.</div>';
    return;
  }
  Plotly.react(el, [{
    type: 'scatter', mode: 'lines+markers',
    x: monthly.map(m => m.month), y: monthly.map(m => m.count),
    fill: 'tozeroy', fillcolor: 'rgba(37,99,235,0.09)',
    line: {color: '#2563eb', width: 2}, marker: {size: 5, color: '#2563eb'},
    hovertemplate: '%{x}: <b>%{y}</b> opened<extra></extra>'
  }], layout({
    height: 280,
    margin: {t: 12, r: 20, b: 54, l: 46},
    xaxis: {gridcolor: '#f3f4f6', zeroline: false, tickangle: -35, automargin: true},
    yaxis: {title: {text: 'Requests opened', font: {size: 10}}, gridcolor: '#f3f4f6',
            zeroline: false, rangemode: 'tozero', automargin: true}
  }), PLOTLY_CFG);
}

/* ── Executive page ─────────────────────────────────────────────────────── */

function kpiTiles() {
  const recs = activeRecords();
  const perAccount = {};
  recs.forEach(r => {
    const k = r.account || ('__anon' + r.case);
    perAccount[k] = Math.max(perAccount[k] || 0, Number(r.arr || 0));
  });
  const arr = Object.keys(perAccount).reduce((s, k) => s + perAccount[k], 0);
  const flagged = recs.filter(r => r.bi).length;
  const critHigh = recs.filter(r => sevKey(r) === 'critical' || sevKey(r) === 'high').length;
  const clusters = visibleClusters(null);
  const startNow = clusters.filter(c => c.band.key === 'start_now');
  const drop = clusters.filter(c => c.band.key === 'drop');
  const dropReq = drop.reduce((s, c) => s + c.matchedCount, 0);

  const tiles = [
    ['accent', recs.length.toLocaleString(), 'Requests in scope',
     plural(clusters.length, 'theme') + ' after consolidation, from ' +
     plural(Object.keys(perAccount).length, 'customer')],
    ['green', fmtArr(arr), 'ARR at stake',
     'Each account counted once, however many requests it filed'],
    ['violet', startNow.length, 'Themes to start now',
     startNow.length ? plural(startNow.reduce((s, c) => s + c.matchedCount, 0), 'request') +
       ' &middot; ' + fmtArr(startNow.reduce((s, c) => s + c.arr, 0)) + ' behind them'
       : 'Nothing clears the commit threshold in this scope'],
    ['red', flagged, 'Business-impact flags',
     flagged ? 'Customer stated a consequence — answer each one individually'
             : 'No customer has asserted a business impact here'],
    ['red', critHigh, 'Critical or High severity',
     critHigh ? 'Severity set by support at intake' : 'Nothing above Medium in this scope'],
    ['slate', dropReq, 'Requests to consider closing',
     dropReq ? 'Across ' + plural(drop.length, 'theme') + ' with no repeat demand'
             : 'No theme falls below the keep threshold']
  ];
  return '<div class="kpi-grid">' + tiles.map(t =>
    '<div class="kpi ' + t[0] + '"><div class="k-label">' + t[2] + '</div>' +
    '<div class="k-value">' + t[1] + '</div><div class="k-note">' + t[3] + '</div></div>'
  ).join('') + '</div>';
}

function rankHelp() {
  const rows = M.meta.sort_fields.map(f =>
    '<tr><td><strong>' + esc(f.label) + '</strong></td><td>' + esc(f.help) + '</td></tr>').join('');
  return '<details class="rank-help"><summary>How PI Priority is calculated, and what each sort does</summary>' +
    '<div class="body"><p>PI Priority is a 0&ndash;100 score built from six signals, weighted: ' +
    'severity (22%), ARR (20%), the Salesforce business-impact flag (16%), repetition &mdash; how many ' +
    '<em>distinct</em> customers ask (22%), how recently the requests arrived (8%), and what the request ' +
    'is about, read from its subject and description (12%). Ten customers asking once is treated as a ' +
    'stronger product signal than one customer asking ten times, which is why breadth and depth are ' +
    'scored separately.</p>' +
    '<p>Thresholds are absolute, not relative: <strong>68+</strong> Start now, <strong>54&ndash;67</strong> ' +
    'Plan, <strong>38&ndash;53</strong> Keep in backlog, <strong>below 38</strong> Drop candidate. A quiet ' +
    'quarter is allowed to produce no Start-now themes.</p>' +
    '<p>Five facts carry a floor, because they need a decision rather than a default. ' +
    '<strong>Critical severity</strong> and <strong>a business impact the customer wrote down</strong> ' +
    'cannot rank below Plan. <strong>A business-impact flag</strong>, <strong>$1M or more of ARR at ' +
    'stake</strong>, or <strong>three or more customers asking</strong> cannot rank below Keep in backlog ' +
    '&mdash; whether to tell a customer of that size no is an account conversation, not a scoring ' +
    'outcome. Any row lifted this way says so at the end of its "why" line.</p>' +
    '<p>The timeframe selects the scoring window. The severity, business-impact, ARR and repetition ' +
    'controls are filters: they change what is listed, never what anything scores.</p>' +
    '<table>' + rows + '</table></div></details>';
}

function renderExec() {
  const f = frame();
  const clusters = visibleClusters(null);
  const domains = visibleDomains();
  const recs = activeRecords();
  const nar = f.narrative;

  const showAll = !!state.expandAll.exec;
  const queue = showAll ? clusters : clusters.slice(0, 12);

  const escalations = sortItems(recs.filter(r => r.bi), true);

  let html = '';

  html += '<div class="narrative"><h2>Executive summary</h2>' +
    nar.paragraphs.map(p => '<p>' + p + '</p>').join('') +
    '<div class="scope-note">' +
      (filtersActive()
        ? 'Filters are active: the lists, tables and charts below reflect them. This summary describes all ' +
          scopeRecords().length + ' requests in the selected timeframe.'
        : 'Written from all ' + scopeRecords().length + ' requests in the selected timeframe.') +
    '</div></div>';

  html += kpiTiles();

  // The decision queue — the centre of the page.
  html += '<div class="card"><div class="card-head"><div>' +
    '<div class="card-title">Decision queue &mdash; work down this list</div>' +
    '<div class="card-note">Every theme in scope, ranked by <strong>' +
      esc((M.meta.sort_fields.find(s => s.key === state.sort) || {}).label) +
      '</strong>. Click a row to see its cases, each with a written PM decision summary. ' +
      'Case numbers copy to the clipboard.</div></div>' +
    '<div>' + rankHelp() + '</div></div>' +
    themeTable(queue, 'exec') +
    (clusters.length > 12
      ? '<div style="margin-top:10px"><button class="link-btn" onclick="toggleExpandAll(\'exec\')">' +
        (showAll ? 'Show only the top 12 themes' : 'Show all ' + clusters.length + ' themes') +
        '</button></div>'
      : '') +
    '</div>';

  // Top themes as cards, for the walk-through at the start of the meeting.
  html += '<div class="card"><div class="card-head"><div>' +
    '<div class="card-title">Top 5 strategic epics</div>' +
    '<div class="card-note">The five highest-ranked themes portfolio-wide. Click one to open its ' +
    'cases.</div></div></div>' + epicCards(clusters.slice(0, 5), 'exec', true) + '</div>';

  // Charts — two, both actionable, neither a volume description.
  html += '<div class="two-col">' +
    '<div class="card"><div class="card-head"><div><div class="card-title">Where to act first</div>' +
      '<div class="card-note">Breadth against priority. Top right is build-once-satisfy-many; top left ' +
      'is urgent but single-customer, which is an account conversation. Bubble size is ARR at stake. ' +
      'Click a bubble for its cases.</div></div></div>' +
      '<div class="chart" id="chart-matrix"></div><div class="chart-note" id="chart-matrix-note"></div><div id="matrix-detail"></div></div>' +
    '<div class="card"><div class="card-head"><div><div class="card-title">Where this PI&rsquo;s capacity has to go</div>' +
      '<div class="card-note">Themes per domain, stacked by the decision each one needs. Domains are ' +
      'ordered by PI Priority, so the top row is where the strongest demand sits &mdash; and the width of ' +
      'the blue and violet segments is the work this PI is being asked to absorb.</div></div></div>' +
      '<div class="chart" id="chart-domains"></div><div class="chart-note" id="chart-domains-note"></div></div>' +
    '</div>';

  html += '<div class="two-col">' +
    '<div class="card"><div class="card-head"><div><div class="card-title">What is heating up</div>' +
      '<div class="card-note">Requests opened in the last 90 days against the 90 before. A rising ' +
      'domain is one to re-check against the roadmap before this PI closes.</div></div></div>' +
      '<div class="chart" id="chart-momentum"></div><div class="chart-note" id="chart-momentum-note"></div></div>' +
    '<div class="card"><div class="card-head"><div><div class="card-title">Demand arriving over time</div>' +
      '<div class="card-note">All requests in scope, by month opened.</div></div></div>' +
      '<div class="chart" id="chart-trend"></div><div class="chart-note" id="chart-trend-note"></div></div>' +
    '</div>';

  // Escalations: the requests where a human wrote down the consequence.
  html += '<div class="card"><div class="card-head"><div>' +
    '<div class="card-title">Business-impact escalations &mdash; answer these individually</div>' +
    '<div class="card-note">Requests where Salesforce carries a business-impact flag. These need a ' +
    'disposition per case, whether or not their theme gets committed.</div></div></div>' +
    (escalations.length
      ? caseTable(escalations.map(r => r.case), {})
      : '<div class="empty-state">No request in this scope carries a business-impact flag.</div>') +
    '</div>';

  html += '<div class="card"><div class="card-head"><div>' +
    '<div class="card-title">What these numbers include</div>' +
    '<div class="card-note">Read before arguing with a figure on this page.</div></div></div>' +
    '<ul class="dq">' + nar.data_quality.map(n => '<li>' + n + '</li>').join('') + '</ul></div>';

  document.getElementById('page-exec').innerHTML = html;

  drawMatrix('chart-matrix', clusters, 'matrix-detail');
  drawDomainBands('chart-domains', clusters, domains);
  drawMomentum('chart-momentum', domains);
  drawTrend('chart-trend', f.totals.monthly);
}

function toggleExpandAll(key) {
  state.expandAll[key] = !state.expandAll[key];
  if (key === 'exec') renderExec(); else renderDomain(key);
}

/* ── Domain page ─────────────────────────────────────────────────────────── */

function decideColumn(cls, title, note, clusters, prefix) {
  const items = clusters.slice(0, 6);
  const body = items.length
    ? '<ol>' + items.map(c =>
        '<li onclick="jumpToTheme(\'' + prefix + '\',\'' + esc(c.id) + '\')"><b>' + esc(c.name) +
        '</b> &mdash; ' + plural(c.matchedCount, 'request') + ', ' +
        plural(c.customers, 'customer') + ', ' + fmtArr(c.arr) + '</li>').join('') + '</ol>' +
      (clusters.length > 6 ? '<div class="cnt">and ' + (clusters.length - 6) + ' more below</div>' : '')
    : '<div class="none">Nothing in this band.</div>';
  return '<div class="decide-col ' + cls + '"><h3>' + title + '</h3>' +
    '<div class="cnt">' + plural(clusters.length, 'theme') + ' &middot; ' + note + '</div>' +
    body + '</div>';
}

function jumpToTheme(prefix, clusterId) {
  const idx = window._themeIndex || {};
  const key = Object.keys(idx).find(k => k.indexOf(prefix + '-') === 0 && idx[k].id === clusterId);
  if (!key) return;
  const row = document.getElementById('row-' + key);
  if (row) {
    row.scrollIntoView({behavior: 'smooth', block: 'center'});
    if (!document.getElementById('exp-' + key).classList.contains('open')) toggleTheme(key);
  }
}

function renderDomain(domainId) {
  const f = frame();
  const dom = f.domains.find(d => d.id === domainId);
  const host = document.getElementById('page-' + domainId);
  if (!host || !dom) return;

  const clusters = visibleClusters(domainId);
  const recs = activeRecords().filter(r => r.domain_id === domainId);

  if (!clusters.length) {
    host.innerHTML = '<div class="domain-head"><h2>' + esc(dom.domain) + '</h2><p>' +
      (dom.requests
        ? 'No ' + esc(dom.domain) + ' request matches the current filters. ' +
          dom.requests + ' are in scope for this timeframe &mdash; clear or widen the filters to see them.'
        : dom.narrative) + '</p></div>';
    return;
  }

  const startNow = clusters.filter(c => c.band.key === 'start_now');
  const plan = clusters.filter(c => c.band.key === 'plan');
  const drop = clusters.filter(c => c.band.key === 'drop');
  const showAll = !!state.expandAll[domainId];
  const table = showAll ? clusters : clusters.slice(0, 15);

  // Domains are ranked, never banded: see drawDomainBands for why a domain-level
  // commit band is meaningless. Rank is the honest way to say "this one matters".
  const active = f.domains.filter(d => d.requests).sort((a, b) => b.score - a.score);
  const at = active.findIndex(d => d.id === domainId);
  const rank = at >= 0 ? {at: at + 1, of: active.length} : null;

  let html = '';

  html += '<div class="domain-head"><h2>' + esc(dom.domain) + '</h2>' +
    '<div class="dstats">' +
      '<span><strong style="color:var(--accent)">' + dom.score + '</strong> PI Priority' +
        (rank ? ' <span style="color:var(--muted)">(rank ' + rank.at + ' of ' + rank.of +
          ' active domains)</span>' : '') + '</span>' +
      '<span><strong>' + recs.length + '</strong> requests' +
        (recs.length !== dom.requests ? ' of ' + dom.requests + ' in scope' : '') + '</span>' +
      '<span><strong style="color:var(--green)">' + fmtArr(dom.arr) + '</strong> ARR at stake</span>' +
      '<span><strong>' + dom.customers + '</strong> customers</span>' +
      '<span><strong>' + dom.cluster_count + '</strong> themes</span>' +
      (dom.flagged ? '<span><strong style="color:var(--red)">' + dom.flagged +
        '</strong> business-impact flags</span>' : '') +
    '</div><p>' + dom.narrative + '</p></div>';

  html += '<div class="three-col" style="margin-bottom:16px">' +
    decideColumn('start', 'Start now', 'commit this PI', startNow, domainId) +
    decideColumn('plan', 'Plan', 'size now, commit next PI', plan, domainId) +
    decideColumn('drop', 'Candidates to close', 'propose closing with the customer', drop, domainId) +
    '</div>';

  html += '<div class="card"><div class="card-head"><div>' +
    '<div class="card-title">Top strategic epics in ' + esc(dom.domain) + '</div>' +
    '<div class="card-note">Ranked by ' +
      esc((M.meta.sort_fields.find(s => s.key === state.sort) || {}).label) +
      '. Click a card to open its cases.</div></div></div>' +
    epicCards(clusters.slice(0, 5), domainId, false) + '</div>';

  html += '<div class="card"><div class="card-head"><div>' +
    '<div class="card-title">All ' + esc(dom.domain) + ' themes</div>' +
    '<div class="card-note">Click a row for its cases, each with a written PM decision summary and the ' +
    'original customer text.</div></div></div>' +
    themeTable(table, domainId) +
    (clusters.length > 15
      ? '<div style="margin-top:10px"><button class="link-btn" onclick="toggleExpandAll(\'' + domainId +
        '\')">' + (showAll ? 'Show only the top 15' : 'Show all ' + clusters.length + ' themes') +
        '</button></div>'
      : '') + '</div>';

  html += '<div class="two-col">' +
    '<div class="card"><div class="card-head"><div><div class="card-title">Themes ranked by PI Priority</div>' +
      '<div class="card-note">Colour is the decision band, so the commit line is visible without reading ' +
      'a number.</div></div></div><div class="chart" id="chart-rank-' + domainId + '"></div><div class="chart-note" id="chart-rank-' + domainId + '-note"></div></div>' +
    '<div class="card"><div class="card-head"><div><div class="card-title">ARR at stake by theme</div>' +
      '<div class="card-note">Each account counted once. Compare against the ranking on the left: a tall ' +
      'green bar with a low rank is a single large account, not a broad signal.</div></div></div>' +
      '<div class="chart" id="chart-arr-' + domainId + '"></div><div class="chart-note" id="chart-arr-' + domainId + '-note"></div></div>' +
    '</div>';

  html += '<div class="two-col">' +
    '<div class="card"><div class="card-head"><div><div class="card-title">Where to act first in ' +
      esc(dom.domain) + '</div><div class="card-note">Breadth against priority, sized by ARR. Click a ' +
      'bubble for its cases.</div></div></div><div class="chart" id="chart-matrix-' + domainId + '"></div><div class="chart-note" id="chart-matrix-' + domainId + '-note"></div>' +
      '<div id="matrix-detail-' + domainId + '"></div></div>' +
    '<div class="card"><div class="card-head"><div><div class="card-title">Requests opened over time</div>' +
      '<div class="card-note">Is this domain quiet because it is solved, or because nobody has asked ' +
      'lately?</div></div></div><div class="chart" id="chart-trend-' + domainId + '"></div><div class="chart-note" id="chart-trend-' + domainId + '-note"></div></div>' +
    '</div>';

  html += '<div class="two-col">' +
    '<div class="card"><div class="card-head"><div><div class="card-title">How to read this domain</div>' +
      '</div></div><div class="interp">' +
      dom.interpretation.map(i => '<div class="interp-item ' + i.tone + '">' +
        '<div class="interp-label">' + esc(i.label) + '</div>' +
        '<div class="interp-text">' + i.text + '</div></div>').join('') + '</div></div>' +
    '<div class="card"><div class="card-head"><div><div class="card-title">Recommended actions</div>' +
      '<div class="card-note">Written from this domain\'s data. Each names the theme or case it applies ' +
      'to.</div></div></div><div class="actions">' +
      dom.actions.map((a, i) => '<div class="action"><div class="n">' + (i + 1) + '</div>' +
        '<div class="t">' + a + '</div></div>').join('') + '</div></div>' +
    '</div>';

  host.innerHTML = html;

  drawThemeRanking('chart-rank-' + domainId, clusters);
  drawThemeArr('chart-arr-' + domainId, clusters);
  drawMatrix('chart-matrix-' + domainId, clusters, 'matrix-detail-' + domainId);
  drawTrend('chart-trend-' + domainId, dom.monthly);
}

/* ── Boot ────────────────────────────────────────────────────────────────── */

function init() {
  document.getElementById('headline').innerHTML = esc(frame().narrative.headline);
  renderControls();
  renderTabs();
  renderExec();
  window.addEventListener('resize', () => {
    document.querySelectorAll('.chart').forEach(el => {
      if (el.data) Plotly.Plots.resize(el);
    });
  });
}
document.addEventListener('DOMContentLoaded', init);
"""


SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PI Planning &mdash; RFE Strategic Demand Report</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.26.0/plotly.min.js"></script>
<style>/*__CSS__*/</style>
</head>
<body>
<div id="top-bar">
  <div id="title-row">
    <h1>PI Planning &mdash; RFE Strategic Demand Report
      <span class="sub">__SUBTITLE__</span></h1>
    <div id="headline"></div>
  </div>
  <div id="control-row"></div>
  <div id="tab-row"></div>
</div>

__PAGES__

<footer>Generated __GENERATED__ &middot; ranked by PI Priority (severity, ARR, business impact,
repetition, recency and request subject) &middot; PM decision summaries written during generation</footer>

<script>const PAYLOAD = "__PAYLOAD__";</script>
<script>/*__JS__*/</script>
</body>
</html>"""


def render(model: Dict[str, Any]) -> str:
    """Assemble the standalone report page from the scored model."""
    meta = model["meta"]
    subtitle = f"{meta['record_count']:,} requests"
    if meta["date_range"]:
        subtitle += f" &middot; {meta['date_range']}"

    pages = ['<div class="page active" id="page-exec"></div>']
    for d in meta["domains"]:
        pages.append(f'<div class="page" id="page-{d["id"]}"></div>')

    payload = json.dumps(model, ensure_ascii=False).replace("</script", r"<\/script")

    html = (SHELL
            .replace("/*__CSS__*/", CSS)
            .replace("/*__JS__*/", JS)
            .replace("__SUBTITLE__", subtitle)
            .replace("__GENERATED__", meta["generated"])
            .replace("__PAGES__", "\n".join(pages)))
    # Payload last: the JSON is data and must not be scanned for placeholders.
    return html.replace('"__PAYLOAD__"', payload)
