"""
PI Priority scoring — the ranking engine behind the PI Planning report (v2).

WHY THIS EXISTS
---------------
The v1 report ranked everything by a single expression, `arr + count * 80000`.
That number cannot answer the question a PI Planning meeting actually asks —
"which of these do we start building, and which stay in the backlog?" — because
it is blind to how badly the customer is hurting (Severity), to whether anyone
asserted a business consequence (the Salesforce *Business Impact* flag), to how
many *different* customers are asking for the same thing (repetition), and to
what the request is actually about (a detection blind spot and a tooltip colour
scored identically).

This module replaces it with one explainable score, computed the same way at all
three levels the report ranks — individual RFE, request cluster (Epic), and
product domain — so a domain's rank is defensible in the same language as an
RFE's rank.

THE MODEL
---------
Six normalised signals (each 0.0-1.0), combined with fixed weights:

    signal          weight   source
    ------------------------------------------------------------------
    severity        0.22     Salesforce `Severity` (Critical/High/Med/Low)
    arr             0.20     Salesforce `Account's ARR`, sqrt-scaled
    business_impact 0.16     Salesforce `Business Impact` flag + reason text
    repetition      0.22     computed: how many DISTINCT customers ask,
                             plus how insistently one customer repeats
    recency         0.08     opened-date decay (a 2-year-old ask is colder)
    subject_signal  0.12     computed: what the request is ABOUT, from
                             keyword families over subject/description
    ------------------------------------------------------------------
                    1.00  ->  scaled to 0..100

The weights encode a deliberate stance: **customer breadth (repetition) is
worth as much as severity, and slightly more than ARR.** Ten customers asking
for one capability is a product signal; one large account asking ten times is an
account signal. The repetition sub-model separates those two cases on purpose
(`breadth` vs `depth`) instead of counting rows.

DESIGN RULES
------------
1. **Absolute, not percentile.** A score means the same thing in every report,
   so two PI cycles are comparable and "nothing is urgent this quarter" is a
   result the model is allowed to produce. Percentile banding would always
   nominate a top 10% even when the backlog is quiet.
2. **Every score carries its reasons.** `score_rfe` returns the component
   breakdown and a human "why" string. Nothing in the report shows a rank the
   reader cannot interrogate — and no formula is ever printed on a leadership
   page.
3. **Deterministic and offline.** No model calls, no network, no API key: the
   same upload always produces the same ranking, which is a hard requirement for
   a meeting where people re-open the report and expect it to still say what it
   said an hour ago.
4. **ARR is never double-counted.** At cluster and domain level, ARR is summed
   over DISTINCT ACCOUNTS. v1 summed the account's ARR once per RFE, which
   inflated the portfolio total from the true $20.0M to $64.1M on the reference
   dataset (a 3.2x overstatement) because a 13-request account counted 13 times.

BANDS
-----
The score maps to the four decisions a PI Planning meeting can actually take:

    >= 68  START NOW      commit to this PI
    >= 54  PLAN           size it now, commit next PI
    >= 38  BACKLOG        keep, revisit next cycle
    <  38  DROP CANDIDATE propose closing with the customer

Thresholds were calibrated against the reference export (331 RFEs, Jan-2025 to
Aug-2026) so that START NOW stays a shortlist a team can genuinely commit to in
one PI rather than a wish list. See `docs/PI_PLANNING_V2_DESIGN.md`.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

# ── Weights ──────────────────────────────────────────────────────────────────
# Must sum to 1.0 — `_composite` asserts it, so a future edit cannot silently
# change the scale of every score in the report.
WEIGHTS: Dict[str, float] = {
    "severity":        0.22,
    "arr":             0.20,
    "business_impact": 0.16,
    "repetition":      0.22,
    "recency":         0.08,
    "subject_signal":  0.12,
}

# ── Severity ─────────────────────────────────────────────────────────────────
# Critical exists in the data (4 cases in the reference export) and v1 had no
# handling for it at all — it fell through to the empty-string default and
# rendered with no colour. Blank severity is treated as slightly below Medium:
# unset usually means "nobody triaged it", not "it is fine".
SEVERITY_WEIGHT: Dict[str, float] = {
    "critical": 1.00,
    "high":     0.75,
    "medium":   0.45,
    "low":      0.20,
    "":         0.30,
}
SEVERITY_RANK: Dict[str, int] = {"critical": 4, "high": 3, "medium": 2, "low": 1, "": 0}

# ARR reference point for sqrt scaling. Fixed (not dataset-relative) so scores
# stay comparable between uploads; sqrt rather than log because log10 compresses
# so hard that a $250K account scores 0.86 of a $2M one, which is not how the
# business reads those two numbers. sqrt gives $250K -> 0.35, $1M -> 0.71.
ARR_REFERENCE = 2_000_000.0

# Recency half-life. A request keeps full weight for two quarters, then decays;
# at ~2 years old it retains about a fifth. Deliberately the smallest weight in
# the model: age is a reason to *decide*, not a reason to skip.
RECENCY_FULL_DAYS = 120
RECENCY_HALF_LIFE_DAYS = 300

# ── Subject signal: what the request is ABOUT ────────────────────────────────
# "Common sense" made explicit and auditable. Families are ordered by how much
# they should move a PI decision. The strongest matched family sets the base
# score (families do not add up — a request is not twice as urgent for being
# describable two ways); breadth across families adds a small bonus, and the
# nice-to-have family applies a penalty, so "change the icon colour" cannot ride
# a large ARR into the commit list.
SUBJECT_SIGNALS: List[tuple] = [
    ("security_gap", 1.00,
     r"bypass|evasion|evade|false negative|not detected|undetected|blind spot|"
     r"missed detection|vulnerab|exploit|\bcve\b|zero.?day|ransomware|"
     r"privilege escalation|tamper|unprotected|no protection",
     "detection or protection gap"),
    ("compliance", 0.92,
     r"compliance|complian|audit log|auditing|\bgdpr\b|\bsoc ?2\b|iso ?27001|"
     r"\bhipaa\b|\bpci\b|retention polic|regulat|legal requirement|certification|"
     r"data residency|sovereignty",
     "compliance or audit obligation"),
    ("data_loss", 0.90,
     r"data loss|lost log|missing log|log gap|logs are not|data integrity|"
     r"corrupt|indexing issue|not indexed|data missing|incomplete data",
     "data loss or integrity risk"),
    ("stability", 0.86,
     r"crash|memory leak|high cpu|cpu usage|performance degrad|slowness|freeze|"
     r"\bbsod\b|blue screen|downtime|production down|outage|timeout|\bhangs?\b",
     "stability or performance risk"),
    ("deal_risk", 0.74,
     r"deal breaker|deal blocker|blocker|show ?stopper|renewal|churn|escalat|"
     r"\bpoc\b|proof of concept|\btenders?\b|\brfp\b|competitor|losing the|at risk of",
     "revenue or renewal risk"),
    ("scale_mssp", 0.62,
     r"\bmssp\b|\bmsp\b|multi.?tenant|multi.?site|cross.?site|global level|"
     r"centraliz|centralis|tenant level|partner level|bulk|at scale|"
     r"per.?site|for every site|all sites|all tenants",
     "MSSP / multi-tenant scale"),
    ("coverage", 0.54,
     r"support for|\barm\b|ubuntu|rhel|red hat|opensuse|debian|alma ?linux|"
     r"rocky linux|zorin|raspberry|\blxc\b|container|kubernetes|macos|\bmac\b|"
     r"windows server|new os|os version|end of life|\beol\b",
     "platform coverage gap"),
    ("efficiency", 0.46,
     r"manual|manually|workaround|time.?consuming|repetitive|one by one|"
     r"each time|every time|tedious|automat|streamlin",
     "manual effort the product should absorb"),
    ("visibility", 0.42,
     r"no visibility|cannot see|unable to see|no report|no notification|"
     r"no alert|not shown|not displayed|hidden|export|dashboard",
     "visibility or reporting gap"),
]

# Applied as a penalty, never as a base score.
NICE_TO_HAVE_RE = re.compile(
    r"cosmetic|colou?r of|font size|tooltip|nice to have|would be nice|"
    r"minor (ui|cosmetic)|rename the|wording|typo|look and feel|padding",
    re.IGNORECASE,
)

BAND_START_NOW = 68.0
BAND_PLAN = 54.0
BAND_BACKLOG = 38.0

BANDS = [
    ("start_now", "Start now",       "Commit to this PI",                 BAND_START_NOW),
    ("plan",      "Plan",            "Size now, commit next PI",          BAND_PLAN),
    ("backlog",   "Keep in backlog", "Revisit next cycle",                BAND_BACKLOG),
    ("drop",      "Drop candidate",  "Propose closing with the customer", 0.0),
]


# ── Small helpers ────────────────────────────────────────────────────────────

def normalise_severity(value: Any) -> str:
    """Fold Salesforce severity text to a known key.

    Accepts the values the export actually contains plus common variants
    ('P1', 'Urgent', 'Sev 1'), so a changed picklist degrades to a sensible
    reading instead of scoring every row as blank.
    """
    s = (str(value or "")).strip().lower()
    if not s:
        return ""
    if s in SEVERITY_WEIGHT:
        return s
    if s.startswith("crit") or s in ("p1", "sev1", "sev 1", "urgent", "blocker"):
        return "critical"
    if s.startswith("high") or s in ("p2", "sev2", "sev 2", "major"):
        return "high"
    if s.startswith("med") or s in ("p3", "sev3", "sev 3", "normal", "moderate"):
        return "medium"
    if s.startswith("low") or s in ("p4", "sev4", "sev 4", "minor", "trivial"):
        return "low"
    return ""


def severity_label(value: Any) -> str:
    """Display form: 'Critical', 'High', … or 'Unset' for a blank severity."""
    key = normalise_severity(value)
    return key.capitalize() if key else "Unset"


def is_business_impact(record: Dict) -> bool:
    """True when Salesforce carries an asserted business impact.

    The export writes this as the string '1'/'0', but a CSV re-save can turn it
    into 'true'/'Yes'/'TRUE' and a hand-edited sheet into 'x'. An unrecognised
    value must never silently promote an RFE, so only known positives count.
    """
    raw = record.get("business_impact")
    if raw is None or raw == "":
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "x", "checked")


def business_impact_reason(record: Dict) -> str:
    return (record.get("business_impact_reason") or "").strip()


def _parse_date(value: Any) -> Optional[datetime]:
    """Parse the date formats this pipeline sees, without a hard dependency.

    `load_data` normalises to ISO before scoring, but scoring is also called
    from tests and from the narrative writer, so the raw Salesforce
    'M/D/YYYY H:MM AM/PM' form is accepted too.
    """
    if isinstance(value, datetime):
        return value
    s = (str(value or "")).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %I:%M %p", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 6].strip(), fmt)
        except ValueError:
            continue
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def age_days(record: Dict, now: Optional[datetime] = None) -> Optional[int]:
    d = _parse_date(record.get("created_date") or record.get("opened"))
    if not d:
        return None
    return max(0, ((now or datetime.utcnow()) - d).days)


# ── Component scores ─────────────────────────────────────────────────────────

def arr_component(arr: float) -> float:
    """sqrt-scaled ARR, capped at the reference point.

    Capping matters: without it the single $1.9M account would set the scale for
    everyone else and every other request would score near zero on ARR.
    """
    if not arr or arr <= 0:
        return 0.0
    return math.sqrt(min(float(arr), ARR_REFERENCE) / ARR_REFERENCE)


def recency_component(days: Optional[int]) -> float:
    """1.0 while the request is fresh, then exponential decay.

    A missing date scores 0.5 rather than 0 — unknown age is not evidence of
    staleness, and punishing it would quietly demote every row of an export
    whose date column got renamed.
    """
    if days is None:
        return 0.5
    if days <= RECENCY_FULL_DAYS:
        return 1.0
    return max(0.0, 0.5 ** ((days - RECENCY_FULL_DAYS) / RECENCY_HALF_LIFE_DAYS))


def repetition_component(customers: int, max_repeats: int) -> float:
    """Repetition, split into breadth (how many customers) and depth (insistence).

    `customers`   distinct accounts asking for this theme
    `max_repeats` most requests filed by any single account on this theme

    Breadth is a log curve saturating at 8 customers: the step from 1 to 2
    customers is the most informative one in the whole model — it is the moment
    a request stops being anecdotal — and the step from 7 to 8 is noise. Depth
    is capped at 3 filings: a customer who has asked three times has made their
    point, and counting further only rewards a noisy account.
    """
    c = max(1, int(customers or 1))
    breadth = min(1.0, math.log(c, 2) / 3.0)                    # 1->0, 2->.33, 4->.67, 8->1
    depth = min(1.0, max(0, int(max_repeats or 1) - 1) / 2.0)   # 1->0, 2->.5, 3+->1
    return 0.65 * breadth + 0.35 * depth


def subject_signal(text: str) -> Dict[str, Any]:
    """Score what the request is about, and say which families matched.

    Returns {'score': 0..1, 'families': [key…], 'reasons': [phrase…]}. The
    reasons feed the report's "why this rank" line, so the reader sees
    'compliance or audit obligation' — never a regex and never a weight.
    """
    blob = (text or "").lower()
    if not blob.strip():
        return {"score": 0.0, "families": [], "reasons": []}

    matched: List[tuple] = []
    for key, weight, pattern, phrase in SUBJECT_SIGNALS:
        if re.search(pattern, blob, re.IGNORECASE):
            matched.append((key, weight, phrase))

    if not matched:
        base, families, reasons = 0.0, [], []
    else:
        matched.sort(key=lambda m: -m[1])
        base = matched[0][1]
        # Corroboration bonus, hard-capped: a request that is both a compliance
        # obligation and an MSSP-scale problem is a little stronger than either
        # alone, but never twice as strong.
        base = min(1.0, base + 0.05 * min(2, len(matched) - 1))
        families = [m[0] for m in matched]
        reasons = [m[2] for m in matched[:2]]

    if NICE_TO_HAVE_RE.search(blob):
        base = max(0.0, base - 0.35)
        families = families + ["nice_to_have"]

    return {"score": round(base, 4), "families": families, "reasons": reasons}


def _composite(components: Dict[str, float]) -> float:
    """Weighted sum of the six components, scaled to 0..100."""
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "priority weights must sum to 1.0"
    total = sum(WEIGHTS[k] * max(0.0, min(1.0, components.get(k, 0.0))) for k in WEIGHTS)
    return round(100.0 * total, 1)


def band_for(score: float) -> Dict[str, str]:
    """Map a score to its decision band (key, label, action)."""
    for key, label, action, floor in BANDS:
        if score >= floor:
            return {"key": key, "label": label, "action": action}
    return {"key": "drop", "label": "Drop candidate",
            "action": "Propose closing with the customer"}


def escalation_floor(records: Sequence[Dict]) -> tuple:
    """The lowest band some facts are allowed to produce, and why.

    Calibrating against the reference export exposed a failure the weighted
    model cannot fix on its own: a **Critical** severity request with a written
    business impact, from a single $185K customer, scored 49.7 and landed in
    "Keep in backlog". Arithmetically correct — one customer means no repetition
    and $185K is a fifth of the ARR scale — and wrong for a PI meeting, where
    those two facts are precisely the ones that require somebody to say yes or
    no out loud.

    So two facts carry a floor:

      * **Critical severity** → at least Plan. Support set it to Critical; the
        product team may decline it, but not by default.
      * **A business-impact flag with a written reason** → at least Plan. Someone
        took the trouble to state the consequence; that is the strongest human
        signal in the export.
      * **A business-impact flag alone** → at least Keep in backlog. A customer
        asserted an impact, so this is never proposed for closure silently.

    Two further floors keep the report from making a commercial decision it has
    no standing to make. Reviewing the reference output surfaced a theme asked
    for by two customers carrying $1.2M of ARR, labelled "Candidate to close"
    because its severity was Low and the request was old. Arithmetically that is
    what the weights say; presenting it as a recommendation to close is not
    something a scoring model gets to do:

      * **$1M or more of ARR at stake** → at least Keep in backlog. Whether to
        tell a customer of that size no is an account conversation.
      * **A second distinct customer asking** → at least Keep in backlog.
        Repeat demand across customers is the one signal this report exists to
        surface, and the repetition model already treats the 1→2 step as the
        largest single jump in the backlog — the moment a request stops being
        anecdotal. A theme that clears that bar can never end in a closure
        recommendation by default. (This floor was 3 customers until review
        found a 2-customer, $577K theme badged "Drop candidate", which
        contradicted the model's own premise.)

    The floor raises the score to the band's threshold rather than overriding
    the band underneath it, so score, band and sort order stay consistent — and
    the reason is recorded and shown, so an escalated rank is never mysterious.
    Applied to a single RFE and to a theme alike: a theme containing a request
    that has to be decided also has to be decided.
    """
    floor, reason = 0.0, ""

    def raise_to(value: float, text: str) -> None:
        nonlocal floor, reason
        if value > floor:
            floor, reason = value, text

    for r in records:
        if normalise_severity(r.get("severity")) == "critical":
            raise_to(BAND_PLAN, "severity is Critical")
        if is_business_impact(r):
            if business_impact_reason(r):
                raise_to(BAND_PLAN, "the customer stated the business consequence")
            else:
                raise_to(BAND_BACKLOG, "a customer asserted a business impact")

    # Commercial guards, read across the whole set rather than per record.
    if distinct_account_arr(records) >= 1_000_000:
        raise_to(BAND_BACKLOG, "the accounts behind it carry over $1M of ARR")
    accounts = {(r.get("account_name") or r.get("account") or "").strip() for r in records}
    accounts.discard("")
    if len(accounts) >= 2:
        raise_to(BAND_BACKLOG, f"{len(accounts)} different customers have asked")

    return floor, reason


# ── Repetition index ─────────────────────────────────────────────────────────

def build_repetition_index(records: Sequence[Dict]) -> Dict[str, Dict[str, int]]:
    """Measure repetition per cluster — the field Salesforce does not have.

    Repetition is how a PI Planning meeting tells a one-off from a pattern, and
    it is not in the export: it has to be derived from how the RFEs group. For
    every cluster key ('<domain>||<cluster>') this returns:

        requests         rows in the cluster
        customers        distinct accounts in the cluster
        max_repeats      most rows filed by any one account
        repeat_accounts  accounts that filed more than once

    Callers must set `_domain` and `_cluster` on each record first (the report
    generator does this during clustering).
    """
    per_cluster: Dict[str, Dict[str, Any]] = {}
    for r in records:
        key = f"{r.get('_domain', 'Other')}||{r.get('_cluster', '')}"
        slot = per_cluster.setdefault(key, {"requests": 0, "by_account": defaultdict(int)})
        slot["requests"] += 1
        acct = (r.get("account_name") or "").strip() or "(unnamed account)"
        slot["by_account"][acct] += 1

    out: Dict[str, Dict[str, int]] = {}
    for key, slot in per_cluster.items():
        counts = slot["by_account"]
        out[key] = {
            "requests": slot["requests"],
            "customers": len(counts),
            "max_repeats": max(counts.values()) if counts else 1,
            "repeat_accounts": sum(1 for v in counts.values() if v > 1),
        }
    return out


# ── RFE-level scoring ────────────────────────────────────────────────────────

def score_rfe(record: Dict,
              repetition: Optional[Dict[str, int]] = None,
              now: Optional[datetime] = None) -> Dict[str, Any]:
    """Score one RFE and return the score with everything needed to justify it.

    `repetition` is that RFE's cluster entry from `build_repetition_index`; when
    omitted the RFE is scored as a singleton, which is the honest reading of an
    RFE considered outside its cluster.
    """
    rep = repetition or {"requests": 1, "customers": 1, "max_repeats": 1}
    sev_key = normalise_severity(record.get("severity"))
    bi = is_business_impact(record)
    reason = business_impact_reason(record)
    days = age_days(record, now)
    arr = float(record.get("account_arr") or record.get("arr") or 0)

    sig = subject_signal(
        " ".join([record.get("subject") or "", record.get("description") or "", reason])
    )

    components = {
        "severity":        SEVERITY_WEIGHT.get(sev_key, 0.30),
        "arr":             arr_component(arr),
        # A flag with a written reason is the strongest human signal in the
        # export — someone took the trouble to state the consequence.
        "business_impact": (1.0 if (bi and reason) else 0.85 if bi else 0.0),
        "repetition":      repetition_component(rep.get("customers", 1),
                                                rep.get("max_repeats", 1)),
        "recency":         recency_component(days),
        "subject_signal":  sig["score"],
    }
    score = _composite(components)
    floor, floor_reason = escalation_floor([record])
    escalated = ""
    if floor > score:
        score, escalated = floor, floor_reason

    return {
        "score": score,
        "band": band_for(score),
        "components": components,
        "escalated": escalated,
        "severity_key": sev_key,
        "severity_label": severity_label(record.get("severity")),
        "severity_rank": SEVERITY_RANK.get(sev_key, 0),
        "business_impact": bi,
        "business_impact_reason": reason,
        "age_days": days,
        "customers": rep.get("customers", 1),
        "requests": rep.get("requests", 1),
        "max_repeats": rep.get("max_repeats", 1),
        "signal_families": sig["families"],
        "why": _with_escalation(
            _why_rfe(record, components, sev_key, bi, reason, rep, sig, days), escalated),
    }


def _with_escalation(why: str, escalated: str) -> str:
    """Append the escalation reason so a lifted rank explains itself."""
    if not escalated:
        return why
    return f"{why} · ranked up because {escalated}"


def _why_rfe(record, components, sev_key, bi, reason, rep, sig, days) -> str:
    """One line naming the drivers, strongest first — never a formula.

    This is what the report prints under a rank, so it has to read like a
    colleague explaining the call, and it must never claim a driver the data
    does not support.
    """
    parts: List[str] = []
    ranked = sorted(components.items(), key=lambda kv: -WEIGHTS[kv[0]] * kv[1])
    for name, value in ranked:
        if value <= 0.01:
            continue
        if name == "severity" and sev_key in ("critical", "high"):
            parts.append(f"{sev_key.capitalize()} severity")
        elif name == "arr":
            arr = float(record.get("account_arr") or record.get("arr") or 0)
            if arr >= 100_000:
                parts.append(f"{_money(arr)} account")
        elif name == "business_impact" and bi:
            parts.append("business impact stated by the customer"
                         if reason else "business-impact flagged")
        elif name == "repetition":
            c, d = rep.get("customers", 1), rep.get("max_repeats", 1)
            if c > 1:
                parts.append(f"{c} customers asking")
            elif d > 1:
                parts.append(f"the same customer asked {d} times")
        elif name == "recency" and days is not None and days <= 90:
            parts.append("opened in the last quarter")
        elif name == "subject_signal" and sig["reasons"]:
            parts.append(sig["reasons"][0])
    if "nice_to_have" in sig["families"]:
        parts.append("reads as a cosmetic refinement")
    if not parts:
        parts.append("low severity, no ARR attached and no repeat demand")
    return " · ".join(parts[:4])


def _money(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


# ── Aggregate scoring (clusters and domains) ─────────────────────────────────

def distinct_account_arr(records: Iterable[Dict]) -> float:
    """ARR at stake across a set of RFEs, counting each account ONCE.

    The v1 report summed `account_arr` per row, so an account with 13 open RFEs
    contributed its ARR 13 times and the portfolio headline read $64.1M against
    a true $20.0M. Every ARR total in the report goes through this function.
    """
    per_account: Dict[str, float] = {}
    anon = 0
    for r in records:
        name = (r.get("account_name") or r.get("account") or "").strip()
        arr = float(r.get("account_arr") or r.get("arr") or 0)
        if not name:
            # Unnamed accounts cannot be de-duplicated against each other, so
            # keep them separate rather than collapsing unrelated revenue.
            anon += 1
            per_account[f"__anon_{anon}"] = arr
            continue
        per_account[name] = max(per_account.get(name, 0.0), arr)
    return float(sum(per_account.values()))


def score_group(records: Sequence[Dict], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Score a cluster (Epic) or a domain from its member RFEs.

    The same six signals, read at group level, so a cluster's rank is
    explainable in the same words as an RFE's:

      severity        the worst severity present, softened by how much of the
                      group sits at that level — one Critical among forty Lows
                      is a real signal but not a Critical group
      arr             distinct-account ARR at stake, sqrt-scaled
      business_impact share of members carrying the flag, with any flag at all
                      worth a floor of 0.5 (one asserted consequence in a group
                      is material even when the rest are silent)
      repetition      distinct customers and deepest single-customer repeat
      recency         share of the group opened in the last two quarters
      subject_signal  the strongest signal in the group, pulled toward its mean
                      so one dramatic subject line cannot carry a whole group
    """
    records = list(records)
    if not records:
        return {
            "score": 0.0, "band": band_for(0.0), "components": {},
            "requests": 0, "customers": 0, "arr": 0.0, "flagged": 0,
            "flag_reasons": [], "severity_mix": {}, "top_severity": "",
            "max_repeats": 0, "recent_90": 0, "recent_180": 0,
            "median_age_days": None, "signal_reasons": [],
            "why": "no requests in scope",
        }

    n = len(records)
    now = now or datetime.utcnow()

    sev_keys = [normalise_severity(r.get("severity")) for r in records]
    mix: Dict[str, int] = defaultdict(int)
    for k in sev_keys:
        mix[k or ""] += 1
    top_sev = max(sev_keys, key=lambda k: SEVERITY_RANK.get(k, 0))
    top_share = sum(1 for k in sev_keys if k == top_sev) / n
    # Worst severity present, pulled toward the group mean by how rare it is.
    sev_mean = sum(SEVERITY_WEIGHT.get(k, 0.30) for k in sev_keys) / n
    sev_component = SEVERITY_WEIGHT.get(top_sev, 0.30) * (0.55 + 0.45 * top_share)
    sev_component = max(sev_mean, min(1.0, sev_component))

    arr = distinct_account_arr(records)
    per_account: Dict[str, int] = defaultdict(int)
    for r in records:
        per_account[(r.get("account_name") or r.get("account") or "").strip()] += 1
    accounts = {a for a in per_account if a}
    max_repeats = max(per_account.values()) if per_account else 1

    flagged = sum(1 for r in records if is_business_impact(r))
    reasons_written = sum(1 for r in records
                          if is_business_impact(r) and business_impact_reason(r))
    bi_component = 0.0
    if flagged:
        bi_component = max(0.5, flagged / n)
        if reasons_written:
            bi_component = min(1.0, bi_component + 0.15)

    ages = [age_days(r, now) for r in records]
    known_ages = sorted([a for a in ages if a is not None])
    recency = sum(recency_component(a) for a in ages) / n

    sigs = [subject_signal(" ".join([r.get("subject") or "",
                                     r.get("description") or "",
                                     business_impact_reason(r)])) for r in records]
    sig_scores = [s["score"] for s in sigs]
    sig_component = 0.6 * max(sig_scores) + 0.4 * (sum(sig_scores) / n)
    sig_reasons: List[str] = []
    for s in sorted(sigs, key=lambda s: -s["score"]):
        for phrase in s["reasons"]:
            if phrase not in sig_reasons:
                sig_reasons.append(phrase)
        if len(sig_reasons) >= 2:
            break

    components = {
        "severity":        sev_component,
        "arr":             arr_component(arr),
        "business_impact": bi_component,
        "repetition":      repetition_component(len(accounts) or 1, max_repeats),
        "recency":         recency,
        "subject_signal":  sig_component,
    }
    score = _composite(components)
    floor, floor_reason = escalation_floor(records)
    escalated = ""
    if floor > score:
        score, escalated = floor, floor_reason

    return {
        "score": score,
        "band": band_for(score),
        "components": components,
        "escalated": escalated,
        "requests": n,
        "customers": len(accounts),
        "arr": arr,
        "flagged": flagged,
        "flag_reasons": [business_impact_reason(r) for r in records
                         if is_business_impact(r) and business_impact_reason(r)],
        "severity_mix": dict(mix),
        "top_severity": top_sev,
        "max_repeats": max_repeats,
        "recent_90": sum(1 for a in ages if a is not None and a <= 90),
        "recent_180": sum(1 for a in ages if a is not None and a <= 180),
        "median_age_days": known_ages[len(known_ages) // 2] if known_ages else None,
        "signal_reasons": sig_reasons,
        "why": _with_escalation(
            _why_group(components, top_sev, arr, len(accounts), flagged,
                       max_repeats, sig_reasons, n), escalated),
    }


def _why_group(components, top_sev, arr, customers, flagged,
               max_repeats, sig_reasons, n) -> str:
    """The group-level 'why', in the same voice as the RFE one."""
    parts: List[str] = []
    ranked = sorted(components.items(), key=lambda kv: -WEIGHTS[kv[0]] * kv[1])
    for name, value in ranked:
        if value <= 0.01:
            continue
        if name == "repetition":
            if customers > 1:
                parts.append(f"{customers} customers asking")
            elif max_repeats > 1:
                parts.append(f"one customer asked {max_repeats} times")
        elif name == "arr" and arr >= 100_000:
            parts.append(f"{_money(arr)} of ARR at stake")
        elif name == "severity" and top_sev in ("critical", "high"):
            parts.append(f"{top_sev.capitalize()} severity in the group")
        elif name == "business_impact" and flagged:
            parts.append(f"{flagged} of {n} carry a business-impact flag")
        elif name == "subject_signal" and sig_reasons:
            parts.append(sig_reasons[0])
        elif name == "recency" and value >= 0.7:
            parts.append("demand is current")
    if not parts:
        parts.append("no severity, ARR or repeat-demand signal")
    return " · ".join(parts[:4])


# ── Sort keys for the report's Sort-by dropdown ──────────────────────────────
# One definition, used for RFEs, clusters and domains, so "Sort by ARR" means
# the same thing on every page of the report.

SORT_FIELDS = [
    ("priority",   "PI Priority",     "Severity, ARR, business impact, repetition and subject together"),
    ("severity",   "Severity",        "Highest Salesforce severity first"),
    ("arr",        "ARR at stake",    "Largest distinct-account ARR first"),
    ("impact",     "Business impact", "Customer-asserted business impact first"),
    ("repetition", "Repetition",      "Most customers asking for the same thing first"),
    ("recency",    "Most recent",     "Newest requests first"),
    ("oldest",     "Oldest",          "Longest-waiting requests first"),
]


def sort_key(kind: str):
    """Return a `key=` callable for a scored dict (RFE, cluster or domain).

    Every key returns a tuple to be sorted DESCENDING, with PI Priority as the
    universal tie-breaker so equal ARR or equal severity still lands in a
    defensible order rather than insertion order.
    """
    def _priority(d):   return (d.get("score", 0), d.get("arr", 0), d.get("customers", 0))
    def _severity(d):   return (d.get("severity_rank",
                                      SEVERITY_RANK.get(d.get("top_severity", ""), 0)),
                                d.get("score", 0))
    def _arr(d):        return (d.get("arr", 0), d.get("score", 0))
    def _impact(d):     return (1 if d.get("business_impact") else d.get("flagged", 0),
                                d.get("score", 0))
    def _repetition(d): return (d.get("customers", 0), d.get("requests", 0), d.get("score", 0))
    def _recency(d):    return (-(d.get("age_days") if d.get("age_days") is not None else 10 ** 6),
                                d.get("score", 0))
    def _oldest(d):     return ((d.get("age_days") if d.get("age_days") is not None else -1),
                                d.get("score", 0))
    return {
        "priority": _priority, "severity": _severity, "arr": _arr, "impact": _impact,
        "repetition": _repetition, "recency": _recency, "oldest": _oldest,
    }.get(kind, _priority)
