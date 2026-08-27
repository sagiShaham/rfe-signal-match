"""Tests for the PM Decision Summary writer.

The cynet-weekly-report skill's Step 4 rules are absolute, so most of these
tests are about what a summary must never be: lifted from the description,
truncated, or missing.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring import pm_summary as ps


def _mk(case="00500001", subject="Provide an Export button for Network Activity",
        description="", arr=171_000.0, severity="Medium", account="NPO TORINO SRL",
        section="wac", days=42):
    return {
        "case_number": case, "subject": subject, "description": description,
        "account_name": account, "arr": arr, "severity": severity,
        "status": "Opened", "section": section, "days": days,
    }


LONG_DESC = (
    "Customer explains that the Network Activity screen shows every connection "
    "but there is no way to save what is on screen. They need the rows in a file "
    "so they can attach them to the monthly service review pack they send out."
)


# ── The absolute rules ───────────────────────────────────────────────────────
def test_empty_description_uses_the_mandated_tam_fallback():
    assert ps.write_summary(_mk(description="")) == ps.NO_DESC_SUMMARY


def test_short_description_uses_the_mandated_tam_fallback():
    assert ps.write_summary(_mk(description="please fix")) == ps.NO_DESC_SUMMARY


def test_summary_never_lifts_a_phrase_from_the_description():
    text = ps.write_summary(_mk(description=LONG_DESC))
    assert not ps.shares_long_ngram(text, LONG_DESC)


def test_summary_never_ends_in_an_ellipsis():
    for desc in (LONG_DESC, LONG_DESC * 3, "x " * 400):
        text = ps.write_summary(_mk(description=desc))
        assert not text.rstrip().endswith(("...", "…"))
        assert text.rstrip().endswith((".", "!", "?"))


def test_description_is_never_the_fallback_even_when_nothing_matches():
    weird = "zzz " * 30                       # no ask type, no signals at all
    text = ps.write_summary(_mk(description=weird))
    assert "zzz zzz" not in text
    assert len(text.split()) > 12


def test_summary_is_three_sentences_of_prose_plus_its_routing_signals():
    """The skill wants 2-4 sentences and fixes each flag's wording exactly, so
    a case carrying both a cluster signal and a routing flag runs to five
    rather than reword one of them."""
    text = ps.write_summary(_mk(description=LONG_DESC))
    assert 2 <= len([c for c in text if c in ".!?"]) <= 3    # no flags on this one

    records = [_mk("00600001", "Export the alert list to CSV", LONG_DESC + " It is broken and does not work."),
               _mk("00600002", "Export the alert list to Excel", LONG_DESC)]
    ctx = ps.build_context(records)
    both = ps.write_summary(records[0], ctx)
    assert "Cluster with" in both and "route to engineering" in both
    assert len([c for c in both if c in ".!?"]) <= 5


# ── The three questions a summary must answer ────────────────────────────────
def test_summary_names_the_account_and_its_arr():
    text = ps.write_summary(_mk(description=LONG_DESC))
    assert "NPO TORINO SRL" in text
    assert "$171K" in text


def test_summary_states_the_gap_not_the_subject():
    text = ps.write_summary(_mk(description=LONG_DESC))
    assert "Provide an Export button" not in text     # the ask verb is stripped
    assert "export" in text.lower()


def test_no_arr_account_is_said_so_rather_than_shown_as_zero():
    text = ps.write_summary(_mk(description=LONG_DESC, arr=0))
    assert "$0" not in text
    assert "no ARR" in text


# ── Routing signals, in the skill's exact wording ────────────────────────────
@pytest.mark.parametrize("description,expected", [
    ("The export button is broken and does not work at all since last week, "
     "it throws an error every time we click it.",
     "route to engineering"),
    ("This used to work in the previous version and no longer does after the "
     "upgrade, the option was removed from the screen entirely.",
     "Regression"),
    ("Our auditor flagged this during the ISO 27001 review and we need it for "
     "compliance evidence before the next audit cycle.",
     "compliance/security failure"),
    ("We want native MCP server support so our AI agent can query Cynet data "
     "through the model context protocol directly.",
     "first-mover"),
])
def test_routing_flags_use_the_skills_wording(description, expected):
    assert expected in ps.write_summary(_mk(description=description))


def test_deal_blocker_needs_real_arr_behind_it():
    desc = ("We are running a POC and this is a blocker for the deal, the "
            "customer wants to see it working before they sign the contract.")
    assert "Deal blocker" in ps.write_summary(_mk(description=desc, arr=800_000))
    assert "Deal blocker" not in ps.write_summary(_mk(description=desc, arr=4_000))


def test_competitor_poc_names_the_competitor():
    desc = ("The account is running a POC against CrowdStrike and this gap is "
            "the reason they are still evaluating alternatives.")
    assert "Active CrowdStrike POC blocker" in ps.write_summary(_mk(description=desc))


# ── Dataset context: clusters and same-account siblings ──────────────────────
def test_similar_subjects_become_a_cluster_flag():
    records = [
        _mk("00100001", "Export the alert list to CSV", LONG_DESC),
        _mk("00100002", "Export the alert list to Excel", LONG_DESC),
    ]
    ctx = ps.build_context(records)
    text = ps.write_summary(records[0], ctx)
    assert "Cluster with #00100002" in text
    assert "single engineering task" in text


def test_second_request_from_a_high_arr_account_flags_churn_risk():
    records = [
        _mk("00200001", "Export the alert list", LONG_DESC, account="Yarix MSSP"),
        _mk("00200002", "Add MFA for admin login", LONG_DESC, account="Yarix MSSP"),
    ]
    ctx = ps.build_context(records)
    text = ps.write_summary(records[1], ctx)
    assert "churn risk" in text
    assert "Yarix MSSP" in text


def test_low_arr_account_does_not_get_the_churn_framing():
    records = [
        _mk("00300001", "Export the alert list", LONG_DESC, arr=9_000, account="Small Co"),
        _mk("00300002", "Add MFA for admin login", LONG_DESC, arr=9_000, account="Small Co"),
    ]
    ctx = ps.build_context(records)
    assert "churn risk" not in ps.write_summary(records[1], ctx)


# ── Readability: a subject that is not a noun phrase must not be slotted in ──
@pytest.mark.parametrize("subject", [
    "Show at the Tenant level, who closed alerts at the Global level",
    "Allow 'View Only' custom role to be able to 'Pause Cynet Agent'",
    "Provide a host field in Forensics where users can enter notes about a host",
    "Change the message when the \"Disable user\" (On Prem) fails.",
])
def test_awkward_subjects_fall_back_to_wording_that_needs_no_noun_phrase(subject):
    text = ps.write_summary(_mk(subject=subject, description=LONG_DESC))
    first = text.split(". ")[0]
    lead = first.split()[0].strip("'\"“”").lower()
    assert lead not in ps._BAD_LEAD, f"summary opens on a fragment: {first!r}"
    assert "  " not in text


def test_a_clean_noun_phrase_is_used_verbatim():
    text = ps.write_summary(_mk(subject="Provide an Export button for Network Activity",
                                description=LONG_DESC))
    assert "Network Activity" in text


def test_capitalisation_of_the_customers_own_words_is_preserved():
    text = ps.write_summary(_mk(subject="New CSPM integration for Huawei Cloud",
                                description=LONG_DESC, section="cspm"))
    assert "Huawei Cloud" in text and "huawei" not in text


# ── Determinism and caching ──────────────────────────────────────────────────
def test_the_same_rfe_always_produces_the_same_summary():
    a = ps.write_summary(_mk(description=LONG_DESC))
    b = ps.write_summary(_mk(description=LONG_DESC))
    assert a == b


def test_different_rfes_do_not_all_read_the_same():
    texts = {
        ps.write_summary(_mk("1", "Export the alert list to CSV", LONG_DESC)),
        ps.write_summary(_mk("2", "Add MFA for the admin console", LONG_DESC,
                             section="identity")),
        ps.write_summary(_mk("3", "Kubernetes node coverage for the agent", LONG_DESC,
                             section="epp")),
    }
    assert len(texts) == 3


class _TempDb:
    def __enter__(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        return self.path

    def __exit__(self, *exc):
        try:
            os.unlink(self.path)
        except OSError:
            pass


def test_ensure_summaries_fills_every_record_and_caches_them():
    with _TempDb() as db:
        records = [_mk("00400001", description=LONG_DESC),
                   _mk("00400002", "Add MFA for admin login", LONG_DESC)]
        result = ps.ensure_summaries(db, records)
        assert result["written"] == 2
        assert all(r["pm_summary"] for r in records)

        # Second pass reuses the cache instead of rewriting.
        again = [_mk("00400001", description=LONG_DESC),
                 _mk("00400002", "Add MFA for admin login", LONG_DESC)]
        result2 = ps.ensure_summaries(db, again)
        assert result2["reused"] == 2 and result2["written"] == 0
        assert again[0]["pm_summary"] == records[0]["pm_summary"]


def test_a_hand_written_summary_in_the_cache_is_left_alone():
    with _TempDb() as db:
        conn = sqlite3.connect(db)
        ps.ensure_cache_columns(conn)
        conn.execute("INSERT INTO pm_summaries (case_number, summary, model, "
                     "generated_at, source_hash) VALUES (?,?,?,?,?)",
                     ("00400003", "Hand-written by the PM.", "human", "", ""))
        conn.commit(); conn.close()

        records = [_mk("00400003", description=LONG_DESC)]
        ps.ensure_summaries(db, records)
        assert records[0]["pm_summary"] == "Hand-written by the PM."


def test_a_changed_description_rewrites_the_cached_summary():
    with _TempDb() as db:
        first = [_mk("00400004", description=LONG_DESC)]
        ps.ensure_summaries(db, first)

        second = [_mk("00400004", subject="Add MFA for admin login",
                      description=LONG_DESC + " They also need MFA on the admin login.")]
        result = ps.ensure_summaries(db, second)
        assert result["written"] == 1
        assert second[0]["pm_summary"] != first[0]["pm_summary"]


def test_records_without_a_description_still_get_the_mandated_fallback():
    with _TempDb() as db:
        records = [_mk("00400005", description="")]
        ps.ensure_summaries(db, records)
        assert records[0]["pm_summary"] == ps.NO_DESC_SUMMARY
