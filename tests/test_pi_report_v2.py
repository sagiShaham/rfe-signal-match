"""Tests for the PI Planning report (v2) — data layer and rendered page.

Most of these encode promises made to the people who will sit in the PI Planning
meeting: nothing is truncated with an ellipsis, no PM Summary is ever a raw
Salesforce description, no chart puts two units on one set of bars, and every
number the page shows can be traced back to a record it names.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring import pi_report_generator as G
from scoring import pi_report_assets as A
from scoring import priority as P

NOW = datetime(2026, 9, 10)
RUN = "report-test"

DESC = ("The customer explains that their console gives no way to export the "
        "network activity list, so every review is done by reading the screen "
        "and retyping what matters into a spreadsheet before it can be shared "
        "with the security team for the weekly review meeting.")


def _row(case, subject, *, account="Acme GmbH", arr=150_000.0, severity="Medium",
         domain="Endpoint", sub_domain="Endpoint Protection", days=30,
         bi=0, reason="", currency="USD", description=DESC):
    return (RUN, case, subject, description, account, arr, "Added to Backlog",
            domain, sub_domain, severity,
            (NOW - timedelta(days=days)).strftime("%Y-%m-%d"),
            NOW.isoformat(), bi, reason, "Amir Olswang", currency)


@pytest.fixture
def db(tmp_path):
    """A temp database shaped like the platform's, with a small realistic set."""
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE rfe_pulls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, case_number TEXT,
        subject TEXT, description TEXT, account_name TEXT, account_arr REAL,
        status TEXT, domain TEXT, sub_domain TEXT, severity TEXT,
        created_date TEXT, pulled_at TEXT, business_impact INTEGER,
        business_impact_reason TEXT, case_owner TEXT, arr_currency TEXT)""")
    conn.execute("""CREATE TABLE run_meta (run_id TEXT, started_at TEXT,
        rfe_count INTEGER, status TEXT, source TEXT)""")
    rows = [
        # A broad theme: three customers asking for the same capability.
        _row("00500001", "[RFE] Antivirus scan for Linux endpoints", account="Acme GmbH"),
        _row("00500002", "Antivirus scan on Linux endpoints", account="Globex srl", arr=90_000),
        _row("00500003", "- AV scan for Linux endpoint hosts", account="Initech", arr=60_000),
        # A Critical single-customer request with a written business impact.
        _row("00500004", "Block transfer of sensitive data.", severity="Critical",
             account="Umbrella AG", arr=185_000, bi=1,
             reason="Data exfiltration risk raised by their auditor"),
        # A low-signal one-off, old, no ARR — the drop-candidate shape.
        _row("00500005", "Would be nice to rename the Hosts tab", severity="Low",
             account="Tiny Ltd", arr=0, days=800),
        # Sub-Domain disagrees with Product Domain: Sub-Domain must win.
        _row("00500006", "Scheduled executive report by site", domain="Endpoint",
             sub_domain="Reporting", account="Verxo srl", arr=151_000),
        # Neither Salesforce field is usable: keyword fallback must place it.
        _row("00500007", "CLM log collection for syslog sources", domain="",
             sub_domain="Any", account="Lantech", arr=310_000, currency="EUR"),
        # No readable date at all.
        _row("00500008", "Device control read/write policy", domain="Other",
             sub_domain="", account="Cyberxperts", arr=45_000),
    ]
    conn.executemany(
        "INSERT INTO rfe_pulls (run_id,case_number,subject,description,account_name,"
        "account_arr,status,domain,sub_domain,severity,created_date,pulled_at,"
        "business_impact,business_impact_reason,case_owner,arr_currency) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("UPDATE rfe_pulls SET created_date='' WHERE case_number='00500008'")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def model(db):
    return G.build_model(db, RUN, now=NOW)


@pytest.fixture
def html(db):
    return G.generate_report(db, RUN)


# ── Classification ───────────────────────────────────────────────────────────

def test_sub_domain_beats_product_domain():
    """Sub-Domain is the field a PM curates per case; Product Domain lumps a
    third of the export into "Endpoint"."""
    assert G.classify_domain("Endpoint", "Reporting", "Scheduled report", "") == "Reporting"
    assert G.classify_domain("Endpoint", "Alert UI", "Alert tags", "") == "Alert UI"


def test_junk_sub_domains_fall_through():
    """'Any' and 'Other' carry no information and must not create a bucket."""
    assert G.classify_domain("SIEM", "Any", "whatever", "") == "SIEM"
    assert G.classify_domain("Cloud", "Other", "whatever", "") == "CSPM"


def test_keyword_fallback_runs_last():
    assert G.classify_domain("", "", "CLM log collection via syslog", "") == "SIEM"
    assert G.classify_domain("", "", "Ninja integration group names", "") == "PSA/RMM"


def test_unclassifiable_lands_in_other():
    assert G.classify_domain("", "", "Request for integration", "") == "Other"


def test_siem_and_identity_are_separate_domains():
    """A standing rule of the report: they never share a tab."""
    assert "SIEM" in G.DOMAIN_ORDER and "Identity" in G.DOMAIN_ORDER
    assert G.DOMAIN_PAGE_ID["SIEM"] != G.DOMAIN_PAGE_ID["Identity"]


def test_every_domain_has_a_page_id():
    for d in G.DOMAIN_ORDER:
        assert d in G.DOMAIN_PAGE_ID


# ── Subject cleaning ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("[RFE] Add an export button", "Add an export button"),
    ("RFE: Add an export button", "Add an export button"),
    ("FR - MSP level audit log", "MSP level audit log"),
    ("- New CLM data sources", "New CLM data sources"),
    ("Email Security Filters [00392493]", "Email Security Filters"),
    ("Block transfer of sensitive data.", "Block transfer of sensitive data"),
    ("[RFE] -  Automated Reporting", "Automated Reporting"),
])
def test_clean_subject(raw, expected):
    assert G.clean_subject(raw) == expected


def test_clean_subject_never_truncates():
    long = "A " + "very " * 40 + "long subject line that must survive intact"
    assert G.clean_subject(long).endswith("intact")
    assert "…" not in G.clean_subject(long)


def test_clean_subject_never_returns_empty():
    assert G.clean_subject("[RFE]") == "[RFE]"
    assert G.clean_subject("- - -").strip()


# ── Clustering ───────────────────────────────────────────────────────────────

def test_similar_subjects_form_one_theme(model):
    clusters = model["tf"]["all"]["clusters"]
    av = [c for c in clusters if "Linux" in c["name"] and c["requests"] > 1]
    assert len(av) == 1, "the three Linux AV requests should be one theme"
    assert av[0]["requests"] == 3
    assert av[0]["customers"] == 3


def test_theme_name_comes_from_the_medoid_not_the_richest_account(model):
    """v1 named a theme after its highest-ARR member, letting one big account's
    phrasing label everyone else's request."""
    av = [c for c in model["tf"]["all"]["clusters"]
          if c["requests"] == 3 and "Linux" in c["name"]][0]
    assert not av["name"].startswith("-")
    assert "[RFE]" not in av["name"]


def test_cluster_membership_is_stable_across_timeframes(model):
    """A theme must keep its identity when the reader changes the window."""
    def members(tf):
        return {c["name"]: tuple(sorted(c["cases"]))
                for c in model["tf"][tf]["clusters"] if c["requests"] == 3}
    assert members("all") == members("12")


# ── Model shape ──────────────────────────────────────────────────────────────

def test_all_timeframes_are_built(model):
    assert set(model["tf"]) == {"all", "12", "6", "3"}
    for tf in model["tf"].values():
        assert tf["narrative"]["headline"]
        assert tf["narrative"]["paragraphs"]


def test_every_scoped_record_has_a_score(model):
    for tf_key, frame in model["tf"].items():
        cases = {r["case"] for r in model["records"] if tf_key in r["tfs"]}
        assert cases == set(frame["scores"]), f"score coverage broken for {tf_key}"


def test_every_cluster_case_exists_as_a_record(model):
    known = {r["case"] for r in model["records"]}
    for frame in model["tf"].values():
        for c in frame["clusters"]:
            assert set(c["cases"]) <= known


def test_undated_record_appears_only_under_all_time(model):
    rec = [r for r in model["records"] if r["case"] == "00500008"][0]
    assert rec["tfs"] == ["all"], "an undated request must not be counted as recent"


def test_totals_count_accounts_once(model):
    t = model["tf"]["all"]["totals"]
    accounts = {r["account"] for r in model["records"]}
    expected = sum(max(r["arr"] for r in model["records"] if r["account"] == a)
                   for a in accounts)
    assert t["arr"] == pytest.approx(expected)


def test_non_usd_is_disclosed_not_converted(model):
    t = model["tf"]["all"]["totals"]
    assert t["non_usd"] == 1
    notes = " ".join(model["tf"]["all"]["narrative"]["data_quality"])
    assert "non-USD" in notes and "no conversion" in notes


def test_domains_with_no_requests_still_have_an_entry(model):
    """Every tab has to render something, including the empty ones."""
    ids = {d["id"] for d in model["tf"]["all"]["domains"]}
    assert ids == set(G.DOMAIN_PAGE_ID.values())
    empty = [d for d in model["tf"]["all"]["domains"] if not d["requests"]]
    assert all(d["narrative"] for d in empty)


def test_cluster_records_are_not_shipped_twice(model):
    """`_records` is a narrative-time convenience and must never reach the page."""
    for frame in model["tf"].values():
        for c in frame["clusters"]:
            assert "_records" not in c


# ── PM Summaries ─────────────────────────────────────────────────────────────

def test_pm_summary_is_never_the_raw_description(model):
    for r in model["records"]:
        assert r["pm"], f"{r['case']} has no PM summary"
        if r["original"]:
            assert r["pm"] != r["original"]
            assert r["original"] not in r["pm"]


def test_pm_summary_and_original_are_separate_fields(model):
    """Keeping them apart is what makes a leaked description structurally
    impossible rather than merely discouraged."""
    rec = model["records"][0]
    assert "pm" in rec and "original" in rec


def test_no_pm_summary_ends_in_an_ellipsis(model):
    for r in model["records"]:
        assert not r["pm"].rstrip().endswith("...")
        assert "…" not in r["pm"]


def test_narratives_contain_no_placeholder_text(model):
    for frame in model["tf"].values():
        blob = json.dumps(frame["narrative"]) + json.dumps(
            [d["narrative"] for d in frame["domains"]])
        for bad in ("…", "TBD", "TODO", "lorem", "XXX"):
            assert bad not in blob


def test_domain_panels_are_written_for_every_active_domain(model):
    for d in model["tf"]["all"]["domains"]:
        if d["requests"]:
            assert len(d["insights"]) >= 2, f"{d['domain']} has no findings"
            assert d["actions"] and all(a.strip() for a in d["actions"])


def test_no_second_panel_of_observations(model):
    """The Interpretation panel was removed because it restated the findings
    above it. Nothing may reintroduce a second panel of observations."""
    for d in model["tf"]["all"]["domains"]:
        assert "interpretation" not in d


def test_insights_are_specific_not_filler(model):
    """Every finding carries a number or a named theme — none is a platitude."""
    for d in model["tf"]["all"]["domains"]:
        for ins in d["insights"]:
            text = ins["text"]
            assert re.search(r"\d", text) or "<strong>" in text, text
            assert "..." not in text and "…" not in text


# ── Rendered page ────────────────────────────────────────────────────────────

def test_page_renders_and_embeds_its_payload(html):
    assert html.startswith("<!DOCTYPE html>")
    assert "const PAYLOAD = {" in html
    assert '"__PAYLOAD__"' not in html, "payload placeholder was not substituted"
    assert "/*__CSS__*/" not in html and "/*__JS__*/" not in html


def test_no_secondary_axis_anywhere(html):
    """The chart v1 shipped drew its ARR bars on a secondary axis, so Plotly did
    not group-offset them and they landed on top of the count bars. No chart in
    this report is allowed a second axis."""
    assert "yaxis2" not in html
    assert "overlaying" not in html


def test_no_grouped_bar_charts(html):
    """Grouped bars are where the overlap came from; stacked or single only."""
    assert "barmode: 'group'" not in html
    assert 'barmode:"group"' not in html


def test_no_ellipsis_truncation_in_the_page(html):
    """Rows grow, labels wrap, containers scroll — nothing is cut."""
    assert "…" not in html
    assert "-webkit-line-clamp" not in html
    assert "text-overflow:ellipsis" not in html.replace(" ", "")
    # No string field is ever cut short. (A `.slice()` on a LIST is fine — that
    # is chart density, and `test_chart_subsets_are_disclosed` covers it.)
    for field in ("subject", "name", "pm", "cluster", "rationale", "original"):
        assert not re.search(field + r"\s*(\|\|\s*'')?\s*\)?\.slice\(", html), field


def test_no_entity_leaks_in_chart_text(html):
    """Plotly draws SVG text and does NOT decode HTML entities.

    The shipped v2.6 put `$318K &middot; 1 customer` on a bar label and
    `classified as &quot;high&quot;` on a y-axis, because chart strings carried
    `&middot;` directly and `wrapLabel` ran the HTML escaper over category
    labels. Anything bound for a chart goes through `plotlyText` instead.
    """
    assert "function plotlyText(" in html
    assert "lines.map(plotlyText)" in html, "wrapLabel must not HTML-escape a chart label"
    for line in html.splitlines():
        if "hovertemplate:" in line or "text: items.map" in line:
            assert "&middot;" not in line and "&quot;" not in line and "&amp;" not in line, line


def test_chart_subsets_are_disclosed(html):
    """A chart plotting only its top N must say so, for the same reason a table
    must not clip text: the reader cannot see what is missing."""
    assert "function setChartNote(" in html
    assert "Showing the " in html
    assert html.count('class="chart-note"') >= 4


def test_pm_summary_cells_are_explicitly_unclamped(html):
    assert ".pm{white-space:normal !important" in html.replace("\n", "")


def test_tables_are_fixed_layout_inside_a_scroll_container(html):
    """The structural fix for columns riding over each other."""
    assert "table-layout:fixed" in html.replace(" ", "")
    assert ".tscroll{overflow-x:auto" in html.replace("\n", "")
    assert "<colgroup>" in html


def test_all_sort_fields_reach_the_dropdown(html, model):
    for field in model["meta"]["sort_fields"]:
        assert field["label"] in html


def test_required_filters_are_present(html):
    for label in ("Severity", "Business impact", "ARR", "Repetition", "Timeframe"):
        assert label in html
    for option in ("Flagged only", "$1M and above", "2+ customers", "No ARR recorded"):
        assert option in html


def test_case_numbers_are_copy_to_clipboard_with_stoppropagation(html):
    assert "function copyCase(" in html
    assert "ev.stopPropagation()" in html
    assert "navigator.clipboard" in html


def test_all_eighteen_tabs_are_present(html):
    assert ">Executive<" in html
    for d in G.DOMAIN_ORDER:
        assert d in html


def test_default_timeframe_is_all_time(model):
    """This report decides the fate of an accumulated backlog; a 12-month
    default would silently hide the oldest requests, which are precisely the
    ones a cleanup session needs to see."""
    assert model["meta"]["default_tf"] == "all"
    assert model["meta"]["timeframes"][0]["label"] == "All time"


def test_light_mode_only(html):
    assert "paper_bgcolor: 'white'" in html
    assert "prefers-color-scheme: dark" not in html
    assert "--bg:#f7f8fa" in html


def test_empty_dataset_renders_a_useful_page(tmp_path):
    path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE rfe_pulls (id INTEGER PRIMARY KEY, run_id TEXT,
        case_number TEXT, subject TEXT, description TEXT, account_name TEXT,
        account_arr REAL, status TEXT, domain TEXT, sub_domain TEXT,
        severity TEXT, created_date TEXT, pulled_at TEXT)""")
    conn.execute("CREATE TABLE run_meta (run_id TEXT, started_at TEXT)")
    conn.commit(); conn.close()
    out = G.generate_report(path, "nothing-here")
    assert "No RFE data found" in out


# ── Injection safety ─────────────────────────────────────────────────────────

def test_customer_text_cannot_inject_script(tmp_path):
    """Subjects are customer-authored and land inside build-time HTML prose."""
    path = str(tmp_path / "xss.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE rfe_pulls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, case_number TEXT,
        subject TEXT, description TEXT, account_name TEXT, account_arr REAL,
        status TEXT, domain TEXT, sub_domain TEXT, severity TEXT,
        created_date TEXT, pulled_at TEXT, business_impact INTEGER,
        business_impact_reason TEXT, case_owner TEXT, arr_currency TEXT)""")
    conn.execute("CREATE TABLE run_meta (run_id TEXT, started_at TEXT)")
    conn.execute(
        "INSERT INTO rfe_pulls (run_id,case_number,subject,description,account_name,"
        "account_arr,status,domain,sub_domain,severity,created_date,pulled_at,"
        "business_impact,business_impact_reason,case_owner,arr_currency) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, "00500099", "<script>alert(1)</script> broken export", DESC,
         "<img src=x onerror=alert(2)>", 500_000.0, "Added to Backlog", "Endpoint",
         "Endpoint Protection", "High", "2026-08-01", NOW.isoformat(), 0, "", "", "USD"))
    conn.commit(); conn.close()

    out = G.generate_report(path, RUN)
    # Three script tags, all the report's own: plotly, payload, behaviour.
    assert out.count("</script>") == 3
    assert "<script>alert(1)</script>" not in out

    # The hostile strings are allowed to exist as DATA inside the JSON payload —
    # they sit in a JS string and are escaped again by esc() before they reach
    # the DOM. What matters is that they never appear in the markup the browser
    # parses as HTML, so everything outside the payload line is checked.
    markup = "\n".join(l for l in out.splitlines() if "const PAYLOAD = " not in l)
    assert "<img src=x onerror=" not in markup
    assert "<script>alert" not in markup

    # The narratives are written at build time and injected as HTML by the page,
    # so they are where an unescaped subject would actually become script. Pull
    # them back out of the payload and check they carry the escaped form.
    payload = json.loads(re.search(r"const PAYLOAD = (\{.*\});", out).group(1)
                         .replace(r"<\/script", "</script"))
    prose = json.dumps(payload["tf"]["all"]["narrative"]) + json.dumps(
        [d["narrative"] for d in payload["tf"]["all"]["domains"]])
    assert "&lt;script&gt;" in prose
    assert "<script>" not in prose


def test_payload_cannot_close_the_script_tag(tmp_path, db):
    """A description containing '</script>' would otherwise end the block."""
    conn = sqlite3.connect(db)
    conn.execute("UPDATE rfe_pulls SET description=? WHERE case_number='00500001'",
                 ("Broken by </script><script>alert(1)</script> in the text. " + DESC,))
    conn.commit(); conn.close()
    out = G.generate_report(db, RUN)
    assert "</script><script>alert(1)" not in out
    assert r"<\/script" in out


# ── Regression guards on the numbers ─────────────────────────────────────────

def test_flagged_request_is_visible_as_an_escalation(model):
    rec = [r for r in model["records"] if r["case"] == "00500004"][0]
    assert rec["bi"] is True
    assert rec["bi_reason"]
    score = model["tf"]["all"]["scores"]["00500004"]
    assert score["band"] in ("plan", "start_now")


def test_drop_candidate_shape_is_recognised(model):
    """Old, Low, no ARR, single customer, cosmetic subject."""
    assert model["tf"]["all"]["scores"]["00500005"]["band"] == "drop"


def test_totals_and_domain_counts_agree(model):
    for tf_key, frame in model["tf"].items():
        assert sum(d["requests"] for d in frame["domains"]) == frame["totals"]["requests"]
        assert sum(c["requests"] for c in frame["clusters"]) == frame["totals"]["requests"]
