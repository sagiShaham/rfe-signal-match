"""
Weekly Analysis Report Generator
================================
Implements the Cynet weekly/bi-weekly RFE report as a native platform tab.

Purpose: help PMs decide which **state** to assign each RFE (backlog vs.
out-of-scope vs. needs info) without opening Salesforce.

Reads RFE data from the SQLite DB (no file upload), classifies each RFE into one
of 10 product-domain sections, computes a priority score, groups every RFE into
a thematic cluster, and renders a single self-contained light-mode HTML report
with Plotly charts.

PM Decision Summaries are written for every RFE while the report is being
generated (see `scoring/pm_summary.py`) — no button to press, no API key needed.
Per the skill's absolute rules, a raw Salesforce description is NEVER used as a
summary — when no description exists, the mandated TAM-follow-up flag is shown.

LIGHT MODE ONLY. Never use dark backgrounds.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from scoring import pm_summary
from scoring import priority as P
from scoring import pi_report_generator as PIG
from scoring import report_ui

# ── Sections (skill: 10 domains + Executive) ─────────────────────────────────

SECTIONS: List[Tuple[str, str, str]] = [
    ("exec",        "Executive Overview",  "🏠"),
    ("epp",         "EPP",                 "🛡️"),
    ("wac",         "Web Access Control",  "🌐"),
    ("email",       "Email Security",      "📧"),
    ("siem",        "SIEM",                "📊"),
    ("identity",    "Identity",            "🔐"),
    ("cspm",        "CSPM",                "☁️"),
    ("platform",    "Platform",            "🖥️"),
    ("reporting",   "Reporting",           "📋"),
    ("automations", "Automations",          "⚙️"),
    ("ai",          "AI Initiatives",      "🤖"),
]
DOMAIN_IDS = [sid for sid, _, _ in SECTIONS if sid != "exec"]
SECTION_NAME = {sid: name for sid, name, _ in SECTIONS}
SECTION_EMOJI = {sid: emoji for sid, _, emoji in SECTIONS}

# ── Product Domain (Salesforce) → section ────────────────────────────────────
SF_DOMAIN_MAP = {
    "endpoint": "epp",
    "epp": "epp",
    "siem": "siem",
    "clm": "siem",
    "platform": "platform",
    "ux/ui": "platform",
    "alert management": "platform",
    "user and site management": "platform",
    "user management": "platform",
    "group settings": "platform",
    "on-prem": "platform",
    "on prem": "platform",
    "mobile": "platform",
    "automation": "automations",
    "automations": "automations",
    "product tools": "automations",
    "actions, playbooks, and integrations": "automations",
    "cloud": "cspm",
    "cspm": "cspm",
    "sspm": "cspm",
    "espm": "cspm",
    "email": "email",
    "identity": "identity",
    "reporting": "reporting",
    "reports": "reporting",
    "web access control": "wac",
}

# ── Subject/description keyword → section (used when domain is blank/Other) ──
# Order matters: earlier patterns win. WAC and AI must precede the broader
# Platform/EPP patterns so they aren't swallowed.
KEYWORD_SECTIONS: List[Tuple[str, str]] = [
    ("ai", r"\bmcp\b|mcp server|model context protocol|\bai agent|\bllm\b|genai|gen ai|claude integration|artificial intelligence"),
    ("wac", r"web content filter|\bwcf\b|file-filter|file filter|domain category|blocklist.*categor|categor.*blocklist"),
    ("email", r"email security|email digest|quarantine|release request|email.*allowlist|email.*blocklist|email api|add email security|spam detection|\barc\b.*\bsrs\b|\bsrs\b|dkim|link protection"),
    ("siem", r"\bclm\b|\bsiem\b|log collection|log source|field indexing|logstash|syslog|google workspace data|cloudflare|\buba\b|windows events correlation|netdocuments|log management|correlat"),
    ("identity", r"active directory|entra id|\bldap\b|\bsso\b|mfa for admin|identity.*alert|suspicious login|account lockout|\bitdr\b|user security posture"),
    ("cspm", r"\bsspm\b|\bcspm\b|aws misconfig|aws region|cloud misconfig|cloud account|aws security data lake|xdr allowlist|assume.*role|alibaba|huawei cloud|azure.*misconfig|disable user.*on prem"),
    ("automations", r"playbook|ninjaone|ninjarmm|ninja integration|connectwise|\bpsa\b|\brmm\b|alert comments.*api|vmware nsx|msp hierarchy|custom remediation|auto-rem|remediation script|\bdatto\b|autotask|it glue"),
    ("reporting", r"scheduled report|quarterly period|report branding|report logo|customize report|generate report|av scan report|executive report|all-in-one report|digest report"),
    ("epp", r"antivirus|\bav scan\b|full av|\bepp\b|\bedr\b|endpoint protect|linux|macos|mac agent|vulnerabilit|\bcve\b|\bepss\b|kubernetes|container|\busb\b|optical drive|cd/dvd|storage device|host migration|tenant migration|agent uninstall|rollback|threat hunting|deception|responder dns|inactive agent|non-deployed|non deployed|full disk access|windows arm|arm device|tray icon|menu bar icon|anti-tamper|anti-tempering|\bndr\b|network detection|isolat|file deletion|uninstall password|\bmalware\b|\bsandbox\b|bitlocker|remote wipe|device control|agent pause"),
    ("platform", r"tenant search|console lockdown|ip restriction|site settings template|template settings|cyops|white-label|white label|branding|logo|device migration|mssp|timezone|time zone|remove group|delete group|\bapi\b|dashboard|\bmsi\b|multi-site|multi site|reverse proxy|exclusion catalog|global.*setting|global.*admin|\brbac\b|custom role|permission"),
]

# API disambiguation (skill "Difficult Classification Cases")
API_RULES: List[Tuple[str, str]] = [
    ("automations", r"api.*(remote )?agent uninstall|uninstall.*api|alert comments.*api"),
    ("email",       r"api.*email security|email security.*api"),
    ("wac",         r"api.*wcf|wcf.*api|global rules management via api"),
    ("platform",    r"api.*(endpoint list|billing|usage|vulnerability)|endpoint list.*api"),
]

SEV_WEIGHT = {"Critical": 5, "High": 4, "Medium": 2, "Low": 1}

STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "by",
    "with", "from", "as", "is", "are", "be", "has", "have", "that", "this",
    "it", "its", "can", "will", "cynet", "rfe", "request", "feature",
    "ability", "support", "add", "allow", "enable", "new", "improve",
    "enhance", "update", "please", "external", "inquiry", "option", "when",
    "not", "all", "via", "per", "we", "our", "should", "would", "need",
}

# Strips leading noise tags from Salesforce subjects: "[RFE]", "[EXTERNAL]",
# "[AccountName]", "RFE:", "Feature request:" — repeatedly, in any order.
SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:\[[^\]]{1,24}\]|rfe|feature request)\s*[:\-]?\s*)+",
    re.IGNORECASE,
)

# ── Thematic cluster patterns per section (skill: 6–12 clusters, 100% cover) ─
CLUSTER_THEMES: Dict[str, List[Tuple[str, str]]] = {
    "epp": [
        ("Kubernetes & Container Coverage", r"kubernetes|container|\bk8s\b"),
        ("AV Scan — macOS & Linux",         r"av scan|antivirus|full scan|on-demand scan|scan schedul"),
        ("USB & Storage Device Control",    r"\busb\b|storage device|optical|cd/dvd|device control"),
        ("Vulnerability & CVE Detection",   r"vulnerabilit|\bcve\b|\bepss\b|patch"),
        ("Agent Deployment & Installation", r"deploy|install|\bmsi\b|uninstall|rollback|migration|arm"),
        ("Threat Hunting & Forensics",      r"threat hunting|forensic|hunt|query"),
        ("Allowlist, Exclusions & Rules",   r"allowlist|whitelist|exclusion|exclude|block"),
        ("Endpoint Visibility & Agent UX",  r"tray icon|menu bar|inactive|non-deployed|visibility|console|status"),
        ("Isolation & Response Actions",    r"isolat|contain|remediat|kill|quarantin"),
    ],
    "wac": [
        ("WCF Policy & Category Management", r"categor|policy|profile|select all"),
        ("Blocklist & Allowlist Rules",      r"blocklist|allowlist|block|allow"),
        ("WCF API & Automation",             r"\bapi\b|automat"),
        ("WCF Alerting & Reporting",         r"alert|report|log"),
    ],
    "email": [
        ("Quarantine & Release Workflow",  r"quarantin|release request|release"),
        ("Allowlist & Blocklist",          r"allowlist|blocklist|allow|block|sender"),
        ("Digest & Notifications",         r"digest|notif|alert"),
        ("Email API & Permissions",        r"\bapi\b|permission|\brbac\b|role"),
        ("Authentication (ARC / SRS / DKIM)", r"\barc\b|\bsrs\b|dkim|spf|dmarc|authenticat"),
        ("Investigation & Console UX",     r"console|\bui\b|view|search|investigat"),
    ],
    "siem": [
        ("CLM Field Indexing & Retention", r"field index|index|retention|storage"),
        ("New Log Source Integrations",    r"log source|integrat|google workspace|cloudflare|netdocuments|\bapi\b"),
        ("Log Collection & Forwarding",    r"log collection|collect|logstash|syslog|forward"),
        ("Query, Pagination & Search",     r"quer|paginat|search|filter|over 100"),
        ("UBA & Correlation Rules",        r"\buba\b|correlat|detection rule|windows events"),
    ],
    "identity": [
        ("Active Directory Integration",  r"active directory|\bad\b|\bldap\b|domain"),
        ("Entra ID & SSO",                r"entra|azure ad|\bsso\b|saml"),
        ("MFA & Access Control",          r"\bmfa\b|multi-factor|lockout|password"),
        ("Identity Alerts & Posture",     r"alert|posture|suspicious|risk"),
    ],
    "cspm": [
        ("SSPM / SaaS Posture",            r"\bsspm\b|saas|m365|google workspace|salesforce"),
        ("AWS Coverage & Misconfiguration", r"\baws\b|region|misconfig|data lake"),
        ("Cloud Account Onboarding",       r"account|connect|verif|disconnect|onboard"),
        ("Multi-Cloud Support",            r"azure|\bgcp\b|alibaba|huawei|oracle"),
        ("Cloud Findings & Remediation",   r"finding|remediat|alert|report"),
    ],
    "platform": [
        ("Console Access Control & Lockdown", r"lockdown|ip restrict|access|login|session"),
        ("Tenant & Site Management",          r"tenant|site|multi-site|group|template|migration"),
        ("MSSP & Multi-Tenant Administration", r"mssp|\bmsp\b|centraliz|hierarchy|cross-site"),
        ("API & Integrations Platform",       r"\bapi\b|integrat|webhook|billing|usage"),
        ("Alert UI & Triage Experience",      r"alert|triage|\bui\b|view|filter|tag"),
        ("Roles, Permissions & RBAC",         r"\brbac\b|role|permission|user right|admin"),
        ("Branding & White-Label",            r"brand|white-label|white label|logo|co-brand"),
        ("Dashboard, Search & Console UX",    r"dashboard|search|console|timezone|\bux\b|navigat"),
    ],
    "reporting": [
        ("Scheduled & Periodic Reports", r"schedul|quarterly|weekly|monthly|periodic|automat"),
        ("Report Branding & Templates",  r"brand|logo|template|customiz|white"),
        ("Report Content & Coverage",    r"content|includ|detail|misconfig|vulnerabilit|remediat|\bav\b"),
        ("Export & Delivery",            r"export|\bpdf\b|\bcsv\b|excel|email|deliver"),
    ],
    "automations": [
        ("PSA/RMM Integrations (NinjaOne, ConnectWise)", r"ninja|connectwise|\bpsa\b|\brmm\b|datto|autotask|it glue"),
        ("Playbook Scope & Hierarchy",   r"playbook|global|hierarch|template|scope"),
        ("Custom Remediation & Scripts", r"remediat|script|action|command"),
        ("API Completeness",             r"\bapi\b|endpoint|webhook|comment"),
        ("Third-Party Response Actions", r"nsx|vmware|firewall|block.*ip|integrat"),
    ],
    "ai": [
        ("MCP Server & Protocol Support", r"\bmcp\b|model context protocol"),
        ("AI Agents & Assistants",        r"\bai agent|assistant|copilot|agentic"),
        ("LLM Integration & Governance",  r"\bllm\b|genai|gen ai|unauthorized|governance|claude|openai"),
    ],
}

DECISIONS = [
    ("backlog",      "Add to Backlog"),
    ("out_of_scope", "Out of Scope"),
    ("needs_info",   "Needs Info"),
    ("deferred",     "Deferred"),
]
DECISION_LABEL = {k: v for k, v in DECISIONS}

NO_DESC_SUMMARY = pm_summary.NO_DESC_SUMMARY


# ══════════════════════════════════════════════════════════════════════════════
# Data loading
# ══════════════════════════════════════════════════════════════════════════════

def _parse_date(raw: str) -> Optional[datetime]:
    """Parse Salesforce date formats seen in the export."""
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:len(fmt) + 4].strip(), fmt)
        except ValueError:
            continue
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return None


def clean_subject(subject: str) -> str:
    return SUBJECT_PREFIX_RE.sub("", subject or "").strip() or (subject or "").strip()


def classify_section(sf_domain: str, subject: str, description: str) -> str:
    """Map an RFE to one of the 10 report sections. Default: platform."""
    text = f"{subject} {description}".lower()

    # API requests are ambiguous — resolve them first (skill: difficult cases)
    if "api" in text:
        for section, pattern in API_RULES:
            if re.search(pattern, text):
                return section

    domain = (sf_domain or "").strip().lower()
    mapped = SF_DOMAIN_MAP.get(domain)
    if mapped:
        # MSP + API subject → Platform; MSP + automation subject → Automations
        if domain == "msp" and re.search(r"playbook|ninja|connectwise|\brmm\b|\bpsa\b", text):
            return "automations"
        # A blank-ish bucket shouldn't override a strong keyword signal
        if mapped == "platform" and domain in ("platform", "ux/ui", "alert management",
                                               "user and site management", "group settings",
                                               "on-prem", "on prem", "mobile", "user management"):
            for section, pattern in KEYWORD_SECTIONS:
                if section in ("wac", "ai", "email", "siem", "identity") and re.search(pattern, text):
                    return section
        return mapped

    for section, pattern in KEYWORD_SECTIONS:
        if re.search(pattern, text):
            return section
    return "platform"


def load_rfes(db_path: str, run_id: str | None = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Load a run's RFEs, backfilling descriptions from other runs.

    Pass `run_id` to report on a specific dataset — e.g. a CSV a PM uploaded to
    the Weekly Analysis tab, which is deliberately independent of the shared
    Signal Match dump. With no `run_id`, the latest shared run is used.

    The Salesforce SOQL pull does not always include Description. When the
    chosen run lacks one, the same case_number from another run (e.g. a CSV
    import) is used so PM summaries can still be written.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    meta: Dict[str, Any] = {}
    try:
        if run_id:
            # An uploaded report dataset has no run_meta row by design — it must
            # not become "the latest run" and hijack Signal Match.
            row = conn.execute(
                "SELECT run_id, started_at, rfe_count, source FROM run_meta WHERE run_id=?",
                (run_id,),
            ).fetchone()
            meta = dict(row) if row else {"run_id": run_id}
        else:
            row = conn.execute(
                "SELECT run_id, started_at, rfe_count, source FROM run_meta "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                return [], meta
            meta = dict(row)
            run_id = row["run_id"]

        # Business Impact is the only field in the export where a person states a
        # consequence, so the weekly report reads it for the same reason the PI
        # report does. Selected only when present, so a report built against a
        # database that predates the migration still works.
        have = {row[1] for row in conn.execute("PRAGMA table_info(rfe_pulls)")}
        extra = [c for c in ("business_impact", "business_impact_reason",
                             "case_owner", "arr_currency") if c in have]
        cols = ("case_number, subject, description, account_name, account_arr, "
                "status, domain, sub_domain, severity, created_date"
                + ("".join(", " + c for c in extra)))
        rows = conn.execute(
            f"SELECT {cols} FROM rfe_pulls WHERE run_id=?", (run_id,)
        ).fetchall()

        # Cross-run description backfill (case_number -> longest description)
        backfill: Dict[str, str] = {}
        for r in conn.execute(
            "SELECT case_number, description FROM rfe_pulls "
            "WHERE run_id!=? AND LENGTH(TRIM(COALESCE(description,'')))>20",
            (run_id,),
        ):
            cn = (r["case_number"] or "").strip()
            desc = (r["description"] or "").strip()
            if cn and len(desc) > len(backfill.get(cn, "")):
                backfill[cn] = desc

        summaries = _load_summaries(conn)
        decisions = _load_decisions(conn)
    finally:
        conn.close()

    seen, records = set(), []
    backfilled = 0
    for r in rows:
        cn = (r["case_number"] or "").strip()
        subj = (r["subject"] or "").strip()
        if not cn or not subj or cn in seen:
            continue
        seen.add(cn)

        desc = (r["description"] or "").strip()
        if len(desc) <= 20 and cn in backfill:
            desc = backfill[cn]
            backfilled += 1

        # No default: an unset severity is recorded as unset. Writing "Low"
        # here asserts something the export does not say, and it is the
        # reading the PI report deliberately avoids — unset usually means
        # nobody triaged it, not that it is harmless.
        sev = (r["severity"] or "").strip()
        opened = _parse_date(r["created_date"] or "")
        records.append({
            "case_number": cn,
            "subject": clean_subject(subj),
            "raw_subject": subj,
            "description": desc,
            "account_name": (r["account_name"] or "").strip() or "Unknown",
            "arr": float(r["account_arr"] or 0),
            "status": (r["status"] or "").strip(),
            "sf_domain": (r["domain"] or "").strip(),
            "sub_domain": (r["sub_domain"] or "").strip(),
            # An unrecognised severity used to be rewritten as "Low", which is a
            # claim the data does not make. It is kept as-is and scored just
            # below Medium, exactly as the PI report does.
            "severity": sev if sev in SEV_WEIGHT else (sev or ""),
            "opened": opened,
            "business_impact": (r["business_impact"]
                                if "business_impact" in r.keys() else ""),
            "business_impact_reason": (r["business_impact_reason"]
                                       if "business_impact_reason" in r.keys() else "") or "",
            "arr_currency": (r["arr_currency"] if "arr_currency" in r.keys() else "") or "",
            "case_owner": (r["case_owner"] if "case_owner" in r.keys() else "") or "",
            # scoring.priority reads these names; the weekly report uses its own.
            "account_arr": float(r["account_arr"] or 0),
            "created_date": (r["created_date"] or ""),
            "pm_summary": summaries.get(cn, ""),
            "decision": decisions.get(cn, {}).get("decision", ""),
            "decision_note": decisions.get(cn, {}).get("note", ""),
        })

    meta["backfilled_descriptions"] = backfilled
    meta["with_description"] = sum(1 for r in records if len(r["description"]) > 20)
    return records, meta


# ══════════════════════════════════════════════════════════════════════════════
# Scoring, trend, clustering
# ══════════════════════════════════════════════════════════════════════════════

def report_date_for(records: List[Dict[str, Any]]) -> Tuple[datetime, bool]:
    """Today, or the dataset's most recent date when generating retroactively.

    The skill allows the latest dataset date so the NEW / GROWING trend badges
    stay meaningful on an older export instead of collapsing to all-STABLE.
    """
    today = datetime.today()
    dates = [r["opened"] for r in records if r["opened"]]
    if not dates:
        return today, False
    newest = max(dates)
    if (today - newest).days > 14:
        return newest, True
    return today, False


def priority_score(rfe: Dict[str, Any], report_date: datetime) -> float:
    """Deprecated: the weekly report's own ad-hoc score.

    Kept only so an older caller does not break. It read ARR, severity and
    recency on an unbounded scale with no business impact and no repetition,
    which meant the weekly report and the PI Planning report could rank the same
    request differently. Both now use `scoring.priority.score_rfe` — see
    `score_records()` below.
    """
    arr_score = (rfe["arr"] / 50_000.0) * 2
    sev_score = SEV_WEIGHT.get(rfe["severity"], 1) * 3
    days = rfe["_days"]
    recency = (4 if days <= 7 else 2 if days <= 14 else 1) * 2
    return arr_score + sev_score + recency


CATCH_ALL_TITLES = {"Other Requests in this Domain", "Other Requests"}


def is_catch_all(cluster: Dict[str, Any]) -> bool:
    """Is this the leftover bucket rather than a real theme?

    `build_clusters` sweeps everything that matched no pattern and no sibling
    into one bucket so the cluster count stays readable. That bucket is not a
    theme: its members have nothing in common beyond not fitting elsewhere, so
    it must not collect repetition credit or be ranked as though 25 customers
    had asked for the same thing.
    """
    return cluster.get("title", "") in CATCH_ALL_TITLES


def score_records(records: List[Dict[str, Any]],
                  clusters_by_section: Dict[str, List[Dict[str, Any]]],
                  report_date: datetime) -> None:
    """Attach the PI Priority score, band and drivers to every record, in place.

    Repetition is the one signal that cannot be read off a single row, so it is
    measured over the weekly report's own clusters: a request inherits the
    distinct-customer count of the theme it belongs to. That is the same
    definition the PI report uses — breadth across accounts, kept separate from
    one account repeating — just computed over this report's grouping.

    Called after clustering, which is why it is not folded into the first pass
    over `records`.
    """
    # Repetition is measured over the SAME grouping the PI Planning report uses —
    # requests asking for the same thing, matched on subject similarity — not
    # over this report's display clusters.
    #
    # The two groupings answer different questions and must not be confused. The
    # weekly report groups into 6-12 broad thematic areas per domain with 100%
    # coverage, because its job is to walk a domain's work end to end; "Allowlist,
    # Exclusions & Rules" legitimately spans 17 accounts. The PI report groups by
    # what was actually asked for. If repetition came from the thematic areas, the
    # same case would read "17 customers" in one report and "2" in the other, and
    # a reader would be right to distrust both. So the tighter clustering is run
    # here purely to measure repetition; the thematic areas still drive the layout.
    # Cluster inside the PI report's domain partition, not this report's ten
    # sections. Clustering only ever compares requests within one domain, so if
    # the partitions differ the clusters differ — and measuring showed exactly
    # that: repetition matched on only 76% of cases, almost all of it caused by
    # the two taxonomies splitting a domain differently (this report keeps Web
    # Access Control separate; the PI report folds it into EPP). Using the same
    # partition makes "how many customers asked for this" one number across both
    # reports. The ten display sections are untouched.
    scope = [dict(r, _domain=PIG.classify_domain(r["sf_domain"], r.get("sub_domain", ""),
                                                 r["subject"], r["description"]))
             for r in records]
    PIG.cluster_records(scope)
    cluster_of = {r["case_number"]: (r["_domain"], r.get("_cluster", ""))
                  for r in scope}
    for r in records:
        key = cluster_of.get(r["case_number"])
        r["_domain"], r["_cluster"] = key if key else (r["_section"], r["subject"])
    rep_index = P.build_repetition_index(records)
    rep_by_case = {r["case_number"]: rep_index[f"{r['_domain']}||{r['_cluster']}"]
                   for r in records}

    singleton = {"requests": 1, "customers": 1, "max_repeats": 1}
    for r in records:
        scored = P.score_rfe(r, rep_by_case.get(r["case_number"], singleton),
                             now=report_date)
        r["_pi"] = scored
        r["_score"] = scored["score"]          # the sort key the report already uses
        r["_band"] = scored["band"]
        r["_customers"] = scored["customers"]
        r["_requests"] = scored["requests"]
        r["_why"] = scored["why"]

    # A cluster's score follows its members, so the ranking a reader sees at
    # theme level is the same number, aggregated the same way as the PI report.
    for section, clusters in clusters_by_section.items():
        for cluster in clusters:
            members = cluster["members"]
            group = P.score_group(members, now=report_date)
            # A thematic area is a reading group, not one request, so it is
            # ranked by the strongest thing inside it rather than by the breadth
            # of the area — otherwise a wide bucket outranks a sharp signal.
            best = max((m.get("_pi") or {"score": 0, "band": group["band"]}
                        for m in members), key=lambda x: x["score"])
            group = dict(group, score=best["score"], band=best["band"],
                         customers=max((m.get("_customers", 1) for m in members),
                                       default=1))
            if is_catch_all(cluster):
                group["why"] = ("a bucket of unrelated requests that did not fit a "
                                "theme — read them individually")
            cluster["pi"] = group
            cluster["score"] = group["score"]
            cluster["band"] = group["band"]
            cluster["customers"] = group["customers"]
            cluster["arr"] = group["arr"]        # distinct accounts, never per-row
            cluster["flagged"] = group["flagged"]
            cluster["why"] = group["why"]
            cluster["catch_all"] = is_catch_all(cluster)


def trend_of(days: int) -> str:
    if days <= 7:
        return "NEW"
    if days <= 14:
        return "GROWING"
    return "STABLE"


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z]{3,}", (text or "").lower())
            if w not in STOPWORDS}


def _label_from_tokens(rfes: List[Dict[str, Any]]) -> str:
    """Human-readable label for an unmatched group, from its commonest tokens."""
    counts: Dict[str, int] = defaultdict(int)
    for r in rfes:
        for tok in _tokens(r["subject"]):
            counts[tok] += 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
    if not top:
        return "Other Requests"
    return " & ".join(t[0].title() for t in top) + " Requests"


def build_clusters(rfes: List[Dict[str, Any]], section: str) -> List[Dict[str, Any]]:
    """Group every RFE in a section into exactly one thematic cluster.

    Predefined themes first; whatever is left is grouped by subject-token
    similarity so coverage is 100% (skill: "no RFE left behind").
    """
    themes = CLUSTER_THEMES.get(section, [])
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    leftover: List[Dict[str, Any]] = []

    for rfe in rfes:
        text = f"{rfe['subject']} {rfe['description']}".lower()
        for title, pattern in themes:
            if re.search(pattern, text):
                buckets[title].append(rfe)
                break
        else:
            leftover.append(rfe)

    # Group leftovers by shared subject tokens (simple single-link grouping)
    groups: List[List[Dict[str, Any]]] = []
    for rfe in leftover:
        toks = _tokens(rfe["subject"])
        placed = False
        for grp in groups:
            gtoks = _tokens(grp[0]["subject"])
            union = toks | gtoks
            if union and len(toks & gtoks) / len(union) >= 0.34:
                grp.append(rfe)
                placed = True
                break
        if not placed:
            groups.append([rfe])

    # Merge singleton leftovers into one catch-all so cluster count stays sane
    singles: List[Dict[str, Any]] = []
    for grp in groups:
        if len(grp) == 1:
            singles.extend(grp)
        else:
            buckets[_label_from_tokens(grp)].extend(grp)
    if singles:
        buckets["Other Requests in this Domain"].extend(singles)

    clusters = []
    for title, members in buckets.items():
        members.sort(key=lambda r: -r["_score"])
        arr_total = sum(m["arr"] for m in members)
        accounts = sorted({m["account_name"] for m in members})
        # SEV_WEIGHT has no entry for an unset severity; scoring it 1 would
        # rank it level with Low, so it is ranked below everything that is set.
        sev = max((m["severity"] for m in members),
                  key=lambda s: SEV_WEIGHT.get(s, 0))
        min_days = min(m["_days"] for m in members)
        clusters.append({
            "title": title,
            "members": members,
            "arr": arr_total,
            "accounts": accounts,
            "severity": sev,
            "trend": trend_of(min_days),
            "score": sum(m["_score"] for m in members),
        })
    clusters.sort(key=lambda c: -c["score"])
    return clusters


# ══════════════════════════════════════════════════════════════════════════════
# PM Decision Summaries (LLM) + decisions — DB layer
# ══════════════════════════════════════════════════════════════════════════════

def ensure_tables(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pm_summaries (
                case_number TEXT PRIMARY KEY,
                summary     TEXT NOT NULL,
                model       TEXT,
                generated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS rfe_decisions (
                case_number TEXT PRIMARY KEY,
                decision    TEXT NOT NULL,
                note        TEXT,
                decided_by  TEXT,
                decided_at  TEXT
            );
        """)
        conn.commit()
    finally:
        conn.close()


def _load_summaries(conn: sqlite3.Connection) -> Dict[str, str]:
    try:
        return {r[0]: r[1] for r in
                conn.execute("SELECT case_number, summary FROM pm_summaries")}
    except sqlite3.OperationalError:
        return {}


def _load_decisions(conn: sqlite3.Connection) -> Dict[str, Dict[str, str]]:
    try:
        return {r[0]: {"decision": r[1], "note": r[2] or ""} for r in
                conn.execute("SELECT case_number, decision, note FROM rfe_decisions")}
    except sqlite3.OperationalError:
        return {}


def save_decision(db_path: str, case_number: str, decision: str,
                  note: str = "", decided_by: str = "") -> None:
    ensure_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        if decision:
            conn.execute(
                "INSERT OR REPLACE INTO rfe_decisions "
                "(case_number, decision, note, decided_by, decided_at) VALUES (?,?,?,?,?)",
                (case_number, decision, note, decided_by, datetime.utcnow().isoformat()),
            )
        else:  # empty decision clears it
            conn.execute("DELETE FROM rfe_decisions WHERE case_number=?", (case_number,))
        conn.commit()
    finally:
        conn.close()


def summary_status(db_path: str) -> Dict[str, Any]:
    """Coverage stats for the shared run — summaries are written at report time."""
    ensure_tables(db_path)
    records, meta = load_rfes(db_path)
    no_desc = [r for r in records
               if len(r["description"]) < pm_summary.MIN_DESC]
    return {
        "total_rfes": len(records),
        "with_description": meta.get("with_description", 0),
        "no_description": len(no_desc),
        "summaries_cached": sum(1 for r in records if r["pm_summary"]),
        "pending": 0,          # nothing to trigger — generation is automatic
        "backfilled_descriptions": meta.get("backfilled_descriptions", 0),
        "summary_writer": pm_summary.VERSION,
        "decided": sum(1 for r in records if r["decision"]),
        "run_id": meta.get("run_id"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# HTML rendering helpers
# ══════════════════════════════════════════════════════════════════════════════

def _esc(s: Any) -> str:
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def fmt_arr(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    if v > 0:
        return f"${v:.0f}"
    return "$0"


def _pm_summary_html(rfe: Dict[str, Any]) -> str:
    """Render the PM Decision Summary. NEVER falls back to raw description.

    Summaries are written up-front by `ensure_summaries`, so this is normally
    just a read. If one is somehow missing it is written here rather than
    showing the PM a placeholder — the report is never published incomplete.
    """
    text = (rfe.get("pm_summary") or "").strip()
    if not text:
        text = pm_summary.write_summary(rfe)
    return (f'<div class="rfe-pm-summary"><div class="rfe-pm-label">PM Decision Summary</div>'
            f'{_esc(text)}</div>')


def _decision_html(rfe: Dict[str, Any]) -> str:
    cn = _esc(rfe["case_number"])
    cur = rfe.get("decision") or ""
    btns = "".join(
        f'<button class="dec-btn{" active" if cur == key else ""}" '
        f'data-case="{cn}" data-decision="{key}" '
        f'onclick="setDecision(this)">{_esc(label)}</button>'
        for key, label in DECISIONS
    )
    clear = (f'<button class="dec-btn clear" data-case="{cn}" data-decision="" '
             f'onclick="setDecision(this)">Clear</button>' if cur else "")
    return (f'<div class="decision-row"><span class="decision-label">Assign state:</span>'
            f'{btns}{clear}<span class="decision-saved" id="saved-{cn}"></span></div>')


# Mirrors report_ui.BAND_META so a server-rendered badge is identical to the
# one the PI report builds in JavaScript — same shape, same label, same tooltip.
BAND_MARK = {"start_now": "\u25b6", "plan": "\u25c6",
             "backlog": "\u25a0", "drop": "\u25cb"}
BAND_TIP = {
    "start_now": ("<b>Start now &mdash; Commit to this PI</b><br>Score 68 or above. "
                  "Carried by more than one signal at once &mdash; severity, ARR, repeat "
                  "demand across customers, or a business impact the customer stated."),
    "plan": ("<b>Plan &mdash; Size now, commit next PI</b><br>Score 54 to 67, or lifted "
             "here because severity is Critical or the customer wrote down the business "
             "consequence."),
    "backlog": ("<b>Keep in backlog &mdash; Revisit next cycle</b><br>Score 38 to 53, or "
                "lifted here because a customer flagged business impact, because $1M or "
                "more of ARR sits behind it, or because a second customer has asked."),
    "drop": ("<b>Drop candidate &mdash; Propose closing with the customer</b><br>Below 38: "
             "one customer, no business-impact flag, limited severity and limited ARR."),
}


def _band_badge(key: str) -> str:
    """The decision badge, server-rendered."""
    if not key:
        return ""
    label = {"start_now": "Start now", "plan": "Plan",
             "backlog": "Keep in backlog", "drop": "Drop candidate"}[key]
    return (f'<span class="band {key}" data-tip="{BAND_TIP[key]}">'
            f'<span class="mark">{BAND_MARK[key]}</span>{label}</span>')


def _rfe_card(rfe: Dict[str, Any]) -> str:
    cn = _esc(rfe["case_number"])
    sev = _esc(rfe["severity"])
    opened = rfe["opened"].strftime("%b %d, %Y") if rfe["opened"] else "—"
    dec = rfe.get("decision") or ""
    dec_chip = (f'<span class="chip chip-decision">{_esc(DECISION_LABEL.get(dec, dec))}</span>'
                if dec else "")
    pi = rfe.get("_pi") or {}
    band = (rfe.get("_band") or {}).get("key", "")
    sev_key = P.normalise_severity(rfe["severity"])
    flagged = P.is_business_impact(rfe)
    days = rfe.get("_days")
    flag_chip = ('<span class="chip chip-flag" title="Business impact stated by the '
                 'customer">&#9873; Business impact</span>' if flagged else "")
    return f"""<div class="rfe-card" data-sev="{sev_key}" data-bi="{1 if flagged else 0}"
     data-arr="{int(rfe['arr'] or 0)}" data-cust="{rfe.get('_customers', 1)}"
     data-reqs="{rfe.get('_requests', 1)}" data-score="{pi.get('score', 0)}"
     data-days="{'' if days is None or days >= 999 else days}">
  <div class="rfe-card-header">
    <span class="case-badge" onclick="copyCase('{cn}')">{cn}</span>
    <span class="rfe-subject">{_esc(rfe['subject'])}</span>
    <span class="trend-badge trend-{_esc(rfe['_trend'])}">{_esc(rfe['_trend'])}</span>
  </div>
  <div class="rfe-meta-row">
    <span class="chip chip-account">{_esc(rfe['account_name'])}</span>
    <span class="chip chip-arr2">{fmt_arr(rfe['arr'])}</span>
    <span class="chip chip-sev-{sev}">{sev or 'Unset'}</span>
    <span class="chip chip-status">{_esc(rfe['status'] or '—')}</span>
    <span class="chip chip-date">{_esc(opened)}</span>
    {flag_chip}
    {dec_chip}
  </div>
  <div class="rfe-rank-row">
    <span class="pi-score" data-tip="<b>PI Priority {pi.get('score', 0)} of 100</b><br>{_esc(rfe.get('_why', ''))}">PI {pi.get('score', 0)}</span>
    {_band_badge(band)}
  </div>
  {_pm_summary_html(rfe)}
  {_decision_html(rfe)}
</div>"""


def _cluster_row(cluster: Dict[str, Any], rank: int, section: str) -> str:
    rid = f"{section}-cl-{rank}"
    rank_cls = {1: "rank-1", 2: "rank-2", 3: "rank-3"}.get(rank, "rank-n")
    read_first = '<span class="read-first">READ FIRST</span>' if rank == 1 else ""
    n_cases = len(cluster["members"])
    n_acct = len(cluster["accounts"])
    top_cases = ", ".join("#" + m["case_number"] for m in cluster["members"][:3])
    insight = (
        f"{n_cases} request{'s' if n_cases != 1 else ''} from {n_acct} "
        f"account{'s' if n_acct != 1 else ''} converge on this area, carrying "
        f"{fmt_arr(cluster['arr'])} of ARR at the highest severity of "
        f"{cluster['severity']}. "
        + ("Multiple distinct accounts asking for the same capability is a market "
           "signal — treat as one engineering track rather than separate tickets. "
           if n_acct > 1 else
           "Single-account demand — weigh account value against roadmap cost. ")
        + f"Cluster: {top_cases}"
        + (" and others." if n_cases > 3 else ".")
    )
    cards = "".join(_rfe_card(m) for m in cluster["members"])
    pi = cluster.get("pi") or {}
    band = (cluster.get("band") or {}).get("key", "")
    sev_key = P.normalise_severity(cluster.get("severity", ""))
    ages = [m.get("_days") for m in cluster["members"]
            if m.get("_days") is not None and m.get("_days") < 999]
    return f"""<div class="request-card" data-sev="{sev_key}"
     data-bi="{1 if cluster.get('flagged') else 0}"
     data-arr="{int(cluster.get('arr') or 0)}" data-cust="{cluster.get('customers', n_acct)}"
     data-reqs="{n_cases}" data-score="{pi.get('score', cluster.get('score', 0))}"
     data-days="{min(ages) if ages else ''}">
  <div class="request-header" onclick="toggleDetail('{rid}')">
    <div class="rank-badge {rank_cls}">{rank}</div>
    <div class="request-info">
      <div class="request-title-row">
        <span class="req-title">{_esc(cluster['title'])}</span>
        {read_first}
        <span class="trend-badge trend-{_esc(cluster['trend'])}">{_esc(cluster['trend'])}</span>
      </div>
      <div class="req-chips">
        <span class="chip chip-arr2">{fmt_arr(cluster['arr'])} ARR</span>
        <span class="chip chip-sev-{_esc(cluster['severity'])}">{_esc(cluster['severity'])}</span>
        <span class="chip chip-count2">{n_cases} case{'s' if n_cases != 1 else ''}</span>
        <span class="chip chip-count2">{n_acct} account{'s' if n_acct != 1 else ''}</span>
        <span class="pi-score" data-tip="<b>PI Priority {pi.get('score', 0)} of 100</b><br>{_esc(cluster.get('why', ''))}">PI {pi.get('score', 0)}</span>
        {_band_badge(band)}
        {f'<span class="chip chip-flag">&#9873; {cluster["flagged"]} business impact</span>' if cluster.get("flagged") else ""}
      </div>
      <div class="req-insight"><strong>Why it matters:</strong> {_esc(insight)}</div>
    </div>
    <div class="req-expand-btn" id="expand-{rid}">▼</div>
  </div>
  <div class="req-detail" id="{rid}">{cards}</div>
</div>"""


def _kpi_row(items: List[Tuple[str, str, str]]) -> str:
    cells = "".join(
        f'<div class="kpi-card"><div class="kpi-val {cls}">{_esc(val)}</div>'
        f'<div class="kpi-label">{_esc(label)}</div></div>'
        for val, label, cls in items
    )
    return f'<div class="kpi-row">{cells}</div>'


def _risk_table(rfes: List[Dict[str, Any]], limit: int = 10) -> str:
    by_acct: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rfes:
        by_acct[r["account_name"]].append(r)
    rows_data = []
    for acct, items in by_acct.items():
        arr = max(i["arr"] for i in items)
        high = sum(1 for i in items if i["severity"] in ("High", "Critical"))
        if arr > 100_000 or high > 0 or len(items) >= 2:
            rows_data.append((acct, arr, len(items), high, items))
    rows_data.sort(key=lambda t: (-t[1], -t[2]))
    if not rows_data:
        return '<div class="muted">No at-risk accounts in this domain.</div>'
    body = "".join(
        f"<tr><td>{_esc(a)}</td><td>{fmt_arr(arr)}</td><td>{n}</td><td>{h}</td>"
        f"<td>{', '.join('#' + _esc(i['case_number']) for i in items[:3])}</td></tr>"
        for a, arr, n, h, items in rows_data[:limit]
    )
    return (f'<table class="risk-table"><thead><tr><th>Account</th><th>ARR</th>'
            f'<th>RFEs</th><th>High/Critical</th><th>Cases</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def _actions_list(items: List[str]) -> str:
    body = "".join(
        f'<div class="action-item"><div class="action-num">{i}</div>'
        f'<div class="action-text">{_esc(t)}</div></div>'
        for i, t in enumerate(items, 1)
    )
    return f'<div class="actions-list">{body}</div>'


def _summary_list(items: List[Tuple[str, str]]) -> str:
    body = "".join(f'<li class="{cls}">{_esc(text)}</li>' for text, cls in items)
    return f'<ul class="summary-list">{body}</ul>'


def _domain_bullets(section: str, rfes: List[Dict[str, Any]],
                    clusters: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """6–7 PM-sharp bullets naming specific case numbers."""
    out: List[Tuple[str, str]] = []
    total_arr = sum(r["arr"] for r in rfes)
    highs = [r for r in rfes if r["severity"] in ("High", "Critical")]
    recent = [r for r in rfes if r["_days"] <= 14]
    top = sorted(rfes, key=lambda r: -r["_score"])[:3]
    undecided = [r for r in rfes if not r["decision"]]
    no_desc = [r for r in rfes if len(r["description"]) <= 20]

    out.append((
        f"{len(rfes)} open request{'s' if len(rfes) != 1 else ''} in "
        f"{SECTION_NAME[section]} carrying {fmt_arr(total_arr)} of account ARR, "
        f"grouped into {len(clusters)} thematic cluster"
        f"{'s' if len(clusters) != 1 else ''} covering every case.", ""))
    if clusters:
        c = clusters[0]
        out.append((
            f"Top cluster is “{c['title']}” — {len(c['members'])} case(s), "
            f"{fmt_arr(c['arr'])} ARR, "
            f"{', '.join('#' + m['case_number'] for m in c['members'][:3])}. "
            f"Start the review here.", "green"))
    if highs:
        out.append((
            f"{len(highs)} High/Critical severity request(s) need a routing decision: "
            f"{', '.join('#' + r['case_number'] for r in highs[:4])}.", "danger"))
    if top:
        r = top[0]
        out.append((
            f"Highest single-request priority is #{r['case_number']} "
            f"({r['account_name']}, {fmt_arr(r['arr'])}, {r['severity']}) — "
            f"{r['subject'][:90]}.", "warn"))
    if recent:
        out.append((
            f"{len(recent)} request(s) filed in the last 14 days: "
            f"{', '.join('#' + r['case_number'] for r in recent[:4])}. "
            f"These are the freshest signals in this domain.", ""))
    multi = [(a, n) for a, n in
             sorted(((a, sum(1 for r in rfes if r["account_name"] == a))
                     for a in {r["account_name"] for r in rfes}),
                    key=lambda t: -t[1]) if n >= 2]
    if multi:
        a, n = multi[0]
        out.append((
            f"{a} has filed {n} requests in this domain alone — concentrated "
            f"demand from one account; check for churn risk.", "warn"))
    if no_desc:
        out.append((
            f"{len(no_desc)} request(s) have no Salesforce description and cannot be "
            f"routed safely: {', '.join('#' + r['case_number'] for r in no_desc[:4])}. "
            f"Follow up with the TAM before any backlog decision.", "danger"))
    out.append((
        f"{len(undecided)} of {len(rfes)} request(s) still have no assigned state. "
        f"Use the “Assign state” buttons on each card to record "
        f"backlog / out-of-scope decisions as you review.", ""))
    return out[:8]


# ══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════════════════════

def _print_sheet(sid: str, name: str, emoji: str, rfes: List[Dict[str, Any]],
                 clusters: List[Dict[str, Any]], report_date: datetime) -> str:
    """One domain, one page — the sheet that becomes the PDF.

    Deliberately not a copy of the screen. It answers the four things a reviewer
    who was not in the meeting needs: how big is this, what has to be decided,
    which themes carry it, and which cases need an individual answer.
    """
    if not rfes:
        return ""

    accounts = {r["account_name"] for r in rfes if r.get("account_name")}
    arr = P.distinct_account_arr(rfes)
    flagged = [r for r in rfes if P.is_business_impact(r)]
    recent = [r for r in rfes if r.get("_days") is not None and r["_days"] <= 14]
    bands: Dict[str, int] = {}
    for c in clusters:
        key = (c.get("band") or {}).get("key", "backlog")
        bands[key] = bands.get(key, 0) + 1

    stat = lambda v, l: (f'<div class="ps-stat"><div class="ps-stat-v">{v}</div>'
                         f'<div class="ps-stat-l">{l}</div></div>')
    stats = "".join([
        stat(len(rfes), "requests"),
        stat(len(accounts), "customers"),
        stat(fmt_arr(arr), "ARR at stake"),
        stat(len(flagged), "business-impact flags"),
        stat(len(recent), "opened in 14 days"),
    ])

    band_order = [("start_now", "Start now"), ("plan", "Plan"),
                  ("backlog", "Keep in backlog"), ("drop", "Propose closing")]
    band_cells = "".join(
        f'<div class="ps-band ps-band-{k}"><span class="ps-mark">{BAND_MARK[k]}</span>'
        f'<span class="ps-band-n">{bands.get(k, 0)}</span>'
        f'<span class="ps-band-l">{lbl}</span></div>'
        for k, lbl in band_order)

    rows = ""
    for i, c in enumerate(clusters[:8], 1):
        band = (c.get("band") or {}).get("label", "")
        rows += (
            f'<tr><td class="ps-n">{i}</td>'
            f'<td><strong>{_esc(c["title"])}</strong>'
            f'<div class="ps-why">{_esc(c.get("why", ""))}</div></td>'
            f'<td class="ps-c">{int(c.get("score", 0))}<div class="ps-band-t">{_esc(band)}</div></td>'
            f'<td class="ps-c">{_esc(c.get("severity", "") or "Unset")}</td>'
            f'<td class="ps-c">{fmt_arr(c.get("arr", 0))}</td>'
            f'<td class="ps-c">{c.get("customers", len(c.get("accounts", [])))}</td>'
            f'<td class="ps-c">{len(c["members"])}</td></tr>')

    esc_rows = ""
    for r in sorted(flagged, key=lambda r: -(r.get("_score") or 0))[:6]:
        reason = P.business_impact_reason(r)
        esc_rows += (
            f'<tr><td class="ps-case">#{_esc(r["case_number"])}</td>'
            f'<td>{_esc(r["account_name"])}</td>'
            f'<td class="ps-c">{_esc(r["severity"] or "Unset")}</td>'
            f'<td class="ps-c">{fmt_arr(r["arr"])}</td>'
            f'<td>{_esc(r["subject"])}'
            + (f'<div class="ps-why">Stated impact: {_esc(reason)}</div>' if reason else "")
            + '</td></tr>')

    esc_block = (
        '<div class="ps-h2">Business-impact escalations &mdash; each needs an answer</div>'
        '<table class="ps-table"><thead><tr><th>Case</th><th>Account</th><th>Sev</th>'
        '<th>ARR</th><th>Request</th></tr></thead><tbody>' + esc_rows + '</tbody></table>'
    ) if esc_rows else (
        '<div class="ps-note">No request in this domain carries a customer-stated '
        'business impact.</div>')

    return f"""<div class="print-sheet" id="print-sheet-{sid}">
  <div class="ps-head">
    <div>
      <div class="ps-title">{emoji} {_esc(name)}</div>
      <div class="ps-sub">Weekly RFE review &middot; {report_date.strftime('%B %d, %Y')}</div>
    </div>
    <div class="ps-brand">Cynet Product</div>
  </div>
  <div class="ps-stats">{stats}</div>
  <div class="ps-h2">What has to be decided</div>
  <div class="ps-bands">{band_cells}</div>
  <div class="ps-h2">Themes worth discussing</div>
  <table class="ps-table ps-themes"><thead><tr>
    <th>#</th><th>Theme</th><th>PI</th><th>Sev</th><th>ARR</th><th>Cust.</th><th>Cases</th>
  </tr></thead><tbody>{rows}</tbody></table>
  {esc_block}
  <div class="ps-foot">
    Ranked by PI Priority &mdash; severity, ARR, the Salesforce business-impact flag,
    how many distinct customers ask, recency and the request subject. ARR counts each
    account once, however many requests it filed.
    Generated {report_date.strftime('%Y-%m-%d')} from the Cynet RFE platform.
  </div>
</div>"""


def _empty_page(message: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Weekly Analysis</title></head>
<body style="font-family:'Inter','Segoe UI',Arial,sans-serif;background:#f0f4fc;
color:#1a2340;padding:60px;text-align:center">
<h2 style="margin-bottom:8px">Weekly Analysis</h2>
<p style="color:#5a6a8a">{_esc(message)}</p></body></html>"""


def generate_report(db_path: str, run_id: str | None = None, progress=None) -> str:
    """Build the self-contained weekly-analysis HTML report.

    `run_id` selects an uploaded dataset; omit it for the shared latest run.
    `progress` is an optional callable(stage_text, pct) used to report what the
    build is doing, so the UI can show real status instead of a bare spinner.
    """
    def _tick(stage: str, pct: int) -> None:
        if progress:
            try:
                progress(stage, pct)
            except Exception:
                pass          # progress reporting must never break the report

    ensure_tables(db_path)
    _tick("Loading your RFEs…", 15)
    records, meta = load_rfes(db_path, run_id)
    if not records:
        return _empty_page("No RFE data found. Upload a CSV of RFEs on the "
                           "Weekly Analysis tab to generate a report.")

    report_date, retro = report_date_for(records)
    window_start = report_date - timedelta(days=14)

    for r in records:
        r["_days"] = (report_date - r["opened"]).days if r["opened"] else 999
        r["_score"] = priority_score(r, report_date)
        r["_trend"] = trend_of(r["_days"])
        r["_recent"] = r["_days"] <= 14
        r["_section"] = classify_section(r["sf_domain"], r["subject"], r["description"])

    # PM Decision Summaries are written here, as part of the build — a report is
    # never handed over with them missing (skill Step 4: write every summary
    # BEFORE generating HTML).
    _tick(f"Writing PM decision summaries for {len(records)} RFEs…", 28)
    pm_summary.ensure_summaries(db_path, records)

    by_section: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_section[r["_section"]].append(r)

    _tick(f"Grouping {len(records)} RFEs into themes…", 40)
    clusters_by_section = {}
    for i, sid in enumerate(DOMAIN_IDS):
        clusters_by_section[sid] = build_clusters(
            sorted(by_section.get(sid, []), key=lambda r: -r["_score"]), sid)
        _tick(f"Grouping {SECTION_NAME.get(sid, sid)}…",
              40 + int(30 * (i + 1) / len(DOMAIN_IDS)))

    # Score with the same engine the PI Planning report uses, now that themes
    # exist and repetition can be measured. This overwrites the provisional
    # `_score` set above.
    _tick("Ranking by severity, ARR, business impact and repetition…", 72)
    score_records(records, clusters_by_section, report_date)
    for sid in DOMAIN_IDS:
        # Catch-all last: it is a reading list, not a candidate for commitment.
        clusters_by_section[sid].sort(
            key=lambda c: (is_catch_all(c), -c["score"]))

    # One print sheet per domain — hidden on screen, shown only while printing.
    print_sheets = "".join(
        _print_sheet(sid, SECTION_NAME[sid], SECTION_EMOJI[sid],
                     by_section.get(sid, []), clusters_by_section.get(sid, []),
                     report_date)
        for sid in DOMAIN_IDS)

    # ── Sidebar ──────────────────────────────────────────────────────────────
    nav = []
    for sid, name, emoji in SECTIONS:
        count = len(records) if sid == "exec" else len(by_section.get(sid, []))
        badge = f'<span class="nav-badge">{count}</span>' if sid != "exec" else ""
        active = " active" if sid == "exec" else ""
        nav.append(
            f'<div class="nav-btn{active}" id="nav-{sid}" onclick="showSection(\'{sid}\')">'
            f'<span>{emoji}</span><span>{_esc(name)}</span>{badge}</div>'
        )
    label = "SECTIONS"
    sidebar = f"""<div id="sidebar">
  <div class="sidebar-brand">
    <div class="brand-name">Weekly Analysis</div>
    <div class="report-date">{report_date.strftime('%B %d, %Y')}</div>
    <div class="report-period">14-DAY WINDOW: {window_start.strftime('%b %d')} – {report_date.strftime('%b %d')}</div>
    {'<div class="report-period">RETROACTIVE — dated from newest RFE</div>' if retro else ''}
  </div>
  <div class="sidebar-section-label">{label}</div>
  {''.join(nav)}
</div>"""

    # ── Executive page ───────────────────────────────────────────────────────
    total_arr = sum(r["arr"] for r in records)
    recent_all = [r for r in records if r["_recent"]]
    highs_all = [r for r in records if r["severity"] in ("High", "Critical")]
    top_arr = max((r["arr"] for r in records), default=0)
    decided = [r for r in records if r["decision"]]

    exec_kpis = _kpi_row([
        (str(len(records)), "Total RFEs", "blue"),
        (str(len(recent_all)), "Filed ≤ 14 Days", "teal"),
        (str(len(highs_all)), "High / Critical", "red"),
        (fmt_arr(total_arr), "Total ARR", "green"),
        (fmt_arr(top_arr), "Top Single-RFE ARR", "yellow"),
    ])

    sec_sorted = sorted(DOMAIN_IDS, key=lambda s: -len(by_section.get(s, [])))
    top_overall = sorted(records, key=lambda r: -r["_score"])[:5]
    exec_bullets: List[Tuple[str, str]] = [
        (f"{len(records)} open RFEs across {sum(1 for s in DOMAIN_IDS if by_section.get(s))} "
         f"product domains, carrying {fmt_arr(total_arr)} of combined account ARR.", ""),
        (f"Highest-priority request overall is #{top_overall[0]['case_number']} "
         f"({top_overall[0]['account_name']}, {fmt_arr(top_overall[0]['arr'])}, "
         f"{top_overall[0]['severity']}) — {top_overall[0]['subject'][:90]}.", "green"),
        (f"{len(highs_all)} High/Critical requests need routing decisions: "
         f"{', '.join('#' + r['case_number'] for r in highs_all[:5])}.", "danger"),
        (f"Busiest domain is {SECTION_NAME[sec_sorted[0]]} with "
         f"{len(by_section.get(sec_sorted[0], []))} requests; second is "
         f"{SECTION_NAME[sec_sorted[1]]} with {len(by_section.get(sec_sorted[1], []))}.", ""),
        (f"{len(recent_all)} request(s) landed in the last 14 days: "
         f"{', '.join('#' + r['case_number'] for r in recent_all[:5]) or 'none'}.", "warn"),
        (f"{len(decided)} of {len(records)} RFEs have an assigned state; "
         f"{len(records) - len(decided)} still await a backlog / out-of-scope decision.", ""),
        (f"{meta.get('with_description', 0)} of {len(records)} requests have a usable "
         f"Salesforce description; the rest are flagged for TAM follow-up before routing.", "danger"),
    ]

    domain_cards = "".join(
        f'<div class="domain-card" onclick="showSection(\'{sid}\')">'
        f'<div class="domain-card-emoji">{SECTION_EMOJI[sid]}</div>'
        f'<div class="domain-card-name">{_esc(SECTION_NAME[sid])}</div>'
        f'<div class="domain-card-stats">'
        f'<div class="domain-stat">RFEs <span>{len(by_section.get(sid, []))}</span></div>'
        f'<div class="domain-stat">ARR <span>{fmt_arr(sum(r["arr"] for r in by_section.get(sid, [])))}</span></div>'
        f'<div class="domain-stat">Clusters <span>{len(clusters_by_section.get(sid, []))}</span></div>'
        f'</div>'
        f'<div class="domain-card-top">Top: '
        f'<span>{("#" + sorted(by_section[sid], key=lambda r: -r["_score"])[0]["case_number"]) if by_section.get(sid) else "—"}</span></div>'
        f'</div>'
        for sid in DOMAIN_IDS
    )

    exec_page = f"""<div class="section-page active" id="section-exec">
  <div class="hero hero-exec">
    <div class="hero-title">\U0001F3E0 Executive Overview</div>
    <div class="hero-subtitle">Decide which state each RFE should be assigned — backlog, out of scope, deferred, or needs more information.</div>
    <div class="hero-meta">
      <span class="hero-chip chip-count">{len(records)} RFEs</span>
      <span class="hero-chip chip-recent">{len(recent_all)} in last 14 days</span>
      <span class="hero-chip chip-arr">{fmt_arr(total_arr)} ARR</span>
      <span class="hero-chip chip-high">{len(highs_all)} High/Critical</span>
    </div>
  </div>
  {exec_kpis}
  <div class="card"><div class="card-title">Executive Summary</div>{_summary_list(exec_bullets)}</div>
  <div class="card"><div class="card-title">Domain Overview — click to open</div>
    <div class="domain-grid">{domain_cards}</div></div>
  <div class="charts-grid">
    <div class="chart-container"><div id="chart-count"></div></div>
    <div class="chart-container"><div id="chart-arr"></div></div>
    <div class="chart-container"><div id="chart-sev"></div></div>
    <div class="chart-container"><div id="chart-recent"></div></div>
  </div>
  <div class="card"><div class="card-title">Global At-Risk Accounts</div>{_risk_table(records)}</div>
</div>"""

    # ── Domain pages ─────────────────────────────────────────────────────────
    pages = [exec_page]
    for sid in DOMAIN_IDS:
        rfes = sorted(by_section.get(sid, []), key=lambda r: -r["_score"])
        name, emoji = SECTION_NAME[sid], SECTION_EMOJI[sid]
        if not rfes:
            pages.append(f"""<div class="section-page" id="section-{sid}">
  <div class="hero hero-{sid}"><div class="hero-title">{emoji} {_esc(name)}</div>
  <div class="hero-subtitle">No open requests classified into this domain in the current dataset.</div></div>
</div>""")
            continue

        clusters = clusters_by_section[sid]
        d_arr = sum(r["arr"] for r in rfes)
        d_recent = [r for r in rfes if r["_recent"]]
        d_high = [r for r in rfes if r["severity"] in ("High", "Critical")]
        d_accts = {r["account_name"] for r in rfes}
        kpis = _kpi_row([
            (str(len(rfes)), "Total", "blue"),
            (str(len(d_recent)), "Last 14 Days", "teal"),
            (str(len(d_high)), "High / Critical", "red"),
            (fmt_arr(d_arr), "ARR at Stake", "green"),
            (str(len(d_accts)), "Unique Accounts", "yellow"),
        ])
        cluster_rows = "".join(
            _cluster_row(c, i, sid) for i, c in enumerate(clusters, 1))
        actions = [
            f"Open the top cluster “{clusters[0]['title']}” and assign a state to each of "
            f"its {len(clusters[0]['members'])} case(s) before anything else.",
            (f"Route the {len(d_high)} High/Critical request(s) "
             f"({', '.join('#' + r['case_number'] for r in d_high[:3])}) to engineering "
             f"triage this week." if d_high else
             "No High/Critical requests here — this domain can be triaged at normal cadence."),
            (f"Chase the TAM for descriptions on "
             f"{', '.join('#' + r['case_number'] for r in rfes if len(r['description']) <= 20)[:120]}"
             if any(len(r["description"]) <= 20 for r in rfes) else
             "All requests here have descriptions — no TAM follow-up needed."),
            f"Confirm the {len(clusters)} clusters match how engineering would scope the work; "
            f"merge or split before committing to the backlog.",
            f"Record an out-of-scope decision for anything you will not build, so it stops "
            f"reappearing in the next cycle.",
        ]
        bubble_id = f"bubble-{sid}"
        pages.append(f"""<div class="section-page" id="section-{sid}">
  <div class="hero hero-{sid}">
    <div class="hero-title">{emoji} {_esc(name)}</div>
    <div class="hero-subtitle">{len(rfes)} request(s) in {len(clusters)} cluster(s) — every case is covered by exactly one cluster.</div>
    <div class="hero-meta">
      <span class="hero-chip chip-count">{len(rfes)} RFEs</span>
      <button class="pdf-btn" onclick="exportDomainPdf('{sid}')"
              title="Open your browser's print dialog and choose Save as PDF">
        &#8681; Export this domain as PDF</button>
      <span class="hero-chip chip-recent">{len(d_recent)} recent</span>
      <span class="hero-chip chip-arr">{fmt_arr(d_arr)} ARR</span>
      <span class="hero-chip chip-high">{len(d_high)} High/Critical</span>
    </div>
  </div>
  {kpis}
  <div class="card"><div class="card-title">Executive Summary</div>
    {_summary_list(_domain_bullets(sid, rfes, clusters))}</div>
  <div class="card"><div class="card-title">Top Priority Requests — by cluster
    <span class="muted" style="font-weight:400;font-size:12px">(expand a cluster to assign states)</span></div>
    <div class="requests-list">{cluster_rows}</div></div>
  <div class="chart-container" style="margin-bottom:16px"><div id="{bubble_id}"></div></div>
  <div class="card"><div class="card-title">At-Risk Accounts</div>{_risk_table(rfes)}</div>
  <div class="card"><div class="card-title">Recommended Actions</div>{_actions_list(actions)}</div>
</div>""")

    # ── Chart data (JSON-embedded) ───────────────────────────────────────────
    chart_data = {
        "domains": [SECTION_NAME[s] for s in DOMAIN_IDS],
        "counts": [len(by_section.get(s, [])) for s in DOMAIN_IDS],
        "arrs": [round(sum(r["arr"] for r in by_section.get(s, [])) / 1000.0, 1)
                 for s in DOMAIN_IDS],
        "severity": {k: sum(1 for r in records if r["severity"] == k)
                     for k in ("Critical", "High", "Medium", "Low")},
        "recent": [sum(1 for r in by_section.get(s, []) if r["_recent"]) for s in DOMAIN_IDS],
        "older": [sum(1 for r in by_section.get(s, []) if not r["_recent"]) for s in DOMAIN_IDS],
        "bubbles": {
            sid: {
                "x": list(range(1, len(clusters_by_section[sid]) + 1)),
                "y": [round(c["arr"] / 1000.0, 1) for c in clusters_by_section[sid]],
                "size": [max(8, min(60, c["score"] / 2)) for c in clusters_by_section[sid]],
                "text": [c["title"] for c in clusters_by_section[sid]],
                "name": SECTION_NAME[sid],
            }
            for sid in DOMAIN_IDS if clusters_by_section.get(sid)
        },
    }

    _tick("Rendering charts and pages…", 88)
    return _page_shell(sidebar, "".join(pages), chart_data, print_sheets)


def _page_shell(sidebar: str, pages: str, chart_data: Dict[str, Any],
                print_sheets: str = "") -> str:
    """Assemble the single-file HTML document. LIGHT MODE ONLY."""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cynet Weekly Analysis</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.27.0/plotly.min.js"></script>
<style>
:root {{
  --bg:#f0f4fc; --panel:#ffffff; --panel2:#f5f7fd; --panel3:#eaf0fb;
  --text:#1a2340; --muted:#5a6a8a; --accent:#2563eb; --accent2:#16a34a;
  --warn:#b45309; --danger:#dc2626; --purple:#7c3aed; --teal:#0891b2;
  --border:#d1daf0; --border2:#b8c8e8;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
  font-family:'Inter','Segoe UI',Arial,sans-serif; font-size:14px;
  display:flex; min-height:100vh; }}
body {{ display:grid; grid-template-columns:250px 1fr;
        grid-template-areas:"side ctl" "side main"; }}
#sidebar {{ grid-area:side; }}
#ctl-bar {{ grid-area:ctl; position:sticky; top:0; z-index:50; }}
#main {{ grid-area:main; }}
#sidebar {{ width:250px; min-width:250px; background:#f8faff;
  border-right:1px solid var(--border); position:fixed; top:0; left:0;
  height:100vh; overflow-y:auto; z-index:100; }}
#main {{ margin-left:250px; flex:1; padding:24px; }}
.section-page {{ display:none; }} .section-page.active {{ display:block; }}
.muted {{ color:var(--muted); }}

.sidebar-brand {{ padding:20px 16px 12px; border-bottom:1px solid var(--border); }}
.sidebar-brand .brand-name {{ font-size:18px; font-weight:800;
  background:linear-gradient(135deg,#1d4ed8,#0891b2);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent; }}
.sidebar-brand .report-date {{ color:var(--muted); font-size:11px; margin-top:4px; }}
.sidebar-brand .report-period {{ color:var(--warn); font-size:10px; font-weight:600; margin-top:2px; }}
.sidebar-section-label {{ padding:14px 16px 6px; color:var(--muted); font-size:10px;
  font-weight:700; text-transform:uppercase; letter-spacing:1.2px; }}
.nav-btn {{ display:flex; align-items:center; gap:8px; padding:9px 16px; cursor:pointer;
  border-radius:8px; margin:2px 8px; transition:all .2s; border:1px solid transparent;
  color:var(--muted); font-size:13px; }}
.nav-btn:hover {{ background:var(--panel2); color:var(--text); }}
.nav-btn.active {{ background:linear-gradient(135deg,rgba(37,99,235,.12),rgba(8,145,178,.08));
  border-color:var(--accent); color:var(--accent); }}
.nav-badge {{ background:var(--border); color:var(--muted); font-size:10px; padding:2px 6px;
  border-radius:10px; font-weight:700; margin-left:auto; }}
.nav-btn.active .nav-badge {{ background:rgba(37,99,235,.15); color:var(--accent); }}

.hero {{ border-radius:16px; padding:28px 32px; margin-bottom:24px; }}
.hero-exec {{ background:linear-gradient(135deg,#dbeafe 0%,#e0e7ff 40%,#ede9fe 100%); border:1px solid var(--border2); }}
.hero-epp {{ background:linear-gradient(135deg,#dcfce7 0%,#d1fae5 100%); border:1px solid #86efac; }}
.hero-wac {{ background:linear-gradient(135deg,#fef9c3 0%,#fef3c7 100%); border:1px solid #fcd34d; }}
.hero-email {{ background:linear-gradient(135deg,#fce7f3 0%,#ede9fe 100%); border:1px solid #f9a8d4; }}
.hero-siem {{ background:linear-gradient(135deg,#dbeafe 0%,#e0f2fe 100%); border:1px solid #93c5fd; }}
.hero-identity {{ background:linear-gradient(135deg,#f5f3ff 0%,#ede9fe 100%); border:1px solid #c4b5fd; }}
.hero-cspm {{ background:linear-gradient(135deg,#fff7ed 0%,#fef3c7 100%); border:1px solid #fed7aa; }}
.hero-platform {{ background:linear-gradient(135deg,#e0e7ff 0%,#dbeafe 100%); border:1px solid var(--border2); }}
.hero-reporting {{ background:linear-gradient(135deg,#f1f5f9 0%,#e2e8f0 100%); border:1px solid #cbd5e1; }}
.hero-automations {{ background:linear-gradient(135deg,#d1fae5 0%,#cffafe 100%); border:1px solid #6ee7b7; }}
.hero-ai {{ background:linear-gradient(135deg,#fae8ff 0%,#ede9fe 100%); border:1px solid #d8b4fe; }}
.hero-title {{ font-size:26px; font-weight:800; margin-bottom:6px; }}
.hero-subtitle {{ color:var(--muted); font-size:13px; }}
.hero-meta {{ display:flex; gap:16px; margin-top:12px; flex-wrap:wrap; }}
.hero-chip {{ padding:4px 12px; border-radius:20px; font-size:12px; font-weight:600; }}
.chip-count {{ background:rgba(37,99,235,.1); color:var(--accent); border:1px solid rgba(37,99,235,.25); }}
.chip-recent {{ background:rgba(8,145,178,.1); color:var(--teal); border:1px solid rgba(8,145,178,.25); }}
.chip-arr {{ background:rgba(22,163,74,.1); color:var(--accent2); border:1px solid rgba(22,163,74,.25); }}
.chip-high {{ background:rgba(220,38,38,.1); color:var(--danger); border:1px solid rgba(220,38,38,.25); }}

.kpi-row {{ display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-bottom:24px; }}
.kpi-card {{ background:var(--panel); border:1px solid var(--border); border-radius:14px;
  padding:16px; text-align:center; box-shadow:0 1px 3px rgba(0,0,0,.05); }}
.kpi-val {{ font-size:24px; font-weight:800; margin-bottom:4px; }}
.kpi-label {{ color:var(--muted); font-size:11px; font-weight:600;
  text-transform:uppercase; letter-spacing:.8px; }}
.kpi-val.blue {{ color:var(--accent); }} .kpi-val.teal {{ color:var(--teal); }}
.kpi-val.green {{ color:var(--accent2); }} .kpi-val.red {{ color:var(--danger); }}
.kpi-val.yellow {{ color:var(--warn); }}

.card {{ background:var(--panel); border:1px solid var(--border); border-radius:14px;
  padding:20px; margin-bottom:16px; box-shadow:0 1px 4px rgba(0,0,0,.06); }}
.card-title {{ font-size:16px; font-weight:700; margin-bottom:12px; }}
.summary-list {{ list-style:none; display:flex; flex-direction:column; gap:8px; padding:0; margin:0; }}
.summary-list li {{ padding:8px 12px; background:var(--panel2); border-radius:8px;
  border-left:3px solid var(--accent); font-size:13px; line-height:1.5; }}
.summary-list li.warn {{ border-left-color:var(--warn); }}
.summary-list li.danger {{ border-left-color:var(--danger); }}
.summary-list li.green {{ border-left-color:var(--accent2); }}
.risk-table {{ width:100%; border-collapse:collapse; }}
.risk-table th {{ text-align:left; padding:10px 12px; background:var(--panel2);
  color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.8px;
  border-bottom:1px solid var(--border); }}
.risk-table td {{ padding:10px 12px; border-bottom:1px solid var(--border); font-size:13px; }}
.risk-table tr:hover td {{ background:var(--panel2); }}
.actions-list {{ display:flex; flex-direction:column; gap:10px; }}
.action-item {{ display:flex; gap:12px; align-items:flex-start; }}
.action-num {{ color:var(--accent); font-weight:800; font-size:16px; min-width:24px; }}
.action-text {{ font-size:13px; line-height:1.5; }}

.requests-list {{ display:flex; flex-direction:column; gap:8px; }}
.request-card {{ background:var(--panel2); border:1px solid var(--border);
  border-radius:12px; overflow:hidden; }}
.request-header {{ display:flex; align-items:flex-start; gap:12px; padding:14px 16px;
  cursor:pointer; transition:background .15s; }}
.request-header:hover {{ background:#eef2fb; }}
.rank-badge {{ width:32px; height:32px; border-radius:50%; display:flex;
  align-items:center; justify-content:center; font-weight:800; font-size:14px; flex-shrink:0; }}
.rank-1 {{ background:linear-gradient(135deg,#f5a623,#f7c948); color:#000; }}
.rank-2 {{ background:linear-gradient(135deg,#9e9e9e,#bdbdbd); color:#000; }}
.rank-3 {{ background:linear-gradient(135deg,#cd7f32,#e09b5a); color:#fff; }}
.rank-n {{ background:var(--panel3); color:var(--muted); border:1px solid var(--border); }}
.request-info {{ flex:1; min-width:0; }}
.request-title-row {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:4px; }}
.req-title {{ font-size:14px; font-weight:700; }}
.read-first {{ background:rgba(180,83,9,.1); color:var(--warn); border:1px solid rgba(180,83,9,.25);
  padding:2px 8px; border-radius:6px; font-size:10px; font-weight:700; }}
.req-chips {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:4px; }}
.chip {{ padding:3px 8px; border-radius:8px; font-size:11px; font-weight:600; }}
.chip-arr2 {{ background:rgba(22,163,74,.1); color:var(--accent2); }}
.chip-sev-High {{ background:rgba(220,38,38,.1); color:var(--danger); }}
.chip-sev-Critical {{ background:rgba(220,38,38,.18); color:var(--danger); }}
.chip-sev-Medium {{ background:rgba(180,83,9,.08); color:var(--warn); }}
.chip-sev-Low {{ background:rgba(90,106,138,.1); color:var(--muted); }}
.chip-count2 {{ background:rgba(37,99,235,.1); color:var(--accent); }}
.chip-account {{ background:rgba(124,58,237,.09); color:var(--purple); }}
.chip-status {{ background:var(--panel3); color:var(--muted); }}
.chip-date {{ background:var(--panel3); color:var(--muted); }}
.chip-decision {{ background:rgba(22,163,74,.14); color:var(--accent2); border:1px solid rgba(22,163,74,.3); }}
.req-insight {{ color:var(--muted); font-size:11px; margin-top:6px; line-height:1.5; }}
.req-expand-btn {{ color:var(--muted); font-size:18px; cursor:pointer; flex-shrink:0; padding-top:4px; }}
.req-detail {{ display:none; border-top:1px solid var(--border); padding:12px 16px 16px; }}
.req-detail.open {{ display:block; }}

.rfe-card {{ background:var(--panel2); border:1px solid var(--border); border-radius:10px;
  padding:14px; margin:6px 0; }}
.rfe-card-header {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:8px; }}
.case-badge {{ font-family:'Courier New',monospace; background:#eff6ff;
  border:1px solid var(--border2); color:var(--accent); padding:3px 8px; border-radius:6px;
  font-size:12px; cursor:pointer; font-weight:700; }}
.case-badge:hover {{ background:#dbeafe; }}
.rfe-subject {{ font-weight:600; font-size:13px; flex:1; min-width:200px; }}
.rfe-meta-row {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:8px; }}
.rfe-pm-summary {{ background:#f5f3ff; border-left:3px solid var(--purple);
  border-radius:6px; padding:10px 12px; font-size:12px; line-height:1.6; }}
.rfe-pm-label {{ font-size:10px; font-weight:700; color:var(--purple);
  text-transform:uppercase; letter-spacing:.8px; margin-bottom:4px; }}

.decision-row {{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-top:10px;
  padding-top:10px; border-top:1px dashed var(--border); }}
.decision-label {{ font-size:10px; font-weight:700; color:var(--muted);
  text-transform:uppercase; letter-spacing:.8px; margin-right:2px; }}
.dec-btn {{ font-family:inherit; font-size:11.5px; font-weight:600; padding:4px 10px;
  border:1px solid var(--border2); border-radius:20px; background:var(--panel);
  color:var(--muted); cursor:pointer; transition:all .15s; }}
.dec-btn:hover {{ border-color:var(--accent); color:var(--accent); }}
.dec-btn.active {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
.dec-btn.clear {{ border-style:dashed; }}
.decision-saved {{ font-size:11px; color:var(--accent2); font-weight:600; margin-left:4px; }}

.trend-badge {{ padding:2px 8px; border-radius:10px; font-size:10px; font-weight:700; }}
.trend-NEW {{ background:rgba(8,145,178,.12); color:var(--teal); }}
.trend-GROWING {{ background:rgba(180,83,9,.1); color:var(--warn); }}
.trend-STABLE {{ background:rgba(90,106,138,.08); color:var(--muted); }}
.domain-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
  gap:12px; }}
.domain-card {{ background:#ffffff; border:1px solid var(--border); border-radius:14px;
  padding:16px; cursor:pointer; transition:all .2s; box-shadow:0 1px 3px rgba(0,0,0,.06); }}
.domain-card:hover {{ border-color:var(--accent); transform:translateY(-2px);
  box-shadow:0 4px 12px rgba(37,99,235,.1); }}
.domain-card-emoji {{ font-size:24px; margin-bottom:8px; }}
.domain-card-name {{ font-weight:700; font-size:14px; margin-bottom:6px; }}
.domain-card-stats {{ display:flex; gap:8px; flex-wrap:wrap; }}
.domain-stat {{ font-size:11px; color:var(--muted); }}
.domain-stat span {{ color:var(--text); font-weight:600; }}
.domain-card-top {{ margin-top:8px; font-size:11px; color:var(--muted);
  border-top:1px solid var(--border); padding-top:8px; }}
.domain-card-top span {{ color:var(--accent); font-family:monospace; }}
.charts-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:24px; }}
.chart-container {{ background:var(--panel); border:1px solid var(--border);
  border-radius:14px; padding:20px; box-shadow:0 1px 4px rgba(0,0,0,.06); }}
.toast {{ position:fixed; bottom:24px; right:24px; background:var(--accent); color:#fff;
  padding:10px 18px; border-radius:8px; font-size:13px; font-weight:600; z-index:9999;
  display:none; box-shadow:0 4px 12px rgba(37,99,235,.3); }}
@media (max-width:1100px) {{ .kpi-row {{ grid-template-columns:repeat(2,1fr); }}
  .charts-grid {{ grid-template-columns:1fr; }} }}

/* ── Shared with the PI Planning report (scoring/report_ui.py) ───────────── */
{report_ui.BAND_CSS}
{report_ui.FILTER_CSS}

/* PI score chip and the business-impact flag chip on a card */
.pi-score {{ font-size:10px; font-weight:800; padding:2px 8px; border-radius:10px;
            background:#eef4ff; color:#1d4ed8; cursor:help; white-space:nowrap; }}
.chip-flag {{ background:#fee2e2 !important; color:#991b1b !important; font-weight:700; }}
.rfe-rank-row {{ display:flex; gap:6px; align-items:center; margin:6px 0 2px; flex-wrap:wrap; }}
.pdf-btn {{ background:#fff; border:1px solid #cbd5e1; color:#334155; font:inherit;
           font-size:11px; font-weight:600; padding:4px 10px; border-radius:14px;
           cursor:pointer; margin-left:6px; }}
.pdf-btn:hover {{ border-color:#2563eb; color:#1d4ed8; }}

/* ── The per-domain one-pager ────────────────────────────────────────────── */
/* Hidden on screen; the print rules below reveal exactly one of them. */
.print-sheet {{ display:none; }}
.ps-head {{ display:flex; justify-content:space-between; align-items:flex-start;
           border-bottom:2px solid #111827; padding-bottom:8px; margin-bottom:12px; }}
.ps-title {{ font-size:19px; font-weight:800; color:#111827; }}
.ps-sub {{ font-size:11px; color:#6b7280; margin-top:2px; }}
.ps-brand {{ font-size:10px; font-weight:700; letter-spacing:.8px; text-transform:uppercase;
            color:#9ca3af; }}
.ps-stats {{ display:flex; gap:10px; margin-bottom:14px; }}
.ps-stat {{ flex:1; border:1px solid #e5e7eb; border-radius:6px; padding:7px 9px; }}
.ps-stat-v {{ font-size:17px; font-weight:800; color:#111827; line-height:1.1; }}
.ps-stat-l {{ font-size:9px; text-transform:uppercase; letter-spacing:.4px; color:#6b7280;
             margin-top:2px; }}
.ps-h2 {{ font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:.7px;
         color:#374151; margin:14px 0 6px; padding-bottom:3px;
         border-bottom:1px solid #e5e7eb; }}
.ps-bands {{ display:flex; gap:8px; }}
.ps-band {{ flex:1; display:flex; align-items:center; gap:6px; padding:6px 9px;
           border-radius:6px; border:1px solid #e5e7eb; }}
.ps-band-start_now {{ background:#eff4ff; border-color:#bfd3ff; }}
.ps-band-plan {{ background:#f5f0ff; border-color:#ddd0fb; }}
.ps-mark {{ font-size:10px; color:#4b5563; }}
.ps-band-n {{ font-size:15px; font-weight:800; color:#111827; }}
.ps-band-l {{ font-size:10px; color:#4b5563; }}
.ps-table {{ width:100%; border-collapse:collapse; font-size:10px; }}
.ps-table th {{ text-align:left; font-size:8.5px; text-transform:uppercase;
               letter-spacing:.4px; color:#6b7280; border-bottom:1px solid #d1d5db;
               padding:4px 5px; }}
.ps-table td {{ padding:5px; border-bottom:1px solid #f0f1f4; vertical-align:top;
               color:#1f2937; }}
.ps-table .ps-c {{ text-align:center; white-space:nowrap; }}
.ps-themes {{ table-layout:fixed; }}
.ps-themes th:nth-child(1), .ps-themes td:nth-child(1) {{ width:4%; }}
.ps-themes th:nth-child(2), .ps-themes td:nth-child(2) {{ width:50%; }}
.ps-themes th:nth-child(3), .ps-themes td:nth-child(3) {{ width:9%; }}
.ps-themes th:nth-child(4), .ps-themes td:nth-child(4) {{ width:11%; }}
.ps-themes th:nth-child(5), .ps-themes td:nth-child(5) {{ width:11%; }}
.ps-themes th:nth-child(6), .ps-themes td:nth-child(6) {{ width:8%; }}
.ps-themes th:nth-child(7), .ps-themes td:nth-child(7) {{ width:7%; }}
.print-sheet {{ max-width:186mm; }}
.ps-n {{ color:#9ca3af; font-weight:700; width:16px; }}
.ps-case {{ font-family:ui-monospace,Consolas,monospace; white-space:nowrap; }}
.ps-why {{ font-size:9px; color:#6b7280; margin-top:2px; line-height:1.4; }}
.ps-band-t {{ font-size:8px; color:#6b7280; }}
.ps-note {{ font-size:10px; color:#6b7280; font-style:italic; padding:6px 0; }}
.ps-foot {{ margin-top:14px; padding-top:7px; border-top:1px solid #e5e7eb;
           font-size:8.5px; color:#9ca3af; line-height:1.5; }}

@media print {{
  @page {{ size:A4 portrait; margin:12mm; }}
  /* The screen layout is a grid with a 250px sidebar column. Leaving it in
     place put the whole sheet inside that column; the sheet needs the page. */
  body {{ background:#fff !important; display:block !important; }}
  /* Everything interactive is left out of the PDF on purpose. */
  #sidebar, #main, #ctl-bar, .toast, #tip, .pdf-btn {{ display:none !important; }}
  .print-sheet.printing {{ display:block !important; }}
  .ps-table {{ page-break-inside:auto; }}
  .ps-table tr {{ page-break-inside:avoid; }}
}}
</style></head>
<body>
{sidebar}
<div id="ctl-bar"></div>
<div id="main">{pages}</div>
<div id="print-area">{print_sheets}</div>
<div class="toast" id="toast">Copied to clipboard</div>
<script>
{report_ui.BAND_JS}
{report_ui.FILTER_JS}

/* Filters and sorting act on the cards this report renders in Python:
   a theme is `.request-card`, a request inside it is `.rfe-card`. */
var WEEKLY_FILTER_CFG = {{
  sectionSel: '.section-page', groupSel: '.request-card', rowSel: '.rfe-card'
}};

/** Export one domain as a PDF through the browser's own print dialog.
 *  The screen layout is never printed — a sheet built for the page is. */
function exportDomainPdf(sid) {{
  var sheet = document.getElementById('print-sheet-' + sid);
  if (!sheet) {{
    alert('This domain has no requests in the current dataset, so there is nothing to export.');
    return;
  }}
  document.querySelectorAll('.print-sheet').forEach(function (el) {{
    el.classList.remove('printing');
  }});
  sheet.classList.add('printing');
  var done = function () {{
    sheet.classList.remove('printing');
    window.removeEventListener('afterprint', done);
  }};
  window.addEventListener('afterprint', done);
  window.print();
  // Safari and some embedded viewers never fire afterprint.
  setTimeout(done, 60000);
}}

document.addEventListener('DOMContentLoaded', function () {{
  initTips();
  renderControlBar('ctl-bar', WEEKLY_FILTER_CFG);
}});

var CHART = {json.dumps(chart_data)};
var LIGHT = {{
  paper_bgcolor:'transparent', plot_bgcolor:'transparent',
  font:{{ color:'#5a6a8a', family:'Inter,Segoe UI,Arial,sans-serif', size:11 }},
  xaxis:{{ color:'#5a6a8a', gridcolor:'#d1daf0' }},
  yaxis:{{ color:'#5a6a8a', gridcolor:'#d1daf0' }},
  legend:{{ font:{{ color:'#5a6a8a' }} }},
  margin:{{ t:44, r:16, b:80, l:56 }}
}};
var CFG = {{ displayModeBar:false, responsive:true }};

function showSection(id) {{
  document.querySelectorAll('.section-page').forEach(function(el){{ el.classList.remove('active'); }});
  document.querySelectorAll('.nav-btn').forEach(function(el){{ el.classList.remove('active'); }});
  var page = document.getElementById('section-' + id);
  var nav = document.getElementById('nav-' + id);
  if (page) page.classList.add('active');
  if (nav) nav.classList.add('active');
  window.scrollTo({{ top:0, behavior:'smooth' }});
  drawBubble(id);
}}

function toggleDetail(id) {{
  var el = document.getElementById(id);
  var btn = document.getElementById('expand-' + id);
  if (!el) return;
  el.classList.toggle('open');
  if (btn) btn.textContent = el.classList.contains('open') ? '▲' : '▼';
}}

function _showToast(msg) {{
  var t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(function(){{ t.style.display = 'none'; }}, 1800);
}}

// The report runs inside an iframe, where navigator.clipboard is often blocked
// by permissions policy. Fall back to a hidden textarea + execCommand, and
// always surface a toast so the click never looks like it did nothing.
function copyCase(num) {{
  var text = String(num);
  function fallback() {{
    try {{
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      _showToast(ok ? 'Copied ' + text : 'Press Ctrl+C to copy ' + text);
    }} catch (e) {{
      _showToast('Could not copy — case ' + text);
    }}
  }}
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(function() {{
      _showToast('Copied ' + text);
    }}).catch(fallback);
  }} else {{
    fallback();
  }}
}}

// Assign an RFE state — posts to the platform backend (same origin).
function setDecision(btn) {{
  var caseNum = btn.getAttribute('data-case');
  var decision = btn.getAttribute('data-decision');
  var row = btn.parentNode;
  row.querySelectorAll('.dec-btn').forEach(function(b){{ b.classList.remove('active'); }});
  if (decision) btn.classList.add('active');
  var saved = document.getElementById('saved-' + caseNum);
  if (saved) saved.textContent = 'Saving…';
  fetch('/api/weekly-report/decision', {{
    method:'POST', headers:{{ 'Content-Type':'application/json' }},
    body: JSON.stringify({{ case_number: caseNum, decision: decision }})
  }}).then(function(r){{ return r.json(); }}).then(function(){{
    if (saved) {{ saved.textContent = decision ? '✓ Saved' : '✓ Cleared';
      setTimeout(function(){{ saved.textContent=''; }}, 2500); }}
  }}).catch(function(){{ if (saved) saved.textContent = 'Save failed'; }});
}}

var drawn = {{}};
function drawBubble(sid) {{
  if (drawn[sid] || !CHART.bubbles[sid]) return;
  var b = CHART.bubbles[sid];
  Plotly.newPlot('bubble-' + sid, [{{
    x:b.x, y:b.y, text:b.text, mode:'markers',
    marker:{{ size:b.size, color:'#2563eb', opacity:.62,
             line:{{ color:'#1d4ed8', width:1 }} }},
    hovertemplate:'%{{text}}<br>ARR: $%{{y}}K<extra></extra>'
  }}], Object.assign({{}}, LIGHT, {{
    title:'Cluster momentum — ARR vs. priority (bubble size = combined priority score)',
    xaxis:Object.assign({{}}, LIGHT.xaxis, {{ title:'Cluster rank' }}),
    yaxis:Object.assign({{}}, LIGHT.yaxis, {{ title:'ARR ($K)' }})
  }}), CFG);
  drawn[sid] = true;
}}

Plotly.newPlot('chart-count', [{{
  x:CHART.domains, y:CHART.counts, type:'bar',
  marker:{{ color:'#2563eb' }}
}}], Object.assign({{}}, LIGHT, {{ title:'RFE count by domain' }}), CFG);

Plotly.newPlot('chart-arr', [{{
  x:CHART.domains, y:CHART.arrs, type:'bar',
  marker:{{ color:'#16a34a' }},
  hovertemplate:'%{{x}}<br>$%{{y}}K<extra></extra>'
}}], Object.assign({{}}, LIGHT, {{ title:'ARR at stake by domain ($K)' }}), CFG);

Plotly.newPlot('chart-sev', [{{
  labels:Object.keys(CHART.severity), values:Object.values(CHART.severity),
  type:'pie', hole:.45,
  marker:{{ colors:['#dc2626','#f97316','#b45309','#5a6a8a'] }}
}}], Object.assign({{}}, LIGHT, {{ title:'Severity distribution' }}), CFG);

Plotly.newPlot('chart-recent', [
  {{ x:CHART.domains, y:CHART.recent, type:'bar', name:'≤ 14 days', marker:{{ color:'#0891b2' }} }},
  {{ x:CHART.domains, y:CHART.older, type:'bar', name:'Older', marker:{{ color:'#b8c8e8' }} }}
], Object.assign({{}}, LIGHT, {{ title:'Recent vs. older requests', barmode:'stack' }}), CFG);
</script>
</body></html>"""
