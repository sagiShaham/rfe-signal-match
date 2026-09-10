"""Tests for the PI Priority ranking engine.

The engine decides what a PI Planning meeting looks at first, so these tests are
mostly about the claims it is NOT allowed to make: no ARR double-counting, no
"close this" recommendation on a large or repeatedly-requested theme, and no
rank that arrives without a reason attached.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring import priority as P

NOW = datetime(2026, 9, 10)


def _rfe(case="00500001", subject="Add an export button to Network Activity",
         description="", account="Acme GmbH", arr=150_000.0, severity="Medium",
         bi="", reason="", days=30, sub_domain="Endpoint Protection"):
    return {
        "case_number": case, "subject": subject, "description": description,
        "account_name": account, "account_arr": arr, "severity": severity,
        "business_impact": bi, "business_impact_reason": reason,
        "sub_domain": sub_domain, "status": "Added to Backlog",
        "created_date": (NOW - timedelta(days=days)).strftime("%Y-%m-%d"),
    }


# ── Weights and scale ────────────────────────────────────────────────────────

def test_weights_sum_to_one():
    """The composite is only interpretable as 0-100 if the weights sum to 1."""
    assert abs(sum(P.WEIGHTS.values()) - 1.0) < 1e-9


def test_score_is_bounded():
    """No combination of inputs can leave the 0-100 range."""
    worst = _rfe(arr=0, severity="Low", days=3000, subject="minor cosmetic tooltip wording")
    best = _rfe(arr=50_000_000, severity="Critical", bi="1", reason="production down",
                days=0, subject="detection bypass causes data loss for all MSSP tenants")
    lo = P.score_rfe(worst)["score"]
    hi = P.score_rfe(best, {"requests": 40, "customers": 30, "max_repeats": 9}, now=NOW)["score"]
    assert 0 <= lo < hi <= 100


def test_arr_component_is_capped_and_monotonic():
    assert P.arr_component(0) == 0
    assert P.arr_component(250_000) < P.arr_component(1_000_000) < P.arr_component(2_000_000)
    # Beyond the reference point everything is equally "large" — otherwise one
    # outlier account sets the scale for the whole portfolio.
    assert P.arr_component(2_000_000) == P.arr_component(50_000_000) == 1.0


# ── Severity ─────────────────────────────────────────────────────────────────

def test_critical_severity_is_recognised():
    """v1 had no handling for Critical at all; it fell through to the blank default."""
    assert P.normalise_severity("Critical") == "critical"
    assert P.SEVERITY_WEIGHT["critical"] > P.SEVERITY_WEIGHT["high"]
    assert P.severity_label("Critical") == "Critical"


@pytest.mark.parametrize("raw,expected", [
    ("P1", "critical"), ("Urgent", "critical"), ("P2", "high"), ("Major", "high"),
    ("Normal", "medium"), ("Minor", "low"), ("", ""), ("wat", ""),
])
def test_severity_variants_fold(raw, expected):
    assert P.normalise_severity(raw) == expected


def test_blank_severity_is_not_treated_as_harmless():
    """Unset usually means nobody triaged it, so it sits just below Medium."""
    assert P.SEVERITY_WEIGHT["low"] < P.SEVERITY_WEIGHT[""] < P.SEVERITY_WEIGHT["medium"]


def test_unset_severity_renders_as_a_word_not_an_empty_cell():
    assert P.severity_label("") == "Unset"


# ── Business impact ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["1", 1, "true", "TRUE", "Yes", "y", "x", True])
def test_business_impact_positives(raw):
    assert P.is_business_impact({"business_impact": raw})


@pytest.mark.parametrize("raw", ["0", 0, "", None, "false", "No", "maybe", "-"])
def test_business_impact_negatives(raw):
    """An unrecognised value must never silently promote an RFE."""
    assert not P.is_business_impact({"business_impact": raw})


def test_written_reason_outranks_a_bare_flag():
    bare = P.score_rfe(_rfe(bi="1"), now=NOW)
    written = P.score_rfe(_rfe(bi="1", reason="Blocks their onboarding"), now=NOW)
    assert written["score"] > bare["score"]


# ── Repetition ───────────────────────────────────────────────────────────────

def test_repetition_rewards_breadth_over_depth():
    """Ten customers asking once is a product signal; one asking ten times is an
    account signal. The model must not confuse them."""
    broad = P.repetition_component(customers=10, max_repeats=1)
    deep = P.repetition_component(customers=1, max_repeats=10)
    assert broad > deep


def test_repetition_first_extra_customer_matters_most():
    """1->2 customers is the moment a request stops being anecdotal; 7->8 is noise."""
    first_step = P.repetition_component(2, 1) - P.repetition_component(1, 1)
    last_step = P.repetition_component(8, 1) - P.repetition_component(7, 1)
    assert first_step > last_step


def test_repetition_index_counts_customers_not_rows():
    records = [
        {"_domain": "EPP", "_cluster": "AV for Linux", "account_name": "Acme"},
        {"_domain": "EPP", "_cluster": "AV for Linux", "account_name": "Acme"},
        {"_domain": "EPP", "_cluster": "AV for Linux", "account_name": "Globex"},
    ]
    idx = P.build_repetition_index(records)["EPP||AV for Linux"]
    assert idx == {"requests": 3, "customers": 2, "max_repeats": 2, "repeat_accounts": 1}


# ── Subject signal ───────────────────────────────────────────────────────────

def test_subject_signal_separates_a_security_gap_from_a_tooltip():
    gap = P.subject_signal("detection bypass leaves endpoints unprotected")["score"]
    cosmetic = P.subject_signal("change the colour of the tray icon")["score"]
    assert gap > 0.9 and cosmetic == 0.0


def test_subject_signal_does_not_match_inside_words():
    """Regression: 'hang' matched inside 'change', inflating the stability family
    on every request that used the word 'change'."""
    assert P.subject_signal("change the report layout")["families"] == []
    assert "stability" in P.subject_signal("the console hangs on load")["families"]


def test_subject_signal_families_do_not_add_up():
    """A request is not twice as urgent for being describable two ways."""
    one = P.subject_signal("audit log retention policy")["score"]
    two = P.subject_signal("audit log retention policy for all MSSP tenants")["score"]
    assert two > one
    assert two <= 1.0


def test_nice_to_have_penalty_cannot_be_outvoted_by_arr_alone():
    cosmetic = _rfe(subject="Would be nice to rename the Hosts tab", arr=2_000_000,
                    severity="Low")
    assert P.score_rfe(cosmetic, now=NOW)["band"]["key"] in ("backlog", "drop")


# ── ARR aggregation ──────────────────────────────────────────────────────────

def test_arr_counts_each_account_once():
    """The v1 bug: an account with 13 requests contributed its ARR 13 times, which
    reported $64.1M of portfolio ARR against a true $20.0M."""
    records = [_rfe(case=f"0050000{i}", account="Hossdorf", arr=105_777.05)
               for i in range(13)]
    assert P.distinct_account_arr(records) == pytest.approx(105_777.05)


def test_arr_sums_across_distinct_accounts():
    records = [_rfe(account="Acme", arr=100_000), _rfe(account="Globex", arr=50_000)]
    assert P.distinct_account_arr(records) == pytest.approx(150_000)


def test_unnamed_accounts_are_not_collapsed_together():
    """Two blank account names are not evidence of one customer."""
    records = [_rfe(account="", arr=10_000), _rfe(account="", arr=20_000)]
    assert P.distinct_account_arr(records) == pytest.approx(30_000)


# ── Bands and escalation floors ──────────────────────────────────────────────

def test_bands_are_ordered_and_absolute():
    assert P.band_for(100)["key"] == "start_now"
    assert P.band_for(60)["key"] == "plan"
    assert P.band_for(45)["key"] == "backlog"
    assert P.band_for(10)["key"] == "drop"


def test_quiet_backlog_may_produce_no_start_now():
    """Absolute thresholds must allow "nothing is urgent" as an answer."""
    quiet = [_rfe(case=f"005000{i}", subject="Small usability improvement",
                  arr=1_000, severity="Low", days=800, account=f"Tiny {i}")
             for i in range(5)]
    scored = [P.score_rfe(r, now=NOW) for r in quiet]
    assert all(s["band"]["key"] != "start_now" for s in scored)


def test_critical_severity_cannot_land_in_backlog():
    """Calibration case: Critical + a written business impact from a single
    $185K customer scored 49.7 and came out "Keep in backlog"."""
    r = _rfe(subject="Block transfer of sensitive data", severity="Critical",
             arr=185_000, bi="1", reason="Data exfiltration risk")
    s = P.score_rfe(r, now=NOW)
    assert s["band"]["key"] == "plan"
    assert s["escalated"]
    assert "Critical" in s["why"] or "Critical" in s["escalated"]


def test_flagged_request_is_never_a_drop_candidate():
    r = _rfe(subject="Some small thing", severity="Low", arr=0, bi="1", days=900)
    assert P.score_rfe(r, now=NOW)["band"]["key"] != "drop"


def test_large_account_theme_is_never_a_drop_candidate():
    """A $1.2M two-customer theme was being labelled "Candidate to close"."""
    records = [_rfe(account="Big One", arr=1_200_000, severity="Low", days=700),
               _rfe(account="Big Two", arr=90_000, severity="Low", days=700)]
    g = P.score_group(records, now=NOW)
    assert g["band"]["key"] != "drop"
    assert "ARR" in g["escalated"]


def test_three_customers_is_never_a_drop_candidate():
    records = [_rfe(account=f"Acct {i}", arr=1_000, severity="Low", days=900)
               for i in range(3)]
    g = P.score_group(records, now=NOW)
    assert g["band"]["key"] != "drop"
    assert "customers" in g["escalated"]


def test_escalation_does_not_lower_a_score():
    """A floor is a floor: something already above it is untouched."""
    strong = [_rfe(account=f"A{i}", arr=1_500_000, severity="Critical", bi="1",
                   reason="production down",
                   subject="detection bypass causes data loss") for i in range(6)]
    g = P.score_group(strong, now=NOW)
    assert g["score"] >= P.BAND_START_NOW
    assert g["escalated"] == ""


# ── Explainability ───────────────────────────────────────────────────────────

def test_every_rfe_score_carries_a_reason():
    for r in (_rfe(), _rfe(severity="Low", arr=0, days=900),
              _rfe(severity="Critical", bi="1", reason="down")):
        s = P.score_rfe(r, now=NOW)
        assert s["why"].strip()
        assert "..." not in s["why"]


def test_reason_never_leaks_a_formula():
    """Leadership pages get sentences, not arithmetic."""
    s = P.score_rfe(_rfe(severity="High", arr=900_000), now=NOW)
    for token in ("*", "0.22", "weight", "score =", "component"):
        assert token not in s["why"].lower()


def test_group_reason_mentions_breadth_when_breadth_is_the_driver():
    records = [_rfe(account=f"Acct {i}", arr=10_000) for i in range(6)]
    assert "6 customers asking" in P.score_group(records, now=NOW)["why"]


def test_empty_group_is_safe():
    """Domains with no requests in the timeframe still have to render."""
    g = P.score_group([], now=NOW)
    assert g["score"] == 0 and g["requests"] == 0 and g["why"]


# ── Sorting ──────────────────────────────────────────────────────────────────

def test_sort_fields_all_have_a_key_function():
    for key, _label, _help in P.SORT_FIELDS:
        assert callable(P.sort_key(key))


def test_sort_by_arr_orders_by_arr():
    a = {"score": 10, "arr": 900_000, "customers": 1}
    b = {"score": 90, "arr": 10_000, "customers": 1}
    assert sorted([a, b], key=P.sort_key("arr"), reverse=True)[0] is a


def test_sort_by_repetition_orders_by_customers():
    a = {"score": 90, "arr": 900_000, "customers": 1, "requests": 1}
    b = {"score": 10, "arr": 10_000, "customers": 9, "requests": 9}
    assert sorted([a, b], key=P.sort_key("repetition"), reverse=True)[0] is b


def test_sort_by_severity_uses_the_severity_rank():
    a = {"score": 90, "severity_rank": 1}
    b = {"score": 10, "severity_rank": 4}
    assert sorted([a, b], key=P.sort_key("severity"), reverse=True)[0] is b


def test_recency_and_oldest_are_opposites():
    fresh = {"score": 50, "age_days": 5}
    stale = {"score": 50, "age_days": 500}
    assert sorted([fresh, stale], key=P.sort_key("recency"), reverse=True)[0] is fresh
    assert sorted([fresh, stale], key=P.sort_key("oldest"), reverse=True)[0] is stale


def test_missing_age_sorts_last_under_recency():
    """A row with no date must not masquerade as the newest thing in the list."""
    fresh = {"score": 50, "age_days": 5}
    undated = {"score": 50, "age_days": None}
    assert sorted([undated, fresh], key=P.sort_key("recency"), reverse=True)[0] is fresh


# ── Recency ──────────────────────────────────────────────────────────────────

def test_recency_decays_but_never_to_zero_for_a_recent_quarter():
    assert P.recency_component(0) == 1.0
    assert P.recency_component(90) == 1.0
    assert 0 < P.recency_component(700) < P.recency_component(300) < 1.0


def test_missing_date_scores_mid_not_zero():
    """Unknown age is not evidence of staleness."""
    assert P.recency_component(None) == 0.5
