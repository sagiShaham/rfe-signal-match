"""
PI Planning Report Generator
Reads RFE data from SQLite DB, clusters by subject similarity, generates standalone HTML.
"""
import sqlite3
import re
import json
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Any, Optional


# ── Domain mapping from SF domain field ──────────────────────────────────────

SF_DOMAIN_MAP = {
    "endpoint": "EPP",
    "siem": "SIEM",
    "cloud": "CSPM",
    "platform": "Platform",
    "product tools": "PSA/RMM",
    "identity": "Identity",
    "email": "Email Security",
    "automation": "Automations",
    "msp": "Platform",
    "ux/ui": "Platform",
    "mobile": "Endpoint Management",
    "espm": "CSPM",
    "cspm": "CSPM",
    "sspm": "CSPM",
    "reporting": "Reporting",
}

# ── Keyword patterns for fallback domain classification ─────────────────────

KEYWORD_DOMAINS = [
    ("Email Security",       r"email security|mail security|email digest|dkim|arc and srs|link protection|email.*filter|email integration|email.*allow|email.*block|email.*rbac|email.*setting|email.*audit|email.*log"),
    ("SIEM",                 r"\bclm\b|\bsiem\b|log management|log.*collect|syslog|log.*export|\buba\b|threat hunting|correl|mitre.*alert|forensic|xdr whitelist|xdr.*country"),
    ("CSPM",                 r"\bcspm\b|\bsspm\b|aws.*misconfig|aws.*role|assume.*role|alibaba|ali cloud|huawei cloud|azure.*misconfig|cloud.*integrat|cloud account"),
    ("Identity",             r"entra id|\bitdr\b|mfa enforcement|account lockout|active directory|user security posture|suspicious login|identity protect|\bldap\b"),
    ("PSA/RMM",              r"connectwise|ninjaone|ninja.*rmm|\bdatto\b|autotask|\bpsa\b|it glue|mcp server|native mcp|mcp support"),
    ("API",                  r"api rest|rest api|api call|\bapiv[12]\b|api.*endpoint|management.*api|api.*billing|api.*alert|api.*uninstall"),
    ("Automations",          r"custom remediation|auto-rem|global playbook|remediation script|import.*script|\bautomat|global.*remediation"),
    ("Reporting",            r"\breport\b|executive report|scheduled report|all-in-one report|vulnerability report|digest report|quarterly.*report|report.*branding|report.*logo"),
    ("Alert UI",             r"alert.*filter|alert.*view|alert.*ui|alert.*tag|unscanned host alert|inactive.*alert.*rule|test alert|alert.*classify|reopen alert"),
    ("User Management",      r"\brbac\b|custom role|user role|granular.*permission|user.*permission|role.*permission|co-brand|branding.*logo|custom.*logo|user right"),
    ("Endpoint Management",  r"non-deployed|non deployed|uninstall.*host|uninstalled host|inactive.*host|offline.*host|host.*migration|tray icon|cynet.*agent.*install|cynet.*deploy|msi.*installer|arm.*device|arm.*server|linux.*install|opensuse|zorin|raspberry pi|alma linux|rhel.*support|mac.*install|arm architecture|host.*serial|host.*uptime|remove.*host|suspend.*agent|facilitate.*deploy|endpoint management"),
    ("Platform",             r"\btimezone\b|time zone|centralized.*mssp|on-prem mssp|multi-site|multi site|global.*mssp|mssp.*level|tenant search|expiration.*allow|exclusion catalog|global.*setting|global.*admin|reverse proxy|console lockdown|cross.*site|centralized.*email|centralized.*remediation|centralized.*alert|centralized.*scanner"),
    ("EPP",                  r"\bantivirus\b|av scan|full av|on-demand.*scan|\bepp\b|endpoint protect|storage device control|usb.*scan|usb.*block|linux isolat|mac.*scan|file.*hash|anti-tamper|\bepss\b|\bsandbox\b|\bmalware\b|tamper.*protect|process.*protect|\bbitlocker\b|remote wipe|web content filter|\bwcf\b|network.*detect|\bndr\b|dhcp.*starvation|rogue dhcp|device.*control.*usb"),
    ("Remediation",          r"undo remediation|remote shell|remote.*script.*host|block.*ip.*nsx|unblock.*ip|nsx block|application control.*block|software.*vulnerability.*remediat"),
    ("Playbooks",            r"playbook.*global|global.*playbook|playbook.*apply|playbook.*template"),
    ("AI Initiatives",       r"\bai\s|artificial intelligence|machine learning|\bllm\b|\bcopilot\b|ai agent"),
]

DOMAIN_ORDER = [
    "Executive", "EPP", "Email Security", "SIEM", "Identity", "CSPM",
    "Platform", "Reporting", "User Management", "Endpoint Management",
    "Alert UI", "AI Initiatives", "Automations", "Playbooks", "Remediation",
    "PSA/RMM", "API", "Other"
]

STOPWORDS = {
    "the","a","an","and","or","for","to","of","in","on","at","by","with","from",
    "as","is","are","be","has","have","that","this","it","its","can","will",
    "cynet","rfe","request","feature","ability","support","add","allow","enable",
    "new","improve","enhance","update","please"
}

SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(\[rfe\]|rfe[:\-]\s*|rfe for\s*|\[external\]\s*)+",
    re.IGNORECASE
)


def classify_domain(sf_domain: str, subject: str, description: str) -> str:
    """Classify an RFE into a domain."""
    if sf_domain:
        mapped = SF_DOMAIN_MAP.get(sf_domain.strip().lower())
        if mapped:
            return mapped

    text = (subject + " " + (description or "")).lower()
    for domain, pattern in KEYWORD_DOMAINS:
        if re.search(pattern, text):
            return domain
    return "Other"


def clean_subject(subject: str) -> str:
    """Remove RFE/EXTERNAL prefixes from subject."""
    return SUBJECT_PREFIX_RE.sub("", subject).strip()


def tokenize(text: str) -> set:
    """Extract meaningful tokens from text."""
    words = re.findall(r"[a-z]{3,}", text.lower())
    return {w for w in words if w not in STOPWORDS}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1


def cluster_domain(rfes: List[Dict]) -> List[Dict]:
    """Cluster RFEs within a domain by subject similarity."""
    n = len(rfes)
    if n == 0:
        return []

    tokens = [tokenize(r["subject"]) for r in rfes]
    uf = UnionFind(n)

    for i in range(n):
        for j in range(i + 1, n):
            if jaccard(tokens[i], tokens[j]) >= 0.28:
                uf.union(i, j)

    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        groups[uf.find(i)].append(i)

    now = datetime.utcnow()
    six_months_ago = now - timedelta(days=182)

    clusters = []
    for indices in groups.values():
        members = [rfes[i] for i in indices]

        # Find name from highest-ARR member
        best = sorted(members, key=lambda r: (-r["account_arr"], r["subject"]))[0]
        name = clean_subject(best["subject"])

        arr_total = sum(r["account_arr"] for r in members)
        accounts = list({r["account_name"] for r in members if r["account_name"]})
        count = len(members)

        # Recency
        recent = 0
        for r in members:
            cd = r.get("created_date", "")
            if cd:
                try:
                    dt = datetime.strptime(cd[:10], "%Y-%m-%d")
                    if dt >= six_months_ago:
                        recent += 1
                except (ValueError, TypeError):
                    pass

        score = arr_total + count * 80000
        momentum = (recent / max(count, 1)) * count * 0.7 + (arr_total / 500000) * 0.3

        if momentum > 3:
            trend = "Accelerating"
        elif momentum > 1.5:
            trend = "Persistent"
        elif momentum > 0.5:
            trend = "Emerging"
        else:
            trend = "Stable"

        cases = sorted(members, key=lambda r: -r["account_arr"])
        clusters.append({
            "name": name,
            "count": count,
            "arr": arr_total,
            "accounts": accounts,
            "score": score,
            "recent": recent,
            "momentum": momentum,
            "trend": trend,
            "cases": cases,
        })

    clusters.sort(key=lambda c: -c["score"])
    return clusters


def load_data(db_path: str):
    """Load RFE data from the latest run."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get latest run_id
    row = conn.execute("SELECT run_id FROM run_meta ORDER BY rowid DESC LIMIT 1").fetchone()
    if not row:
        conn.close()
        return []
    run_id = row["run_id"]

    rows = conn.execute(
        """SELECT case_number, subject, description, account_name, account_arr,
                  status, domain, sub_domain, severity, created_date
           FROM rfe_pulls WHERE run_id = ?""",
        (run_id,)
    ).fetchall()
    conn.close()

    seen = set()
    result = []
    for r in rows:
        cn = (r["case_number"] or "").strip()
        subj = (r["subject"] or "").strip()
        if not cn or not subj:
            continue
        if cn in seen:
            continue
        seen.add(cn)

        # Parse created_date — handle "M/D/YYYY H:MM AM/PM" and ISO formats
        cd_raw = (r["created_date"] or "").strip()
        cd_iso = ""
        if cd_raw:
            for fmt in ("%m/%d/%Y %I:%M %p", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    cd_iso = datetime.strptime(cd_raw[:len(fmt)+5], fmt).strftime("%Y-%m-%d")
                    break
                except (ValueError, TypeError):
                    pass
            if not cd_iso:
                try:
                    datetime.strptime(cd_raw[:10], "%Y-%m-%d")
                    cd_iso = cd_raw[:10]
                except (ValueError, TypeError):
                    cd_iso = ""

        result.append({
            "case_number": cn,
            "subject": subj,
            "description": (r["description"] or "").strip(),
            "account_name": (r["account_name"] or "").strip(),
            "account_arr": float(r["account_arr"] or 0),
            "status": (r["status"] or "").strip(),
            "domain": (r["domain"] or "").strip(),
            "sub_domain": (r["sub_domain"] or "").strip(),
            "severity": (r["severity"] or "").strip(),
            "created_date": cd_iso,
        })

    return result


def build_domain_data(records: List[Dict]) -> Dict[str, List[Dict]]:
    """Group records by domain and cluster each group."""
    by_domain: Dict[str, List[Dict]] = defaultdict(list)
    for r in records:
        domain = classify_domain(r["domain"], r["subject"], r["description"])
        r["_domain"] = domain
        by_domain[domain].append(r)
    return {d: cluster_domain(rfes) for d, rfes in by_domain.items()}


def _fmt_arr_py(v: float) -> str:
    """Format ARR for Python-rendered HTML."""
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    elif v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


def _esc(s: str) -> str:
    """HTML-escape a string."""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _render_cases_table(cases: List[Dict]) -> str:
    """Render a sub-table of cases in Python (for exec epics)."""
    rows = ""
    for c in cases:
        sev_class = {"High": "sev-high", "Medium": "sev-medium", "Low": "sev-low"}.get(c.get("severity", ""), "")
        cn = _esc(c.get("case_number", ""))
        rows += f"""<tr>
  <td><span class="case-num" onclick="copyCaseNum('{cn}', this)">
    {cn}
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
  </span></td>
  <td>{_esc(c.get('subject',''))}</td>
  <td>{_esc(c.get('account_name',''))}</td>
  <td>{_fmt_arr_py(float(c.get('account_arr') or 0))}</td>
  <td>{_esc(str(c.get('created_date','') or '')[:10])}</td>
  <td class="{sev_class}">{_esc(c.get('severity',''))}</td>
  <td class="pm-summary">{_esc(c.get('description',''))}</td>
</tr>"""
    return f"""<table class="sub-table"><thead><tr>
  <th>Case #</th><th>Subject</th><th>Account</th><th>ARR</th><th>Opened</th><th>Severity</th><th style="min-width:200px">PM Summary</th>
</tr></thead><tbody>{rows}</tbody></table>"""


def generate_report(db_path: str) -> str:
    """Generate a standalone HTML PI Planning report from the SQLite DB."""
    records = load_data(db_path)

    if not records:
        return """<!DOCTYPE html><html><head><title>PI Report</title></head>
<body style="font-family:sans-serif;padding:40px;text-align:center;color:#6b7280">
<h2>No RFE data found</h2>
<p>Import a CSV or pull from Salesforce first, then regenerate the report.</p>
</body></html>"""

    # Classify domains and build clusters
    domain_clusters = build_domain_data(records)

    # Assign cluster name back to each record so ALL_RECORDS.cluster is correct
    for domain, clusters in domain_clusters.items():
        for cluster in clusters:
            for case in cluster["cases"]:
                case["_cluster"] = cluster["name"]

    # Build executive top-5 across all domains
    all_clusters = []
    for domain, clusters in domain_clusters.items():
        for c in clusters:
            all_clusters.append(dict(c, domain=domain))
    all_clusters.sort(key=lambda c: -c["score"])
    top5_global = all_clusters[:5]

    # ── Domain → page ID mapping (matches reference JS renderDomainCharts) ────
    DOMAIN_PAGE_ID = {
        "EPP": "epp",
        "Email Security": "email",
        "SIEM": "siem",
        "Identity": "identity",
        "CSPM": "cspm",
        "Platform": "platform",
        "Reporting": "reporting",
        "User Management": "usermgmt",
        "Endpoint Management": "endpointmgmt",
        "Alert UI": "alertui",
        "AI Initiatives": "ai",
        "Automations": "automations",
        "Playbooks": "playbooks",
        "Remediation": "remediation",
        "PSA/RMM": "psarmm",
        "API": "api",
        "Other": "other",
    }

    # ── Build ALL_RECORDS in reference format ─────────────────────────────────
    # Reference fields: {case, account, subject, description, arr, severity, opened, status, domain, cluster}
    all_records_list = []
    for r in records:
        domain_classified = r.get("_domain", "Other")
        cluster_name = r.get("_cluster", "")
        all_records_list.append({
            "case":        r.get("case_number", ""),
            "account":     r.get("account_name", ""),
            "subject":     r.get("subject", ""),
            "description": r.get("description", ""),
            "arr":         float(r.get("account_arr") or 0),
            "severity":    r.get("severity", ""),
            "opened":      r.get("created_date", ""),
            "status":      r.get("status", ""),
            "domain":      domain_classified,
            "cluster":     cluster_name,
        })

    def _safe_json(obj) -> str:
        s = json.dumps(obj, ensure_ascii=False)
        return s.replace("</script", r"<\/script")

    all_records_js = _safe_json(all_records_list)

    # ── Build TOP5_GLOBAL in reference format ─────────────────────────────────
    # Cases inside TOP5_GLOBAL must also use reference field names
    top5_global_js_list = []
    for c in top5_global:
        cases_ref = []
        for case in c.get("cases", []):
            domain_classified = case.get("_domain", c.get("domain", "Other"))
            cases_ref.append({
                "case":        case.get("case_number", ""),
                "account":     case.get("account_name", ""),
                "subject":     case.get("subject", ""),
                "description": case.get("description", ""),
                "arr":         float(case.get("account_arr") or 0),
                "severity":    case.get("severity", ""),
                "opened":      case.get("created_date", ""),
                "status":      case.get("status", ""),
            })
        top5_global_js_list.append({
            "cluster":  c["name"],
            "domain":   c["domain"],
            "count":    c["count"],
            "arr":      c["arr"],
            "score":    c["score"],
            "trend":    c["trend"],
            "momentum": c["momentum"],
            "cases":    cases_ref,
        })
    top5_global_js = _safe_json(top5_global_js_list)

    # ── Build DOMAIN_STATS in reference format ────────────────────────────────
    # Reference: {domain, rfe, arr, accts, score}
    domain_stats_list = []
    for d in DOMAIN_ORDER[1:]:  # skip Executive
        clusters = domain_clusters.get(d, [])
        if not clusters:
            continue
        rfe_count = sum(c["count"] for c in clusters)
        arr_total = sum(c["arr"] for c in clusters)
        accts = len({
            acct
            for c in clusters
            for acct in c.get("accounts", [])
        })
        score = arr_total + rfe_count * 50000
        domain_stats_list.append({
            "domain": d,
            "rfe":    rfe_count,
            "arr":    arr_total,
            "accts":  accts,
            "score":  score,
        })
    # Keep DOMAIN_ORDER ordering (not sorted by ARR) to match reference JS that uses DOMAIN_STATS.map
    domain_stats_js = _safe_json(domain_stats_list)

    # ── Executive summary text ────────────────────────────────────────────────
    total_arr = sum(r.get("account_arr", 0) for r in records)
    total_rfes = len(records)
    sorted_by_arr = sorted(domain_stats_list, key=lambda x: -x["arr"])
    top_domains = [d["domain"] for d in sorted_by_arr[:3]]
    accel_themes = [c["name"] for c in all_clusters if c["trend"] == "Accelerating"][:3]

    exec_summary = (
        f"This report covers {total_rfes:,} unique RFE signals aggregated across "
        f"{len(domain_clusters)} product domains, representing "
        f"{_fmt_arr_py(total_arr)} in combined customer ARR. "
        f"The highest-demand domains by ARR impact are {', '.join(top_domains[:3])}. "
    )
    if accel_themes:
        exec_summary += (
            f"Accelerating themes include: {'; '.join(accel_themes[:3])}. "
        )
    exec_summary += (
        f"The top 5 strategic epics span {len({c['domain'] for c in top5_global})} "
        f"domains and collectively represent a significant portion of total customer ARR pressure. "
        f"Use this report to prioritize PI Planning commitments and validate roadmap alignment."
    )

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ── Date range label for header ───────────────────────────────────────────
    opened_dates = [r["opened"] for r in all_records_list if r["opened"]]
    if opened_dates:
        opened_dates.sort()
        date_range = f"{opened_dates[0][:7]} – {opened_dates[-1][:7]}"
    else:
        date_range = ""
    header_subtitle = f"{total_rfes:,} RFEs" + (f" · {date_range}" if date_range else "")

    # ── Build executive epic cards ────────────────────────────────────────────
    exec_epics_html = ""
    for i, c in enumerate(top5_global):
        domain_label = c.get("domain", "")
        exec_epics_html += f"""<div class="epic-card" id="g-epic-{i}" onclick="toggleGlobalEpic({i})">
          <div class="epic-rank">#{i+1} {_esc(domain_label)}</div>
          <div class="epic-title">{_esc(c['name'])}</div>
          <div class="epic-meta">
            <span class="epic-pill rfe">{c['count']} RFEs</span>
            <span class="epic-pill arr">{_fmt_arr_py(c['arr'])}</span>
          </div>
        </div>"""

    # ── Build domain pages with pre-rendered static content ──────────────────
    def _trend_badge(trend: str) -> str:
        cls = {
            "Accelerating": "badge-accelerating",
            "Persistent": "badge-persistent",
            "Emerging": "badge-emerging",
            "Stable": "badge-stable",
        }.get(trend, "badge-stable")
        return f'<span class="badge {cls}">{_esc(trend)}</span>'

    def _dup_label(count: int) -> str:
        if count >= 10: return "High"
        if count >= 5: return "Medium"
        return "Low"

    def _render_sub_table_rows(cases: List[Dict]) -> str:
        rows = ""
        for c in cases[:50]:
            cn = _esc(c.get("case_number", ""))
            sev = c.get("severity", "")
            sev_cls = {"High": "sev-high", "Medium": "sev-medium", "Low": "sev-low"}.get(sev, "")
            rows += (
                f'<tr>'
                f'<td><span class="case-num" title="Click to copy" onclick="copyCaseNum(\'{cn}\',this)">'
                f'<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">'
                f'<rect x="9" y="9" width="13" height="13" rx="2"/>'
                f'<path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>'
                f'&nbsp;{cn}</span></td>'
                f'<td style="max-width:200px">{_esc(c.get("subject",""))}</td>'
                f'<td>{_esc(c.get("account_name",""))}</td>'
                f'<td style="color:var(--green);font-weight:600;white-space:nowrap">{_fmt_arr_py(float(c.get("account_arr") or 0))}</td>'
                f'<td style="white-space:nowrap">{_esc(str(c.get("created_date","") or "")[:10])}</td>'
                f'<td><span class="{sev_cls}">{_esc(sev)}</span></td>'
                f'<td class="pm-summary-cell">{_esc(c.get("description","")) or "<em>No description available</em>"}</td>'
                f'</tr>\n'
            )
        return rows

    domain_pages_html = ""
    for d in DOMAIN_ORDER[1:]:  # skip Executive
        page_id = DOMAIN_PAGE_ID.get(d, d.lower().replace(" ", "").replace("/", ""))
        clusters = domain_clusters.get(d, [])
        total_rfe = sum(c["count"] for c in clusters)
        total_arr_d = sum(c["arr"] for c in clusters)
        uniq_accts = len({acct for c in clusters for acct in c.get("accounts", [])})

        # Domain summary text
        top5_d = clusters[:5]
        dom_summary = (
            f"{_esc(d)} demand spans {total_rfe} RFEs from {uniq_accts}+ unique accounts, "
            f"representing {_fmt_arr_py(total_arr_d)} in ARR."
        )
        if top5_d:
            top_c = top5_d[0]
            dom_summary += (
                f" {_esc(top_c['name'])} leads with {top_c['count']} RFEs"
                f" and {_fmt_arr_py(top_c['arr'])} ARR."
            )

        # Epic cards (top 5) — no inline expand detail (reference uses CSS .expanded class only)
        epic_cards = ""
        for i, c in enumerate(top5_d):
            epic_cards += (
                f'<div class="epic-card" id="epic-{page_id}-{i}" onclick="toggleEpic(\'{page_id}\',{i})">\n'
                f'          <div class="epic-rank">#{i+1}</div>\n'
                f'          <div class="epic-title">{_esc(c["name"])}</div>\n'
                f'          <div class="epic-meta">\n'
                f'            <span class="epic-pill rfe">{c["count"]} RFEs</span>\n'
                f'            <span class="epic-pill arr">{_fmt_arr_py(c["arr"])}</span>\n'
                f'          </div>\n'
                f'        </div>'
            )

        # Main table rows
        table_rows = ""
        for i, c in enumerate(clusters):
            sub_rows = _render_sub_table_rows(c.get("cases", []))
            table_rows += (
                f'<tr class="main-row" onclick="toggleRow(\'{page_id}\',{i})">'
                f'<td><strong>{_esc(c["name"])}</strong></td>'
                f'<td style="color:var(--green);font-weight:600">{_fmt_arr_py(c["arr"])}</td>'
                f'<td>{len(c.get("accounts", []))}</td>'
                f'<td>{c["count"]}</td>'
                f'<td>{_dup_label(c["count"])}</td>'
                f'<td>{_trend_badge(c["trend"])}</td>'
                f'</tr>\n'
                f'<tr class="expand-row" id="row-{page_id}-{i}">'
                f'<td colspan="6" style="padding:0">'
                f'<div class="expand-content">'
                f'<table class="sub-table"><thead><tr>'
                f'<th>Case #</th><th>Subject</th><th>Account</th><th>ARR</th><th>Opened</th><th>Severity</th><th>PM Summary</th>'
                f'</tr></thead><tbody>{sub_rows}</tbody></table>'
                f'</div></td></tr>\n'
            )

        domain_pages_html += f"""
<div class="page" id="{page_id}">
  <div class="domain-header">
    <h2>&#128193; {_esc(d)}</h2>
    <div style="display:flex;gap:16px;margin-top:6px;flex-wrap:wrap">
      <span style="font-size:12px;color:#374151"><strong style="color:var(--accent)">{total_rfe}</strong> RFEs</span>
      <span style="font-size:12px;color:#374151"><strong style="color:var(--green)">{_fmt_arr_py(total_arr_d)}</strong> ARR</span>
      <span style="font-size:12px;color:#374151"><strong>{uniq_accts}</strong> Unique Accounts</span>
    </div>
    <p>{dom_summary}</p>
  </div>

  <div class="epics-panel full-width">
    <div class="card-title">Top 5 Strategic Epics</div>
    <div class="epics-grid">{epic_cards}</div>
  </div>

  <div class="card full-width">
    <div class="card-title" style="display:flex;justify-content:space-between;align-items:center">
      <span>Top Strategic Requests</span>
      <div class="sort-controls">
        <span>Sort by:</span>
        <button class="sort-btn active" onclick="sortDomain('{page_id}','arr',this)">ARR</button>
        <button class="sort-btn" onclick="sortDomain('{page_id}','count',this)">RFE Count</button>
        <button class="sort-btn" onclick="sortDomain('{page_id}','recent',this)">Recent</button>
      </div>
    </div>
    <div style="overflow-x:auto">
      <table class="rfe-table" id="tbl-{page_id}">
        <thead><tr>
          <th>Request</th>
          <th>Total ARR</th>
          <th>Customers</th>
          <th>RFEs</th>
          <th>Duplication</th>
          <th>Trend</th>
        </tr></thead>
        <tbody id="tbody-{page_id}">{table_rows}</tbody>
      </table>
    </div>
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-title">ARR by Cluster</div>
      <div id="chart-arr-{page_id}" style="min-height:280px"></div>
    </div>
    <div class="card">
      <div class="card-title">Unique Customers by Cluster</div>
      <div id="chart-cust-{page_id}" style="min-height:280px"></div>
    </div>
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-title">Momentum Bubble Chart</div>
      <div id="chart-bubble-{page_id}" style="min-height:280px"></div>
    </div>
    <div class="card">
      <div class="card-title">RFE Volume Trend</div>
      <div id="chart-trend-{page_id}" style="min-height:200px"></div>
    </div>
  </div>
</div>
"""

    domain_order_json = _safe_json(DOMAIN_ORDER[1:])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PI Planning RFE Strategic Demand Report</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.26.0/plotly.min.js"></script>
<style>
:root{{
  --bg:#f7f8fa;--bg2:#ffffff;--bg3:#f4f6fa;--border:#e1e5ec;
  --accent:#2563eb;--accent2:#7c3aed;--green:#16a34a;--orange:#d97706;
  --red:#dc2626;--text:#111827;--muted:#6b7280;
  --card-shadow:0 1px 4px rgba(0,0,0,0.07),0 0 0 1px #e1e5ec;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:"Segoe UI",Arial,sans-serif;font-size:14px;min-height:100vh}}
#top-bar{{background:var(--bg2);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;box-shadow:0 1px 3px rgba(0,0,0,0.06)}}
#logo-bar{{display:flex;align-items:center;justify-content:space-between;padding:10px 20px 6px;border-bottom:1px solid var(--border)}}
#logo-bar h1{{font-size:15px;font-weight:700;color:var(--accent)}}
#timeframe-toggle{{display:flex;gap:6px;align-items:center}}
#timeframe-toggle span{{color:var(--muted);font-size:12px;margin-right:4px}}
.tf-btn{{background:var(--bg3);border:1px solid var(--border);color:var(--muted);padding:4px 13px;border-radius:20px;cursor:pointer;font-size:12px;font-weight:500;transition:all .18s}}
.tf-btn.active{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.tf-btn:hover:not(.active){{border-color:var(--accent);color:var(--accent)}}
#nav-tabs{{display:flex;overflow-x:auto;padding:0 16px;scrollbar-width:thin}}
#nav-tabs::-webkit-scrollbar{{height:3px}}
#nav-tabs::-webkit-scrollbar-thumb{{background:var(--border)}}
.nav-tab{{padding:9px 13px;cursor:pointer;color:var(--muted);font-size:12px;font-weight:500;border-bottom:2px solid transparent;white-space:nowrap;transition:all .18s;flex-shrink:0}}
.nav-tab:hover{{color:var(--text)}}
.nav-tab.active{{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}}
.page{{display:none;padding:20px;max-width:1600px;margin:0 auto}}
.page.active{{display:block}}
.card{{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:16px;box-shadow:var(--card-shadow)}}
.card-title{{font-size:11px;font-weight:700;color:var(--muted);margin-bottom:12px;text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid var(--border);padding-bottom:8px}}
.exec-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}}
.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}}
.full-width{{margin-bottom:16px}}
@media(max-width:900px){{.exec-grid,.two-col{{grid-template-columns:1fr}}}}
.exec-summary{{background:linear-gradient(135deg,#eff6ff,#eef2ff);border:1px solid #bfdbfe;border-radius:10px;padding:16px 20px;margin-bottom:18px}}
.exec-summary h2{{font-size:12px;font-weight:700;color:var(--accent);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px}}
.exec-summary p{{font-size:13px;color:#374151;line-height:1.7}}
.domain-header{{background:linear-gradient(90deg,#eff6ff,#f5f3ff);border:1px solid #c7d2fe;border-radius:10px;padding:14px 18px;margin-bottom:16px}}
.domain-header h2{{font-size:15px;font-weight:700;color:var(--accent)}}
.domain-header p{{font-size:13px;color:#374151;margin-top:5px;line-height:1.55}}
.epics-panel{{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:16px;box-shadow:var(--card-shadow);margin-bottom:16px}}
.epics-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}}
@media(max-width:1200px){{.epics-grid{{grid-template-columns:repeat(3,1fr)}}}}
@media(max-width:700px){{.epics-grid{{grid-template-columns:1fr 1fr}}}}
.epic-card{{background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:12px 14px;cursor:pointer;transition:all .18s}}
.epic-card:hover{{border-color:var(--accent);box-shadow:0 2px 8px rgba(37,99,235,.1);transform:translateY(-1px)}}
.epic-card.expanded{{border-color:var(--accent);background:#eff6ff}}
.epic-rank{{font-size:11px;font-weight:700;color:var(--accent);margin-bottom:4px}}
.epic-title{{font-size:12px;font-weight:600;color:var(--text);line-height:1.4;margin-bottom:8px;min-height:34px}}
.epic-meta{{display:flex;gap:6px;flex-wrap:wrap}}
.epic-pill{{font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px}}
.epic-pill.rfe{{background:#dbeafe;color:#1d4ed8}}
.epic-pill.arr{{background:#dcfce7;color:#15803d}}
.no-signal{{background:#f9fafb;border:1px dashed #d1d5db;border-radius:8px;padding:20px 12px;text-align:center;color:var(--muted);font-size:11px}}
.rfe-table{{width:100%;border-collapse:collapse;font-size:12px}}
.rfe-table th{{background:var(--bg3);color:var(--muted);padding:7px 10px;text-align:left;font-weight:600;text-transform:uppercase;font-size:10px;letter-spacing:.4px;border-bottom:1px solid var(--border);cursor:pointer;white-space:nowrap}}
.rfe-table th:hover{{color:var(--accent)}}
.rfe-table td{{padding:8px 10px;border-bottom:1px solid rgba(225,229,236,.7);vertical-align:top}}
.rfe-table tr.main-row{{cursor:pointer;transition:background .15s}}
.rfe-table tr.main-row:hover{{background:#f0f7ff}}
.expand-row{{display:none}}
.expand-row.open{{display:table-row}}
.expand-content{{background:#f8fbff;border-top:1px solid #dbeafe;padding:12px 16px}}
.sub-table{{width:100%;border-collapse:collapse;font-size:11px}}
.sub-table th{{background:#eff6ff;color:#4b5563;padding:6px 8px;text-align:left;border-bottom:1px solid #dbeafe;font-weight:600;font-size:10px;text-transform:uppercase}}
.sub-table td{{padding:7px 8px;border-bottom:1px solid #e0ebff;vertical-align:top}}
.sub-table tr:last-child td{{border-bottom:none}}
.pm-summary-cell{{white-space:normal!important;word-wrap:break-word;overflow:visible;max-width:400px;line-height:1.5;color:#374151}}
.case-num{{font-family:monospace;font-size:11px;color:var(--accent);cursor:pointer;display:inline-flex;align-items:center;gap:3px;padding:2px 5px;border-radius:4px;border:1px solid #bfdbfe;background:#eff6ff;transition:all .15s}}
.case-num:hover{{background:#dbeafe;border-color:var(--accent)}}
.copy-flash{{color:var(--green)!important;background:#dcfce7!important;border-color:#86efac!important}}
.badge{{display:inline-block;padding:2px 7px;border-radius:10px;font-size:10px;font-weight:600}}
.badge-emerging{{background:#dcfce7;color:#15803d;border:1px solid #bbf7d0}}
.badge-accelerating{{background:#fef3c7;color:#92400e;border:1px solid #fde68a}}
.badge-persistent{{background:#dbeafe;color:#1d4ed8;border:1px solid #bfdbfe}}
.badge-stable{{background:#f3f4f6;color:#6b7280;border:1px solid #e5e7eb}}
.sev-high{{color:var(--red);font-weight:600}}
.sev-medium{{color:var(--orange);font-weight:600}}
.sev-low{{color:var(--green);font-weight:600}}
.sort-controls{{display:flex;gap:8px;align-items:center}}
.sort-btn{{background:var(--bg3);border:1px solid var(--border);color:var(--muted);padding:4px 10px;border-radius:4px;cursor:pointer;font-size:11px;font-weight:500;transition:all .15s}}
.sort-btn.active{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.sort-btn:hover:not(.active){{color:var(--accent);border-color:var(--accent)}}
.interp-panel{{background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:14px}}
.interp-item{{margin-bottom:10px}}
.interp-item:last-child{{margin-bottom:0}}
.interp-label{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}}
.interp-text{{font-size:12px;color:#374151;line-height:1.5}}
.action-panel{{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:14px}}
.action-item{{display:flex;gap:10px;margin-bottom:10px;align-items:flex-start}}
.action-item:last-child{{margin-bottom:0}}
.action-num{{background:var(--accent);color:#fff;border-radius:50%;width:20px;height:20px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;flex-shrink:0;margin-top:1px}}
.action-text{{font-size:12px;color:#111827;line-height:1.5}}
footer{{text-align:center;padding:16px;color:#9ca3af;font-size:11px;border-top:1px solid var(--border);margin-top:20px}}
.ranking-note{{font-size:10px;font-weight:400;color:var(--muted);text-transform:none;letter-spacing:0}}
</style>
</head>
<body>
<div id="top-bar">
  <div id="logo-bar">
    <h1>&#128202; PI Planning RFE Strategic Demand Report &nbsp;<span style="font-size:11px;color:var(--muted);font-weight:400">{header_subtitle}</span></h1>
    <div id="timeframe-toggle">
      <span>Timeframe:</span>
      <button class="tf-btn active" onclick="setTimeframe(12,this)">Last 12 mo</button>
      <button class="tf-btn" onclick="setTimeframe(6,this)">Last 6 mo</button>
      <button class="tf-btn" onclick="setTimeframe(3,this)">Last 3 mo</button>
      <button class="tf-btn" onclick="setTimeframe(0,this)">All Time</button>
    </div>
  </div>
  <div id="nav-tabs">
    <div class="nav-tab active" onclick="showPage('exec',this)">Executive</div>
    <div class="nav-tab" onclick="showPage('epp',this)">EPP</div>
    <div class="nav-tab" onclick="showPage('email',this)">Email Security</div>
    <div class="nav-tab" onclick="showPage('siem',this)">SIEM</div>
    <div class="nav-tab" onclick="showPage('identity',this)">Identity</div>
    <div class="nav-tab" onclick="showPage('cspm',this)">CSPM</div>
    <div class="nav-tab" onclick="showPage('platform',this)">Platform</div>
    <div class="nav-tab" onclick="showPage('reporting',this)">Reporting</div>
    <div class="nav-tab" onclick="showPage('usermgmt',this)">User Management</div>
    <div class="nav-tab" onclick="showPage('endpointmgmt',this)">Endpoint Management</div>
    <div class="nav-tab" onclick="showPage('alertui',this)">Alert UI</div>
    <div class="nav-tab" onclick="showPage('ai',this)">AI Initiatives</div>
    <div class="nav-tab" onclick="showPage('automations',this)">Automations</div>
    <div class="nav-tab" onclick="showPage('playbooks',this)">Playbooks</div>
    <div class="nav-tab" onclick="showPage('remediation',this)">Remediation</div>
    <div class="nav-tab" onclick="showPage('psarmm',this)">PSA/RMM</div>
    <div class="nav-tab" onclick="showPage('api',this)">API</div>
    <div class="nav-tab" onclick="showPage('other',this)">Other</div>
  </div>
</div>

<div class="page active" id="exec">
  <div class="exec-summary">
    <h2>Executive Summary</h2>
    <p>{_esc(exec_summary)}</p>
  </div>

  <div class="epics-panel full-width">
    <div class="card-title">Top 5 Strategic Epics <span class="ranking-note">— ranked by ARR signal + request volume (ARR + RFE count × $80K)</span></div>
    <div class="epics-grid" id="exec-epics-grid"></div>
    <div id="g-epic-expand" style="margin-top:12px"></div>
  </div>

  <div class="exec-grid">
    <div class="card">
      <div class="card-title">Portfolio Demand Snapshot</div>
      <div id="chart-portfolio" style="min-height:320px"></div>
    </div>
    <div class="card">
      <div class="card-title">Top 10 Most Important Domains</div>
      <div id="chart-top10" style="min-height:320px"></div>
    </div>
  </div>

  <div class="card full-width">
    <div class="card-title">Trends Chart</div>
    <div id="chart-trends" style="min-height:400px"></div>
    <div id="trend-detail-container" style="margin-top:12px"></div>
  </div>
</div>

{domain_pages_html}

<footer>Generated {generated_at}</footer>

<script>
const ALL_RECORDS = {all_records_js};
const TOP5_GLOBAL = {top5_global_js};
const DOMAIN_STATS = {domain_stats_js};
function mkLayout(overrides){{
  return Object.assign({{
    paper_bgcolor:'white',plot_bgcolor:'white',
    font:{{color:'#374151',family:"'Segoe UI',Arial,sans-serif",size:11}},
    xaxis:{{gridcolor:'#f3f4f6',zeroline:false}},
    yaxis:{{gridcolor:'#f3f4f6',zeroline:false,automargin:true}},
    autosize:true
  }}, overrides);
}}
const PLOTLY_CFG = {{responsive:true,displayModeBar:false}};
let curPage='exec', curMonths=12, renderedPages=new Set(['exec']);

function showPage(id,tabEl){{
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  tabEl.classList.add('active');
  curPage=id;
  if(!renderedPages.has(id)){{ renderedPages.add(id); renderDomainCharts(id); }}
}}

function setTimeframe(months,btn){{
  document.querySelectorAll('.tf-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  curMonths=months;
  renderedPages.forEach(pid=>{{
    if(pid==='exec') renderExecCharts();
    else renderDomainCharts(pid);
  }});
}}

function filterRecords(months){{
  if(!months) return ALL_RECORDS;
  const cutoff=new Date(); cutoff.setMonth(cutoff.getMonth()-months);
  return ALL_RECORDS.filter(r=>r.opened && new Date(r.opened)>=cutoff);
}}

function filterDomainChartData(domain,months){{
  const recs=filterRecords(months).filter(r=>r.domain===domain);
  const agg={{}};
  recs.forEach(r=>{{
    if(!agg[r.cluster]) agg[r.cluster]={{cluster:r.cluster,arr:0,count:0,customers:new Set()}};
    agg[r.cluster].arr+=r.arr||0;
    agg[r.cluster].count++;
    agg[r.cluster].customers.add(r.account);
  }});
  return Object.values(agg).map(v=>({{...v,customers:v.customers.size}}));
}}

function fmtArr(v){{
  if(v>=1e6) return '$'+(v/1e6).toFixed(2)+'M';
  if(v>=1e3) return '$'+(v/1e3).toFixed(0)+'K';
  return '$'+v.toFixed(0);
}}

function copyCaseNum(c,el){{
  event.stopPropagation();
  navigator.clipboard.writeText(c).then(()=>{{
    el.classList.add('copy-flash');
    const orig=el.innerHTML;
    el.innerHTML='✓ Copied!';
    setTimeout(()=>{{el.classList.remove('copy-flash');el.innerHTML=orig;}},1500);
  }});
}}

function renderExecEpics(filtered){{
  const agg={{}};
  filtered.forEach(r=>{{
    if(!r.cluster) return;
    const key=r.domain+'||'+r.cluster;
    if(!agg[key]) agg[key]={{cluster:r.cluster,domain:r.domain,arr:0,count:0,cases:[]}};
    agg[key].arr+=r.arr||0;
    agg[key].count++;
    agg[key].cases.push(r);
  }});
  const top5=Object.values(agg)
    .map(c=>({{...c,score:c.arr+c.count*80000}}))
    .sort((a,b)=>b.score-a.score)
    .slice(0,5);
  window._currentTop5=top5;
  const grid=document.getElementById('exec-epics-grid');
  if(!grid) return;
  let html='';
  top5.forEach((c,i)=>{{
    html+=`<div class="epic-card" id="g-epic-${{i}}" onclick="toggleGlobalEpic(${{i}})">
      <div class="epic-rank">#${{i+1}} <span style="color:var(--muted);font-weight:500">${{c.domain}}</span></div>
      <div class="epic-title">${{c.cluster}}</div>
      <div class="epic-meta">
        <span class="epic-pill rfe">${{c.count}} RFEs</span>
        <span class="epic-pill arr">${{fmtArr(c.arr)}}</span>
      </div>
    </div>`;
  }});
  for(let j=top5.length;j<5;j++) html+='<div class="no-signal">No additional clusters with material signal</div>';
  grid.innerHTML=html;
  document.getElementById('g-epic-expand').innerHTML='';
}}

function toggleGlobalEpic(i){{
  const epics=window._currentTop5||[];
  const epic=epics[i];
  if(!epic) return;
  const card=document.getElementById('g-epic-'+i);
  const container=document.getElementById('g-epic-expand');
  const isOpen=card&&card.classList.contains('expanded');
  document.querySelectorAll('#exec-epics-grid .epic-card').forEach(c=>c.classList.remove('expanded'));
  if(isOpen){{container.innerHTML='';return;}}
  if(card) card.classList.add('expanded');
  let rows='';
  (epic.cases||[]).forEach(c=>{{
    const caseNum=c.case||'';
    rows+=`<tr>
      <td><span class="case-num" title="Click to copy" onclick="copyCaseNum('${{caseNum}}',this)"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>&nbsp;${{caseNum}}</span></td>
      <td style="max-width:220px">${{(c.subject||'').slice(0,90)}}${{(c.subject||'').length>90?'…':''}}</td>
      <td>${{c.account||''}}</td>
      <td style="color:var(--green);font-weight:600;white-space:nowrap">${{fmtArr(c.arr||0)}}</td>
      <td style="white-space:nowrap">${{c.opened||''}}</td>
      <td class="pm-summary-cell">${{c.description||'<em style=\\'color:var(--muted)\\'>No description available</em>'}}</td>
    </tr>`;
  }});
  container.innerHTML=`<div class="expand-content" style="border-radius:8px;border:1px solid #bfdbfe;background:#f8fbff;margin-top:8px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <strong style="color:#1d4ed8;font-size:13px">${{epic.cluster}} <span style="color:var(--muted);font-weight:400">— ${{epic.domain}}</span></strong>
      <button onclick="document.getElementById('g-epic-expand').innerHTML='';document.querySelectorAll('#exec-epics-grid .epic-card').forEach(c=>c.classList.remove('expanded'))" style="background:none;border:1px solid var(--border);border-radius:4px;padding:3px 8px;cursor:pointer;font-size:11px">✕ Close</button>
    </div>
    <div style="overflow-x:auto"><table class="sub-table">
      <thead><tr><th>Case #</th><th>Subject</th><th>Account</th><th>ARR</th><th>Opened</th><th>PM Summary</th></tr></thead>
      <tbody>${{rows}}</tbody>
    </table></div>
  </div>`;
}}

function toggleEpic(containerId,i){{
  const epicsInPage=document.querySelectorAll(`[id^="epic-${{containerId}}-"]`);
  const card=document.getElementById(`epic-${{containerId}}-${{i}}`);
  if(!card) return;
  const isOpen=card.classList.contains('expanded');
  epicsInPage.forEach(c=>c.classList.remove('expanded'));
  const detail=document.getElementById(`epic-detail-${{containerId}}`);
  if(detail) detail.remove();
  if(isOpen) return;
  card.classList.add('expanded');
}}

function toggleRow(domain,i){{
  const row=document.getElementById(`row-${{domain}}-${{i}}`);
  if(!row) return;
  row.classList.toggle('open');
}}

function sortDomain(pageId,by,btn){{
  btn.closest('.sort-controls').querySelectorAll('.sort-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  const tbody=document.getElementById(`tbody-${{pageId}}`);
  if(!tbody) return;
  const rows=Array.from(tbody.querySelectorAll('.main-row'));
  const pairs=rows.map((r,i)=>{{
    const expRow=document.getElementById(`row-${{pageId}}-${{i}}`);
    return [r,expRow];
  }});
  const getVal=(r)=>{{
    const tds=r.querySelectorAll('td');
    if(by==='arr'){{const t=tds[1]?.textContent||'0';return parseFloat(t.replace(/[\\$MK,]/g,''))*((t.includes('M')?1e6:t.includes('K')?1e3:1));}}
    if(by==='count')return parseInt(tds[3]?.textContent||0);
    if(by==='recent')return parseInt(tds[3]?.textContent||0);
    return 0;
  }};
  pairs.sort((a,b)=>getVal(b[0])-getVal(a[0]));
  pairs.forEach(([mr,er])=>{{tbody.appendChild(mr);if(er)tbody.appendChild(er);}});
}}

function renderExecCharts(){{
  const filtered=filterRecords(curMonths);
  renderExecEpics(filtered);
  const domMap={{}};
  filtered.forEach(r=>{{
    if(!domMap[r.domain]) domMap[r.domain]={{rfe:0,arr:0,accts:new Set()}};
    domMap[r.domain].rfe++;
    domMap[r.domain].arr+=r.arr||0;
    domMap[r.domain].accts.add(r.account);
  }});
  // Top 10 domains by score for portfolio chart (avoid label crowding)
  const domScored=DOMAIN_STATS.map(d=>{{
    const dm=domMap[d.domain]||{{rfe:0,arr:0,accts:new Set()}};
    return {{domain:d.domain,rfe:dm.rfe||0,arr:dm.arr||0,accts:dm.accts?dm.accts.size:0,score:(dm.arr||0)+(dm.rfe||0)*50000}};
  }}).sort((a,b)=>b.score-a.score).slice(0,10);
  Plotly.newPlot('chart-portfolio',[
    {{name:'RFEs',x:domScored.map(d=>d.domain),y:domScored.map(d=>d.rfe),type:'bar',marker:{{color:'rgba(59,130,246,0.85)'}}}},
    {{name:'Customers',x:domScored.map(d=>d.domain),y:domScored.map(d=>d.accts),type:'bar',marker:{{color:'rgba(139,92,246,0.8)'}}}},
    {{name:'ARR $M',x:domScored.map(d=>d.domain),y:domScored.map(d=>d.arr/1e6),type:'bar',yaxis:'y2',marker:{{color:'rgba(16,185,129,0.85)'}}}},
  ],mkLayout({{
    barmode:'group',height:380,
    xaxis:{{gridcolor:'#f3f4f6',zeroline:false,tickangle:-35,automargin:true}},
    yaxis:{{gridcolor:'#f3f4f6',zeroline:false,rangemode:'tozero',title:'Count',titlefont:{{size:10}}}},
    yaxis2:{{overlaying:'y',side:'right',title:'ARR $M',titlefont:{{size:10}},gridcolor:'#f3f4f6',zeroline:false,rangemode:'tozero'}},
    legend:{{orientation:'h',y:-0.28,x:0.5,xanchor:'center'}},
    margin:{{t:20,b:120,l:50,r:70}}
  }}),PLOTLY_CFG);
  const scored=DOMAIN_STATS.map(d=>{{
    const dm=domMap[d.domain]||{{rfe:0,arr:0}};
    return {{...d,score:dm.arr+dm.rfe*50000,rfe:dm.rfe,arr:dm.arr}};
  }}).sort((a,b)=>b.score-a.score).slice(0,10);
  const opacities=scored.map((_,i)=>Math.max(0.3,1-i*0.07));
  Plotly.newPlot('chart-top10',[{{
    type:'bar',orientation:'h',
    x:scored.map(d=>d.arr/1e6),
    y:scored.map(d=>d.domain+' | '+fmtArr(d.arr)+' | '+d.rfe+' RFEs'),
    marker:{{color:opacities.map(o=>`rgba(37,99,235,${{o}})`)}}
  }}],mkLayout({{height:340,xaxis:{{gridcolor:'#f3f4f6',zeroline:false,title:'ARR $M'}},yaxis:{{gridcolor:'#f3f4f6',zeroline:false,automargin:true,type:'category'}},margin:{{t:20,b:40,l:320,r:20}}}}),PLOTLY_CFG);
  const clusterMap={{}};
  filtered.forEach(r=>{{
    const key=r.domain+'||'+r.cluster;
    if(!clusterMap[key]) clusterMap[key]={{cluster:r.cluster,domain:r.domain,arr:0,count:0,recent:0,accts:new Set()}};
    clusterMap[key].arr+=r.arr||0;
    clusterMap[key].count++;
    const d=new Date(r.opened); const cutoff=new Date(); cutoff.setMonth(cutoff.getMonth()-6);
    if(d>=cutoff) clusterMap[key].recent++;
    clusterMap[key].accts.add(r.account);
  }});
  const cls=Object.values(clusterMap).map(c=>{{
    const mom=(c.recent/Math.max(c.count,1))*c.count*0.7+(c.arr/500000)*0.3;
    return {{...c,momentum:mom,label:c.domain+': '+c.cluster.slice(0,35)}};
  }}).sort((a,b)=>b.arr-a.arr).slice(0,40);
  Plotly.newPlot('chart-trends',[{{
    type:'scatter',mode:'markers',
    x:cls.map(c=>c.momentum),
    y:cls.map(c=>c.label),
    marker:{{size:cls.map(c=>Math.max(8,Math.min(30,c.count*1.5))),color:cls.map(c=>c.arr/1e6),colorscale:'YlOrRd',showscale:true,colorbar:{{title:'ARR $M',thickness:12}}}},
    text:cls.map(c=>c.cluster+': '+c.count+' RFEs, '+fmtArr(c.arr)),
    hovertemplate:'<b>%{{y}}</b><br>Momentum: %{{x:.2f}}<br>%{{text}}<extra></extra>'
  }}],mkLayout({{height:Math.max(420,cls.length*18+80),margin:{{t:20,b:40,l:340,r:90}},xaxis:{{gridcolor:'#f3f4f6',zeroline:false,title:'Momentum Score'}},yaxis:{{gridcolor:'#f3f4f6',zeroline:false,automargin:true,type:'category'}}}}),PLOTLY_CFG);
  const tc=document.getElementById('chart-trends');
  tc.removeAllListeners && tc.removeAllListeners('plotly_click');
  tc.on('plotly_click',function(evt){{
    const pt=evt.points[0]; if(!pt) return;
    const cObj=cls[pt.pointIndex];
    const cases=filtered.filter(r=>r.domain===cObj.domain && r.cluster===cObj.cluster);
    let rows='';
    cases.forEach(c=>{{
      rows+=`<tr>
        <td><span class="case-num" onclick="copyCaseNum('${{c.case}}',this);event.stopPropagation()"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>&nbsp;${{c.case}}</span></td>
        <td>${{c.subject.slice(0,70)}}</td><td>${{c.account}}</td>
        <td style="color:var(--green);font-weight:600">${{fmtArr(c.arr)}}</td>
        <td>${{c.opened}}</td>
        <td class="pm-summary-cell">${{c.description||'<em>No description</em>'}}</td>
      </tr>`;
    }});
    document.getElementById('trend-detail-container').innerHTML=`<div class="expand-content" style="border-radius:8px;border:1px solid #bfdbfe;background:#f8fbff">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <strong style="color:#1d4ed8">${{cObj.cluster}} — ${{cObj.domain}}</strong>
        <button onclick="document.getElementById('trend-detail-container').innerHTML=''" style="background:none;border:1px solid var(--border);border-radius:4px;padding:3px 8px;cursor:pointer;font-size:11px">✕ Close</button>
      </div>
      <div style="overflow-x:auto"><table class="sub-table">
        <thead><tr><th>Case #</th><th>Subject</th><th>Account</th><th>ARR</th><th>Opened</th><th>PM Summary</th></tr></thead>
        <tbody>${{rows}}</tbody>
      </table></div>
    </div>`;
  }});
}}

function renderDomainCharts(pageId){{
  const domainMap={{'epp':'EPP','email':'Email Security','siem':'SIEM','identity':'Identity','cspm':'CSPM','platform':'Platform','reporting':'Reporting','usermgmt':'User Management','endpointmgmt':'Endpoint Management','alertui':'Alert UI','ai':'AI Initiatives','automations':'Automations','playbooks':'Playbooks','remediation':'Remediation','psarmm':'PSA/RMM','api':'API','other':'Other'}};
  const domain=domainMap[pageId]; if(!domain) return;
  const items=filterDomainChartData(domain,curMonths).sort((a,b)=>b.arr-a.arr).slice(0,12);
  if(!items.length) return;
  // Wrap labels at 42 chars with ellipsis so y-axis stays readable
  const labels=items.map(c=>c.cluster.length>42?c.cluster.slice(0,40)+'…':c.cluster);
  const barH=Math.max(300,items.length*38+70);
  const barMargin={{t:15,b:45,l:0,r:20}};
  const barYaxis={{gridcolor:'#f3f4f6',zeroline:false,automargin:true,type:'category',tickfont:{{size:11}}}};
  // ARR bar chart — gradient blue
  const arrColors=items.map((_,i)=>`rgba(37,99,235,${{Math.max(0.35,0.9-i*0.055)}})`)
  Plotly.newPlot('chart-arr-'+pageId,[{{
    type:'bar',orientation:'h',
    x:items.map(c=>c.arr/1e6),y:labels,
    marker:{{color:arrColors}},
    text:items.map(c=>fmtArr(c.arr)),
    textposition:'outside',
    hovertemplate:'<b>%{{y}}</b><br>ARR: %{{text}}<extra></extra>'
  }}],mkLayout({{
    height:barH,margin:barMargin,
    xaxis:{{gridcolor:'#f3f4f6',zeroline:false,title:'ARR $M',titlefont:{{size:10}}}},
    yaxis:barYaxis
  }}),PLOTLY_CFG);
  // Customers bar chart — gradient purple
  const custColors=items.map((_,i)=>`rgba(124,58,237,${{Math.max(0.35,0.9-i*0.055)}})`)
  Plotly.newPlot('chart-cust-'+pageId,[{{
    type:'bar',orientation:'h',
    x:items.map(c=>c.customers),y:labels,
    marker:{{color:custColors}},
    text:items.map(c=>c.customers+' cust.'),
    textposition:'outside',
    hovertemplate:'<b>%{{y}}</b><br>Customers: %{{x}}<extra></extra>'
  }}],mkLayout({{
    height:barH,margin:barMargin,
    xaxis:{{gridcolor:'#f3f4f6',zeroline:false,title:'Unique Customers',titlefont:{{size:10}}}},
    yaxis:barYaxis
  }}),PLOTLY_CFG);
  // Bubble chart — momentum vs clusters
  Plotly.newPlot('chart-bubble-'+pageId,[{{
    type:'scatter',mode:'markers+text',
    x:items.map(c=>+(c.momentum||0).toFixed(2)),
    y:labels,
    marker:{{
      size:items.map(c=>Math.max(10,Math.min(32,c.count*2.5))),
      color:items.map(c=>c.arr/1e6),
      colorscale:'Blues',showscale:false,
      line:{{color:'rgba(37,99,235,0.4)',width:1}}
    }},
    text:items.map(c=>c.count>1?c.count+'×':''),
    textfont:{{size:9,color:'#374151'}},
    hovertemplate:'<b>%{{y}}</b><br>Momentum: %{{x}}<br>%{{customdata}}<extra></extra>',
    customdata:items.map(c=>c.count+' RFEs · '+fmtArr(c.arr))
  }}],mkLayout({{
    height:barH,margin:{{t:15,b:45,l:0,r:20}},
    xaxis:{{gridcolor:'#f3f4f6',zeroline:true,zerolinecolor:'#e5e7eb',title:'Momentum Score',titlefont:{{size:10}}}},
    yaxis:barYaxis
  }}),PLOTLY_CFG);
  // Trend over time — area chart
  const recs=filterRecords(curMonths).filter(r=>r.domain===domain);
  const monthly={{}};
  recs.forEach(r=>{{if(!r.opened) return;const m=r.opened.slice(0,7);monthly[m]=(monthly[m]||0)+1;}});
  const months=Object.keys(monthly).sort();
  Plotly.newPlot('chart-trend-'+pageId,[{{
    type:'scatter',mode:'lines+markers',
    x:months,y:months.map(m=>monthly[m]),
    fill:'tozeroy',
    line:{{color:'#2563eb',width:2}},
    marker:{{color:'#2563eb',size:5}},
    fillcolor:'rgba(37,99,235,0.08)',
    hovertemplate:'%{{x}}: <b>%{{y}}</b> RFEs<extra></extra>'
  }}],mkLayout({{
    height:220,margin:{{t:10,b:45,l:50,r:20}},
    xaxis:{{gridcolor:'#f3f4f6',zeroline:false,title:'Month',titlefont:{{size:10}},tickangle:-30}},
    yaxis:{{gridcolor:'#f3f4f6',zeroline:false,automargin:true,title:'RFEs',titlefont:{{size:10}},rangemode:'tozero'}}
  }}),PLOTLY_CFG);
  const pageEl=document.getElementById(pageId);
  if(!pageEl.querySelector('.domain-charts-rendered')){{
    const marker=document.createElement('span');
    marker.className='domain-charts-rendered';
    marker.style.display='none';
    pageEl.appendChild(marker);
    const domStats=filterRecords(curMonths).filter(r=>r.domain===domain);
    const totalRfe=domStats.length;
    const totalArr=domStats.reduce((s,r)=>s+(r.arr||0),0);
    const uniqAccts=new Set(domStats.map(r=>r.account)).size;
    const domHeader=pageEl.querySelector('.domain-header');
    if(domHeader){{
      domHeader.querySelector('h2').textContent='&#128193; '+domain;
      const statsEl=domHeader.querySelector('.dom-stats');
      if(statsEl){{
        statsEl.innerHTML='<span style="font-size:12px;color:#374151"><strong style="color:var(--accent)">'+totalRfe+'</strong> RFEs</span>'
          +'<span style="font-size:12px;color:#374151"><strong style="color:var(--green)">'+fmtArr(totalArr)+'</strong> ARR</span>'
          +'<span style="font-size:12px;color:#374151"><strong>'+uniqAccts+'</strong> Unique Accounts</span>';
      }}
    }}
  }}
}}

// Initial render
renderExecCharts();
</script>
</body>
</html>"""
