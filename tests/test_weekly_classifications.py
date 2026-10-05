"""The Weekly Analysis report classifies on the same dimensions as PI Planning.

A PM reads both reports about the same backlog in the same week. If the weekly
report calls a request a drop candidate while the PI report has it in Plan, the
reader is right to distrust both — so these tests are mostly about the two
reports agreeing, and about the ways that agreement was quietly broken before.
"""
import re
import sqlite3
import sys
import os
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring import weekly_report_generator as W
from scoring import pi_report_generator as G
from scoring import priority as P
from scoring import report_ui

RUN = "report-weekly-test"
NOW = datetime(2026, 9, 10)

DESC = ("The customer explains that the console offers no way to export this "
        "view, so every review is retyped into a spreadsheet before it can be "
        "shared with their security team at the weekly meeting.")


def _row(case, subject, *, account="Acme GmbH", arr=150_000.0, severity="Medium",
         domain="Endpoint", sub_domain="Endpoint Protection", days=5, bi=0, reason=""):
    return (RUN, case, subject, DESC, account, arr, "Added to Backlog", domain,
            sub_domain, severity, (NOW - timedelta(days=days)).strftime("%Y-%m-%d"),
            NOW.isoformat(), bi, reason, "Amir Olswang", "USD")


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "weekly.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE rfe_pulls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, case_number TEXT,
        subject TEXT, description TEXT, account_name TEXT, account_arr REAL,
        status TEXT, domain TEXT, sub_domain TEXT, severity TEXT,
        created_date TEXT, pulled_at TEXT, business_impact INTEGER,
        business_impact_reason TEXT, case_owner TEXT, arr_currency TEXT)""")
    conn.execute("CREATE TABLE run_meta (run_id TEXT, started_at TEXT, rfe_count INTEGER, "
                 "status TEXT, source TEXT)")
    rows = [
        _row("00600001", "Antivirus scan for Linux endpoints", account="Acme GmbH"),
        _row("00600002", "AV scan on Linux endpoints", account="Globex srl", arr=90_000),
        _row("00600003", "Antivirus scanning for Linux hosts", account="Initech", arr=60_000),
        _row("00600004", "Block transfer of sensitive data", severity="Critical",
             account="Umbrella AG", arr=185_000, bi=1, reason="Raised by their auditor"),
        _row("00600005", "Rename the Hosts tab", severity="Low", account="Tiny Ltd",
             arr=0, days=900),
        _row("00600006", "Scheduled executive report by site", sub_domain="Reporting",
             account="Verxo srl", arr=151_000),
        _row("00600007", "CLM log collection for syslog sources", domain="SIEM",
             sub_domain="SIEM / CLM", account="Lantech", arr=310_000),
    ]
    conn.executemany(
        "INSERT INTO rfe_pulls (run_id,case_number,subject,description,account_name,"
        "account_arr,status,domain,sub_domain,severity,created_date,pulled_at,"
        "business_impact,business_impact_reason,case_owner,arr_currency) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit(); conn.close()
    return path


@pytest.fixture
def html(db):
    return W.generate_report(db, RUN)


def _prepare(db):
    """Run the generator's own pre-pass, then cluster and score.

    Mirrors generate_report: a provisional score exists before clustering
    (build_clusters orders members by it), and score_records overwrites it with
    the shared PI Priority afterwards.
    """
    records, _meta = W.load_rfes(db, RUN)
    report_date, _retro = W.report_date_for(records)
    for r in records:
        r["_days"] = (report_date - r["opened"]).days if r["opened"] else 999
        r["_score"] = W.priority_score(r, report_date)
        r["_trend"] = W.trend_of(r["_days"])
        r["_recent"] = r["_days"] <= 14
        r["_section"] = W.classify_section(r["sf_domain"], r["subject"], r["description"])
    clusters = {sid: W.build_clusters(
                    sorted([r for r in records if r["_section"] == sid],
                           key=lambda r: -r["_score"]), sid)
                for sid in W.DOMAIN_IDS}
    W.score_records(records, clusters, report_date)
    return records, clusters, report_date


# ── The data the classifications need ───────────────────────────────────────

def test_weekly_reads_the_business_impact_columns(db):
    records, _ = W.load_rfes(db, RUN)
    flagged = [r for r in records if P.is_business_impact(r)]
    assert len(flagged) == 1
    assert P.business_impact_reason(flagged[0]) == "Raised by their auditor"


def test_unknown_severity_is_not_rewritten_as_low(db):
    """It used to be coerced to 'Low', a claim the data does not make."""
    conn = sqlite3.connect(db)
    conn.execute("UPDATE rfe_pulls SET severity='' WHERE case_number='00600005'")
    conn.commit(); conn.close()
    records, _ = W.load_rfes(db, RUN)
    rec = [r for r in records if r["case_number"] == "00600005"][0]
    assert rec["severity"] != "Low"
    assert P.normalise_severity(rec["severity"]) == ""


def test_report_survives_a_database_without_the_v2_columns(tmp_path):
    """A report built before the migration must degrade, not fail."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE rfe_pulls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, case_number TEXT,
        subject TEXT, description TEXT, account_name TEXT, account_arr REAL,
        status TEXT, domain TEXT, sub_domain TEXT, severity TEXT,
        created_date TEXT, pulled_at TEXT)""")
    conn.execute("CREATE TABLE run_meta (run_id TEXT, started_at TEXT, "
                 "rfe_count INTEGER, status TEXT, source TEXT)")
    conn.execute("INSERT INTO rfe_pulls (run_id,case_number,subject,description,"
                 "account_name,account_arr,status,domain,sub_domain,severity,"
                 "created_date,pulled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (RUN, "00600099", "Export the activity view", DESC, "Acme", 10_000.0,
                  "Added to Backlog", "Endpoint", "Endpoint Protection", "Medium",
                  "2026-08-01", NOW.isoformat()))
    conn.commit(); conn.close()
    out = W.generate_report(path, RUN)
    assert "Cynet Weekly Analysis" in out


# ── The same engine, the same answers ───────────────────────────────────────

def test_weekly_scores_with_the_shared_engine(db):
    records, clusters, report_date = _prepare(db)
    for r in records:
        assert 0 <= r["_score"] <= 100, "weekly must use the 0-100 PI Priority scale"
        assert r["_band"]["key"] in ("start_now", "plan", "backlog", "drop")
        assert r["_why"].strip()


def test_both_reports_agree_on_the_same_request(db):
    """The point of sharing scoring/priority.py, asserted rather than assumed."""
    model = G.build_model(db, RUN, now=NOW)
    pi = model["tf"]["all"]["scores"]

    records, clusters, report_date = _prepare(db)
    weekly = {r["case_number"]: r for r in records}

    common = [c for c in pi if c in weekly]
    assert common
    agree = sum(1 for c in common if pi[c]["band"] == weekly[c]["_band"]["key"])
    assert agree == len(common), (
        "every request must land in the same decision band in both reports; "
        + ", ".join(f"{c}: PI {pi[c]['band']} vs weekly {weekly[c]['_band']['key']}"
                    for c in common if pi[c]["band"] != weekly[c]["_band"]["key"]))


def test_repetition_is_measured_in_the_same_partition(db):
    """Repetition used to be read off the weekly's own broad thematic areas, so
    one case read '17 customers' there and '2' in the PI report."""
    model = G.build_model(db, RUN, now=NOW)
    pi = model["tf"]["all"]["scores"]
    records, clusters, report_date = _prepare(db)
    for r in records:
        if r["case_number"] in pi:
            assert r["_customers"] == pi[r["case_number"]]["customers"], r["case_number"]


# ── The catch-all bucket ────────────────────────────────────────────────────

def test_catch_all_is_recognised():
    assert W.is_catch_all({"title": "Other Requests in this Domain"})
    assert not W.is_catch_all({"title": "AV Scan — macOS & Linux"})


def test_catch_all_gets_no_breadth_credit(db):
    """It collected 25 customers in EPP and became the top-ranked theme."""
    records, clusters, report_date = _prepare(db)
    for sid, cs in clusters.items():
        for c in cs:
            if W.is_catch_all(c):
                members = c["members"]
                assert all(m["_customers"] <= 1 or
                           m["_customers"] < len(c["accounts"]) for m in members)


# ── The page ────────────────────────────────────────────────────────────────

def test_the_five_controls_are_present(html):
    for label in ("Sort by", "Severity", "Business impact", "ARR",
                  "Repetition", "Timeframe"):
        assert ">" + label + "<" in html, label
    for option in ("Flagged only", "$1M and above", "2+ customers", "Last 3 months"):
        assert option in html


def test_cards_carry_the_filter_attributes(html):
    assert 'data-sev="' in html and 'data-bi="' in html
    assert 'data-arr="' in html and 'data-cust="' in html
    assert 'data-score="' in html and 'data-days="' in html


def test_badges_match_the_pi_report_exactly(html):
    """Same shapes, same labels, same explanations — one source, two pages."""
    from scoring import pi_report_assets as A
    assert report_ui.BAND_JS in html
    assert report_ui.BAND_JS in A.JS
    for mark in ("▶", "◆", "■", "○"):
        assert mark in html
    for label in ("Start now", "Plan", "Keep in backlog", "Drop candidate"):
        assert label in html


def test_shared_filter_engine_is_injected(html):
    assert report_ui.FILTER_JS in html
    assert "renderControlBar('ctl-bar'" in html


# ── The per-domain PDF ──────────────────────────────────────────────────────

def test_every_domain_with_requests_gets_a_print_sheet(html):
    assert 'class="print-sheet"' in html
    assert "exportDomainPdf(" in html
    assert 'id="print-sheet-epp"' in html


def test_print_sheet_has_the_one_pager_blocks(html):
    sheet = html[html.index('id="print-sheet-epp"'):]
    sheet = sheet[:sheet.index("</div>\n<div class=\"print-sheet\"")] if \
        "</div>\n<div class=\"print-sheet\"" in sheet else sheet[:20000]
    for block in ("What has to be decided", "Themes worth discussing",
                  "Weekly RFE review", "ARR at stake"):
        assert block in sheet, block


def test_print_sheet_is_hidden_on_screen(html):
    assert ".print-sheet { display:none; }" in html.replace("  ", " ") or \
           ".print-sheet {{ display:none; }}" in html or \
           re.search(r"\.print-sheet\s*\{\s*display:none", html)


def test_printing_does_not_inherit_the_sidebar_grid(html):
    """The screen layout is a grid with a 250px sidebar column; leaving it in
    place printed the whole sheet inside that column."""
    assert "display:block !important" in html
    assert "@page" in html and "A4 portrait" in html


def test_interactive_chrome_is_left_out_of_the_pdf(html):
    print_block = html[html.index("@media print"):]
    for hidden in ("#sidebar", "#main", "#ctl-bar", ".pdf-btn"):
        assert hidden in print_block, hidden

def test_a_section_without_request_cards_is_left_alone(html):
    """The Executive Overview holds KPIs and charts, not request cards. It was
    permanently reporting "No request in this section matches the current
    filters" with no filters active."""
    assert "if (!sec.querySelectorAll(cfg.groupSel).length) return;" in html


# ── v2.8: filters drive everything below them ───────────────────────────────

def _section(html, sid):
    start = html.index(f'id="section-{sid}"')
    nxt = html.find('<div class="section-page"', start + 10)
    return html[start:nxt if nxt != -1 else len(html)]


def test_overview_sits_above_the_bar_and_live_panels_below(html):
    """The rule: above the bar = the whole section, below it = the filters."""
    for sid in ("exec", "epp"):
        sec = _section(html, sid)
        summary = sec.index("Executive Summary")
        slot = sec.index('class="filter-slot"')
        kpis = sec.index('data-live="kpis"')
        risk = sec.index('data-live="risk"')
        assert summary < slot < kpis < risk, sid


def test_recommended_actions_moved_into_the_overview(html):
    """Actions describe the whole domain, so they sit above the bar."""
    sec = _section(html, "epp")
    assert sec.index("Recommended Actions") < sec.index('class="filter-slot"')


def test_every_panel_below_the_bar_is_live(html):
    sec = _section(html, "exec")
    below = sec[sec.index('class="filter-slot"'):]
    for kind in ("kpis", "domains", "risk"):
        assert f'data-live="{kind}"' in below, kind
    for chart in ("chart-count", "chart-arr", "chart-sev", "chart-recent"):
        assert chart in below


def test_server_side_copies_of_live_panels_are_gone():
    """One implementation per panel, or they drift apart."""
    assert not hasattr(W, "_kpi_row")
    assert not hasattr(W, "_risk_table")


def test_layout_is_not_offset_twice(html):
    """A grid column of 250px on top of the fixed sidebar's own 250px margin
    pushed every page right by a sidebar's width."""
    assert "grid-template-areas" not in html
    assert '<div id="main"><div id="ctl-bar"></div>' in html


def test_arr_counts_each_account_once(tmp_path):
    """The weekly report was still summing ARR per request, the bug the PI
    report fixed in v2.6 ($64.1M reported against a true $20.0M)."""
    path = str(tmp_path / "arr.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE rfe_pulls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, case_number TEXT,
        subject TEXT, description TEXT, account_name TEXT, account_arr REAL,
        status TEXT, domain TEXT, sub_domain TEXT, severity TEXT,
        created_date TEXT, pulled_at TEXT, business_impact INTEGER,
        business_impact_reason TEXT, case_owner TEXT, arr_currency TEXT)""")
    conn.execute("CREATE TABLE run_meta (run_id TEXT, started_at TEXT, rfe_count INTEGER, "
                 "status TEXT, source TEXT)")
    conn.executemany(
        "INSERT INTO rfe_pulls (run_id,case_number,subject,description,account_name,"
        "account_arr,status,domain,sub_domain,severity,created_date,pulled_at,"
        "business_impact,business_impact_reason,case_owner,arr_currency) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [_row(f"0070000{i}", f"Distinct request number {i} about exports", account="Solo AG",
              arr=200_000.0) for i in range(3)])
    conn.commit(); conn.close()
    out = W.generate_report(path, RUN)
    epp = _section(out, "epp")
    assert "$200K ARR" in epp, "one account with three requests is $200K, not $600K"
    assert "$600K" not in epp


def test_pdf_button_is_a_real_button_in_every_section(html):
    assert html.count('class="pdf-btn"') >= 2
    assert "exportDomainPdf('exec')" in html
    assert "Export the whole report as PDF" in html
    assert "Export this domain as PDF" in html


def test_executive_one_pager_exists_and_skips_the_catch_all(html):
    sheet = html[html.index('id="print-sheet-exec"'):]
    sheet = sheet[:sheet.index('id="print-sheet-', 20)] if 'id="print-sheet-' in sheet[20:] else sheet
    assert "Themes worth discussing" in sheet
    assert "Other Requests in this Domain" not in sheet


def test_no_business_impact_column_is_reported_as_such(db):
    """NULL (column absent) and 0 (nobody flagged) are different answers. A
    41-row upload with no flags read as a broken filter during review."""
    conn = sqlite3.connect(db)
    conn.execute("UPDATE rfe_pulls SET business_impact=NULL")
    conn.commit(); conn.close()
    out = W.generate_report(db, RUN)
    assert '"bi_available": false' in out


def test_zero_flags_with_the_column_present_is_reported_as_such(db):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE rfe_pulls SET business_impact=0")
    conn.commit(); conn.close()
    out = W.generate_report(db, RUN)
    assert '"bi_available": true' in out
    assert "nobody set it on any of these requests" in out


def test_tam_chase_never_cuts_a_case_number():
    """It used to slice the list at 120 characters, mid-number."""
    rfes = [{"case_number": f"0050000{i}", "description": ""} for i in range(8)]
    line = W._tam_chase(rfes)
    assert "#00500004" in line and "and 3 more" in line
    assert "#00500005" not in line


def test_domain_card_click_handler_is_valid_js(html):
    """A backslash-escaped quote inside the Python f-string was consumed, which
    emitted two adjacent string literals — a SyntaxError that stopped every
    live panel on the page."""
    assert "showSection('' + sid" not in html
    assert "showSection(&#39;' + sid + '&#39;)" in html


def test_chart_text_escaper_is_shared(html):
    """renderBubble calls plotlyText; it existed only in the PI page."""
    assert report_ui.CHART_TEXT_JS in html
    from scoring import pi_report_assets as A
    assert report_ui.CHART_TEXT_JS in A.JS
