"""
Shared report UI — the decision bands, their badges, and the tooltip engine.

WHY THIS MODULE EXISTS
----------------------
The PI Planning report and the Weekly Analysis report are read by the same
people, often in the same week, about the same backlog. When the PI report
classifies a theme as "Keep in backlog" and the weekly report says something
different about the same request, the reports stop being trustworthy — so both
pages must classify on the same dimensions, with the same thresholds, the same
floors and the same wording.

"The same" is only reliable if it is the same code. This module holds the parts
that must not drift:

  * ``BAND_CSS``  — badge styling (shape + colour), the key strip, the tooltip
  * ``BAND_JS``   — band metadata and explanations, ``bandBadge()``,
                    ``bandKey()``, ``initTips()``
  * ``FILTER_CSS``/``FILTER_JS`` — the control bar and the filter/sort engine
    that drives a server-rendered page from ``data-`` attributes

The scoring behind the badges is shared the same way: both reports call
``scoring.priority``. Nothing here knows about either report's layout.
"""

from __future__ import annotations

# ── Decision-band badges, key strip and tooltip ─────────────────────────────
# Injected into both reports' <style> and <script> blocks verbatim.

BAND_CSS = """\
/* Decision badges carry a shape as well as a colour, so the four bands stay
   distinguishable in greyscale, in print, and to a colour-blind reader — and
   every one of them explains itself on hover. */
.band{display:inline-flex;align-items:center;gap:4px;font-size:10px;font-weight:700;
      padding:2px 9px 2px 7px;border-radius:10px;color:#fff;white-space:nowrap;cursor:help;
      border:1px solid transparent}
.band .mark{font-size:9px;line-height:1}
.band.start_now{background:var(--band-start)}
.band.plan{background:var(--band-plan)}
.band.backlog{background:#eef1f6;color:#3f4a5a;border-color:#cbd3e0}
.band.drop{background:#fff;color:#6b7280;border-color:#d7dce5;border-style:dashed}
.band:hover{filter:brightness(1.06)}
.band.backlog:hover,.band.drop:hover{filter:none;border-color:var(--accent);color:var(--accent-dark)}

/* Key strip: the four bands, spelled out, above the first thing that uses them. */
.band-key{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px;
          padding:9px 12px;background:var(--card);border:1px solid var(--border);
          border-radius:9px;box-shadow:var(--shadow)}
.band-key .kl{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
              color:var(--muted);margin-right:2px}
.band-key .item{display:flex;align-items:center;gap:6px;font-size:11px;color:#4b5563}
.band-key .item .what{color:var(--muted)}

#tip{position:absolute;z-index:900;max-width:330px;background:#111827;color:#f9fafb;
     font-size:11.5px;line-height:1.55;padding:9px 11px;border-radius:7px;
     box-shadow:0 6px 20px rgba(17,24,39,.22);pointer-events:none;opacity:0;
     transition:opacity .12s;display:none}
#tip.on{opacity:1;display:block}
#tip b{color:#fff}
[data-tip]{cursor:help}
"""

BAND_JS = """\
/* The decision bands, with the shape that identifies each one and the sentence
   it shows on hover. The explanations name the thresholds AND the floors,
   because "why is this Critical single-customer request in Plan?" is the first
   question a reader asks of the badge. */
const BAND_META = {
  start_now: {mark: '\u25b6', label: 'Start now', action: 'Commit to this PI',
    tip: 'Score 68 or above. Carried by more than one signal at once \u2014 severity, ARR, ' +
         'repeat demand across customers, or a business impact the customer stated. ' +
         'These are the themes to write epics for in this meeting.'},
  plan: {mark: '\u25c6', label: 'Plan', action: 'Size now, commit next PI',
    tip: 'Score 54 to 67 \u2014 or lifted here because severity is Critical, or because the ' +
         'customer wrote down the business consequence. Real signal, not yet enough to ' +
         'displace the commit list. Size it now so the next PI opens with it understood.'},
  backlog: {mark: '\u25a0', label: 'Keep in backlog', action: 'Revisit next cycle',
    tip: 'Score 38 to 53 \u2014 or lifted here because a customer flagged business impact, ' +
         'because 1M dollars or more of ARR sits behind it, or because a second customer ' +
         'has asked. Worth keeping and re-reading next cycle; not worth capacity now.'},
  drop: {mark: '\u25cb', label: 'Drop candidate', action: 'Propose closing with the customer',
    tip: 'Below 38: one customer, no business-impact flag, limited severity and limited ARR ' +
         'behind it. A proposal to close with the requesting customer \u2014 not an ' +
         'instruction, and never applied to anything the floors protect.'}
};

/** A decision badge: shape, label, and its explanation on hover. */
function bandBadge(key) {
  const m = BAND_META[key];
  if (!m) return '';
  return '<span class="band ' + key + '" data-tip="<b>' + m.label + ' \u2014 ' + m.action +
         '</b><br>' + m.tip + '">' +
         '<span class="mark">' + m.mark + '</span>' + m.label + '</span>';
}

/** The key strip shown above the first thing on a page that uses the bands. */
function bandKey() {
  return '<div class="band-key"><span class="kl">Decision bands</span>' +
    ['start_now', 'plan', 'backlog', 'drop'].map(k =>
      '<span class="item">' + bandBadge(k) +
      '<span class="what">' + BAND_META[k].action + '</span></span>').join('') +
    '</div>';
}

/* One floating tooltip, appended to the body, so an explanation is never clipped
   by a table's horizontal scroll container the way a CSS ::after tooltip is. */
function initTips() {
  let el = document.getElementById('tip');
  if (!el) {
    el = document.createElement('div');
    el.id = 'tip';
    document.body.appendChild(el);
  }
  const show = ev => {
    const host = ev.target.closest && ev.target.closest('[data-tip]');
    if (!host) return;
    el.innerHTML = host.getAttribute('data-tip');
    el.classList.add('on');
    const r = host.getBoundingClientRect();
    const w = el.offsetWidth, h = el.offsetHeight;
    let left = r.left + window.scrollX + r.width / 2 - w / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
    let top = r.top + window.scrollY - h - 9;
    if (top < window.scrollY + 4) top = r.bottom + window.scrollY + 9;   // flip under
    el.style.left = left + 'px';
    el.style.top = top + 'px';
  };
  const hide = ev => {
    if (ev.target.closest && ev.target.closest('[data-tip]')) el.classList.remove('on');
  };
  document.addEventListener('mouseover', show);
  document.addEventListener('mouseout', hide);
  document.addEventListener('click', () => el.classList.remove('on'));
}

"""


# ── The control bar, and a filter/sort engine for a server-rendered page ────
#
# The PI Planning report renders its rows from an embedded payload, so it can
# re-render on every control change. The Weekly Analysis report renders its
# cards in Python and ships them as HTML — rewriting it to match would be a
# large change for no reader-visible gain. So the same five controls drive a
# different mechanism here: every card carries `data-` attributes, and this
# engine shows, hides and reorders the DOM it is given.
#
# What matters is that the SEMANTICS are identical to the PI report's:
#   * the five dimensions are the same, with the same option boundaries;
#   * filters narrow what is listed and never change a score;
#   * a container disappears only when nothing inside it matches;
#   * sorting uses the same ordering rules as scoring.priority.sort_key.

FILTER_CSS = """\
#ctl-bar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:9px 16px;
         background:#f4f6fa;border-top:1px solid #e1e5ec;border-bottom:1px solid #e1e5ec}
#ctl-bar .ctl{display:flex;align-items:center;gap:6px;min-width:0}
#ctl-bar label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
               color:#6b7280;white-space:nowrap}
#ctl-bar select{font-family:inherit;font-size:12px;color:#111827;background:#fff;
                border:1px solid #e1e5ec;border-radius:6px;padding:5px 8px;cursor:pointer;
                max-width:220px}
#ctl-bar select:hover{border-color:#2563eb}
#ctl-bar select:focus{outline:2px solid #bfdbfe;outline-offset:1px}
#ctl-bar .chip-row{display:flex;gap:4px}
#ctl-bar .fchip{font-size:11px;font-weight:600;padding:4px 9px;border-radius:14px;
                border:1px solid #e1e5ec;background:#fff;color:#6b7280;cursor:pointer;
                user-select:none;white-space:nowrap}
#ctl-bar .fchip:hover{border-color:#2563eb;color:#2563eb}
#ctl-bar .fchip.on{color:#fff;border-color:transparent}
#ctl-bar .fchip.on.s-critical{background:#991b1b}
#ctl-bar .fchip.on.s-high{background:#dc2626}
#ctl-bar .fchip.on.s-medium{background:#d97706}
#ctl-bar .fchip.on.s-low{background:#16a34a}
#ctl-bar .fchip.on.s-unset{background:#64748b}
#ctl-status{margin-left:auto;display:flex;align-items:center;gap:10px;font-size:11px;color:#6b7280}
#ctl-status strong{color:#111827}
#ctl-bar .linkish{background:none;border:none;color:#2563eb;font:inherit;font-size:11px;
                  cursor:pointer;text-decoration:underline;padding:0}
.filtered-out{display:none !important}
.no-match-note{padding:22px 14px;text-align:center;color:#6b7280;font-size:12px;
               background:#f4f6fa;border:1px dashed #d1d5db;border-radius:9px;margin:10px 0}
"""

# `rowSel` is the per-request card; `groupSel` the container that holds them.
FILTER_JS = r"""
/* Filter/sort engine for a server-rendered report.
   Reads these attributes, written by the generator onto each request card:
     data-sev     severity key ('critical'|'high'|'medium'|'low'|'')
     data-bi      '1' when Salesforce carries a business-impact flag
     data-arr     account ARR, a plain number
     data-cust    distinct customers asking for this card's theme
     data-days    age in days, or '' when the date was unreadable
     data-score   PI Priority 0-100
   and onto each group (theme) container, aggregated the same way. */
const FILTER_STATE = {
  sort: 'priority',
  sev: new Set(['critical', 'high', 'medium', 'low', '']),
  bi: 'any', arr: 'any', rep: 'any', tf: 'all'
};

const TF_DAYS = {all: null, '12': 365, '6': 182, '3': 91};

function _num(el, name, dflt) {
  const v = el.getAttribute(name);
  if (v === null || v === '') return dflt;
  const n = Number(v);
  return isNaN(n) ? dflt : n;
}

/** Does one request card pass the current filters? */
function cardPasses(el) {
  const st = FILTER_STATE;
  if (!st.sev.has(el.getAttribute('data-sev') || '')) return false;
  const flagged = el.getAttribute('data-bi') === '1';
  if (st.bi === 'flagged' && !flagged) return false;
  if (st.bi === 'unflagged' && flagged) return false;
  if (st.arr !== 'any') {
    const a = _num(el, 'data-arr', 0);
    if (st.arr === 'high' && a < 1e6) return false;
    if (st.arr === 'mid' && (a < 250000 || a >= 1e6)) return false;
    if (st.arr === 'low' && (a <= 0 || a >= 250000)) return false;
    if (st.arr === 'none' && a > 0) return false;
  }
  if (st.rep !== 'any') {
    const c = _num(el, 'data-cust', 1);
    if (st.rep === 'multi' && c < 2) return false;
    if (st.rep === 'three' && c < 3) return false;
    if (st.rep === 'single' && c > 1) return false;
  }
  const days = TF_DAYS[st.tf];
  if (days !== null) {
    const d = el.getAttribute('data-days');
    // A card with no readable date belongs to All time only — treating it as
    // recent would quietly inflate every timeframe below it.
    if (d === null || d === '') return false;
    if (Number(d) > days) return false;
  }
  return true;
}

/** Sort comparator mirroring scoring.priority.sort_key, read off the DOM. */
function cardOrder(kind) {
  const SEV = {critical: 4, high: 3, medium: 2, low: 1, '': 0};
  return (a, b) => {
    const pick = el => {
      const score = _num(el, 'data-score', 0);
      switch (kind) {
        case 'severity':   return [SEV[el.getAttribute('data-sev') || ''] || 0, score];
        case 'arr':        return [_num(el, 'data-arr', 0), score];
        case 'impact':     return [el.getAttribute('data-bi') === '1' ? 1 : 0, score];
        case 'repetition': return [_num(el, 'data-cust', 0), _num(el, 'data-reqs', 0), score];
        case 'recency':    return [-_num(el, 'data-days', 1e6), score];
        case 'oldest':     return [_num(el, 'data-days', -1), score];
        default:           return [score, _num(el, 'data-arr', 0), _num(el, 'data-cust', 0)];
      }
    };
    const va = pick(a), vb = pick(b);
    for (let i = 0; i < Math.max(va.length, vb.length); i++) {
      const d = (vb[i] || 0) - (va[i] || 0);
      if (d) return d;
    }
    return 0;
  };
}

/** Apply filters and sort order to every group on the page. */
function applyFilters(cfg) {
  if (!cfg) return;
  const groups = Array.from(document.querySelectorAll(cfg.groupSel));
  let shown = 0, total = 0;

  groups.forEach(group => {
    const cards = Array.from(group.querySelectorAll(cfg.rowSel));
    let any = false;
    cards.forEach(card => {
      total++;
      const ok = cardPasses(card);
      card.classList.toggle('filtered-out', !ok);
      if (ok) { any = true; shown++; }
    });
    group.classList.toggle('filtered-out', !any);
    const parent = cards.length ? cards[0].parentElement : null;
    if (parent) {
      cards.filter(c => !c.classList.contains('filtered-out'))
           .sort(cardOrder(FILTER_STATE.sort))
           .forEach(c => parent.appendChild(c));
    }
  });

  // Order the groups themselves within whatever contains them.
  const bySection = new Map();
  groups.filter(g => !g.classList.contains('filtered-out')).forEach(g => {
    const p = g.parentElement;
    if (!bySection.has(p)) bySection.set(p, []);
    bySection.get(p).push(g);
  });
  bySection.forEach((gs, parent) => {
    gs.sort(cardOrder(FILTER_STATE.sort)).forEach(g => parent.appendChild(g));
  });

  // Tell each section whether anything is left in it.
  document.querySelectorAll(cfg.sectionSel).forEach(sec => {
    const visible = sec.querySelectorAll(cfg.groupSel + ':not(.filtered-out)').length;
    let note = sec.querySelector('.no-match-note');
    if (!visible) {
      if (!note) {
        note = document.createElement('div');
        note.className = 'no-match-note';
        note.textContent = 'No request in this section matches the current filters. '
          + 'Clear or widen them to see what is here.';
        sec.appendChild(note);
      }
      note.classList.remove('filtered-out');
    } else if (note) {
      note.classList.add('filtered-out');
    }
  });

  const status = document.getElementById('ctl-count');
  if (status) {
    status.innerHTML = (shown === total)
      ? '<strong>' + total + '</strong> requests in scope'
      : 'Showing <strong>' + shown + '</strong> of ' + total + ' requests';
  }
  const clear = document.getElementById('ctl-clear');
  if (clear) clear.style.display = filtersAreActive() ? '' : 'none';
}

function filtersAreActive() {
  const st = FILTER_STATE;
  return st.sev.size !== 5 || st.bi !== 'any' || st.arr !== 'any'
      || st.rep !== 'any' || st.tf !== 'all';
}

/** Build the control bar. `cfg` carries this report's selectors. */
function renderControlBar(hostId, cfg) {
  const host = document.getElementById(hostId);
  if (!host) return;
  const sel = (id, opts, cur) =>
    '<select id="' + id + '" onchange="onFilterChange(&quot;' + id + '&quot;, this.value)">' +
    opts.map(o => '<option value="' + o[0] + '"' + (o[0] === cur ? ' selected' : '') + '>' +
      o[1] + '</option>').join('') + '</select>';

  const sevChips = [['critical', 'Critical'], ['high', 'High'], ['medium', 'Medium'],
                    ['low', 'Low'], ['', 'Unset']].map(function (kv) {
    return '<span class="fchip s-' + (kv[0] || 'unset') + ' on" data-sev-key="' + kv[0] +
      '" onclick="onSevToggle(&quot;' + kv[0] + '&quot;)">' + kv[1] + '</span>';
  }).join('');

  host.innerHTML =
    '<div class="ctl"><label for="w-sort">Sort by</label>' +
      sel('w-sort', [['priority', 'PI Priority'], ['severity', 'Severity'],
                     ['arr', 'ARR at stake'], ['impact', 'Business impact'],
                     ['repetition', 'Repetition'], ['recency', 'Most recent'],
                     ['oldest', 'Oldest']], 'priority') + '</div>' +
    '<div class="ctl"><label>Severity</label><div class="chip-row">' + sevChips + '</div></div>' +
    '<div class="ctl"><label for="w-bi">Business impact</label>' +
      sel('w-bi', [['any', 'Any'], ['flagged', 'Flagged only'],
                   ['unflagged', 'Not flagged']], 'any') + '</div>' +
    '<div class="ctl"><label for="w-arr">ARR</label>' +
      sel('w-arr', [['any', 'Any'], ['high', '$1M and above'], ['mid', '$250K &ndash; $1M'],
                    ['low', 'Under $250K'], ['none', 'No ARR recorded']], 'any') + '</div>' +
    '<div class="ctl"><label for="w-rep">Repetition</label>' +
      sel('w-rep', [['any', 'Any'], ['multi', '2+ customers'], ['three', '3+ customers'],
                    ['single', 'Single customer']], 'any') + '</div>' +
    '<div class="ctl"><label for="w-tf">Timeframe</label>' +
      sel('w-tf', [['all', 'All time'], ['12', 'Last 12 months'],
                   ['6', 'Last 6 months'], ['3', 'Last 3 months']], 'all') + '</div>' +
    '<div id="ctl-status"><span id="ctl-count"></span>' +
      '<button class="linkish" id="ctl-clear" style="display:none" ' +
      'onclick="clearFilters()">Clear filters</button></div>';

  window._filterCfg = cfg;
  applyFilters(cfg);
}

function onFilterChange(id, value) {
  const map = {'w-sort': 'sort', 'w-bi': 'bi', 'w-arr': 'arr', 'w-rep': 'rep', 'w-tf': 'tf'};
  if (map[id]) FILTER_STATE[map[id]] = value;
  applyFilters(window._filterCfg);
}

function onSevToggle(key) {
  const st = FILTER_STATE;
  if (st.sev.has(key)) {
    if (st.sev.size > 1) st.sev.delete(key);   // never filter everything away
  } else {
    st.sev.add(key);
  }
  document.querySelectorAll('#ctl-bar .fchip').forEach(c => {
    c.classList.toggle('on', st.sev.has(c.getAttribute('data-sev-key')));
  });
  applyFilters(window._filterCfg);
}

function clearFilters() {
  FILTER_STATE.sev = new Set(['critical', 'high', 'medium', 'low', '']);
  FILTER_STATE.bi = 'any'; FILTER_STATE.arr = 'any';
  FILTER_STATE.rep = 'any'; FILTER_STATE.tf = 'all';
  ['w-bi', 'w-arr', 'w-rep', 'w-tf'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = (id === 'w-tf') ? 'all' : 'any';
  });
  document.querySelectorAll('#ctl-bar .fchip').forEach(c => c.classList.add('on'));
  applyFilters(window._filterCfg);
}
"""
