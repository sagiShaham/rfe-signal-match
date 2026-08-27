"""
PM Decision Summary writer
==========================
Writes the PM Decision Summary for every RFE **while the report is being
generated** — no button, no API key, no LLM call. A report is never handed to a
PM with summaries missing.

Follows Step 4 of the cynet-weekly-report skill, whose rules are absolute:

  1. NEVER paste raw description text into a summary.
  2. NEVER truncate with "..." — every summary is complete sentences.
  3. NEVER use the raw description as a fallback.
  4. The one allowed fallback (description empty/unreadable) is the mandated
     TAM follow-up flag.

Every summary answers the three questions the skill demands:
  1. what is missing or broken today (the gap, not a restatement of the subject)
  2. the operational consequence — what it blocks and who it hurts
  3. the PM routing signal, using the skill's exact flag wording

The inputs are the ones a PM would use: the **subject** (what surface is being
asked for), the **ARR** and severity (what it is worth), the **description**
(mined for facts — defect language, regressions, compliance exposure, named
platforms and vendors, scale numbers — never for prose), and the rest of the
dataset (sibling cases from the same account, and cases asking for the same
thing, which become cluster routing signals).

Deterministic: the same RFE always produces the same summary, so cached
summaries and freshly written ones never disagree.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "rules-v1"

# The skill's one permitted fallback — used only when there is no description.
NO_DESC_SUMMARY = (
    "⚠️ No description provided — follow up with TAM before routing. "
    "Do not proceed to backlog without PM review."
)

MIN_DESC = 20          # below this a description carries no usable signal


# ══════════════════════════════════════════════════════════════════════════════
# Subject parsing — what surface is being asked for
# ══════════════════════════════════════════════════════════════════════════════

# "[RFE]", "[EXTERNAL]", "RFE:", "Feature request -" …
_PREFIX_RE = re.compile(
    r"^\s*(?:(?:\[[^\]]{1,24}\]|rfe|feature request|enhancement request|fr|"
    r"inquiry(?:\s+(?:regarding|about|on))?|question\s+(?:regarding|about|on)|"
    r"request\s+for\s+implementation\s+of)\s*[:\-–—]?\s*)+",
    re.IGNORECASE,
)
# A short leading product tag: "WCF - ", "EPP: ", "CLM — "
_PRODUCT_TAG_RE = re.compile(r"^\s*([A-Za-z0-9/&+ ]{2,22}?)\s*[-–—:]\s+")
# Leading ask verbs: "Provide an", "Add the ability to", "Ability to", "Support for"
_ASK_VERB_RE = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"provide(?:\s+(?:an?|the))?|add(?:\s+(?:an?|the))?|"
    r"(?:an?|the)\s+(?:way|method|means|mechanism)\s+(?:to|for|of)|"
    r"(?:an?|the\s+)?(?:ability|option|possibility)\s+(?:to|for|of)|"
    r"allow(?:\s+(?:us|the\s+\w+))?\s*(?:to)?|enable|support(?:\s+for)?|"
    r"introduce|implement|create|include|expose|request(?:\s+(?:for|to))?|"
    r"need(?:\s+(?:for|to))?|would\s+like\s+(?:to|a|an)|make\s+it\s+possible\s+to"
    r")\s+",
    re.IGNORECASE,
)
_TRAILING_JUNK_RE = re.compile(r"[\s\.\-–—:;,]+$")

# Filler nouns that describe the control, not the thing being controlled.
_CONTROL_NOUNS = (
    "button", "option", "feature", "capability", "functionality", "ability",
    "support", "mechanism", "action", "toggle", "checkbox", "field", "flag",
)


def clean_subject(subject: str) -> str:
    """Subject with Salesforce noise tags and a leading product tag removed."""
    text = _PREFIX_RE.sub("", subject or "").strip()
    tag = _PRODUCT_TAG_RE.match(text)
    if tag and len(tag.group(1).split()) <= 3:
        text = text[tag.end():].strip()
    return _TRAILING_JUNK_RE.sub("", text) or (subject or "").strip()


def _target_phrase(subject: str, ask: str) -> str:
    """The thing the RFE is about, as a noun phrase for the gap sentence.

    "WCF - Provide an Export button for Network Activity" → "Network Activity"
    "Add an API for the endpoint list"                    → "the endpoint list"
    """
    text = clean_subject(subject)
    prev = None
    while prev != text:                      # verbs can stack: "Please add support for"
        prev = text
        text = _ASK_VERB_RE.sub("", text).strip()

    # Drop the ask keyword itself plus any control noun, then keep what follows
    # the preposition — that tail is the surface the PM cares about.
    trigger = _ASK_TRIGGER_WORD.get(ask)
    if trigger:
        m = re.search(
            rf"\b{trigger}\b\s*(?:{'|'.join(_CONTROL_NOUNS)})?\s*"
            r"(?:for|of|to|on|in|from|into|within)\s+(.+)$",
            text, re.IGNORECASE,
        )
        if m and len(m.group(1).split()) >= 1:
            text = m.group(1).strip()

    # A subject that still opens with an action verb ("download the swagger
    # file") reads badly inside a gap sentence — keep the object, drop the verb.
    text = _LEAD_ACTION_RE.sub("", text).strip()

    text = _TRAILING_JUNK_RE.sub("", text).strip()
    # Keep it to a phrase, not a sentence — cut at the first clause break.
    text = re.split(
        r"\s+(?:so\s+that|because|but|when|where|which|who|that)\s+",
        text, 1)[0]
    # Past this length it is a sentence being truncated, and a truncated
    # phrase reads as a fragment — hand back nothing and let the caller use
    # the wording that needs no noun phrase.
    if len(text.split()) > 8:
        return ""
    return text.strip(" .,-–—:;\"'“”")


# Action verbs an RFE subject tends to open with once the ask verb is gone.
_LEAD_ACTION_RE = re.compile(
    r"^(?:export|import|download|upload|extract|view|see|display|show|filter|"
    r"sort|search|manage|configure|customi[sz]e|create|delete|remove|assign|"
    r"schedule|send|notify|alert|integrate|sync|connect|block|allow|exclude|"
    r"include|copy|move|migrate|deploy|install|uninstall|restrict|limit|"
    r"disable|enable|rename|set|change|update|modify|improve|adjust|extend|"
    r"expand|reduce|increase|stop|start|pause|resume|hide|separate|split|"
    r"merge|attach|link|map|tag|group|raise|make)\s+(?:(?:an?|the|all|our|their)\s+)?",
    re.IGNORECASE,
)

# Subjects written as a complaint rather than an ask: "X does not have Y".
_NEGATION_RE = re.compile(
    r"\s+(?:does\s*n[o']?t\s+(?:have|support|show|include|allow)|do\s*n[o']?t\s+"
    r"(?:have|support|show|include|allow)|is\s+not\s+(?:possible|supported|available)|"
    r"cannot|can\s*not|can'?t|unable\s+to|has\s+no|have\s+no|there\s+is\s+no|"
    r"missing|lacks?)\s+", re.IGNORECASE)


def split_problem_statement(subject: str) -> Optional[Tuple[str, str]]:
    """Split "<surface> does not have <thing>" into (surface, missing thing)."""
    text = clean_subject(subject)
    m = _NEGATION_RE.search(text)
    if not m or m.start() < 3:
        return None
    head = text[:m.start()].strip(" .,-–—:;")
    tail = text[m.end():].strip(" .,-–—:;")
    if not head or not tail:
        return None
    return " ".join(head.split()[:8]), " ".join(tail.split()[:8])


# ══════════════════════════════════════════════════════════════════════════════
# Ask-type classification — what kind of gap this is
# ══════════════════════════════════════════════════════════════════════════════

# Order matters: the first match wins, so the specific patterns come first.
ASK_TYPES: List[Tuple[str, str]] = [
    ("export",     r"\bexport|\bdownload|\bcsv\b|\bxlsx?\b|\bexcel\b|save as a? ?file|extract .{0,20}(data|list|report)|dump"),
    ("api",        r"\bapi\b|\brest\b|programmatic|webhook|swagger|api call|graphql"),
    ("bulk",       r"\bbulk\b|in bulk|select all|connect all|multi-?select|at once|mass |batch |all of them|multiple .{0,20}at the same time"),
    ("integration", r"integrat|\bsync\b|connector|forward .{0,15}to|ingest .{0,15}from|two-way|bi-directional"),
    ("automation", r"playbook|remediat|auto-?action|automat|script|orchestrat|trigger .{0,15}action"),
    ("alerting",   r"notif|\bnotify\b|be informed|be alerted|alert me|alerting|"
                   r"email when|send .{0,12}(email|message)|when .{0,25} (happens|occurs|changes)|escalat"),
    ("reporting",  r"\breport\b|scheduled report|quarterly|periodic|digest|summary email"),
    ("rbac",       r"\brbac\b|\brole\b|permission|read-?only|privilege|access level|least privilege|restrict access"),
    ("auth",       r"\bmfa\b|multi-?factor|\bsso\b|\bsaml\b|\bldap\b|single sign|password polic|session timeout"),
    ("retention",  r"retention|archiv|\bindex(ing)?\b|storage period|keep .{0,12}(logs|data)|log volume"),
    ("search",     r"\bfilter\b|\bsearch\b|\bsort\b|paginat|over 100|find .{0,15}(quickly|easily)|group by|drill ?down"),
    ("coverage",   r"\blinux\b|\bmacos\b|\bmac\b|\barm\b|kubernetes|container|\bk8s\b|\bios\b|\bandroid\b|not supported on|no support for"),
    ("lifecycle",  r"uninstall|deploy|install|provision|migrat|rollback|onboard|decommission|\bmsi\b"),
    ("policy",     r"\bpolicy\b|\brule\b|profile|template|exclu(?:de|sion)|exempt|allowlist|whitelist|blocklist|exception|tuning"),
    ("branding",   r"brand|white-?label|\blogo\b|co-?brand|customi[sz]e .{0,15}(look|appearance|report)"),
    ("visibility", r"dashboard|\bview\b|column|display|\bshow\b|visib|\bui\b|console|widget|graph"),
]

# The word to look past when extracting the target phrase from the subject.
_ASK_TRIGGER_WORD = {
    "export": "export", "api": "API", "alerting": "alert", "reporting": "report",
    "rbac": "role", "auth": "MFA", "retention": "retention", "bulk": "bulk",
    "search": "filter", "policy": "policy", "branding": "branding",
    "visibility": "view", "integration": "integration", "automation": "playbook",
}


# Ask types precise enough to be trusted from the description alone. The
# looser ones (search, visibility, policy…) match stray words in almost any
# long Salesforce thread, so a description-only hit there means nothing.
DESC_TRUSTED = {"export", "api", "rbac", "auth", "coverage", "retention",
                "branding", "bulk", "integration"}


def classify_ask(subject: str, description: str) -> str:
    """Which kind of gap this is. The subject wins; the description is a hint."""
    subj = (subject or "").lower()
    for name, pattern in ASK_TYPES:
        if re.search(pattern, subj):
            return name
    body = (description or "").lower()
    for name, pattern in ASK_TYPES:
        if name in DESC_TRUSTED and len(re.findall(pattern, body)) >= 2:
            return name
    return "generic"


# ══════════════════════════════════════════════════════════════════════════════
# Signals mined from the description (facts, never prose)
# ══════════════════════════════════════════════════════════════════════════════

# Deliberately narrow: flagging an RFE as a defect routes it to engineering,
# so only unambiguous failure language counts. "wrong" and "unexpected" are
# left out — they appear just as often in ordinary feature asks.
DEFECT_RE = re.compile(
    r"\b(bug|defect|broken|does ?n[o']?t work|doesn't work|not working|"
    r"stopped working|fails? to\b|failing|error message|crash(?:es|ing)?|"
    r"freezes?|gets? stuck|throws an error)", re.IGNORECASE)
REGRESSION_RE = re.compile(
    r"\b(used to|previously (?:worked|existed|was)|no longer|was removed|"
    r"since the (?:upgrade|update|migration)|after the (?:upgrade|update|migration)|"
    r"regression|worked in the old)", re.IGNORECASE)
COMPLIANCE_RE = re.compile(
    r"\b(compliance|complian\w+|audit(?:or|ing)?\b|\bgdpr\b|\bhipaa\b|\bpci\b|"
    r"iso ?27001|soc ?2|\bnis2\b|\bdora\b|regulat|cyber ?insurance|"
    r"security polic\w+ violation|data residency|legal requirement)", re.IGNORECASE)
POC_RE = re.compile(
    r"\b(\bpoc\b|proof of concept|bake-?off|competitive evaluation|"
    r"evaluating (?:us|cynet|alternatives)|\brfp\b|\btender\b)", re.IGNORECASE)
DEAL_RE = re.compile(
    r"\b(renewal|churn|cancel|contract|deal|will not (?:sign|renew)|"
    r"won'?t renew|blocker for the (?:deal|renewal)|escalat\w+ to management)",
    re.IGNORECASE)
URGENCY_RE = re.compile(r"\b(urgent|asap|immediately|critical|blocking|blocker)", re.IGNORECASE)

COMPETITORS = ["CrowdStrike", "SentinelOne", "Microsoft Defender", "Defender",
               "Sophos", "Trend Micro", "Bitdefender", "Palo Alto", "Cortex",
               "Sentinel", "Rapid7", "Arctic Wolf", "Huntress", "Cybereason"]
PLATFORMS = ["Kubernetes", "Linux", "macOS", "Windows Server", "Windows", "ARM",
             "Docker", "iOS", "Android", "VDI", "Citrix", "On-Prem"]
VENDORS = ["NinjaOne", "ConnectWise", "Datto", "Autotask", "IT Glue", "Cloudflare",
           "Google Workspace", "Microsoft 365", "Office 365", "AWS", "Azure", "GCP",
           "VMware", "NSX", "Jira", "ServiceNow", "Slack", "Teams", "Okta",
           "Entra ID", "Active Directory", "Splunk", "Logstash", "NetDocuments"]
SCALE_RE = re.compile(
    r"\b(\d{2,7})\s*\+?\s*(endpoints?|devices?|users?|sites?|tenants?|agents?|"
    r"hosts?|servers?|alerts?|events?|logs?|mailboxes?)", re.IGNORECASE)

AI_RE = re.compile(r"\bmcp\b|model context protocol|\bai agent|\bllm\b|genai|gen ai|copilot",
                   re.IGNORECASE)


def _found(names: Iterable[str], text: str) -> List[str]:
    """Named entities present in the text, de-duplicated, order preserved."""
    hits, low = [], text.lower()
    for name in names:
        if re.search(rf"\b{re.escape(name.lower())}\b", low) and name not in hits:
            # Skip a generic hit already covered by a more specific one
            if any(name != h and name.lower() in h.lower() for h in hits):
                continue
            hits.append(name)
    return hits


def _scale_phrase(match) -> str:
    """"26 tenant" reads as "26 tenants" — the count is a fact worth keeping."""
    if not match:
        return ""
    count = int(match.group(1))
    noun = match.group(2).lower()
    if count != 1 and not noun.endswith("s"):
        noun += "es" if noun.endswith(("s", "x", "ch", "sh")) else "s"
    return f"{count:,} {noun}"


def mine_signals(subject: str, description: str) -> Dict[str, Any]:
    text = f"{subject}\n{description}"
    platforms = _found(PLATFORMS, text)
    competitors = _found(COMPETITORS, text)
    vendors = [v for v in _found(VENDORS, text) if v not in competitors]
    scale = SCALE_RE.search(description or "")
    return {
        "defect": bool(DEFECT_RE.search(description or "")),
        "regression": bool(REGRESSION_RE.search(text)),
        "compliance": bool(COMPLIANCE_RE.search(text)),
        "poc": bool(POC_RE.search(text)),
        "deal": bool(DEAL_RE.search(text)),
        "urgent": bool(URGENCY_RE.search(text)),
        "ai": bool(AI_RE.search(text)),
        "platforms": platforms,
        "vendors": vendors,
        "competitors": competitors,
        "scale": _scale_phrase(scale),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Sentence templates
# ══════════════════════════════════════════════════════════════════════════════

AREA_NAME = {
    "epp": "endpoint protection", "wac": "Web Access Control",
    "email": "Email Security", "siem": "the SIEM/CLM stack",
    "identity": "identity protection", "cspm": "cloud posture",
    "platform": "the console", "reporting": "reporting",
    "automations": "automation", "ai": "the AI surface",
}
ACTOR = {
    "epp": "endpoint admins", "wac": "policy admins", "email": "email admins",
    "siem": "SOC analysts", "identity": "identity admins",
    "cspm": "cloud security owners", "platform": "console admins",
    "reporting": "the account team", "automations": "MSP operators",
    "ai": "the security team",
}

# Each ask type has two gap sentences: `slot` names the surface the subject
# asked for, `plain` does not. Salesforce subjects are written by humans and
# often refuse to behave as a noun phrase, so `plain` is used whenever the
# extracted phrase would not read as English (see `_target_ok`).
GAP: Dict[str, Dict[str, List[str]]] = {
    "export": {
        "slot": ["{Target} can be read in the console but not taken out of it — there is no export, download or file-delivery path on that view.",
                 "There is no way to get {target} out of the product as a file; it is display-only once it renders."],
        "plain": ["The data behind this request can be read in the console but not taken out of it — no export, download or file-delivery path exists on that view.",
                  "What the customer needs is a file, and {area} has no way to produce one from this view."],
    },
    "api": {
        "slot": ["{Target} is reachable only by a human clicking through the console — no API covers it, so nothing downstream can read or drive it.",
                 "The API does not expose {target}, which leaves a console-only island in an otherwise scriptable platform."],
        "plain": ["This surface is reachable only by a human clicking through the console — no API covers it, so nothing downstream can read or drive it.",
                  "The API stops short of what the customer needs here, leaving a console-only island in an otherwise scriptable platform."],
    },
    "integration": {
        "slot": ["Cynet and the customer's own tooling each hold part of the picture for {target}, and nothing carries data between them.",
                 "{Target} has no supported connector, so the two systems are reconciled by hand or not at all."],
        "plain": ["Cynet and the customer's own tooling each hold part of this picture, and nothing carries data between them.",
                  "There is no supported connector for the system named here, so the two sides are reconciled by hand or not at all."],
    },
    "automation": {
        "slot": ["{Target} still needs a person to decide and act; no playbook or automated remediation covers it.",
                 "Nothing automates {target} — every occurrence waits on a human, including the cases where the decision never varies."],
        "plain": ["This still needs a person to decide and act; no playbook or automated remediation covers it.",
                  "Every occurrence waits on a human, including the cases where the decision never varies."],
    },
    "alerting": {
        "slot": ["Nothing announces {target} — it surfaces only if someone happens to open the console and look.",
                 "{Target} raises no alert or notification, so noticing it depends on a human checking at the right moment."],
        "plain": ["Nothing announces this condition — it surfaces only if someone happens to open the console and look.",
                  "No alert or notification exists for it, so noticing depends on a human checking at the right moment."],
    },
    "reporting": {
        "slot": ["{Target} is not available as a report, so the same numbers are reassembled by hand every cycle.",
                 "There is no report covering {target} — the output the customer expects is built manually each period."],
        "plain": ["This is not available as a report, so the same numbers are reassembled by hand every cycle.",
                  "The output the customer expects has no report behind it and is built manually each period."],
    },
    "rbac": {
        "slot": ["Access around {target} is all-or-nothing; the role needed to scope it does not exist.",
                 "{Target} cannot be granted to a narrower role, so permissions end up wider than the customer's own policy allows."],
        "plain": ["Access here is all-or-nothing; the role needed to scope it does not exist.",
                  "The permission cannot be granted narrowly, so rights end up wider than the customer's own policy allows."],
    },
    "auth": {
        "slot": ["{Target} does not meet the login and session controls the customer's security standard requires.",
                 "The authentication controls around {target} are missing, leaving access weaker than the customer's own policy."],
        "plain": ["The login and session controls here fall short of the customer's own security standard.",
                  "The authentication control the customer expects is missing, leaving access weaker than their policy allows."],
    },
    "retention": {
        "slot": ["{Target} is not retained or indexed the way investigations need, so evidence ages out before anyone looks at it.",
                 "The retention and indexing behind {target} does not match what an investigation actually needs."],
        "plain": ["The data is not retained or indexed the way investigations need, so evidence ages out before anyone looks at it.",
                  "Retention and indexing here do not match what an investigation actually needs."],
    },
    "bulk": {
        "slot": ["{Target} can only be handled one record at a time — there is no bulk or select-all path.",
                 "Every action on {target} is single-item, so a large tenant means the same click repeated hundreds of times."],
        "plain": ["This can only be handled one record at a time — there is no bulk or select-all path.",
                  "Every action here is single-item, so a large tenant means the same click repeated hundreds of times."],
    },
    "search": {
        "slot": ["{Target} cannot be narrowed to the records that matter — the whole set has to be scrolled.",
                 "There is no way to filter, sort or page {target} down to the rows a person is actually looking for."],
        "plain": ["The view cannot be narrowed to the records that matter — the whole set has to be scrolled.",
                  "There is no way to filter, sort or page down to the rows a person is actually looking for."],
    },
    "coverage": {
        "slot": ["{Target} sits outside what the product covers today, so part of the estate is unprotected while appearing protected.",
                 "Coverage does not extend to {target}, which leaves a blind spot the customer assumes is closed."],
        "plain": ["The platform named here sits outside what the product covers today, so part of the estate is unprotected while appearing protected.",
                  "Coverage does not extend this far, which leaves a blind spot the customer assumes is closed."],
    },
    "lifecycle": {
        "slot": ["{Target} has no self-service path — the operation runs manually, per machine or per site.",
                 "Getting {target} done means a manual, repeated procedure with no product support behind it."],
        "plain": ["There is no self-service path for this — the operation runs manually, per machine or per site.",
                  "It is a manual, repeated procedure today with no product support behind it."],
    },
    "policy": {
        "slot": ["{Target} cannot be expressed as policy — the rule, exclusion or template needed to hold it does not exist.",
                 "There is no way to configure {target} centrally, so the same setting is recreated by hand everywhere it applies."],
        "plain": ["This cannot be expressed as policy — the rule, exclusion or template needed to hold it does not exist.",
                  "There is no way to configure it centrally, so the same setting is recreated by hand everywhere it applies."],
    },
    "branding": {
        "slot": ["{Target} still carries Cynet's identity where the partner's should appear, with no control to change it.",
                 "There is no branding control over {target}, so what the customer's own users see is not theirs."],
        "plain": ["It still carries Cynet's identity where the partner's should appear, with no control to change it.",
                  "There is no branding control here, so what the customer's own users see is not theirs."],
    },
    "visibility": {
        "slot": ["{Target} is not surfaced where the decision is made — the data exists but the view does not show it.",
                 "The console does not present {target}, so the information has to be pieced together elsewhere."],
        "plain": ["The information is not surfaced where the decision is made — it exists, but this view does not show it.",
                  "The console does not present what the customer needs to see, so it is pieced together elsewhere."],
    },
    "generic": {
        "slot": ["There is no path today for {target} anywhere in {area}; the capability simply is not there.",
                 "{Target} is unsupported in {area} today, so the workflow around it stops at the product boundary."],
        "plain": ["{Area} has no path for what is being asked here; the capability simply is not there.",
                  "The workflow the customer describes stops at the product boundary — {area} does not support this today."],
    },
}

# Used when the subject is phrased as a complaint rather than an ask —
# "<surface> does not have <thing>". Both halves are known, which makes for a
# sharper gap sentence than any template above.
PROBLEM_GAP = [
    "{Head} surfaces the problem but stops short of {tail}, so the screen that finds the issue is not the screen that can act on it.",
    "{Head} is missing {tail} — everything up to the point of acting is there, and then the trail goes cold.",
]

CONSEQUENCE: Dict[str, List[str]] = {
    "export": [
        "{Actor} re-key or screenshot what they see to move it into a ticket, an audit pack or a spreadsheet, so any workflow that finishes outside Cynet stalls.",
        "Anything that has to leave the console — an audit response, a customer report, a ticket attachment — gets rebuilt by hand.",
    ],
    "api": [
        "That keeps the surface manual and console-bound: it cannot be scheduled, scripted or driven from the customer's own tooling.",
        "{Actor} cannot fold it into the automation they already run, so the work stays clicked rather than orchestrated.",
    ],
    "integration": [
        "{Actor} maintain the overlap manually, and the two sides drift apart between touches.",
        "The gap gets bridged by copy-paste, which is where records go stale and get missed.",
    ],
    "automation": [
        "Response time is bounded by how quickly a person notices, and out of hours the delay is however long until the next shift.",
        "{Actor} spend attention on decisions that never vary instead of the ones that do.",
    ],
    "alerting": [
        "The window between the event and someone noticing is unbounded, which is exactly the window an attacker needs.",
        "{Actor} end up polling the console instead of being told, so anything outside working hours is found late.",
    ],
    "reporting": [
        "{Actor} rebuild the same output every cycle, which burns hours and drifts between periods.",
        "What the customer sees depends on who assembled it that month, which is a defensibility problem as much as a time one.",
    ],
    "rbac": [
        "Routine work forces broader rights than intended — a finding waiting to happen at the customer's next access review.",
        "{Actor} either over-grant access or block the work; there is no correct option on the menu.",
    ],
    "auth": [
        "The customer carries it as accepted risk or compensates outside the product, and it resurfaces at every security review.",
        "It leaves an authentication path the customer's own standard would not approve.",
    ],
    "retention": [
        "Investigations lose the evidence window, and questions asked weeks later cannot be answered at all.",
        "{Actor} are limited to whatever survived, which is rarely what the incident needed.",
    ],
    "bulk": [
        "It does not scale past a handful of records, and at real tenant sizes the repetition itself becomes the error source.",
        "{Actor} either spend the afternoon clicking or skip the work entirely — usually the second.",
    ],
    "search": [
        "At real data volumes the view stops being usable, and the records that matter are the ones never reached.",
        "{Actor} lose time to scrolling and still cannot prove they saw everything relevant.",
    ],
    "coverage": [
        "Those hosts report nothing, so the console shows a clean estate that is not actually clean.",
        "The customer is exposed exactly where they believe they are covered, which is the worst shape a gap can take.",
    ],
    "lifecycle": [
        "Every rollout, migration or cleanup becomes a manual project, and {actor} absorb the cost each time.",
        "It turns a routine operation into scheduled work with a person attached to each machine.",
    ],
    "policy": [
        "The same configuration is recreated per site or per group, so environments diverge and nobody can say what is actually enforced.",
        "{Actor} cannot enforce a standard consistently, which undoes the point of central management.",
    ],
    "branding": [
        "The partner's end customers see the wrong identity, which undercuts the reseller relationship the account is built on.",
        "It is visible to the customer's own users, so the gap is felt outside the security team.",
    ],
    "visibility": [
        "{Actor} switch context or export elsewhere to answer a question the console should already be answering.",
        "Decisions get made on partial information, or get deferred until someone assembles the rest.",
    ],
    "generic": [
        "{Actor} work around it manually, and that workaround is the process until this ships.",
        "The gap is absorbed as manual effort on the customer's side every time the workflow runs.",
    ],
}


# ══════════════════════════════════════════════════════════════════════════════
# Composition
# ══════════════════════════════════════════════════════════════════════════════

def _field(rfe: Dict[str, Any], *names: str, default: Any = "") -> Any:
    """Read a field under any of the names the two generators use."""
    for n in names:
        v = rfe.get(n)
        if v not in (None, ""):
            return v
    return default


def rfe_arr(rfe: Dict[str, Any]) -> float:
    try:
        return float(_field(rfe, "arr", "account_arr", default=0) or 0)
    except (TypeError, ValueError):
        return 0.0


def fmt_arr(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    if v > 0:
        return f"${v:.0f}"
    return "no recorded ARR"


def _seed(case_number: str) -> int:
    return int(hashlib.md5((case_number or "x").encode("utf-8")).hexdigest()[:8], 16)


def _pick(options: List[str], seed: int) -> str:
    return options[seed % len(options)]


def _join(items: List[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


# Words that mean the extracted phrase is a sentence fragment, not a noun phrase.
_BAD_LEAD = {
    "be", "been", "being", "have", "has", "had", "do", "does", "did", "to", "at",
    "who", "whom", "that", "which", "and", "or", "but", "of", "in", "on", "for",
    "from", "when", "if", "is", "are", "was", "were", "it", "this", "these",
    "they", "we", "you", "so", "as", "with", "by", "not", "no", "t", "s",
}


def _target_ok(target: str, subject: str) -> bool:
    """Would this phrase read as English inside a sentence?"""
    if not target:
        return False
    words = target.split()
    if not (2 <= len(words) <= 8) or len(target) < 6:
        return False
    if words[0].strip("'\"“”").lower() in _BAD_LEAD:
        return False
    if words[-1].lower() in _BAD_LEAD:
        return False
    for q in ('"', "'", "“", "”"):
        if target.count(q) % 2:                 # a stray opening quote
            return False
    if "|" in target or target.endswith((":", ",")):
        return False
    return True


def _routing_flag(sig: Dict[str, Any], arr: float, severity: str,
                  ctx: Dict[str, Any]) -> str:
    """The skill's routing signal, in its exact wording. Most serious wins."""
    if sig["compliance"]:
        return "⚠️ Active compliance/security failure — escalate urgently."
    if sig["regression"]:
        return "⚠️ Regression — feature previously existed and was removed."
    if sig["defect"]:
        return "⚠️ This is a bug/defect, not a feature request — route to engineering."
    if sig["poc"] and sig["competitors"]:
        return f"Active {sig['competitors'][0]} POC blocker."
    # "Deal blocker" is a claim about money — only make it where money is at stake.
    if (sig["poc"] or sig["deal"]) and arr >= 100_000:
        return "Deal blocker for enterprise prospect."
    if sig["ai"]:
        return "Strategic first-mover opportunity."
    return ""


def _cluster_flag(rfe: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    """Cluster / same-account signals, in the skill's wording."""
    case = rfe["case_number"]
    siblings = ctx.get("similar", {}).get(case, [])
    same_acct = ctx.get("same_account", {}).get(case, [])
    if siblings:
        cases = ", ".join("#" + c for c in siblings[:3])
        return f"Cluster with {cases} — route as single engineering task."
    if same_acct:
        return (f"Same {fmt_arr(rfe_arr(rfe))} ARR account as #{same_acct[0]} — "
                f"{len(same_acct) + 1} requests from the same customer, churn risk.")
    return ""


def _detail_clause(sig: Dict[str, Any]) -> str:
    """A factual detail list mined from the description — never its wording."""
    bits: List[str] = []
    if sig["platforms"]:
        bits.append(_join(sig["platforms"][:3]))
    if sig["vendors"]:
        bits.append(_join(sig["vendors"][:2]))
    if sig["scale"]:
        bits.append(sig["scale"])
    if not bits:
        return ""
    return f" — specifically for {_join(bits)}"


def _business_sentence(rfe: Dict[str, Any], seed: int) -> str:
    value = rfe_arr(rfe)
    account = rfe.get("account_name") or "The account"
    sev = rfe.get("severity") or "Low"
    days = _field(rfe, "days", "_days", default=None)
    age = ""
    if isinstance(days, int) and 0 <= days < 900:
        age = (" and less than a week old" if days <= 7 else
               f" and open {days} days" if days <= 400 else " and long-standing")
    if value > 0:
        return f"{account} carries {fmt_arr(value)} of ARR; the case is {sev} severity{age}."
    return (f"{account} filed this at {sev} severity{age}; there is no ARR "
            f"recorded against the account to weigh it against.")


def write_summary(rfe: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> str:
    """Write one PM Decision Summary. Always complete sentences, never raw text."""
    ctx = ctx or {}
    description = (rfe.get("description") or "").strip()
    subject = rfe.get("subject") or ""

    # Skill rule 4: the one and only permitted fallback.
    if len(description) < MIN_DESC:
        return NO_DESC_SUMMARY

    section = _field(rfe, "section", "_section", default="platform")
    seed = _seed(rfe.get("case_number", ""))
    ask = classify_ask(subject, description)
    sig = mine_signals(subject, description)

    area = AREA_NAME.get(section, "the platform")
    actor = ACTOR.get(section, "admins")
    forms = GAP.get(ask, GAP["generic"])

    problem = split_problem_statement(subject)
    target = _target_phrase(subject, ask)
    if problem and _target_ok(problem[0], subject) and _target_ok(problem[1], subject):
        head, tail = problem
        gap = _pick(PROBLEM_GAP, seed).format(
            Head=head[0].upper() + head[1:], tail=tail)
    elif _target_ok(target, subject):
        gap = _pick(forms["slot"], seed).format(
            target=target, Target=target[0].upper() + target[1:],
            area=area, Area=area[0].upper() + area[1:])
    else:
        gap = _pick(forms["plain"], seed).format(
            area=area, Area=area[0].upper() + area[1:])

    consequence = _pick(CONSEQUENCE.get(ask, CONSEQUENCE["generic"]), seed >> 3).format(
        actor=actor, Actor=actor[0].upper() + actor[1:])
    detail = _detail_clause(sig)
    if detail:
        consequence = consequence.rstrip(".") + detail + "."

    sentences = [gap, consequence, _business_sentence(rfe, seed)]

    # Three sentences of prose — gap, consequence, what it is worth — and
    # then the routing signals. The skill fixes the flags' wording exactly,
    # so a case carrying two of them gets both verbatim rather than one
    # reworded to save a sentence.
    cluster = _cluster_flag(rfe, ctx)
    if cluster:
        sentences.append(cluster)
    flag = _routing_flag(sig, rfe_arr(rfe), rfe.get("severity", ""), ctx)
    if flag:
        sentences.append(flag)

    text = " ".join(s.strip() for s in sentences if s.strip())
    return _enforce_rules(text, description, rfe)


# ══════════════════════════════════════════════════════════════════════════════
# Rule enforcement — the skill's absolutes, checked at the boundary
# ══════════════════════════════════════════════════════════════════════════════

def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def shares_long_ngram(summary: str, description: str, n: int = 8) -> bool:
    """True if the summary lifts a run of n+ consecutive words from the description."""
    sw, dw = _words(summary), _words(description)
    if len(sw) < n or len(dw) < n:
        return False
    grams = {tuple(dw[i:i + n]) for i in range(len(dw) - n + 1)}
    return any(tuple(sw[i:i + n]) in grams for i in range(len(sw) - n + 1))


def _enforce_rules(text: str, description: str, rfe: Dict[str, Any]) -> str:
    """Never emit a summary that breaks a skill absolute."""
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"\.{2,}$|…$", ".", text)            # rule 2: no truncation
    if not text:
        return NO_DESC_SUMMARY
    if shares_long_ngram(text, description):           # rule 1: nothing lifted
        # Fall back to the account/severity framing, which is ours by construction.
        text = (f"{_business_sentence(rfe, _seed(rfe.get('case_number', '')))} "
                f"The description needs a PM read before routing — the request as "
                f"written does not state the gap in product terms.")
    if not text.endswith((".", "!", "?")):
        text += "."
    return text


# ══════════════════════════════════════════════════════════════════════════════
# Dataset context — clusters and same-account siblings
# ══════════════════════════════════════════════════════════════════════════════

_CTX_STOP = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "by",
    "with", "from", "as", "is", "are", "be", "has", "have", "that", "this",
    "it", "its", "can", "will", "cynet", "rfe", "request", "feature", "ability",
    "support", "add", "allow", "enable", "new", "improve", "enhance", "update",
    "please", "external", "inquiry", "option", "when", "not", "all", "via",
    "per", "we", "our", "should", "would", "need", "provide", "console",
}


def _ctx_tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower())
            if w not in _CTX_STOP}


def build_context(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Find the cluster and same-account signals the routing flags need.

    Candidate pairs come from a shared-token index, so this stays linear in
    practice instead of comparing every RFE with every other one.
    """
    toks = {r["case_number"]: _ctx_tokens(r.get("subject", "")) for r in records}
    by_token: Dict[str, List[str]] = defaultdict(list)
    for case, ts in toks.items():
        for t in ts:
            by_token[t].append(case)

    similar: Dict[str, List[str]] = {}
    for case, ts in toks.items():
        if len(ts) < 2:
            continue
        counts: Dict[str, int] = defaultdict(int)
        for t in ts:
            if len(by_token[t]) > 40:        # a token this common says nothing
                continue
            for other in by_token[t]:
                if other != case:
                    counts[other] += 1
        matches = []
        for other, shared in counts.items():
            if shared < 2:
                continue
            union = len(ts | toks[other])
            if union and shared / union >= 0.34:
                matches.append((shared, other))
        if matches:
            matches.sort(key=lambda m: (-m[0], m[1]))
            similar[case] = [m[1] for m in matches[:3]]

    by_account: Dict[str, List[str]] = defaultdict(list)
    for r in records:
        acct = (r.get("account_name") or "").strip().lower()
        if acct and acct != "unknown":
            by_account[acct].append(r["case_number"])
    same_account: Dict[str, List[str]] = {}
    for r in records:
        if rfe_arr(r) < 100_000:             # churn framing needs real ARR
            continue
        acct = (r.get("account_name") or "").strip().lower()
        others = [c for c in by_account.get(acct, []) if c != r["case_number"]]
        if others:
            same_account[r["case_number"]] = others[:3]
    return {"similar": similar, "same_account": same_account}


# ══════════════════════════════════════════════════════════════════════════════
# Description backfill
# ══════════════════════════════════════════════════════════════════════════════

def backfill_descriptions(db_path: str, records: List[Dict[str, Any]],
                          run_id: Optional[str] = None,
                          field: str = "description") -> int:
    """Fill in descriptions this run is missing from other runs of the same case.

    The Salesforce SOQL pull frequently returns no Description, while a CSV
    import of the same case has one. Without this a summary would fall back to
    the TAM flag for a case the platform can actually describe.
    """
    missing = [r for r in records if len((r.get(field) or "").strip()) < MIN_DESC]
    if not missing:
        return 0
    conn = sqlite3.connect(db_path)
    try:
        if run_id:
            rows = conn.execute(
                "SELECT case_number, description FROM rfe_pulls "
                "WHERE run_id!=? AND LENGTH(TRIM(COALESCE(description,'')))>?",
                (run_id, MIN_DESC))
        else:
            rows = conn.execute(
                "SELECT case_number, description FROM rfe_pulls "
                "WHERE LENGTH(TRIM(COALESCE(description,'')))>?", (MIN_DESC,))
        best: Dict[str, str] = {}
        for case, desc in rows:
            case = (case or "").strip()
            desc = (desc or "").strip()
            if case and len(desc) > len(best.get(case, "")):
                best[case] = desc
    finally:
        conn.close()

    filled = 0
    for r in missing:
        found = best.get(str(r.get("case_number", "")).strip(), "")
        if found:
            r[field] = found
            filled += 1
    return filled


# ══════════════════════════════════════════════════════════════════════════════
# Cache — write once per (case, source material), reuse afterwards
# ══════════════════════════════════════════════════════════════════════════════

def _source_hash(rfe: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    """Changes whenever anything the summary is derived from changes."""
    parts = [
        VERSION, rfe.get("subject", ""), rfe.get("description", ""),
        rfe.get("account_name", ""), f"{rfe_arr(rfe):.0f}",
        rfe.get("severity", ""),
        str(_field(rfe, "section", "_section", default="")),
        ",".join(ctx.get("similar", {}).get(rfe["case_number"], [])),
        ",".join(ctx.get("same_account", {}).get(rfe["case_number"], [])),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def ensure_cache_columns(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS pm_summaries (
        case_number TEXT PRIMARY KEY, summary TEXT NOT NULL,
        model TEXT, generated_at TEXT)""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pm_summaries)")}
    if "source_hash" not in cols:
        conn.execute("ALTER TABLE pm_summaries ADD COLUMN source_hash TEXT")


def ensure_summaries(db_path: str, records: List[Dict[str, Any]],
                     progress=None) -> Dict[str, int]:
    """Write a PM Decision Summary for every record, in place, and cache it.

    Called during report generation, so a report always ships complete. A
    human- or LLM-written summary already in the cache is left alone; only
    rule-written ones are refreshed, and only when their source material moved.
    """
    if not records:
        return {"written": 0, "reused": 0, "total": 0}

    ctx = build_context(records)
    conn = sqlite3.connect(db_path)
    try:
        ensure_cache_columns(conn)
        cached = {r[0]: (r[1], r[2] or "", r[3] or "") for r in conn.execute(
            "SELECT case_number, summary, model, source_hash FROM pm_summaries")}

        written = reused = 0
        now = datetime.utcnow().isoformat()
        rows = []
        for i, rfe in enumerate(records):
            case = rfe["case_number"]
            want = _source_hash(rfe, ctx)
            have = cached.get(case)
            if have and have[0].strip():
                is_rule_written = have[1].startswith("rules-")
                if not is_rule_written or have[2] == want:
                    rfe["pm_summary"] = have[0]      # keep LLM/manual text as-is
                    reused += 1
                    continue
            text = write_summary(rfe, ctx)
            rfe["pm_summary"] = text
            rows.append((case, text, VERSION, now, want))
            written += 1
            if progress and i % 50 == 0:
                progress(i + 1, len(records))

        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO pm_summaries "
                "(case_number, summary, model, generated_at, source_hash) "
                "VALUES (?,?,?,?,?)", rows)
            conn.commit()
    finally:
        conn.close()

    if progress:
        progress(len(records), len(records))
    return {"written": written, "reused": reused, "total": len(records)}
