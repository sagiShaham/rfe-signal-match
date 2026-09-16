"""
Narrative writer for the PI Planning report (v2).

WHY THIS EXISTS
---------------
v1 filled its prose slots with a template: "<Domain> demand spans N RFEs from M+
unique accounts, representing $X in ARR." That is a caption, not a summary — it
restates three numbers already shown in the header beside it and tells the
reader nothing they can act on. The Interpretation and Action panels the design
called for were never written at all (their CSS shipped unused).

Every sentence this module produces is composed at build time from the scored
data, names the specific themes, accounts and case numbers involved, and lands
somewhere a PM team can act. The rules are absolute:

  * **No placeholders, no ellipsis, no "TBD".** A sentence that cannot be
    grounded in the data is not written at all — a section renders a shorter,
    honest paragraph rather than a padded one.
  * **Never restate the KPI strip.** Numbers appear in the prose only when they
    carry an argument ("three of the five carry a business-impact flag").
  * **Always land on a decision.** Executive and domain summaries close on what
    to start, what to keep and what to propose closing.
  * **Never assert what the data does not say.** The writer says "N customers
    stay on the current workaround", never "customers will churn".

Output is deterministic: the same upload produces the same words, which matters
because this report is re-opened during the meeting and must not change under
the people reading it.

SAFETY
------
Subjects, theme names and account names are customer-authored text arriving from
Salesforce, and these sentences are injected into the page as HTML (the writer
emits its own `<strong>` emphasis). Every interpolated value therefore goes
through `_e()`. A subject containing `<script>` renders as text, not script.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from scoring import priority as P


# ── Formatting helpers ───────────────────────────────────────────────────────

def _e(value: Any) -> str:
    """HTML-escape an interpolated value. Applied to ALL Salesforce-sourced text."""
    return (str(value if value is not None else "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _b(value: Any) -> str:
    """Escaped and emphasised — the form theme and account names take in prose."""
    return f"<strong>{_e(value)}</strong>"


def money(v: float) -> str:
    """ARR in the form leadership reads it — $1.9M, $250K, $0."""
    v = float(v or 0)
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def plural(n: int, word: str, suffix: str = "s") -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}{suffix}"


def join_list(items: Sequence[str], conj: str = "and") -> str:
    """Oxford-comma list: 'a', 'a and b', 'a, b and c'."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {conj} {items[1]}"
    return ", ".join(items[:-1]) + f" {conj} {items[-1]}"


def months_of(days: Optional[float]) -> int:
    return int(round((days or 0) / 30.4))


def severity_phrase(mix: Dict[str, int]) -> str:
    """'2 Critical and 9 High' — only the levels actually present."""
    parts = []
    for key in ("critical", "high", "medium", "low"):
        n = mix.get(key, 0)
        if n:
            parts.append(f"{n} {key.capitalize()}")
    unset = mix.get("", 0)
    if unset:
        parts.append(f"{unset} with severity unset")
    return join_list(parts)


# ── Executive narrative ──────────────────────────────────────────────────────

def exec_narrative(model: Dict[str, Any]) -> List[str]:
    """The executive story, as a list of paragraphs.

    Paragraph 1 — what this backlog is, and where the pressure actually sits.
    Paragraph 2 — the shortlist: what to start, and what carries each one.
    Paragraph 3 — the counterweight: the customer-stated escalations that must
                  be answered individually, and what should leave the backlog.
    """
    domains = model["domains"]
    clusters = model["clusters"]
    totals = model["totals"]
    paras: List[str] = []

    ranked_domains = sorted([d for d in domains if d["requests"]],
                            key=P.sort_key("priority"), reverse=True)
    ranked_clusters = sorted(clusters, key=P.sort_key("priority"), reverse=True)

    start_now = [c for c in ranked_clusters if c["band"]["key"] == "start_now"]
    plan = [c for c in ranked_clusters if c["band"]["key"] == "plan"]
    drop = [c for c in ranked_clusters if c["band"]["key"] == "drop"]

    # ── Paragraph 1: the shape of the demand ─────────────────────────────────
    if ranked_domains:
        dom_bits = [
            f"{_e(d['domain'])} ({plural(d['requests'], 'request')} from "
            f"{plural(d['customers'], 'customer')}, {money(d['arr'])} at stake)"
            for d in ranked_domains[:3]
        ]
        p1 = (
            f"{totals['requests']:,} open requests from {totals['customers']} "
            f"customers consolidate into {totals['cluster_count']} distinct themes "
            f"across {totals['active_domains']} product domains, carrying "
            f"{money(totals['arr'])} of customer ARR. Demand is not evenly spread: "
            f"{join_list(dom_bits)} account for {totals['top3_share']}% of the "
            f"weighted signal."
        )
    else:
        p1 = (f"{totals['requests']:,} open requests are in scope for this "
              f"timeframe, carrying {money(totals['arr'])} of customer ARR.")

    if totals["repeat_theme_count"]:
        p1 += (
            f" {plural(totals['repeat_theme_count'], 'theme')} "
            f"{'is' if totals['repeat_theme_count'] == 1 else 'are'} asked for by "
            f"more than one customer — that repetition, not raw volume, is what "
            f"separates a product signal from an account request."
        )
    paras.append(p1)

    # ── Paragraph 2: the shortlist ───────────────────────────────────────────
    if start_now:
        bits = []
        for c in start_now[:3]:
            drivers = []
            if c["flagged"]:
                drivers.append(f"{c['flagged']} business-impact "
                               f"{'flag' if c['flagged'] == 1 else 'flags'}")
            if c["customers"] > 1:
                drivers.append(f"{c['customers']} customers")
            if c["top_severity"] in ("critical", "high"):
                drivers.append(f"{c['top_severity'].capitalize()} severity")
            if not drivers:
                drivers.append(f"{money(c['arr'])} at stake")
            bits.append(f"{_b(c['name'])} ({_e(c['domain'])} — {join_list(drivers)})")
        p2 = (
            f"{plural(len(start_now), 'theme')} clear the Start-now threshold, "
            f"covering {plural(sum(c['requests'] for c in start_now), 'request')} "
            f"and {money(sum(c['arr'] for c in start_now))} of ARR. The three "
            f"ranked highest are {join_list(bits)}. Walk these first: each is "
            f"carried by more than one signal rather than by a single large "
            f"account."
        )
        if plan:
            p2 += (f" A further {plural(len(plan), 'theme')} sit in the Plan band — "
                   f"worth sizing in this meeting, worth committing next PI.")
    elif plan:
        names = join_list([f"{_b(c['name'])} ({_e(c['domain'])})" for c in plan[:3]])
        p2 = (
            f"No theme clears the Start-now threshold in this timeframe. The "
            f"strongest signals sit in the Plan band, led by {names}. That is a "
            f"quiet backlog rather than an empty one: size these now so the next "
            f"PI opens with candidates that are already understood."
        )
    else:
        p2 = (
            "No theme in this timeframe reaches the Plan threshold. What is left "
            "is single-customer requests with limited severity and ARR behind "
            "them, which makes this a cleanup session rather than a commitment "
            "session."
        )
    paras.append(p2)

    # ── Paragraph 3: escalations, and the other direction ────────────────────
    bits3: List[str] = []
    if totals["flagged"]:
        flagged_domains = [_e(d["domain"]) for d in ranked_domains if d["flagged"]][:3]
        bits3.append(
            f"{plural(totals['flagged'], 'request')} carry a customer-stated "
            f"business impact"
            + (f", concentrated in {join_list(flagged_domains)}" if flagged_domains else "")
            + ". Those are listed in full further down this page and need "
              "dispositioning one by one rather than being absorbed into a theme"
        )
    if drop:
        bits3.append(
            f"at the other end, {plural(len(drop), 'theme')} covering "
            f"{plural(sum(c['requests'] for c in drop), 'request')} score below the "
            f"keep threshold — low severity, no ARR attached and no second "
            f"customer asking. Closing those with the requesting customers is the "
            f"cheapest way to make the remaining backlog mean something"
        )
    if totals["stale_count"]:
        bits3.append(
            f"{plural(totals['stale_count'], 'request')} have been open more than "
            f"a year, which is a decision the team has been deferring rather than "
            f"a queue that needs draining"
        )
    if bits3:
        # Each of these is a full clause, so they are separate sentences. Joining
        # them with "and" (as a first draft did) produced comma splices on the
        # most-read paragraph of the report.
        paras.append(" ".join(b[0].upper() + b[1:] + "." for b in bits3))

    return paras


def exec_headline(model: Dict[str, Any]) -> str:
    """One sentence for the top of the page — the most useful single fact."""
    totals = model["totals"]
    start_now = [c for c in model["clusters"] if c["band"]["key"] == "start_now"]
    if start_now:
        return (f"{plural(len(start_now), 'theme')} ready to commit this PI; "
                f"{plural(totals['drop_requests'], 'request')} are candidates to "
                f"close.")
    if totals["drop_requests"]:
        return (f"Nothing reaches the commit threshold this cycle; "
                f"{plural(totals['drop_requests'], 'request')} are candidates to "
                f"close.")
    return f"{totals['requests']:,} requests in scope, none at commit threshold."


# ── Domain narrative ─────────────────────────────────────────────────────────

def domain_narrative(dom: Dict[str, Any]) -> str:
    """The story of one domain, in three to five sentences.

    Opens with what the demand is *about* (its leading themes) rather than how
    much of it there is; then who is pushing; then the risk read; then the
    decision this tab exists to support. A domain with no requests in scope says
    exactly that, in one sentence, instead of padding.
    """
    if not dom["requests"]:
        return (f"No {_e(dom['domain'])} requests fall inside the selected "
                f"timeframe. Switch the timeframe to All time to check whether "
                f"this domain has older demand worth revisiting.")

    clusters = sorted(dom["clusters"], key=P.sort_key("priority"), reverse=True)
    lead = clusters[0]
    sentences: List[str] = []

    # 1. What the demand is about.
    theme_names = [_b(c["name"]) for c in clusters[:3]]
    sentences.append(
        f"{_e(dom['domain'])} demand is led by {join_list(theme_names)}, the "
        f"{'theme' if len(theme_names) == 1 else 'themes'} carrying the most "
        f"weight once severity, ARR, business impact and repetition are read "
        f"together."
    )

    # 2. Who is pushing, and how hard.
    multi = [c for c in clusters if c["customers"] > 1]
    if multi:
        widest = max(multi, key=lambda c: c["customers"])
        who = (f"{plural(len(multi), 'theme')} "
               f"{'has' if len(multi) == 1 else 'have'} more than one customer "
               f"behind {'it' if len(multi) == 1 else 'them'}, the widest being "
               f"{_b(widest['name'])} at {plural(widest['customers'], 'customer')}")
    else:
        who = ("Every theme here comes from a single customer, so nothing in this "
               "domain is yet a broad product signal")
    top_accounts = dom["top_accounts"][:3]
    if top_accounts:
        acct_bits = [f"{_e(a['name'])} ({plural(a['requests'], 'request')}, "
                     f"{money(a['arr'])})" for a in top_accounts]
        who += f". The most active accounts are {join_list(acct_bits)}"
    sentences.append(who + ".")

    # 3. Severity, asserted impact and age — the risk read.
    risk_bits = []
    sev = severity_phrase(dom["severity_mix"])
    if sev:
        risk_bits.append(f"severity splits as {sev}")
    if dom["flagged"]:
        risk_bits.append(f"{plural(dom['flagged'], 'request')} carry a "
                         f"customer-stated business impact")
    if dom["median_age_days"] is not None:
        risk_bits.append(f"the median request has been open "
                         f"{months_of(dom['median_age_days'])} months")
    if risk_bits:
        # Semicolons, not "and": these are independent clauses about different
        # things and reading them as a list makes the sentence trip.
        joined = "; ".join(risk_bits)
        sentences.append(joined[0].upper() + joined[1:] + ".")

    # 4. The decision this tab supports.
    bands = dom["band_counts"]
    decision: List[str] = []
    if bands.get("start_now"):
        decision.append(f"commit {plural(bands['start_now'], 'theme')} now")
    if bands.get("plan"):
        decision.append(f"size {plural(bands['plan'], 'theme')} for the next PI")
    if bands.get("drop"):
        decision.append(f"propose closing {plural(bands['drop'], 'theme')}")
    if decision:
        sentences.append(
            f"For this tab the recommendation is to {join_list(decision)}, starting "
            f"with {_b(lead['name'])} &mdash; what carries it: {lead['why']}."
        )
    else:
        sentences.append(
            f"Everything here sits in the keep-and-revisit band; {_b(lead['name'])} "
            f"is the one to watch &mdash; what carries it: {lead['why']}."
        )
    return " ".join(sentences)


def domain_actions(dom: Dict[str, Any]) -> List[str]:
    """Concrete next actions for the Action panel — named, countable, ownable.

    Actions name specific themes and case numbers so they can be pasted into a
    PI board or a Salesforce comment without another lookup.
    """
    if not dom["requests"]:
        return ["Nothing to action for this domain in the selected timeframe."]

    clusters = sorted(dom["clusters"], key=P.sort_key("priority"), reverse=True)
    actions: List[str] = []

    start_now = [c for c in clusters if c["band"]["key"] == "start_now"]
    plan = [c for c in clusters if c["band"]["key"] == "plan"]
    drop = [c for c in clusters if c["band"]["key"] == "drop"]
    flagged = [c for c in clusters if c["flagged"]]

    for c in start_now[:2]:
        actions.append(
            f"Write the epic for {_b(c['name'])} and commit it this PI &mdash; "
            f"{plural(c['requests'], 'request')} from "
            f"{plural(c['customers'], 'customer')}. What carries it: {c['why']}."
        )
    if plan:
        names = join_list([_b(c["name"]) for c in plan[:3]])
        actions.append(
            f"Size {names} in this meeting so "
            f"{'it' if len(plan) == 1 else 'they'} can be committed next PI "
            f"without re-litigating the priority."
        )
    if flagged:
        c = flagged[0]
        cases = [_e(r.get("case_number", "")) for r in c.get("_records", [])
                 if P.is_business_impact(r)][:3]
        actions.append(
            f"Disposition the business-impact "
            f"{'flag' if c['flagged'] == 1 else 'flags'} on {_b(c['name'])} "
            f"individually"
            + (f" (case {join_list(cases)})" if cases else "")
            + " — a stated business consequence needs an answer to the customer "
              "whether or not the theme gets committed."
        )
    if drop:
        actions.append(
            f"Propose closing {plural(len(drop), 'theme')} "
            f"({plural(sum(c['requests'] for c in drop), 'request')}) with the "
            f"requesting customers: no second customer, no business-impact flag "
            f"and no material ARR behind any of them."
        )
    if not actions:
        lead = clusters[0]
        actions.append(
            f"Re-confirm with the requesting accounts whether {_b(lead['name'])} is "
            f"still needed — it leads this domain on score, but nothing here "
            f"reaches the commit threshold."
        )
    return actions


def domain_insights(dom: Dict[str, Any]) -> List[Dict[str, str]]:
    """"What stands out" — the observations that replaced three of four charts.

    A domain tab used to carry four charts. On a small domain (Reporting: 13
    requests across 8 themes) two of them simply redrew columns of the table
    directly above — theme priority and theme ARR — a third plotted six bubbles
    that mostly sat on top of each other at x=1, and the fourth drew a line
    wobbling between 0 and 5 requests a month. None of them told the reader
    anything the table had not already said.

    These take their place. Each is a single finding, stated with the number
    that supports it and the consequence that follows, and each is emitted ONLY
    when it is true of this domain — so a tab shows three findings or six, never
    a fixed grid of filler. Ordered most-decision-relevant first; the caller
    takes as many as it has room for.
    """
    if not dom["requests"]:
        return []

    clusters = dom["clusters"]
    out: List[Dict[str, str]] = []

    def add(tone: str, text: str) -> None:
        out.append({"tone": tone, "text": text})

    # 1. Is there any broad demand here at all? The single most useful fact
    #    about a domain, and the one the charts were worst at showing.
    multi = [c for c in clusters if c["customers"] > 1]
    if multi:
        widest = max(multi, key=lambda c: c["customers"])
        if len(multi) == 1:
            add("accent",
                f"Only one theme here has more than one customer behind it — "
                f"{_b(widest['name'])}, at {plural(widest['customers'], 'customer')}. "
                f"Everything else in this domain is a single account's ask.")
        else:
            add("accent",
                f"{plural(len(multi), 'theme')} of {len(clusters)} are asked for by "
                f"more than one customer, the widest being {_b(widest['name'])} at "
                f"{plural(widest['customers'], 'customer')}. Those are the "
                f"build-once-satisfy-many candidates.")
    else:
        add("orange",
            f"No theme in this domain has a second customer behind it. All "
            f"{len(clusters)} come from one account each, so nothing here is yet a "
            f"product signal — it is {plural(len(clusters), 'account conversation')}.")

    # 2. Is one account driving the whole tab?
    top = dom["top_accounts"][0] if dom["top_accounts"] else None
    if top and top["requests"] > 1:
        share = round(100 * top["requests"] / dom["requests"])
        if share >= 25:
            add("orange",
                f"{_e(top['name'])} alone filed {plural(top['requests'], 'request')} "
                f"here — {share}% of the domain. Read this tab as that account's "
                f"priorities before reading it as the market's.")

    # 3. Has demand stopped? Replaces the per-domain trend chart, which at this
    #    volume was a line wobbling between zero and five.
    monthly = [m for m in dom.get("monthly", []) if m["count"]]
    if monthly:
        last = monthly[-1]["month"]
        if dom["recent_90"]:
            add("accent",
                f"{plural(dom['recent_90'], 'request')} arrived in the last 90 days, "
                f"the most recent in {_month(last)} — this demand is live.")
        else:
            add("orange",
                f"Nothing new has arrived in this domain since {_month(last)}. "
                f"Either it is solved, or nobody is asking any more — worth "
                f"confirming which before committing capacity to it.")

    # 4. Severity ceiling — cheap to read, and it frames the whole tab.
    mix = dom["severity_mix"]
    crit, high = mix.get("critical", 0), mix.get("high", 0)
    if crit:
        add("red",
            f"{plural(crit, 'request')} here {'is' if crit == 1 else 'are'} "
            f"Critical severity. Those cannot sit in the backlog by default — "
            f"each needs an explicit yes or no.")
    elif not high:
        add("muted",
            f"All {dom['requests']} requests here are Medium severity or below, so "
            f"severity is not what should decide this tab — repetition and ARR are.")

    # 5. Customer-stated business impact.
    if dom["flagged"]:
        add("red",
            f"{plural(dom['flagged'], 'request')} carry a customer-stated business "
            f"impact and need an answer to the customer whether or not the theme "
            f"gets committed.")

    # 6. The shape of the decision, stated plainly.
    bands = dom["band_counts"]
    if not bands.get("start_now") and not bands.get("plan"):
        add("muted",
            f"None of the {len(clusters)} themes here reaches the commit threshold "
            f"this cycle. Treat this tab as a cleanup pass rather than a planning "
            f"one.")
    elif bands.get("drop"):
        drop_requests = sum(c["requests"] for c in clusters
                            if c["band"]["key"] == "drop")
        add("muted",
            f"{plural(bands['drop'], 'theme')} ({plural(drop_requests, 'request')}) "
            f"score below the keep threshold. Closing them is the cheapest way to "
            f"make what remains in this domain mean something.")

    # 7. Where the revenue actually sits (absorbed from the Interpretation panel).
    by_arr = sorted(clusters, key=P.sort_key("arr"), reverse=True)
    if by_arr and by_arr[0]["arr"] > 0:
        total = sum(c["arr"] for c in by_arr) or 1
        lead = by_arr[0]
        share = round(100 * lead["arr"] / total)
        if share >= 30:
            add("green",
                f"{_b(lead['name'])} carries {money(lead['arr'])} of the "
                f"{money(total)} across this domain — {share}% of its revenue "
                f"exposure in one theme. ARR counts each account once, so that is "
                f"exposure, not request volume dressed up as money.")
    else:
        add("muted",
            f"No ARR is recorded against the accounts asking in this domain, so "
            f"prioritise it on severity, repetition and product judgement rather "
            f"than revenue.")

    # 8. Something old that still scores — the decision the team keeps deferring.
    stale = [c for c in clusters
             if c["median_age_days"] is not None and c["median_age_days"] > 365
             and c["band"]["key"] in ("start_now", "plan", "backlog")]
    if stale:
        oldest = max(stale, key=lambda c: c["median_age_days"])
        add("orange",
            f"{_b(oldest['name'])} has been waiting "
            f"{months_of(oldest['median_age_days'])} months and still scores in the "
            f"{oldest['band']['label'].lower()} band — a decision that keeps being "
            f"deferred rather than taken.")

    return out


def _month(iso: str) -> str:
    """'2026-04' → 'April 2026'."""
    names = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
    try:
        year, month = iso.split("-")[:2]
        return f"{names[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return iso


def epic_rationale(cluster: Dict[str, Any]) -> str:
    """The 'why now' line on an epic card and in the decision queue.

    Two clauses: what carries this theme, and what the decision costs in
    practice. Never speculates about churn — it states who stays on a workaround.

    The second clause is **band-aware**. An earlier version was not, and put
    "defensible to keep in the backlog if capacity is tight" on a card badged
    *Drop candidate* — the rationale arguing with the badge directly above it.
    """
    carried = cluster["why"]
    band = cluster.get("band", {}).get("key", "backlog")

    if band == "drop":
        # The consequence of closing, not of deferring — and the size of the
        # account is said out loud, because "propose closing" reads very
        # differently to the person who has to make that call to a $318K customer.
        if cluster["arr"] >= 250_000:
            consequence = (f"Closing it means telling a {money(cluster['arr'])} account "
                           f"no, so make that call deliberately rather than in bulk.")
        elif cluster["max_repeats"] > 1:
            consequence = (f"One account asked {cluster['max_repeats']} times and nobody "
                           f"else has — worth a word with them before it is closed.")
        else:
            consequence = ("One customer, no impact flag and little revenue behind it: "
                           "the cheapest thing on this page to close.")
    elif cluster["customers"] > 1:
        consequence = (f"Deferring leaves {plural(cluster['customers'], 'customer')} "
                       f"({money(cluster['arr'])}) on their current workaround.")
    elif cluster["max_repeats"] > 1:
        consequence = (f"The requesting account has already come back "
                       f"{cluster['max_repeats']} times on this.")
    elif cluster["arr"] >= 250_000:
        consequence = (f"One account at {money(cluster['arr'])} is behind it, so this "
                       f"is an account conversation as much as a roadmap one.")
    else:
        consequence = ("Single customer, limited revenue behind it — defensible to "
                       "keep in the backlog if capacity is tight.")
    return f"{carried[0].upper() + carried[1:]}. {consequence}"


def data_quality_notes(model: Dict[str, Any]) -> List[str]:
    """What the numbers on this page do and do not include.

    A PI meeting argues about numbers, so it should be able to see the caveats
    without asking. Only issues actually present in the upload are listed — a
    clean export gets a one-line list.
    """
    notes: List[str] = []
    t = model["totals"]
    if t["zero_arr_accounts"]:
        notes.append(
            f"{plural(t['zero_arr_accounts'], 'account')} of {t['customers']} have "
            f"no ARR recorded, so their requests are scored on severity, "
            f"repetition and subject alone."
        )
    if t.get("non_usd"):
        notes.append(
            f"{plural(t['non_usd'], 'request')} come from accounts whose ARR is "
            f"booked in a non-USD currency; figures are used exactly as exported, "
            f"with no conversion applied."
        )
    if t["missing_dates"]:
        notes.append(
            f"{plural(t['missing_dates'], 'request')} have no readable opened date. "
            f"They appear under All time only, and are left out of the momentum "
            f"charts rather than being counted as recent."
        )
    if t["missing_severity"]:
        notes.append(
            f"{plural(t['missing_severity'], 'request')} have no severity set and "
            f"are scored just below Medium rather than as harmless."
        )
    notes.append(
        f"ARR totals count each account once, however many requests it filed: the "
        f"{t['requests']:,} requests in scope come from {t['customers']} distinct "
        f"accounts."
    )
    return notes
