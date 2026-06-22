"""
sqlite-vec vector store wrapper.

Falls back gracefully on macOS/systems where SQLite was compiled without
loadable-extension support (enable_load_extension is unavailable).
In that case SQLITE_VEC_AVAILABLE = False and all functions are no-ops or
return empty results — the pipeline embeds the corpus in-memory per run.
"""
from __future__ import annotations
import numpy as np

SQLITE_VEC_AVAILABLE: bool = False

try:
    import sqlite_vec as _sv
    import sqlite3 as _sqlite3

    def _connect(db_path: str):
        con = _sqlite3.connect(db_path)
        con.enable_load_extension(True)
        _sv.load(con)
        con.enable_load_extension(False)
        return con

    # Probe once to confirm extension loading actually works
    _probe = _connect(":memory:")
    _probe.execute("CREATE VIRTUAL TABLE _probe USING vec0(v FLOAT[1])")
    _probe.close()
    SQLITE_VEC_AVAILABLE = True

except Exception:
    pass


def init_vector_table(db_path: str, dim: int = 768) -> None:
    """Create the vec0 virtual table if sqlite-vec is available."""
    if not SQLITE_VEC_AVAILABLE:
        return
    con = _connect(db_path)
    con.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors
        USING vec0(chunk_id TEXT, embedding FLOAT[{dim}])
    """)
    con.commit()
    con.close()


def upsert_embeddings(db_path: str, chunk_ids: list, embeddings: np.ndarray) -> None:
    if not SQLITE_VEC_AVAILABLE:
        return
    con = _connect(db_path)
    for cid, vec in zip(chunk_ids, embeddings):
        con.execute(
            "INSERT OR REPLACE INTO chunk_vectors(chunk_id, embedding) VALUES (?, ?)",
            [cid, _sv.serialize_float32(vec)],
        )
    con.commit()
    con.close()


def query_top_k(db_path: str, query_vec: np.ndarray, k: int = 30) -> list:
    """Return chunk_ids of top-k nearest neighbours, or [] if unavailable."""
    if not SQLITE_VEC_AVAILABLE:
        return []
    con = _connect(db_path)
    rows = con.execute(
        """
        SELECT chunk_id, distance
        FROM chunk_vectors
        WHERE embedding MATCH ?
        AND k = ?
        ORDER BY distance
        """,
        [_sv.serialize_float32(query_vec), k],
    ).fetchall()
    con.close()
    return [r[0] for r in rows]
