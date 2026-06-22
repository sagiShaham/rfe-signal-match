"""Smoke test for v2 scoring pipeline (no DB, no Anthropic API required)."""
import datetime as dt
import scoring.v2_pipeline as pipeline


def test_csv_export_rfe_matches_release_notes(monkeypatch):
    monkeypatch.setattr(pipeline, "JUDGE_AVAILABLE", False)

    chunk_a = pipeline.ContextChunk(
        chunk_id="a1",
        source="release_notes",
        text="Export SSPM Users data to CSV from the SaaS tab.",
        imported_at=dt.date.today(),
    )
    chunk_b = pipeline.ContextChunk(
        chunk_id="b1",
        source="ado_current_pi",
        text="SIEM Essentials — centralized events and custom XDR rules.",
        imported_at=dt.date.today(),
        epic_state="In Progress",
    )

    rfe = pipeline.RFE(
        case_number="C-001",
        subject="Export SaaS & Cloud user list as CSV",
        description="Customer wants to download all users to a spreadsheet.",
    )

    corpus = pipeline.embed_corpus([chunk_a, chunk_b])
    result = pipeline.score_rfe(rfe, corpus)

    assert result.status == "delivered", (
        f"Expected 'delivered', got '{result.status}'. "
        f"all_scores={result.all_scores}, reason={result.reason}"
    )
    assert result.probability > 0, "Probability should be positive"
    assert result.source == "release_notes", (
        f"Expected source 'release_notes', got '{result.source}'"
    )
