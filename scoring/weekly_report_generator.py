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

PM Decision Summaries are LLM-generated and cached in the `pm_summaries` table.
Per the skill's absolute rules, a raw Salesforce description is NEVER used as a
summary — when no description exists, the mandated TAM-follow-up flag is shown.

LIGHT MODE ONLY. Never use dark backgrounds.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

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

NO_DESC_SUMMARY = (
    "⚠️ No description provided — follow up with TAM before routing. "
    "Do not proceed to backlog without PM review."
)


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


def load_rfes(db_path: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Load the latest run's RFEs, backfilling descriptions from earlier runs.

    The Salesforce SOQL pull does not always include Description. When the
    latest run lacks one, the same case_number from an earlier run (e.g. a CSV
    import) is used so PM summaries can still be written.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    meta: Dict[str, Any] = {}
    try:
        row = conn.execute(
            "SELECT run_id, started_at, rfe_count, source FROM run_meta "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return [], meta
        meta = dict(row)
        run_id = row["run_id"]

        rows = conn.execute(
            """SELECT case_number, subject, description, account_name, account_arr,
                      status, domain, sub_domain, severity, created_date
               FROM rfe_pulls WHERE run_id=?""",
            (run_id,),
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

        sev = (r["severity"] or "").strip() or "Low"
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
            "severity": sev if sev in SEV_WEIGHT else "Low",
            "opened": opened,
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
    arr_score = (rfe["arr"] / 50_000.0) * 2
    sev_score = SEV_WEIGHT.get(rfe["severity"], 1) * 3
    days = rfe["_days"]
    recency = (4 if days <= 7 else 2 if days <= 14 else 1) * 2
    return arr_score + sev_score + recency


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
        sev = max((m["severity"] for m in members),
                  key=lambda s: SEV_WEIGHT.get(s, 1))
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
    """Coverage stats so the UI can show what still needs generating."""
    ensure_tables(db_path)
    records, meta = load_rfes(db_path)
    needs = [r for r in records if len(r["description"]) > 20 and not r["pm_summary"]]
    return {
        "total_rfes": len(records),
        "with_description": meta.get("with_description", 0),
        "no_description": len(records) - meta.get("with_description", 0),
        "summaries_cached": sum(1 for r in records if r["pm_summary"]),
        "pending": len(needs),
        "backfilled_descriptions": meta.get("backfilled_descriptions", 0),
        "llm_available": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
        "decided": sum(1 for r in records if r["decision"]),
        "run_id": meta.get("run_id"),
    }


SUMMARY_MODEL = "claude-opus-5"

SUMMARY_SYSTEM = (
    "You are a senior product manager at Cynet, a B2B cybersecurity company, "
    "triaging customer feature requests (RFEs).\n\n"
    "For each RFE write a PM Decision Summary of 2-4 sentences that answers ALL of:\n"
    "1. What is broken or missing today - the specific gap, NOT a restatement of the subject.\n"
    "2. The operational consequence - what it blocks, who is affected, what breaks.\n"
    "3. A PM routing signal where applicable, using this exact wording:\n"
    "   - '⚠️ This is a bug/defect, not a feature request — route to engineering'\n"
    "   - '⚠️ Regression — feature previously existed and was removed'\n"
    "   - '⚠️ Active compliance/security failure — escalate urgently'\n"
    "   - 'Deal blocker for enterprise prospect'\n"
    "   - 'Strategic first-mover opportunity'\n"
    "   - 'Active competitor POC blocker'\n\n"
    "ABSOLUTE RULES:\n"
    "- NEVER copy or paste any part of the raw description. Write fresh, in your own words.\n"
    "- NEVER end a summary with '...' or truncate. Every summary must be complete sentences.\n"
    "- NEVER use the description as a fallback.\n"
    "- Be specific and decision-useful. Avoid generic filler like 'this would improve usability'."
)

SUMMARY_TOOL = {
    "name": "record_summaries",
    "description": "Record one PM Decision Summary per RFE.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summaries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "case_number": {"type": "string"},
                        "summary": {
                            "type": "string",
                            "description": "2-4 complete sentences, written fresh. Never raw description text.",
                        },
                    },
                    "required": ["case_number", "summary"],
                },
            }
        },
        "required": ["summaries"],
    },
}


def generate_summaries(db_path: str, progress=None, batch_size: int = 6,
                       limit: Optional[int] = None) -> Dict[str, Any]:
    """LLM-generate and cache PM Decision Summaries for RFEs that have a
    description but no cached summary. progress(done, total) is optional."""
    ensure_tables(db_path)
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"generated": 0, "skipped": 0,
                "error": "ANTHROPIC_API_KEY is not set — PM summaries require an LLM."}

    records, _ = load_rfes(db_path)
    pending = [r for r in records
               if len(r["description"]) > 20 and not r["pm_summary"]]
    if limit:
        pending = pending[:limit]
    total = len(pending)
    if total == 0:
        return {"generated": 0, "skipped": 0, "total": 0}

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    generated, failed = 0, 0
    for start in range(0, total, batch_size):
        batch = pending[start:start + batch_size]
        payload = "\n\n".join(
            f"RFE {r['case_number']}\n"
            f"Subject: {r['subject']}\n"
            f"Account: {r['account_name']} (ARR ${r['arr']:,.0f}) | "
            f"Severity: {r['severity']} | Status: {r['status']}\n"
            f"Raw description (source material only — do NOT copy):\n{r['description'][:1500]}"
            for r in batch
        )
        try:
            msg = client.messages.create(
                model=SUMMARY_MODEL,
                max_tokens=4000,
                system=[{"type": "text", "text": SUMMARY_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[SUMMARY_TOOL],
                tool_choice={"type": "tool", "name": "record_summaries"},
                messages=[{"role": "user", "content":
                           f"Write a PM Decision Summary for each of these "
                           f"{len(batch)} RFEs:\n\n{payload}"}],
            )
            items = []
            for block in msg.content:
                if getattr(block, "type", "") == "tool_use":
                    items = block.input.get("summaries", []) or []
                    break
            by_case = {str(it.get("case_number", "")).strip(): (it.get("summary") or "").strip()
                       for it in items}
            conn = sqlite3.connect(db_path)
            try:
                for r in batch:
                    text = by_case.get(r["case_number"], "")
                    # Guard the skill's absolute rules at the storage boundary:
                    # reject anything truncated or lifted from the description.
                    if (not text or text.endswith("...") or text.endswith("…")
                            or text[:60].lower() in r["description"].lower()):
                        failed += 1
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO pm_summaries "
                        "(case_number, summary, model, generated_at) VALUES (?,?,?,?)",
                        (r["case_number"], text, SUMMARY_MODEL,
                         datetime.utcnow().isoformat()),
                    )
                    generated += 1
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # keep going; report at the end
            failed += len(batch)
            if progress:
                progress(min(start + len(batch), total), total, str(exc)[:160])
            continue
        if progress:
            progress(min(start + len(batch), total), total, "")

    return {"generated": generated, "skipped": failed, "total": total}


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
    """Render the PM Decision Summary. NEVER falls back to raw description."""
    text = (rfe.get("pm_summary") or "").strip()
    if not text:
        if len(rfe["description"]) > 20:
            text = ("PM summary not generated yet — click "
                    "“Generate PM summaries” on the Weekly Analysis tab.")
        else:
            text = NO_DESC_SUMMARY
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


def _rfe_card(rfe: Dict[str, Any]) -> str:
    cn = _esc(rfe["case_number"])
    sev = _esc(rfe["severity"])
    opened = rfe["opened"].strftime("%b %d, %Y") if rfe["opened"] else "—"
    dec = rfe.get("decision") or ""
    dec_chip = (f'<span class="chip chip-decision">{_esc(DECISION_LABEL.get(dec, dec))}</span>'
                if dec else "")
    return f"""<div class="rfe-card">
  <div class="rfe-card-header">
    <span class="case-badge" onclick="copyCase('{cn}')">{cn}</span>
    <span class="rfe-subject">{_esc(rfe['subject'])}</span>
    <span class="trend-badge trend-{_esc(rfe['_trend'])}">{_esc(rfe['_trend'])}</span>
  </div>
  <div class="rfe-meta-row">
    <span class="chip chip-account">{_esc(rfe['account_name'])}</span>
    <span class="chip chip-arr2">{fmt_arr(rfe['arr'])}</span>
    <span class="chip chip-sev-{sev}">{sev}</span>
    <span class="chip chip-status">{_esc(rfe['status'] or '—')}</span>
    <span class="chip chip-date">{_esc(opened)}</span>
    {dec_chip}
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
    return f"""<div class="request-card">
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

def _empty_page(message: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Weekly Analysis</title></head>
<body style="font-family:'Inter','Segoe UI',Arial,sans-serif;background:#f0f4fc;
color:#1a2340;padding:60px;text-align:center">
<h2 style="margin-bottom:8px">Weekly Analysis</h2>
<p style="color:#5a6a8a">{_esc(message)}</p></body></html>"""


def generate_report(db_path: str) -> str:
    """Build the self-contained weekly-analysis HTML report."""
    ensure_tables(db_path)
    records, meta = load_rfes(db_path)
    if not records:
        return _empty_page("No RFE data found. Import a CSV or pull from "
                           "Salesforce on the Data Sources tab, then reopen this tab.")

    report_date, retro = report_date_for(records)
    window_start = report_date - timedelta(days=14)

    for r in records:
        r["_days"] = (report_date - r["opened"]).days if r["opened"] else 999
        r["_score"] = priority_score(r, report_date)
        r["_trend"] = trend_of(r["_days"])
        r["_recent"] = r["_days"] <= 14
        r["_section"] = classify_section(r["sf_domain"], r["subject"], r["description"])

    by_section: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_section[r["_section"]].append(r)

    clusters_by_section = {
        sid: build_clusters(sorted(by_section.get(sid, []), key=lambda r: -r["_score"]), sid)
        for sid in DOMAIN_IDS
    }

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

    return _page_shell(sidebar, "".join(pages), chart_data)


def _page_shell(sidebar: str, pages: str, chart_data: Dict[str, Any]) -> str:
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
</style></head>
<body>
{sidebar}
<div id="main">{pages}</div>
<div class="toast" id="toast">Copied to clipboard</div>
<script>
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
