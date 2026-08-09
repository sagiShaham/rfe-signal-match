"""Tests for the RFE Mail Broker client.

These cover the pure logic — validation, subject/body normalization, payload
building, response mapping, and secret redaction — with no network access.

Recipients may be on any domain: the guardrail is the broker's review-and-
approve step, not a client-side domain allowlist.
"""
import pytest

import broker_client as bc


# ── Recipient validation — any domain, but must be a real address ─────────────
def test_accepts_any_domain():
    for addr in ("employee@cynet.com", "buyer@customer.com",
                 "a.b@sub.example.co.il", "x+tag@mail.example.org"):
        assert bc.valid_recipient(addr) is True


def test_normalizes_case_and_whitespace():
    assert bc.validate_recipient("  Buyer@Customer.COM  ") == "buyer@customer.com"


def test_rejects_malformed_addresses():
    for addr in ("nope", "no@domain", "a b@c.com", "<x@y.com>",
                 "user@cynet.com@evil.com", "@nolocal.com", "trailing@"):
        assert bc.valid_recipient(addr) is False
        with pytest.raises(bc.BrokerError):
            bc.validate_recipient(addr)


def test_rejects_missing_recipient():
    with pytest.raises(bc.BrokerError):
        bc.validate_recipient("")


# ── Subject — used verbatim, no forced prefix ────────────────────────────────
def test_subject_is_used_verbatim():
    assert bc.ensure_subject("  Your RFE update  ") == "Your RFE update"


def test_no_test_prefix_is_injected():
    # a real customer must never receive a "[TEST]" marker they didn't write
    assert not bc.ensure_subject("Your RFE update").startswith("[TEST]")


def test_caller_supplied_prefix_is_left_alone():
    assert bc.ensure_subject("[TEST] Already prefixed") == "[TEST] Already prefixed"


def test_empty_subject_rejected():
    with pytest.raises(bc.BrokerError):
        bc.ensure_subject("   ")


# ── Body validation ───────────────────────────────────────────────────────────
def test_empty_body_rejected():
    with pytest.raises(bc.BrokerError):
        bc.validate_body("   \n  ")


def test_body_to_html_escapes_and_wraps():
    html = bc.text_to_html("Hello <script>alert(1)</script>\n\nSecond para")
    assert "<script>" not in html          # escaped, not injected
    assert "&lt;script&gt;" in html
    assert html.count("<p>") == 2          # two paragraphs


# ── Batch size cap ────────────────────────────────────────────────────────────
def test_batch_size_enforced():
    with pytest.raises(bc.BrokerError):
        bc.validate_batch_size([{}] * (bc.MAX_TEST_BATCH + 1))
    with pytest.raises(bc.BrokerError):
        bc.validate_batch_size([])
    bc.validate_batch_size([{}])           # 1 is fine


# ── Payload building ──────────────────────────────────────────────────────────
def test_build_test_batch_shape_and_no_sender():
    payload = bc.build_test_batch(
        recipient="Buyer@Customer.com",
        subject="Ping",
        body="Body text",
        rfe_id="RFE-12345",
        customer_name="Acme Corp",
    )
    assert payload["batchId"].startswith("RFE-MAIL-")
    assert len(payload["messages"]) == 1
    msg = payload["messages"][0]
    assert msg["recipient"] == "buyer@customer.com"      # lowercased, external
    assert msg["subject"] == "Ping"                      # verbatim, no prefix
    assert msg["rfeId"] == "RFE-12345"
    assert "bodyHtml" in msg and "bodyText" not in msg    # exactly one body form
    # sender mailbox is fixed by the broker and must never be in the payload
    assert "sender" not in msg and "from" not in msg
    # CC / Reply-To are unsupported by the broker — never smuggled in
    assert "cc" not in {k.lower() for k in msg} and "replyTo" not in msg
    # itemId respects the broker's allowed charset
    import re
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", msg["itemId"])


def test_build_test_batch_defaults_customer_fields():
    payload = bc.build_test_batch(recipient="t@example.com", subject="x", body="y")
    msg = payload["messages"][0]
    assert msg["customerId"] == "UNKNOWN"
    assert msg["customerName"] == "Unknown"


def test_build_test_batch_rejects_malformed_recipient():
    with pytest.raises(bc.BrokerError):
        bc.build_test_batch(recipient="not-an-address", subject="x", body="y")


# ── Submit response mapping ───────────────────────────────────────────────────
def test_map_submit_response():
    out = bc.map_submit_response({
        "batchId": "RFE-TEST-1", "status": "PendingReview",
        "reviewUrl": "https://host/api/review/RFE-TEST-1#token=abc",
        "messageCount": 1, "extra": "ignored",
    })
    assert out == {
        "batchId": "RFE-TEST-1", "status": "PendingReview",
        "reviewUrl": "https://host/api/review/RFE-TEST-1#token=abc",
        "messageCount": 1,
    }


# ── Status mapping — missing counters treated as zero ─────────────────────────
def test_sanitize_status_pending():
    out = bc.sanitize_status({"batchId": "b", "status": "PendingReview", "messageCount": 1})
    assert out["status"] == "PendingReview"
    assert out["counts"]["Sent"] == 0 and out["counts"]["Failed"] == 0
    assert out["items"] == []


def test_sanitize_status_completed():
    out = bc.sanitize_status({
        "batchId": "b", "status": "Completed", "messageCount": 1,
        "approvedBy": "schudinov@cynet.com",
        "counts": {"Sent": 1},
        "preview": [{"itemId": "i1", "recipient": "x@cynet.com", "status": "Sent",
                     "attemptCount": 1, "errorCode": ""}],
    })
    assert out["counts"]["Sent"] == 1
    assert out["approvedBy"] == "schudinov@cynet.com"
    assert out["items"][0]["recipient"] == "x@cynet.com"


def test_sanitize_status_rejected_missing_sent_counter_is_zero():
    out = bc.sanitize_status({
        "batchId": "b", "status": "Rejected",
        "rejectedBy": "schudinov@cynet.com", "counts": {"Rejected": 1},
    })
    assert out["status"] == "Rejected"
    assert out["counts"]["Sent"] == 0          # missing → zero, not KeyError
    assert out["counts"]["Rejected"] == 1
    assert out["rejectedBy"] == "schudinov@cynet.com"


def test_sanitize_status_failed_with_error_code():
    out = bc.sanitize_status({
        "batchId": "b", "status": "Failed", "counts": {"Failed": 1},
        "preview": [{"itemId": "i1", "recipient": "x@cynet.com",
                     "status": "Failed", "attemptCount": 4, "errorCode": "MailboxScopeDenied"}],
    })
    assert out["status"] == "Failed"
    assert out["items"][0]["errorCode"] == "MailboxScopeDenied"
    assert out["items"][0]["attemptCount"] == 4


# ── Secret redaction & error mapping ──────────────────────────────────────────
def _cfg():
    return bc.BrokerConfig(
        base_url="https://broker/api", submit_key="SUBMIT-SECRET-123",
        status_key="STATUS-SECRET-456", test_mode=True)


def test_redact_strips_keys():
    cfg = _cfg()
    text = "boom SUBMIT-SECRET-123 and STATUS-SECRET-456 leaked"
    red = bc._redact(text, cfg)
    assert "SUBMIT-SECRET-123" not in red
    assert "STATUS-SECRET-456" not in red
    assert "***" in red


def test_raise_for_status_auth_does_not_echo_body():
    cfg = _cfg()
    with pytest.raises(bc.BrokerError) as exc:
        bc._raise_for_status(403, {"message": "clientSecretSettingName missing"}, "", cfg)
    assert exc.value.status_code == 502
    assert "clientSecretSettingName" not in str(exc.value)


def test_raise_for_status_maps_codes():
    cfg = _cfg()
    for code, expected in [(400, 400), (404, 404), (409, 409), (429, 429), (500, 502), (503, 502)]:
        with pytest.raises(bc.BrokerError) as exc:
            bc._raise_for_status(code, {}, "", cfg)
        assert exc.value.status_code == expected


# ── Config status is secret-free ──────────────────────────────────────────────
def test_config_status_never_exposes_keys(monkeypatch):
    monkeypatch.setenv("RFE_BROKER_BASE_URL", "https://broker/api")
    monkeypatch.setenv("RFE_BROKER_SUBMIT_KEY", "SUBMIT-SECRET-123")
    monkeypatch.setenv("RFE_BROKER_STATUS_KEY", "STATUS-SECRET-456")
    monkeypatch.setenv("RFE_BROKER_TEST_MODE", "true")
    status = bc.config_status()
    assert status["configured"] is True
    assert status["test_mode"] is True
    blob = repr(status)
    assert "SUBMIT-SECRET-123" not in blob
    assert "STATUS-SECRET-456" not in blob


def test_is_configured_false_when_missing(monkeypatch):
    for name in ("RFE_BROKER_BASE_URL", "RFE_BROKER_SUBMIT_KEY", "RFE_BROKER_STATUS_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert bc.is_configured() is False
    assert bc.config_status()["configured"] is False
