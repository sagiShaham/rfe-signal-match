"""
RFE Mail Broker client — TEST-EMAIL ONLY.
====================================================================
Thin server-side client for the production RFE Mail Broker Azure Function.

Scope guard: this module only ever builds and submits *internal test* batches
(recipients restricted to a single allowed domain, subjects forced to a
"[TEST]" prefix, small batch cap). It deliberately has no path for real
customer/bulk delivery — that workflow is owned separately by Sagi and lives in
the broker's own Entra-authenticated review portal.

Security notes:
- The submit/status Function keys are read from the environment on the server
  only. They are never returned to callers, never logged, and are stripped from
  any error text via `_redact` before it can bubble up to the API layer.
- The broker's `reviewUrl` is a temporary credential (expiring token in the URL
  fragment). This module passes it through to the caller once but never logs or
  persists it.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import os
import re
import uuid
from dataclasses import dataclass
from urllib.parse import quote

# ── Constants ────────────────────────────────────────────────────────────────
DEFAULT_ALLOWED_DOMAIN = "cynet.com"
MAX_TEST_BATCH = 5           # hard cap on messages per test batch
SUBJECT_PREFIX = "[TEST]"
_REQUEST_TIMEOUT = 15.0      # seconds, per broker call
_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class BrokerConfigError(RuntimeError):
    """Raised when required broker configuration is absent. Non-fatal: callers
    should disable the test-send feature, not crash the app."""


class BrokerError(RuntimeError):
    """A broker call failed. Carries an HTTP status code for the API layer and a
    message that is already redacted of any secrets."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class BrokerConfig:
    base_url: str
    submit_key: str
    status_key: str
    test_mode: bool
    allowed_domain: str


# ── Configuration ────────────────────────────────────────────────────────────
def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _test_mode_from_env() -> bool:
    return _env("RFE_BROKER_TEST_MODE", "true").lower() in ("1", "true", "yes", "on")


def _allowed_domain_from_env() -> str:
    return _env("RFE_BROKER_ALLOWED_TEST_DOMAIN", DEFAULT_ALLOWED_DOMAIN).lower() or DEFAULT_ALLOWED_DOMAIN


def get_config() -> BrokerConfig:
    """Return the broker configuration, or raise BrokerConfigError if any of the
    required values (base URL, submit key, status key) are missing."""
    base = _env("RFE_BROKER_BASE_URL").rstrip("/")
    submit = _env("RFE_BROKER_SUBMIT_KEY")
    status = _env("RFE_BROKER_STATUS_KEY")
    missing = [
        name
        for name, value in (
            ("RFE_BROKER_BASE_URL", base),
            ("RFE_BROKER_SUBMIT_KEY", submit),
            ("RFE_BROKER_STATUS_KEY", status),
        )
        if not value
    ]
    if missing:
        raise BrokerConfigError("Missing broker configuration: " + ", ".join(missing))
    return BrokerConfig(
        base_url=base,
        submit_key=submit,
        status_key=status,
        test_mode=_test_mode_from_env(),
        allowed_domain=_allowed_domain_from_env(),
    )


def is_configured() -> bool:
    try:
        get_config()
        return True
    except BrokerConfigError:
        return False


def config_status() -> dict:
    """Safe, secret-free view of the broker config for the frontend to decide
    whether to enable the test-send action."""
    domain = _allowed_domain_from_env()
    test_mode = _test_mode_from_env()
    try:
        cfg = get_config()
        return {
            "configured": True,
            "test_mode": cfg.test_mode,
            "allowed_domain": cfg.allowed_domain,
            "max_batch": MAX_TEST_BATCH,
        }
    except BrokerConfigError as exc:
        return {
            "configured": False,
            "test_mode": test_mode,
            "allowed_domain": domain,
            "max_batch": MAX_TEST_BATCH,
            "reason": str(exc),
        }


# ── Validation & normalization (pure) ────────────────────────────────────────
def valid_recipient(addr: str, domain: str) -> bool:
    """True only for `local@<domain>` exactly. Rejects subdomains
    (`x@a.cynet.com`), suffix tricks (`x@cynet.com.attacker.tld`), double-@,
    and whitespace."""
    candidate = (addr or "").strip().lower()
    pattern = r"[^@\s]+@" + re.escape(domain.lower())
    return re.fullmatch(pattern, candidate) is not None


def validate_recipient(addr: str, domain: str) -> str:
    if not (addr or "").strip():
        raise BrokerError("A test recipient email address is required.", 400)
    if not valid_recipient(addr, domain):
        raise BrokerError(f"Test recipient must be a valid @{domain} address.", 400)
    return addr.strip().lower()


def ensure_test_subject(subject: str) -> str:
    """Force the subject to begin with the [TEST] prefix."""
    text = (subject or "").strip()
    if not text:
        raise BrokerError("An email subject is required.", 400)
    if not text.startswith(SUBJECT_PREFIX):
        text = f"{SUBJECT_PREFIX} {text}"
    return text


def validate_body(body: str) -> str:
    if not (body or "").strip():
        raise BrokerError("The email body is empty.", 400)
    return body


def validate_batch_size(messages: list) -> None:
    count = len(messages or [])
    if count < 1:
        raise BrokerError("A test batch must contain at least one message.", 400)
    if count > MAX_TEST_BATCH:
        raise BrokerError(
            f"A test batch may contain at most {MAX_TEST_BATCH} messages "
            f"(got {count}).",
            400,
        )


def text_to_html(text: str) -> str:
    """Escape plain text and wrap it into simple, injection-safe HTML paragraphs.
    The escaping is done server-side so caller-supplied text can never inject
    markup into the sent email."""
    escaped = _html.escape(text or "")
    paragraphs = [p for p in escaped.split("\n\n") if p.strip()]
    if not paragraphs:
        return "<p>" + escaped.replace("\n", "<br>") + "</p>"
    return "".join("<p>" + p.replace("\n", "<br>") + "</p>" for p in paragraphs)


def _safe_id(value: str, fallback: str) -> str:
    cleaned = _ID_SAFE.sub("-", (value or "").strip()).strip("-")
    cleaned = cleaned[:60]
    return cleaned or fallback


def new_batch_id() -> str:
    """RFE-TEST-<utc-timestamp>-<random>. Matches the broker's batchId charset
    (^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$)."""
    stamp = _dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return f"RFE-TEST-{stamp}-{uuid.uuid4().hex[:6]}"


def build_test_batch(
    *,
    recipient: str,
    subject: str,
    body: str,
    domain: str,
    rfe_id: str | None = None,
    customer_id: str | None = None,
    customer_name: str | None = None,
    batch_id: str | None = None,
) -> dict:
    """Validate inputs and assemble a single-message broker batch payload.

    The sender mailbox is deliberately NOT part of the payload — it is fixed by
    the broker (product-notifications@cynet.com). CC / Reply-To are unsupported
    by the broker and are intentionally not included.
    """
    recipient = validate_recipient(recipient, domain)
    subject = ensure_test_subject(subject)
    validate_body(body)

    rfe = (rfe_id or "RFE-TEST").strip() or "RFE-TEST"
    cust_id = (customer_id or "CYNET-INTERNAL").strip() or "CYNET-INTERNAL"
    cust_name = (customer_name or "Cynet Internal").strip() or "Cynet Internal"
    batch_id = batch_id or new_batch_id()
    item_id = (_safe_id(f"{rfe}-{cust_id}", fallback="item") + "-001")[:120]

    message = {
        "itemId": item_id,
        "rfeId": rfe,
        "customerId": cust_id,
        "customerName": cust_name,
        "recipient": recipient,
        "subject": subject,
        "bodyHtml": text_to_html(body),
    }
    payload = {"batchId": batch_id, "messages": [message]}
    validate_batch_size(payload["messages"])
    return payload


# ── Response mapping / sanitizing (pure) ─────────────────────────────────────
def map_submit_response(data: dict) -> dict:
    """Reduce the broker submit response to the safe fields the frontend needs.
    `reviewUrl` is a temporary credential; it is returned once but never logged
    or persisted by this module."""
    return {
        "batchId": data.get("batchId"),
        "status": data.get("status"),
        "reviewUrl": data.get("reviewUrl"),
        "messageCount": int(data.get("messageCount") or 0),
    }


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def sanitize_status(data: dict) -> dict:
    """Normalize the broker status response. Missing counters are treated as
    zero (e.g. a Rejected batch may omit counts.Sent)."""
    counts = data.get("counts") or {}
    preview = data.get("preview") or data.get("items") or []
    items = [
        {
            "itemId": item.get("itemId"),
            "recipient": item.get("recipient"),
            "status": item.get("status"),
            "attemptCount": _as_int(item.get("attemptCount")),
            "errorCode": item.get("errorCode") or "",
        }
        for item in preview
    ]
    return {
        "batchId": data.get("batchId"),
        "status": data.get("status"),
        "messageCount": int(data.get("messageCount") or 0),
        "counts": {
            "Sent": _as_int(counts.get("Sent")),
            "Failed": _as_int(counts.get("Failed")),
            "Queued": _as_int(counts.get("Queued")),
            "Retrying": _as_int(counts.get("Retrying")),
            "Rejected": _as_int(counts.get("Rejected")),
            "DryRun": _as_int(counts.get("DryRun")),
        },
        "approvedBy": data.get("approvedBy"),
        "rejectedBy": data.get("rejectedBy"),
        "items": items,
    }


def _redact(text, cfg: BrokerConfig) -> str:
    """Strip any Function key value out of arbitrary text before it can be
    surfaced in an error message or log."""
    out = str(text)
    for secret in (cfg.submit_key, cfg.status_key):
        if secret:
            out = out.replace(secret, "***")
    return out


def _raise_for_status(status_code: int, data: dict, raw_text: str, cfg: BrokerConfig) -> None:
    """Translate a non-success broker HTTP response into a BrokerError with a
    safe, secret-free message. Auth failures deliberately do not echo the broker
    body (which could reference key/setting names)."""
    detail = _redact(data.get("message") or data.get("error") or (raw_text or "")[:200], cfg)
    if status_code == 400:
        raise BrokerError(f"The broker rejected the batch: {detail}", 400)
    if status_code in (401, 403):
        raise BrokerError(
            "The mail broker rejected the server's credentials. An administrator "
            "must verify the broker Function keys.",
            502,
        )
    if status_code == 404:
        raise BrokerError("The requested broker resource was not found.", 404)
    if status_code == 409:
        raise BrokerError(f"Broker batch conflict: {detail}", 409)
    if status_code == 429:
        raise BrokerError(
            "The mail broker is rate-limiting requests. Please try again shortly.",
            429,
        )
    if status_code >= 500:
        raise BrokerError("The mail broker reported a server error. Try again later.", 502)
    raise BrokerError(f"Unexpected broker response (HTTP {status_code}).", 502)


# ── HTTP calls ───────────────────────────────────────────────────────────────
def _parse_json(response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def submit_batch(payload: dict) -> dict:
    """POST a validated batch to the broker and return the sanitized result."""
    import httpx  # lazy import keeps module import cheap and startup resilient

    cfg = get_config()
    validate_batch_size(payload.get("messages") or [])
    url = f"{cfg.base_url}/batches"
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.post(
                url,
                headers={
                    "x-functions-key": cfg.submit_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.TimeoutException:
        raise BrokerError("The mail broker did not respond in time. Please try again.", 504)
    except httpx.HTTPError as exc:
        raise BrokerError(_redact(f"Could not reach the mail broker: {exc}", cfg), 502)

    data = _parse_json(response)
    if response.status_code in (200, 201):
        return map_submit_response(data)
    _raise_for_status(response.status_code, data, getattr(response, "text", ""), cfg)


async def get_status(batch_id: str) -> dict:
    """GET a batch status from the broker and return the sanitized result."""
    import httpx

    cfg = get_config()
    if not (batch_id or "").strip():
        raise BrokerError("A batch ID is required.", 400)
    url = f"{cfg.base_url}/batches/{quote(batch_id, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.get(url, headers={"x-functions-key": cfg.status_key})
    except httpx.TimeoutException:
        raise BrokerError("The mail broker did not respond in time. Please try again.", 504)
    except httpx.HTTPError as exc:
        raise BrokerError(_redact(f"Could not reach the mail broker: {exc}", cfg), 502)

    data = _parse_json(response)
    if response.status_code == 200:
        return sanitize_status(data)
    if response.status_code == 404:
        raise BrokerError("Test batch not found.", 404)
    _raise_for_status(response.status_code, data, getattr(response, "text", ""), cfg)
