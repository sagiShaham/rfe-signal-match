"""Tests for the Weekly Analysis report generator (no DB, no LLM required)."""
import sqlite3
from datetime import datetime, timedelta

import pytest

import scoring.weekly_report_generator as wr


# ── Section classification ────────────────────────────────────────────────────
@pytest.mark.parametrize("domain,subject,expected", [
    ("Endpoint",   "Full AV scan on macOS",                      "epp"),
    ("SIEM",       "CLM field indexing over 100 fields",         "siem"),
    ("CLM",        "Add Logstash log source",                    "siem"),
    ("Cloud",      "AWS misconfiguration coverage",              "cspm"),
    ("ESPM",       "SSPM users export",                          "cspm"),
    ("Email",      "Email digest scheduling",                    "email"),
    ("Identity",   "Entra ID suspicious login alerts",           "identity"),
    ("Automation", "NinjaOne integration for deployment",        "automations"),
    ("Reporting",  "Scheduled quarterly report",                 "reporting"),
    ("Platform",   "Tenant search history",                      "platform"),
    ("UX/UI",      "Dashboard refresh behaviour",                "platform"),
])
def test_domain_field_drives_section(domain, subject, expected):
    assert wr.classify_section(domain, subject, "") == expected


@pytest.mark.parametrize("subject,expected", [
    ("Web Content Filter category management",     "wac"),
    ("Select All WCF categories",                  "wac"),
    ("Native MCP server support",                  "ai"),
    ("Unauthorized LLM usage detection",           "ai"),
    ("USB storage device control",                 "epp"),
    ("Kubernetes container protection",            "epp"),
    ("Quarantine release request workflow",        "email"),
    ("Active Directory alert association",         "identity"),
    ("Global playbooks hierarchy for MSP",         "automations"),
])
def test_keyword_fallback_when_domain_blank(subject, expected):
    assert wr.classify_section("", subject, "") == expected


def test_siem_and_identity_are_separate_sections():
    """Skill rule: SIEM and Identity MUST always be separate sections."""
    ids = [s for s, _, _ in wr.SECTIONS]
    assert "siem" in ids and "identity" in ids
    assert wr.classify_section("SIEM", "log collection", "") != \
           wr.classify_section("Identity", "active directory", "")


def test_unclassifiable_defaults_to_platform():
    assert wr.classify_section("", "Miscellaneous unrelated ask", "") == "platform"


def test_api_requests_are_disambiguated():
    # skill "Difficult Classification Cases"
    assert wr.classify_section("", "API for remote agent uninstall", "") == "automations"
    assert wr.classify_section("", "Add Email Security Data to API", "") == "email"
    assert wr.classify_section("", "API for endpoint list", "") == "platform"


def test_clean_subject_strips_prefixes():
    assert wr.clean_subject("[RFE] Action Cancellation in UI") == "Action Cancellation in UI"
    assert wr.clean_subject("[EXTERNAL] Feature request: Exclude group") \
        .startswith("Exclude group")


# ── Priority score & trend ───────────────────────────────────────────────────
def _rfe(arr=0.0, severity="Low", days=100):
    return {"arr": arr, "severity": severity, "_days": days}


def test_priority_score_formula():
    today = datetime(2026, 1, 30)
    # ARR 50k -> 2 ; High -> 12 ; <=7 days -> 8
    assert wr.priority_score(_rfe(50_000, "High", 3), today) == pytest.approx(22.0)
    # Low severity, no ARR, old -> 3 + 2
    assert wr.priority_score(_rfe(0, "Low", 90), today) == pytest.approx(5.0)


def test_higher_arr_and_severity_score_higher():
    today = datetime(2026, 1, 30)
    low = wr.priority_score(_rfe(1_000, "Low", 60), today)
    high = wr.priority_score(_rfe(500_000, "Critical", 2), today)
    assert high > low


@pytest.mark.parametrize("days,badge", [
    (0, "NEW"), (7, "NEW"), (8, "GROWING"), (14, "GROWING"), (15, "STABLE"), (400, "STABLE"),
])
def test_trend_badges(days, badge):
    assert wr.trend_of(days) == badge


def test_report_date_falls_back_to_newest_when_retroactive():
    old = datetime.today() - timedelta(days=120)
    records = [{"opened": old}]
    date, retro = wr.report_date_for(records)
    assert retro is True and date == old


def test_report_date_is_today_for_fresh_data():
    records = [{"opened": datetime.today() - timedelta(days=2)}]
    date, retro = wr.report_date_for(records)
    assert retro is False


# ── Clustering: 100% coverage, no RFE left behind ────────────────────────────
def _mk(case, subject, arr=1000.0, sev="Low", days=30):
    return {"case_number": case, "subject": subject, "description": "",
            "account_name": "Acme", "arr": arr, "severity": sev,
            "_days": days, "_score": 5.0, "_trend": wr.trend_of(days),
            "status": "Opened", "opened": None, "pm_summary": "", "decision": ""}


def test_every_rfe_lands_in_exactly_one_cluster():
    rfes = [
        _mk("1", "Kubernetes container scanning"),
        _mk("2", "Full AV scan on Linux"),
        _mk("3", "USB device control policy"),
        _mk("4", "Totally unrelated one-off ask"),
        _mk("5", "Another unrelated singleton"),
    ]
    clusters = wr.build_clusters(rfes, "epp")
    seen = [m["case_number"] for c in clusters for m in c["members"]]
    assert sorted(seen) == ["1", "2", "3", "4", "5"]
    assert len(seen) == len(set(seen)), "an RFE appeared in more than one cluster"


def test_cluster_aggregates_arr_severity_and_accounts():
    a = _mk("1", "Kubernetes scanning", arr=100_000.0, sev="High")
    b = _mk("2", "Kubernetes runtime protection", arr=50_000.0, sev="Low")
    b["account_name"] = "Globex"
    clusters = wr.build_clusters([a, b], "epp")
    top = clusters[0]
    assert top["arr"] == 150_000.0
    assert top["severity"] == "High"          # highest severity in cluster
    assert set(top["accounts"]) == {"Acme", "Globex"}


def test_clusters_sorted_by_aggregate_score():
    small = _mk("1", "Kubernetes scanning"); small["_score"] = 2.0
    big_a = _mk("2", "USB device control"); big_a["_score"] = 40.0
    big_b = _mk("3", "USB storage blocking"); big_b["_score"] = 30.0
    clusters = wr.build_clusters([small, big_a, big_b], "epp")
    assert clusters[0]["score"] >= clusters[-1]["score"]


def test_empty_section_yields_no_clusters():
    assert wr.build_clusters([], "epp") == []


# ── PM Decision Summary rules (the skill's absolute rules) ───────────────────
def test_missing_description_uses_mandated_tam_fallback():
    rfe = _mk("1", "Something")
    rfe["description"] = ""
    html = wr._pm_summary_html(rfe)
    assert "follow up with TAM" in html
    assert "Do not proceed to backlog" in html


def test_raw_description_is_never_used_as_summary():
    """Absolute rule: never paste raw description text into the summary."""
    raw = ("Customer explains at length that the console does not let them "
           "export the user list to CSV which blocks their monthly audit.")
    rfe = _mk("1", "Export user list")
    rfe["description"] = raw
    rfe["pm_summary"] = ""
    html = wr._pm_summary_html(rfe)
    assert raw[:40] not in html, "raw description leaked into the PM summary"
    assert "not generated yet" in html


def test_cached_summary_is_rendered_when_present():
    rfe = _mk("1", "Export user list")
    rfe["description"] = "some raw text"
    rfe["pm_summary"] = "Handcrafted summary about the export gap."
    assert "Handcrafted summary about the export gap." in wr._pm_summary_html(rfe)


def test_summary_html_never_ends_with_ellipsis():
    rfe = _mk("1", "X")
    rfe["description"] = "y" * 500
    assert not wr._pm_summary_html(rfe).rstrip().endswith("...")


# ── ARR formatting (skill table) ─────────────────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    (0, "$0"), (500, "$500"), (1_500, "$2K"), (215_809, "$216K"),
    (1_100_000, "$1.1M"), (33_000_000, "$33.0M"),
])
def test_arr_formatting(value, expected):
    assert wr.fmt_arr(value) == expected


# ── Decisions persistence ────────────────────────────────────────────────────
def test_decision_save_update_and_clear(tmp_path):
    db = str(tmp_path / "t.db")
    sqlite3.connect(db).close()
    wr.ensure_tables(db)

    wr.save_decision(db, "00123", "backlog", note="ship it", decided_by="pm@cynet.com")
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT decision, note FROM rfe_decisions WHERE case_number='00123'").fetchone()
    assert row == ("backlog", "ship it")

    wr.save_decision(db, "00123", "out_of_scope")
    row = conn.execute("SELECT decision FROM rfe_decisions WHERE case_number='00123'").fetchone()
    assert row[0] == "out_of_scope", "decision should be updated in place"

    wr.save_decision(db, "00123", "")   # empty clears
    assert conn.execute(
        "SELECT COUNT(*) FROM rfe_decisions WHERE case_number='00123'").fetchone()[0] == 0
    conn.close()


def test_ensure_tables_is_idempotent(tmp_path):
    db = str(tmp_path / "t.db")
    sqlite3.connect(db).close()
    wr.ensure_tables(db)
    wr.ensure_tables(db)   # must not raise


# ── Report shell ─────────────────────────────────────────────────────────────
def test_empty_db_renders_friendly_page(tmp_path):
    db = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE run_meta (run_id TEXT, started_at TEXT, rfe_count INT, source TEXT);
        CREATE TABLE rfe_pulls (run_id TEXT, case_number TEXT, subject TEXT,
            description TEXT, account_name TEXT, account_arr REAL, status TEXT,
            domain TEXT, sub_domain TEXT, severity TEXT, created_date TEXT);
    """)
    conn.commit(); conn.close()
    html = wr.generate_report(db)
    assert "No RFE data found" in html
    assert "<html" in html.lower()


def test_generated_report_is_light_mode_and_self_contained(tmp_path):
    db = str(tmp_path / "one.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE run_meta (run_id TEXT, started_at TEXT, rfe_count INT, source TEXT);
        CREATE TABLE rfe_pulls (run_id TEXT, case_number TEXT, subject TEXT,
            description TEXT, account_name TEXT, account_arr REAL, status TEXT,
            domain TEXT, sub_domain TEXT, severity TEXT, created_date TEXT);
    """)
    conn.execute("INSERT INTO run_meta VALUES ('r1','2026-05-01',1,'csv')")
    conn.execute(
        "INSERT INTO rfe_pulls VALUES ('r1','00999','Kubernetes container scanning',"
        "'','Acme',250000,'Opened','Endpoint','','High','5/12/2026 10:15 PM')")
    conn.commit(); conn.close()

    html = wr.generate_report(db)
    # light theme background from the skill's CSS variables
    assert "--bg:#f0f4fc" in html
    assert "background:#0" not in html.replace("rgba(0,0,0", "")  # no dark panels
    # all 11 sections present, SIEM and Identity separate
    for sid, _, _ in wr.SECTIONS:
        assert f'id="section-{sid}"' in html
    # interactivity + light Plotly config
    for fn in ("function showSection", "function toggleDetail", "function copyCase",
               "function setDecision"):
        assert fn in html
    assert "#5a6a8a" in html and "#d1daf0" in html   # Plotly light font/grid
    assert "plotly.min.js" in html
    # the one RFE is rendered with a decision row and a PM summary block
    assert "00999" in html
    assert "decision-row" in html and "rfe-pm-summary" in html
