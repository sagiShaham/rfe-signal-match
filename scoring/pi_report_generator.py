"""
PI Planning report generator — v2 (strategic demand cockpit).

WHAT CHANGED FROM v1, AND WHY
-----------------------------
v1 answered "where is demand loudest". A PI Planning meeting asks a harder
question: *which of these do we start building, which stay in the backlog, and
which do we close?* Every change below serves that question.

1. **One explainable ranking, applied at three levels.** `scoring/priority.py`
   scores each RFE, each request cluster (Epic) and each domain from the same
   six signals — severity, ARR, the Salesforce business-impact flag, repetition,
   recency and what the request is about. v1 ranked on `arr + count * 80000`,
   which ignored severity and business impact entirely.

2. **Repetition is computed, not guessed.** Salesforce has no repetition field,
   so it is derived from cluster membership and split into breadth (how many
   distinct customers) and depth (how insistently one customer repeats). Those
   are different business situations and the report now says which one it is
   looking at.

3. **ARR stopped being double-counted.** v1 summed `account_arr` per row, so a
   13-request account contributed its ARR 13 times; the reference export
   reported $64.1M against a true $20.0M. All ARR now goes through
   `priority.distinct_account_arr`.

4. **Classification uses the Salesforce taxonomy before regexes.** Sub-Domain
   (filled on 94% of the reference export and far more precise than Product
   Domain) is consulted first, then Product Domain, then the keyword fallback.
   This is what keeps the "Other" tab near-empty, which the skill demands.

5. **The scoring window is the timeframe.** Scores, bands, aggregates *and the
   written narratives* are precomputed in Python for each timeframe (All time /
   12 / 6 / 3 months), so switching timeframe never leaves stale prose beside
   fresh numbers, and no scoring logic has to be duplicated in JavaScript.

6. **Filters narrow, they never re-score.** Severity / business-impact / ARR /
   repetition controls select which rows and themes are listed. A theme's score
   is a property of the theme, so it does not change because the reader ticked
   a box — and the UI says so.

7. **PM Summaries cannot be faked.** The raw Salesforce description is never
   used as a summary anywhere; summaries are written during generation by
   `scoring/pm_summary.py`. The customer's original text is embedded only behind
   an explicitly labelled "Original customer text" expander, never in a PM
   Summary cell.

8. **Nothing is truncated with an ellipsis.** No subject, summary, theme name or
   chart label is cut with "…". Long labels wrap; long tables scroll; rows grow.

Structure: this module owns the DATA (load → classify → cluster → score →
narrate) and hands a single `model` dict to `scoring/pi_report_assets.py`, which
owns the PAGE (CSS, JS, HTML shell).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from scoring import pm_summary
from scoring import priority as P
from scoring import pi_narrative as N
from scoring import pi_report_assets as A


# ── Domain taxonomy ──────────────────────────────────────────────────────────
# The report's 18 tabs are fixed by the skill and must not be reordered:
# Executive first, then 17 product domains, with SIEM and Identity always
# separate tabs.
DOMAIN_ORDER = [
    "EPP", "Email Security", "SIEM", "Identity", "CSPM",
    "Platform", "Reporting", "User Management", "Endpoint Management",
    "Alert UI", "AI Initiatives", "Automations", "Playbooks", "Remediation",
    "PSA/RMM", "API", "Other",
]

DOMAIN_PAGE_ID = {
    "EPP": "epp", "Email Security": "email", "SIEM": "siem", "Identity": "identity",
    "CSPM": "cspm", "Platform": "platform", "Reporting": "reporting",
    "User Management": "usermgmt", "Endpoint Management": "endpointmgmt",
    "Alert UI": "alertui", "AI Initiatives": "ai", "Automations": "automations",
    "Playbooks": "playbooks", "Remediation": "remediation", "PSA/RMM": "psarmm",
    "API": "api", "Other": "other",
}

# Salesforce **Sub-Domain** → report domain. Consulted FIRST because it is the
# most precise signal in the export: on the reference file it is filled for 94%
# of rows and its 31 values map almost one-to-one onto the report's tabs, where
# Product Domain lumps 111 rows into a single "Endpoint" bucket.
SUB_DOMAIN_MAP = {
    "endpoint protection": "EPP",
    "web access control": "EPP",
    "device control": "EPP",
    "vulnerability": "EPP",
    "deception": "EPP",
    "whitelisting": "EPP",
    "network detection and response": "EPP",
    "siem / clm": "SIEM",
    "siem/clm": "SIEM",
    "clm": "SIEM",
    "email security": "Email Security",
    "itdr": "Identity",
    "identity": "Identity",
    "sspm / cspm": "CSPM",
    "sspm/cspm": "CSPM",
    "misconfiguration": "CSPM",
    "external surface management (port scan)": "CSPM",
    "reporting": "Reporting",
    "user management": "User Management",
    "endpoint management": "Endpoint Management",
    "deployment": "Endpoint Management",
    "os support": "Endpoint Management",
    "health monitoring": "Endpoint Management",
    "alert ui": "Alert UI",
    "xdr alerts": "Alert UI",
    "ai": "AI Initiatives",
    "remediation (built-in, custom, auto remediation)": "Remediation",
    "remediation": "Remediation",
    "psa / rmm": "PSA/RMM",
    "psa/rmm": "PSA/RMM",
    "api": "API",
    "ux": "Platform",
    "site management": "Platform",
    "cross site management": "Platform",
}

# Sub-Domain values that carry no classification information — they must fall
# through to Product Domain rather than creating a junk bucket.
SUB_DOMAIN_IGNORE = {"", "any", "other", "n/a", "none", "tbd"}

# Salesforce **Product Domain** → report domain (second pass).
SF_DOMAIN_MAP = {
    "endpoint": "EPP",
    "epp": "EPP",
    "siem": "SIEM",
    "clm": "SIEM",
    "cloud": "CSPM",
    "espm": "CSPM",
    "cspm": "CSPM",
    "sspm": "CSPM",
    "platform": "Platform",
    "ux/ui": "Platform",
    "ux": "Platform",
    "msp": "Platform",
    "mssp": "Platform",
    "product tools": "PSA/RMM",
    "identity": "Identity",
    "email": "Email Security",
    "email security": "Email Security",
    "automation": "Automations",
    "automations": "Automations",
    "actions, playbooks, and integrations": "Automations",
    "playbooks": "Playbooks",
    "remediation": "Remediation",
    "alert management": "Alert UI",
    "alert ui": "Alert UI",
    "user and site management": "User Management",
    "user management": "User Management",
    "reporting": "Reporting",
    "mobile": "Endpoint Management",
    "endpoint management": "Endpoint Management",
    "api": "API",
    "ai": "AI Initiatives",
}

# Third pass: keyword classification from Subject + Description, used only when
# both Salesforce fields are empty or uninformative. Ordered — first match wins.
KEYWORD_DOMAINS: List[Tuple[str, str]] = [
    ("Email Security",      r"email security|mail security|email digest|dkim|arc and srs|"
                            r"link protection|email.*filter|email integration|email.*allow|e-?mail settings?|"
                            r"email.*block|email.*rbac|email.*setting|email.*audit|email.*log"),
    ("SIEM",                r"\bclm\b|\bsiem\b|log management|log.*collect|syslog|log.*export|"
                            r"\buba\b|threat hunting|correl|mitre.*alert|forensic|logstash|windows event|"
                            r"xdr whitelist|xdr.*country"),
    ("CSPM",                r"\bcspm\b|\bsspm\b|aws.*misconfig|aws.*role|assume.*role|alibaba|"
                            r"ali cloud|huawei cloud|azure.*misconfig|cloud.*integrat|cloud account"),
    ("Identity",            r"entra id|\bitdr\b|mfa enforcement|account lockout|active directory|"
                            r"user security posture|suspicious login|identity protect|\bldap\b"),
    ("PSA/RMM",             r"connectwise|ninjaone|ninja.*rmm|\bdatto\b|autotask|\bpsa\b|"
                            r"it glue|mcp server|native mcp|mcp support|\bninja\b|\brmm\b"),
    ("API",                 r"api rest|rest api|api call|\bapiv[12]\b|api.*endpoint|"
                            r"management.*api|api.*billing|api.*alert|api.*uninstall"),
    ("Automations",         r"custom remediation|auto-rem|global playbook|remediation script|"
                            r"import.*script|\bautomat|global.*remediation"),
    ("Reporting",           r"\breport\b|executive report|scheduled report|all-in-one report|"
                            r"vulnerability report|digest report|quarterly.*report|"
                            r"report.*branding|report.*logo"),
    ("Alert UI",            r"alert.*filter|alert.*view|alert.*ui|alert.*tag|unscanned host alert|"
                            r"inactive.*alert.*rule|test alert|alert.*classify|reopen alert"),
    ("User Management",     r"\brbac\b|custom role|user role|granular.*permission|user.*permission|"
                            r"role.*permission|co-brand|branding.*logo|custom.*logo|user right"),
    ("Endpoint Management", r"non-deployed|non deployed|uninstall.*host|uninstalled host|"
                            r"inactive.*host|offline.*host|host.*migration|tray icon|"
                            r"cynet.*agent.*install|cynet.*deploy|msi.*installer|arm.*device|"
                            r"arm.*server|linux.*install|opensuse|zorin|raspberry pi|alma linux|"
                            r"rhel.*support|mac.*install|arm architecture|host.*serial|"
                            r"host.*uptime|remove.*host|suspend.*agent|facilitate.*deploy|"
                            r"endpoint management"),
    ("Platform",            r"\btimezone\b|time zone|centralized.*mssp|on-prem mssp|multi-site|"
                            r"multi site|global.*mssp|mssp.*level|tenant search|expiration.*allow|"
                            r"exclusion catalog|global.*setting|global.*admin|reverse proxy|"
                            r"console lockdown|cross.*site|centralized.*email|msp level|mssp level|"
                            r"centralized.*remediation|centralized.*alert|centralized.*scanner"),
    ("EPP",                 r"\bantivirus\b|av scan|full av|on-demand.*scan|\bepp\b|endpoint protect|"
                            r"storage device control|usb.*scan|usb.*block|linux isolat|mac.*scan|"
                            r"file.*hash|anti-tamper|\bepss\b|\bsandbox\b|\bmalware\b|"
                            r"tamper.*protect|process.*protect|\bbitlocker\b|remote wipe|"
                            r"web content filter|\bwcf\b|network.*detect|\bndr\b|"
                            r"dhcp.*starvation|rogue dhcp|device.*control.*usb|device control|usb device"),
    ("Remediation",         r"undo remediation|remote shell|remote.*script.*host|block.*ip.*nsx|"
                            r"unblock.*ip|nsx block|application control.*block|"
                            r"software.*vulnerability.*remediat"),
    ("Playbooks",           r"playbook.*global|global.*playbook|playbook.*apply|playbook.*template"),
    ("AI Initiatives",      r"\bai\s|artificial intelligence|machine learning|\bllm\b|\bcopilot\b|"
                            r"ai agent"),
]

# Tokens that carry no theme information when clustering subjects.
STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "by", "with",
    "from", "as", "is", "are", "be", "has", "have", "that", "this", "it", "its",
    "can", "will", "cynet", "rfe", "request", "feature", "ability", "support",
    "add", "allow", "enable", "new", "improve", "enhance", "update", "please",
    "need", "needs", "would", "like", "should", "customer", "customers", "option",
    "when", "not", "does", "want", "wants", "also", "make", "using", "use",
}

# Salesforce subjects arrive with a handful of bureaucratic prefixes.
SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(\[rfe\]|\[external\]|\[beta\]|rfe[:\-]\s*|rfe for\s*|fr\s*[-–:]\s*|"
    r"fr\s+for\s*|feature request[:\-]\s*)+",
    re.IGNORECASE,
)
# Bullets and dashes left behind once a prefix is stripped. Calibration produced
# theme names like "- New CLM data sources" and "- alma linux 10 support", which
# read as unfinished work on a leadership page.
SUBJECT_BULLET_RE = re.compile(r"^\s*[-–—*•·:>\.]+\s*")
# Some subjects carry the case number inline ("Email Security Filters [00392493]").
# It is already shown in its own copyable column, so it is noise in a theme name.
SUBJECT_CASE_RE = re.compile(r"\s*[\[\(]\s*#?0*\d{5,10}\s*[\]\)]\s*$")

# The timeframes offered in the header. Precomputed in full — scores, aggregates
# and prose — so the page never mixes one window's numbers with another's words.
TIMEFRAMES: List[Tuple[str, int, str]] = [
    ("all", 0,   "All time"),
    ("12",  365, "Last 12 months"),
    ("6",   182, "Last 6 months"),
    ("3",   91,  "Last 3 months"),
]
# All time is the default: this report exists to decide the fate of an
# accumulated backlog, and a 12-month default silently hides the oldest
# requests, which are exactly the ones a cleanup session needs to see.
DEFAULT_TIMEFRAME = "all"

# The section key `pm_summary` uses to choose the right voice and actor.
PM_SECTION_OF = {
    "EPP": "epp", "Endpoint Management": "epp",
    "Email Security": "email", "SIEM": "siem", "Identity": "identity",
    "CSPM": "cspm", "Reporting": "reporting", "AI Initiatives": "ai",
    "Automations": "automations", "Playbooks": "automations",
    "Remediation": "automations", "PSA/RMM": "automations", "API": "automations",
    "Platform": "platform", "User Management": "platform",
    "Alert UI": "platform", "Other": "platform",
}


# ── Classification ───────────────────────────────────────────────────────────

def classify_domain(sf_domain: str, sub_domain: str, subject: str, description: str) -> str:
    """Map one RFE to a report domain: Sub-Domain, then Product Domain, then text.

    The order matters. Sub-Domain is the field a PM actually curates per case,
    so it beats Product Domain ("Endpoint" covers a third of the export) and it
    beats keyword matching, which can only guess. Text classification runs last
    and exists to keep the "Other" tab as close to empty as possible.
    """
    sub = (sub_domain or "").strip().lower()
    if sub and sub not in SUB_DOMAIN_IGNORE:
        mapped = SUB_DOMAIN_MAP.get(sub)
        if mapped:
            return mapped

    dom = (sf_domain or "").strip().lower()
    if dom:
        mapped = SF_DOMAIN_MAP.get(dom)
        if mapped:
            return mapped

    text = f"{subject or ''} {description or ''}".lower()
    for domain, pattern in KEYWORD_DOMAINS:
        if re.search(pattern, text):
            return domain
    return "Other"


def clean_subject(subject: str) -> str:
    """Strip the '[RFE]' / 'FR -' style prefixes and tidy whitespace.

    Never truncates: a theme name is allowed to be a full sentence, and the page
    wraps it rather than cutting it with an ellipsis.
    """
    s = SUBJECT_PREFIX_RE.sub("", subject or "").strip()
    s = SUBJECT_BULLET_RE.sub("", s)
    s = SUBJECT_CASE_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—*•·")
    # A single trailing full stop reads as an unfinished sentence in a list of
    # theme names; anything else (?, !, ellipsis) is left alone as deliberate.
    if s.endswith(".") and not s.endswith(".."):
        s = s[:-1].rstrip()
    # A subject that was nothing but decoration falls back to the raw value —
    # an empty theme name would be worse than an ugly one.
    return s or (subject or "").strip()


def tokenize(text: str) -> set:
    """Meaningful word set for similarity, with light plural folding."""
    words = re.findall(r"[a-z]{3,}", (text or "").lower())
    out = set()
    for w in words:
        if w in STOPWORDS:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
            w = w[:-1]
        out.add(w)
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1


# Subject similarity needed to merge two RFEs into one theme.
CLUSTER_THRESHOLD = 0.30
# Lower bar when Salesforce already says the two cases are the same sub-domain:
# a curated taxonomy match is corroborating evidence, so less textual overlap is
# needed before two requests count as the same theme.
CLUSTER_THRESHOLD_SAME_SUBDOMAIN = 0.17


def cluster_records(records: Sequence[Dict]) -> None:
    """Group each domain's RFEs into themes, in place, setting `_cluster`.

    Clustering happens ONCE over the whole upload, not per timeframe, so a theme
    keeps its identity when the reader changes the window — otherwise "3
    customers asking" could become "1 customer asking" purely because a theme
    got re-cut.

    Naming uses the **medoid**: the member whose subject is most similar to the
    rest of the group, with the shorter subject winning ties. v1 named a cluster
    after its highest-ARR member, which let one big account's idiosyncratic
    phrasing become the label for everyone else's request.
    """
    by_domain: Dict[str, List[Dict]] = defaultdict(list)
    for r in records:
        by_domain[r["_domain"]].append(r)

    for domain, rows in by_domain.items():
        n = len(rows)
        tokens = [tokenize(clean_subject(r["subject"])) for r in rows]
        subs = [(r.get("sub_domain") or "").strip().lower() for r in rows]
        uf = _UnionFind(n)

        sim: Dict[Tuple[int, int], float] = {}
        for i in range(n):
            for j in range(i + 1, n):
                s = jaccard(tokens[i], tokens[j])
                sim[(i, j)] = s
                same_sub = (subs[i] == subs[j] and subs[i] not in SUB_DOMAIN_IGNORE)
                threshold = CLUSTER_THRESHOLD_SAME_SUBDOMAIN if same_sub else CLUSTER_THRESHOLD
                if s >= threshold:
                    uf.union(i, j)

        groups: Dict[int, List[int]] = defaultdict(list)
        for i in range(n):
            groups[uf.find(i)].append(i)

        for members in groups.values():
            if len(members) == 1:
                name = clean_subject(rows[members[0]]["subject"])
            else:
                best_idx, best_score = members[0], -1.0
                for i in members:
                    total = 0.0
                    for j in members:
                        if i == j:
                            continue
                        a, b = (i, j) if i < j else (j, i)
                        total += sim.get((a, b), 0.0)
                    avg = total / (len(members) - 1)
                    subject_len = len(rows[i]["subject"])
                    # Most representative wins; on a near-tie the shorter, more
                    # general subject makes the better theme label.
                    if (avg > best_score + 1e-9) or (
                            abs(avg - best_score) <= 1e-9
                            and subject_len < len(rows[best_idx]["subject"])):
                        best_idx, best_score = i, avg
                name = clean_subject(rows[best_idx]["subject"])
            for i in members:
                rows[i]["_cluster"] = name


# ── Data loading ─────────────────────────────────────────────────────────────

# Columns added for v2. A report generated against an older database simply sees
# them as absent and degrades to "no business impact recorded" rather than
# failing, so the generator never depends on a migration having run.
V2_COLUMNS = ("business_impact", "business_impact_reason", "case_owner", "arr_currency")


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def load_data(db_path: str, run_id: Optional[str] = None) -> List[Dict]:
    """Load one dataset's RFEs, de-duplicated by case number.

    `run_id` selects an uploaded per-PM dataset; omit it for the latest shared
    Signal Match run.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if not run_id:
        row = conn.execute(
            "SELECT run_id FROM run_meta ORDER BY rowid DESC LIMIT 1").fetchone()
        if not row:
            conn.close()
            return []
        run_id = row["run_id"]

    have = _table_columns(conn, "rfe_pulls")
    extra = [c for c in V2_COLUMNS if c in have]
    cols = ["case_number", "subject", "description", "account_name", "account_arr",
            "status", "domain", "sub_domain", "severity", "created_date"] + extra
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM rfe_pulls WHERE run_id = ?", (run_id,)
    ).fetchall()
    conn.close()

    seen, result = set(), []
    for r in rows:
        case = (r["case_number"] or "").strip()
        subject = (r["subject"] or "").strip()
        if not case or not subject or case in seen:
            continue
        seen.add(case)
        rec = {
            "case_number": case,
            "subject": subject,
            "description": (r["description"] or "").strip(),
            "account_name": (r["account_name"] or "").strip(),
            "account_arr": float(r["account_arr"] or 0),
            "status": (r["status"] or "").strip(),
            "domain": (r["domain"] or "").strip(),
            "sub_domain": (r["sub_domain"] or "").strip(),
            "severity": (r["severity"] or "").strip(),
            "created_date": _iso_date(r["created_date"]),
        }
        for c in extra:
            rec[c] = r[c] if r[c] is not None else ""
        for c in V2_COLUMNS:
            rec.setdefault(c, "")
        result.append(rec)
    return result


def _iso_date(raw: Any) -> str:
    """Normalise the export's date formats to YYYY-MM-DD (empty if unreadable)."""
    s = (str(raw or "")).strip()
    if not s:
        return ""
    for fmt in ("%m/%d/%Y %I:%M %p", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 6].strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        datetime.strptime(s[:10], "%Y-%m-%d")
        return s[:10]
    except ValueError:
        return ""


# ── Model building ───────────────────────────────────────────────────────────

def _pm_text(record: Dict) -> str:
    """The record's PM Decision Summary. Never the raw description.

    `pm_summary.ensure_summaries` writes these during generation; this is
    normally a read. The one-line fallback still goes through the summary
    writer, so no code path can put Salesforce prose in a PM Summary cell.
    """
    text = (record.get("pm_summary") or "").strip()
    return text or pm_summary.write_summary(record)


def _account_rollup(records: Sequence[Dict], limit: int = 5) -> List[Dict]:
    """The accounts pushing hardest in a set of RFEs, by requests then ARR."""
    per: Dict[str, Dict[str, Any]] = {}
    for r in records:
        name = (r.get("account_name") or "").strip()
        if not name:
            continue
        slot = per.setdefault(name, {"name": name, "requests": 0, "arr": 0.0})
        slot["requests"] += 1
        slot["arr"] = max(slot["arr"], float(r.get("account_arr") or 0))
    ranked = sorted(per.values(), key=lambda a: (-a["requests"], -a["arr"], a["name"]))
    return ranked[:limit]


def _in_window(record: Dict, cutoff: Optional[datetime]) -> bool:
    """Whether a record falls in a timeframe.

    A record with no readable date is included only in All time — it cannot be
    placed on the calendar, and quietly treating it as recent would inflate
    every momentum reading.
    """
    if cutoff is None:
        return True
    d = P._parse_date(record.get("created_date"))
    return bool(d and d >= cutoff)


def _band_counts(scored: Sequence[Dict]) -> Dict[str, int]:
    out: Dict[str, int] = defaultdict(int)
    for s in scored:
        out[s["band"]["key"]] += 1
    return dict(out)


def _severity_mix(records: Sequence[Dict]) -> Dict[str, int]:
    mix: Dict[str, int] = defaultdict(int)
    for r in records:
        mix[P.normalise_severity(r.get("severity"))] += 1
    return dict(mix)


def _monthly_counts(records: Sequence[Dict]) -> List[Dict[str, Any]]:
    """RFEs opened per month, gap-filled, for the momentum charts."""
    per: Dict[str, int] = defaultdict(int)
    for r in records:
        d = (r.get("created_date") or "")[:7]
        if d:
            per[d] += 1
    if not per:
        return []
    months = sorted(per)
    start = datetime.strptime(months[0], "%Y-%m")
    end = datetime.strptime(months[-1], "%Y-%m")
    out, cur = [], start
    while cur <= end:
        key = cur.strftime("%Y-%m")
        out.append({"month": key, "count": per.get(key, 0)})
        cur = datetime(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
    return out


def build_timeframe(records: Sequence[Dict], cutoff: Optional[datetime],
                    now: datetime) -> Dict[str, Any]:
    """Score and narrate one timeframe.

    Returns everything the page needs for that window: per-RFE scores, cluster
    and domain aggregates, portfolio totals, and the written narratives — so the
    browser only has to filter and draw, never to score.
    """
    scope = [r for r in records if _in_window(r, cutoff)]

    # Repetition is measured inside the window: two customers asking last month
    # is a different signal from two customers asking two years apart.
    rep_index = P.build_repetition_index(scope)

    rfe_scores: Dict[str, Dict[str, Any]] = {}
    for r in scope:
        key = f"{r['_domain']}||{r.get('_cluster', '')}"
        s = P.score_rfe(r, rep_index.get(key), now=now)
        rfe_scores[r["case_number"]] = {
            "score": s["score"],
            "band": s["band"]["key"],
            "band_label": s["band"]["label"],
            "action": s["band"]["action"],
            "why": s["why"],
            "customers": s["customers"],
            "requests": s["requests"],
            "severity_rank": s["severity_rank"],
            "age_days": s["age_days"],
        }

    # ── Clusters ─────────────────────────────────────────────────────────────
    by_cluster: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
    for r in scope:
        by_cluster[(r["_domain"], r.get("_cluster", ""))].append(r)

    clusters: List[Dict[str, Any]] = []
    for (domain, name), members in by_cluster.items():
        g = P.score_group(members, now=now)
        cluster = {
            "id": f"{DOMAIN_PAGE_ID.get(domain, 'other')}--{abs(hash(name)) % 10**9}",
            "name": name,
            "domain": domain,
            "domain_id": DOMAIN_PAGE_ID.get(domain, "other"),
            "cases": [m["case_number"] for m in members],
            "top_accounts": _account_rollup(members, 3),
            "monthly": _monthly_counts(members),
            # The member records themselves, for the narrative writers only —
            # popped before the payload is built so the page never carries the
            # same record twice.
            "_records": members,
        }
        cluster.update({k: g[k] for k in (
            "score", "band", "components", "requests", "customers", "arr", "flagged",
            "flag_reasons", "severity_mix", "top_severity", "max_repeats",
            "recent_90", "recent_180", "median_age_days", "why")})
        cluster["rationale"] = N.epic_rationale(cluster)
        clusters.append(cluster)
    clusters.sort(key=P.sort_key("priority"), reverse=True)

    # ── Domains ──────────────────────────────────────────────────────────────
    by_domain: Dict[str, List[Dict]] = defaultdict(list)
    for r in scope:
        by_domain[r["_domain"]].append(r)

    prev_cut = now - timedelta(days=180)
    domains: List[Dict[str, Any]] = []
    for domain in DOMAIN_ORDER:
        members = by_domain.get(domain, [])
        g = P.score_group(members, now=now)
        dom_clusters = [c for c in clusters if c["domain"] == domain]
        recent_90 = g["recent_90"]
        prior_90 = sum(1 for r in members
                       if (lambda a: a is not None and 90 < a <= 180)(P.age_days(r, now)))
        dom = {
            "id": DOMAIN_PAGE_ID.get(domain, "other"),
            "domain": domain,
            "clusters": dom_clusters,
            "cluster_count": len(dom_clusters),
            "band_counts": _band_counts(dom_clusters),
            "top_accounts": _account_rollup(members, 5),
            "monthly": _monthly_counts(members),
            "prior_90": prior_90,
        }
        dom.update({k: g[k] for k in (
            "score", "band", "components", "requests", "customers", "arr", "flagged",
            "flag_reasons", "severity_mix", "top_severity", "max_repeats",
            "recent_90", "recent_180", "median_age_days", "why")})
        dom["narrative"] = N.domain_narrative(dom)
        dom["interpretation"] = N.domain_interpretation(dom)
        dom["actions"] = N.domain_actions(dom)
        # `clusters` was needed by the narrative writers but would double the
        # payload if embedded here — the page looks clusters up by domain_id.
        dom.pop("clusters", None)
        domains.append(dom)

    # ── Portfolio totals ─────────────────────────────────────────────────────
    accounts = {r["account_name"] for r in scope if r["account_name"]}
    zero_arr = len({r["account_name"] for r in scope
                    if r["account_name"] and not float(r["account_arr"] or 0)})
    active = [d for d in domains if d["requests"]]
    ranked_domains = sorted(active, key=P.sort_key("priority"), reverse=True)
    weighted = sum(d["score"] * d["requests"] for d in active) or 1
    top3_share = round(100 * sum(d["score"] * d["requests"]
                                 for d in ranked_domains[:3]) / weighted)
    drop_clusters = [c for c in clusters if c["band"]["key"] == "drop"]

    totals = {
        "requests": len(scope),
        "customers": len(accounts),
        "arr": P.distinct_account_arr(scope),
        "cluster_count": len(clusters),
        "active_domains": len(active),
        "flagged": sum(1 for r in scope if P.is_business_impact(r)),
        "critical_high": sum(1 for r in scope
                             if P.normalise_severity(r.get("severity")) in ("critical", "high")),
        "band_counts": _band_counts(clusters),
        "start_now_requests": sum(c["requests"] for c in clusters
                                  if c["band"]["key"] == "start_now"),
        "start_now_arr": sum(c["arr"] for c in clusters if c["band"]["key"] == "start_now"),
        "drop_requests": sum(c["requests"] for c in drop_clusters),
        "drop_count": len(drop_clusters),
        "repeat_theme_count": sum(1 for c in clusters if c["customers"] > 1),
        "stale_count": sum(1 for r in scope
                           if (P.age_days(r, now) or 0) > 365),
        "recent_90": sum(1 for r in scope
                         if (lambda a: a is not None and a <= 90)(P.age_days(r, now))),
        "top3_share": top3_share,
        "zero_arr_accounts": zero_arr,
        # Non-USD ARR is reported, never converted: inventing an exchange rate
        # would put a number on a leadership page that no system can reproduce.
        "non_usd": sum(1 for r in scope if (r.get("arr_currency") or "USD").strip().upper()
                       not in ("", "USD")),
        "missing_dates": sum(1 for r in scope if not r["created_date"]),
        "missing_severity": sum(1 for r in scope
                                if not P.normalise_severity(r.get("severity"))),
        "monthly": _monthly_counts(scope),
    }

    model = {"totals": totals, "domains": domains, "clusters": clusters,
             "scores": rfe_scores}
    model["narrative"] = {
        "headline": N.exec_headline(model),
        "paragraphs": N.exec_narrative(model),
        "data_quality": N.data_quality_notes(model),
    }

    # Every narrative is written by this point, so the raw member records can go.
    for c in clusters:
        c.pop("_records", None)
    return model


def build_model(db_path: str, run_id: Optional[str] = None,
                progress=None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Load, classify, cluster, score and narrate — the whole data layer.

    Split out from `generate_report` so tests and calibration scripts can assert
    on the numbers without rendering a megabyte of HTML.
    """
    def tick(stage: str, pct: int) -> None:
        if progress:
            try:
                progress(stage, pct)
            except Exception:
                pass       # progress reporting must never break a report

    now = now or datetime.utcnow()

    tick("Loading your RFEs…", 10)
    records = load_data(db_path, run_id)
    if not records:
        return {"empty": True}

    tick(f"Writing PM decision summaries for {len(records)} requests…", 22)
    pm_summary.backfill_descriptions(db_path, records, run_id)
    for r in records:
        r["_domain"] = classify_domain(r["domain"], r["sub_domain"],
                                       r["subject"], r["description"])
        r["section"] = PM_SECTION_OF.get(r["_domain"], "platform")
    pm_summary.ensure_summaries(db_path, records)

    tick(f"Grouping {len(records)} requests into themes…", 45)
    cluster_records(records)

    tick("Ranking by severity, ARR, business impact and repetition…", 62)
    frames: Dict[str, Dict[str, Any]] = {}
    for key, days, _label in TIMEFRAMES:
        cutoff = None if not days else now - timedelta(days=days)
        frames[key] = build_timeframe(records, cutoff, now)

    tick("Writing the executive and domain summaries…", 80)
    dates = sorted(r["created_date"] for r in records if r["created_date"])
    meta = {
        "generated": now.strftime("%Y-%m-%d %H:%M UTC"),
        "record_count": len(records),
        "date_range": f"{dates[0][:7]} – {dates[-1][:7]}" if dates else "",
        "default_tf": DEFAULT_TIMEFRAME,
        "timeframes": [{"key": k, "label": l} for k, _d, l in TIMEFRAMES],
        "sort_fields": [{"key": k, "label": l, "help": h} for k, l, h in P.SORT_FIELDS],
        "domains": [{"id": DOMAIN_PAGE_ID[d], "name": d} for d in DOMAIN_ORDER],
        "bands": [{"key": k, "label": l, "action": a} for k, l, a, _f in P.BANDS],
    }

    return {"meta": meta, "records": _client_records(records, frames), "tf": frames}


def _client_records(records: Sequence[Dict],
                    frames: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The per-RFE payload the page renders from.

    `pm` is the written PM Decision Summary; `original` is the customer's own
    Salesforce text, carried separately and rendered only behind an explicitly
    labelled expander. Keeping them in different fields is what makes it
    structurally impossible for a raw description to appear as a summary.
    """
    membership = {key: set(frame["scores"]) for key, frame in frames.items()}
    out = []
    for r in records:
        case = r["case_number"]
        out.append({
            "case": case,
            "subject": r["subject"],
            "account": r["account_name"],
            "arr": float(r["account_arr"] or 0),
            "severity": P.severity_label(r["severity"]),
            "severity_key": P.normalise_severity(r["severity"]),
            "bi": P.is_business_impact(r),
            "bi_reason": P.business_impact_reason(r),
            "opened": r["created_date"],
            "status": r["status"],
            "owner": (r.get("case_owner") or "").strip(),
            "domain": r["_domain"],
            "domain_id": DOMAIN_PAGE_ID.get(r["_domain"], "other"),
            "cluster": r.get("_cluster", ""),
            "sub_domain": r["sub_domain"],
            "pm": _pm_text(r),
            "original": r["description"],
            "tfs": [key for key in membership if case in membership[key]],
        })
    return out


# ── Public entry point ───────────────────────────────────────────────────────

def generate_report(db_path: str, run_id: Optional[str] = None, progress=None) -> str:
    """Generate the standalone HTML PI Planning report.

    `run_id` selects an uploaded dataset; omit it for the shared latest run.
    `progress` is an optional callable(stage_text, pct) driving the upload UI.
    """
    model = build_model(db_path, run_id, progress=progress)
    if model.get("empty"):
        return A.EMPTY_REPORT

    if progress:
        try:
            progress("Rendering the report…", 90)
        except Exception:
            pass
    return A.render(model)


def safe_json(obj: Any) -> str:
    """JSON for embedding in a <script> tag."""
    return json.dumps(obj, ensure_ascii=False).replace("</script", r"<\/script")
