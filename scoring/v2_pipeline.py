"""
RFE Signal Match — v2 scoring pipeline
======================================
Drop-in replacement for the v1 token-containment scorer.

Pipeline per RFE:
    1. EMBED   — semantic recall via bi-encoder cosine similarity
    2. RERANK  — cross-encoder reranks the shortlist
    3. JUDGE   — Claude returns a structured verdict + evidence (optional)
    4. FUSE    — combine reranker score, source weight, recency, fulfillment
"""
from __future__ import annotations
import math, datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# ── Lazy-loaded models (downloaded on first use) ─────────────────────────────

import os as _os_dev

_st_model = None
_cross_encoder = None
_DEVICE = None


def _resolve_device() -> str:
    """Pick the compute device: env RFE_DEVICE wins, else auto-detect
    CUDA (NVIDIA) → MPS (Apple Silicon GPU) → CPU."""
    global _DEVICE
    if _DEVICE is not None:
        return _DEVICE
    forced = _os_dev.environ.get("RFE_DEVICE", "").strip().lower()
    if forced in ("cpu", "cuda", "mps"):
        _DEVICE = forced
        return _DEVICE
    try:
        import torch
        if torch.cuda.is_available():
            _DEVICE = "cuda"
        elif torch.backends.mps.is_available():
            _DEVICE = "mps"
        else:
            _DEVICE = "cpu"
    except Exception:
        _DEVICE = "cpu"
    return _DEVICE


def _get_st_model():
    global _st_model
    if _st_model is None:
        from sentence_transformers import SentenceTransformer
        _st_model = SentenceTransformer('BAAI/bge-base-en-v1.5', device=_resolve_device())
    return _st_model


def _get_cross_encoder():
    global _cross_encoder
    if _cross_encoder is None:
        from sentence_transformers import CrossEncoder
        _cross_encoder = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2', device=_resolve_device())
    return _cross_encoder


def _embed(texts: list, input_type: str = "document") -> np.ndarray:
    return _get_st_model().encode(
        texts, normalize_embeddings=True, show_progress_bar=False
    ).astype(np.float32)


# ── Optional judges (Anthropic → OpenAI → Ollama, first available wins) ──────

import os as _os
import urllib.request as _urllib_req

_anthropic_client = None
_openai_client = None
_ollama_client = None
JUDGE_AVAILABLE = False
JUDGE_BACKEND = None   # "anthropic" | "openai" | "ollama" | None

try:
    import anthropic as _anthropic_lib
    _api_key = _os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if _api_key:
        _anthropic_client = _anthropic_lib.Anthropic(api_key=_api_key)
        JUDGE_AVAILABLE = True
        JUDGE_BACKEND = "anthropic"
except Exception:
    pass

if not JUDGE_AVAILABLE:
    try:
        import openai as _openai_lib
        _oai_key = _os.environ.get("OPENAI_API_KEY", "").strip()
        if _oai_key:
            _openai_client = _openai_lib.OpenAI(api_key=_oai_key)
            JUDGE_AVAILABLE = True
            JUDGE_BACKEND = "openai"
    except Exception:
        pass

# Always try to initialize Ollama — it's a fallback even when OpenAI is primary
try:
    import openai as _openai_lib
    _ollama_base = _os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    _urllib_req.urlopen(f"{_ollama_base}/api/tags", timeout=2)  # noqa: S310
    _ollama_client = _openai_lib.OpenAI(
        api_key="ollama",
        base_url=f"{_ollama_base}/v1",
    )
    if not JUDGE_AVAILABLE:
        JUDGE_AVAILABLE = True
        JUDGE_BACKEND = "ollama"
except Exception:
    pass

OPENAI_JUDGE_MODEL = "gpt-4o-mini"
OLLAMA_JUDGE_MODEL = _os.environ.get("OLLAMA_MODEL", "llama3.2")

# The LLM judge is gated behind an explicit opt-in flag and ships OFF by default.
# Local Ollama (llama3.2 3B) rejected ~all RFEs and ran ~20s/RFE in testing, so
# the default pipeline is rerank-only (fast, ~1-2s/RFE, produces usable matches).
# Set RFE_JUDGE_ENABLED=1 once a capable judge (GPT-4o-mini / Claude) is wired up.
_JUDGE_ENABLED = _os.environ.get("RFE_JUDGE_ENABLED", "0").strip().lower() in ("1", "true", "yes")
if not _JUDGE_ENABLED:
    JUDGE_AVAILABLE = False
    JUDGE_BACKEND = None

# ── Constants ─────────────────────────────────────────────────────────────────

SOURCE_WEIGHTS: dict = {
    "release_notes":      1.0,
    "ado_current_pi":     0.9,
    "ado_upcoming_pi":    0.6,
    "confluence_dated":   0.5,
    "roadmap":            0.4,
    "confluence_undated": 0.3,
}

SOURCE_TO_STATUS: dict = {
    "release_notes":      "delivered",
    "ado_current_pi":     "in_current_pi",
    "ado_upcoming_pi":    "planned",
    "roadmap":            "planned",
    "confluence_dated":   "planned",
    "confluence_undated": "planned",
}

STATUS_THRESHOLDS: dict = {
    "delivered":     0.65,
    "in_current_pi": 0.50,
    "planned":       0.35,
}

CONFIDENCE_BANDS   = {"high": 0.75, "medium": 0.50}
RECENCY_DECAY      = 0.20
DECAY_AFTER_MONTHS = 12
# TOP_K_RECALL = how many bi-encoder candidates the cross-encoder reranks per RFE.
# The cross-encoder is ~90% of run time and scales with this number, so it is the
# main speed lever. BGE cosine reliably puts the right evidence near the top, so a
# smaller K is safe. Tunable via RFE_TOP_K_RECALL (default 12 → full run < 5 min).
TOP_K_RECALL       = int(_os.environ.get("RFE_TOP_K_RECALL", "12"))
TOP_N_RERANK       = 5

# ── Stage C: LLM cascade gate ────────────────────────────────────────────────
# The LLM judge is the most expensive stage, so it runs ONLY on a distilled
# shortlist: RFEs whose top cross-encoder rerank score (normalised 0-1) clears
# JUDGE_GATE, sorted best-first and hard-capped at JUDGE_MAX_SHORTLIST. The cap
# bounds LLM cost no matter how many RFEs arrive — this is what keeps a full run
# inside the time budget. Everything below the gate gets the fast rerank-only
# verdict. Both tunable via env.
JUDGE_GATE          = float(_os.environ.get("RFE_JUDGE_GATE", "0.45"))
JUDGE_MAX_SHORTLIST = int(_os.environ.get("RFE_JUDGE_MAX", "150"))
JUDGE_MODEL        = "claude-sonnet-4-6"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ContextChunk:
    chunk_id: str
    source: str
    text: str
    imported_at: dt.date
    epic_id: Optional[str] = None
    epic_state: Optional[str] = None
    domain: Optional[str] = None
    embedding: Optional[np.ndarray] = None


@dataclass
class RFE:
    case_number: str
    subject: str
    description: str
    domain: Optional[str] = None
    sub_domain: Optional[str] = None


@dataclass
class Candidate:
    chunk: ContextChunk
    embed_score: float = 0.0
    rerank_score: float = 0.0


@dataclass
class MatchResult:
    case_number: str
    status: str
    confidence: str
    probability: float
    source: Optional[str]
    evidence: str = ""
    epic_id: Optional[str] = None
    reason: str = ""
    secondary_status: Optional[str] = None
    all_scores: dict = field(default_factory=dict)


# ── Judge prompt ──────────────────────────────────────────────────────────────

JUDGE_SYSTEM = (
    "You are a product-management analyst for Cynet. You decide whether a "
    "customer feature request (RFE) has been DELIVERED, is IN the current "
    "development cycle, is PLANNED, or has NO MATCH, using ONLY the candidate "
    "evidence snippets provided.\n\n"
    "Rules:\n"
    '- "delivered"     -> a release-notes snippet describes the SHIPPED capability the RFE asks for.\n'
    '- "in_current_pi" -> an ADO Current-PI epic covers it AND its epic_state is not done/closed.\n'
    '- "planned"       -> only roadmap / upcoming-PI / backlog / confluence evidence covers it.\n'
    '- "no_match"      -> no snippet actually fulfils the request.\n'
    "- Judge MEANING, not vocabulary. Paraphrases count; shared keywords on unrelated features do NOT.\n"
    '- Distinguish "we shipped exactly this" from "we shipped something adjacent / a different variant".\n'
    "  If only part of the request is met, set status to the met part and secondary_status to the rest.\n"
    "- Ground every verdict in a short verbatim quote from one snippet. If you cannot quote, it is no_match.\n"
    "- Prefer the strongest source (release_notes > ado_current_pi > ado_upcoming_pi > roadmap > confluence)."
)

JUDGE_TOOL = {
    "name": "record_match",
    "description": "Record the lifecycle match verdict for one RFE.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status":           {"type": "string",
                                 "enum": ["delivered", "in_current_pi", "planned", "no_match"]},
            "secondary_status": {"type": ["string", "null"],
                                 "enum": ["delivered", "in_current_pi", "planned", "no_match", None]},
            "source":           {"type": ["string", "null"],
                                 "enum": list(SOURCE_WEIGHTS.keys()) + [None]},
            "epic_id":          {"type": ["string", "null"]},
            "evidence_quote":   {"type": "string",
                                 "description": "verbatim snippet text the verdict rests on"},
            "fulfillment":      {"type": "number", "minimum": 0, "maximum": 1,
                                 "description": "how completely the evidence meets the RFE (0-1)"},
            "reason":           {"type": "string", "description": "one sentence, plain English"},
        },
        "required": ["status", "source", "evidence_quote", "fulfillment", "reason"],
    },
}


# ── Core functions ─────────────────────────────────────────────────────────────

def _normalise_rerank(score: float) -> float:
    return 1.0 / (1.0 + math.exp(-score))


def _recency_factor(imported_at: dt.date) -> float:
    months = (dt.date.today() - imported_at).days / 30.0
    return (1 - RECENCY_DECAY) if months > DECAY_AFTER_MONTHS else 1.0


def _candidate_block(cands: list) -> str:
    lines = []
    for i, c in enumerate(cands, 1):
        meta = f"source={c.chunk.source}"
        if c.chunk.epic_id:
            meta += f" epic_id={c.chunk.epic_id} epic_state={c.chunk.epic_state}"
        if c.chunk.imported_at:
            meta += f" date={c.chunk.imported_at:%Y-%m}"
        lines.append(f"[{i}] ({meta})\n{c.chunk.text}")
    return "\n\n".join(lines)


def _rerank_only_verdict(reranked: list) -> dict:
    if not reranked:
        return {"status": "no_match", "source": None,
                "evidence_quote": "", "fulfillment": 0.0,
                "reason": "No candidates above recall threshold."}
    top = reranked[0]
    norm = _normalise_rerank(top.rerank_score)
    status = SOURCE_TO_STATUS.get(top.chunk.source, "no_match")
    return {
        "status": status if norm >= 0.5 else "no_match",
        "source": top.chunk.source,
        "evidence_quote": top.chunk.text[:200],
        "fulfillment": norm,
        "reason": f"Rerank-only fallback (no judge). Top score: {norm:.2f}",
    }


def judge(rfe: RFE, cands: list) -> dict:
    global JUDGE_AVAILABLE, JUDGE_BACKEND
    if not cands:
        return {"status": "no_match", "source": None, "evidence_quote": "",
                "fulfillment": 0.0, "reason": "No candidate evidence above recall threshold."}
    if JUDGE_BACKEND == "anthropic":
        try:
            return _judge_anthropic(rfe, cands)
        except Exception as _e:
            if any(k in str(_e).lower() for k in ("quota", "401", "403", "invalid_api_key")):
                JUDGE_BACKEND = "openai" if _openai_client else ("ollama" if _ollama_client else None)
                JUDGE_AVAILABLE = bool(JUDGE_BACKEND)
    if JUDGE_BACKEND == "openai":
        try:
            return _judge_openai(rfe, cands)
        except Exception as _e:
            if any(k in str(_e) for k in ("quota", "429", "401", "insufficient")):
                JUDGE_BACKEND = "ollama" if _ollama_client else None
                JUDGE_AVAILABLE = bool(JUDGE_BACKEND)
    if JUDGE_BACKEND == "ollama":
        try:
            return _judge_ollama(rfe, cands)
        except Exception:
            JUDGE_BACKEND = None
            JUDGE_AVAILABLE = False
    return _rerank_only_verdict(cands)


def _judge_anthropic(rfe: RFE, cands: list) -> dict:
    user = (
        f"RFE {rfe.case_number} (domain={rfe.domain}, sub_domain={rfe.sub_domain})\n"
        f"Subject: {rfe.subject}\nDescription: {rfe.description}\n\n"
        f"Candidate evidence:\n{_candidate_block(cands)}"
    )
    msg = _anthropic_client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=600,
        system=[{"type": "text", "text": JUDGE_SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        tools=[JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "record_match"},
        messages=[{"role": "user", "content": user}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input
    return {"status": "no_match", "source": None, "evidence_quote": "",
            "fulfillment": 0.0, "reason": "Judge returned no structured verdict."}


def _judge_openai(rfe: RFE, cands: list) -> dict:
    import json as _json
    user = (
        f"RFE {rfe.case_number} (domain={rfe.domain}, sub_domain={rfe.sub_domain})\n"
        f"Subject: {rfe.subject}\nDescription: {rfe.description}\n\n"
        f"Candidate evidence:\n{_candidate_block(cands)}"
    )
    tool = {
        "type": "function",
        "function": {
            "name": JUDGE_TOOL["name"],
            "description": JUDGE_TOOL["description"],
            "parameters": JUDGE_TOOL["input_schema"],
        },
    }
    resp = _openai_client.chat.completions.create(
        model=OPENAI_JUDGE_MODEL,
        max_tokens=600,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user",   "content": user},
        ],
        tools=[tool],
        tool_choice={"type": "function", "function": {"name": "record_match"}},
    )
    for choice in resp.choices:
        calls = getattr(choice.message, "tool_calls", None) or []
        for call in calls:
            try:
                return _json.loads(call.function.arguments)
            except Exception:
                pass
    return {"status": "no_match", "source": None, "evidence_quote": "",
            "fulfillment": 0.0, "reason": "Judge returned no structured verdict."}


def _judge_ollama(rfe: RFE, cands: list) -> dict:
    import json as _json
    user = (
        f"RFE {rfe.case_number} (domain={rfe.domain}, sub_domain={rfe.sub_domain})\n"
        f"Subject: {rfe.subject}\nDescription: {rfe.description}\n\n"
        f"Candidate evidence:\n{_candidate_block(cands)}\n\n"
        "Respond with a JSON object (no markdown) matching this schema exactly:\n"
        '{"status": "delivered"|"in_current_pi"|"planned"|"no_match", '
        '"secondary_status": null|"...", "source": "<source_key>"|null, '
        '"epic_id": null|"...", "evidence_quote": "...", '
        '"fulfillment": 0.0-1.0, "reason": "one sentence"}'
    )
    resp = _ollama_client.chat.completions.create(
        model=OLLAMA_JUDGE_MODEL,
        max_tokens=600,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user",   "content": user},
        ],
        temperature=0,
    )
    raw = resp.choices[0].message.content.strip() if resp.choices else ""
    # strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return _json.loads(raw)
    except Exception:
        return {"status": "no_match", "source": None, "evidence_quote": "",
                "fulfillment": 0.0, "reason": f"Ollama returned unparseable output: {raw[:120]}"}


def rerank(rfe: RFE, cands: list, n: int = TOP_N_RERANK) -> list:
    if not cands:
        return []
    query = f"{rfe.subject}. {rfe.description}"
    pairs = [(query, c.chunk.text) for c in cands]
    scores = _get_cross_encoder().predict(pairs)
    for c, s in zip(cands, scores):
        c.rerank_score = float(s)
    ranked = sorted(cands, key=lambda c: c.rerank_score, reverse=True)
    return ranked[:n]


def fuse(rfe: RFE, reranked: list, verdict: dict) -> MatchResult:
    if verdict["status"] == "no_match" or not reranked:
        return MatchResult(rfe.case_number, "no_match", "low", 0.0, None,
                           reason=verdict.get("reason", ""))
    chosen = next(
        (c for c in reranked if c.chunk.source == verdict.get("source")),
        reranked[0]
    )
    status = verdict["status"]
    if (chosen.chunk.epic_state
            and chosen.chunk.epic_state.lower() in ("done", "closed", "released")):
        status = "delivered"

    score = (
        _normalise_rerank(chosen.rerank_score)
        * SOURCE_WEIGHTS.get(chosen.chunk.source, 0.3)
        * _recency_factor(chosen.chunk.imported_at)
        * float(verdict["fulfillment"])
    )

    if score < STATUS_THRESHOLDS.get(status, 1.0):
        return MatchResult(
            rfe.case_number, "no_match", "low", round(score, 3),
            chosen.chunk.source,
            reason="Below status threshold after fusion.",
            all_scores={"fused": round(score, 3)},
        )

    conf = (
        "high"   if score >= CONFIDENCE_BANDS["high"]   else
        "medium" if score >= CONFIDENCE_BANDS["medium"] else
        "low"
    )
    return MatchResult(
        case_number=rfe.case_number,
        status=status,
        confidence=conf,
        probability=round(score, 3),
        source=chosen.chunk.source,
        evidence=verdict.get("evidence_quote", ""),
        epic_id=verdict.get("epic_id") or chosen.chunk.epic_id,
        reason=verdict.get("reason", ""),
        secondary_status=verdict.get("secondary_status"),
        all_scores={
            "embed":  round(chosen.embed_score, 3),
            "rerank": round(chosen.rerank_score, 3),
            "fused":  round(score, 3),
        },
    )


def embed_corpus(chunks: list) -> list:
    vecs = _embed([c.text for c in chunks], input_type="document")
    for c, v in zip(chunks, vecs):
        c.embedding = v
    return chunks


def recall_candidates(rfe: RFE, corpus: list, k: int = TOP_K_RECALL) -> list:
    q = _embed([f"{rfe.subject}. {rfe.description}"], input_type="query")[0]
    mat = np.vstack([c.embedding for c in corpus])
    sims = mat @ q
    idx = np.argsort(-sims)[:k]
    cands = [Candidate(chunk=corpus[i], embed_score=float(sims[i])) for i in idx]
    if rfe.domain:
        filtered = [c for c in cands if c.chunk.domain in (None, rfe.domain)]
        if filtered:
            cands = filtered
    return cands


def score_rfe(rfe: RFE, corpus: list) -> MatchResult:
    cands    = recall_candidates(rfe, corpus)
    reranked = rerank(rfe, cands)
    verdict  = judge(rfe, reranked) if JUDGE_AVAILABLE else _rerank_only_verdict(reranked)
    return fuse(rfe, reranked, verdict)


def score_rfes_cascade(rfes: list, corpus: list, progress=None) -> list:
    """Funnel scorer for a whole batch (see Stage C constants above).

    Pass 1 — recall + cross-encoder rerank for EVERY RFE (the vector stage).
    Stage C — pick RFEs whose top rerank score >= JUDGE_GATE, best-first,
              capped at JUDGE_MAX_SHORTLIST → the LLM shortlist.
    Pass 2 — the LLM judge runs ONLY on that shortlist; everyone else keeps the
             fast rerank-only verdict.

    Degrades to plain rerank-only for all RFEs when no judge is available, so it
    is a safe drop-in regardless of RFE_JUDGE_ENABLED. progress(stage, done,
    total) is an optional callback ("rerank" | "judge").
    """
    n = len(rfes)
    stage1 = []   # (idx, reranked_candidates, normalised_top_score)
    for i, rfe in enumerate(rfes):
        reranked = rerank(rfe, recall_candidates(rfe, corpus)) if corpus else []
        norm = _normalise_rerank(reranked[0].rerank_score) if reranked else 0.0
        stage1.append((i, reranked, norm))
        if progress:
            progress("rerank", i + 1, n)

    shortlist = set()
    if JUDGE_AVAILABLE:
        qualified = sorted((s for s in stage1 if s[2] >= JUDGE_GATE),
                           key=lambda s: -s[2])[:JUDGE_MAX_SHORTLIST]
        shortlist = {s[0] for s in qualified}

    results = [None] * n
    judged = 0
    for i, reranked, _norm in stage1:
        if i in shortlist:
            verdict = judge(rfes[i], reranked)
            judged += 1
            if progress:
                progress("judge", judged, len(shortlist))
        else:
            verdict = _rerank_only_verdict(reranked)
        results[i] = fuse(rfes[i], reranked, verdict)
    return results


def run_pipeline(rfes: list, corpus: list) -> list:
    if corpus and corpus[0].embedding is None:
        embed_corpus(corpus)
    return [score_rfe(r, corpus) for r in rfes]


def load_config_from_db(db_path: str) -> None:
    """Read scoring weights and thresholds from app_config, mapping v1 key names to v2 dicts."""
    import sqlite3
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute("SELECT key, value FROM app_config").fetchall()
        con.close()
    except Exception:
        return

    cfg = {k: v for k, v in rows}

    weight_map = {
        "weight_release_notes":      "release_notes",
        "weight_ado_current_pi":     "ado_current_pi",
        "weight_ado_backlog":        "ado_upcoming_pi",
        "weight_confluence_dated":   "confluence_dated",
        "weight_roadmap":            "roadmap",
        "weight_confluence_undated": "confluence_undated",
    }
    for cfg_key, src_key in weight_map.items():
        if cfg_key in cfg:
            try:
                SOURCE_WEIGHTS[src_key] = float(cfg[cfg_key])
            except (ValueError, TypeError):
                pass

    thresh_map = {
        "match_thresh_delivered": "delivered",
        "match_thresh_in_pi":     "in_current_pi",
        "match_thresh_planned":   "planned",
    }
    for cfg_key, status_key in thresh_map.items():
        if cfg_key in cfg:
            try:
                STATUS_THRESHOLDS[status_key] = float(cfg[cfg_key])
            except (ValueError, TypeError):
                pass
