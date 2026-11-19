"""Code embeddings, LanceDB vector store, and fast vectorized Type-3/4 semantic clone detection."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

import duckdb
import httpx
import lancedb
import numpy as np

from kaizen_kit.radar.capsule_schema import SemanticClonePair

EMBED_DIM = 768
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|==|!=|<=|>=|->|::|[{}()\[\];,.+\-*/<>]")


DEFAULT_TEI_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"


def _align_dim(vec: list[float] | np.ndarray, dim: int = EMBED_DIM) -> np.ndarray:
    """Matryoshka dimension alignment (truncate >dim or zero-pad <dim) to preserve 768-dim LanceDB schema."""
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    if arr.shape[0] == dim:
        return arr
    if arr.shape[0] > dim:
        return arr[:dim]
    padded = np.zeros(dim, dtype=np.float32)
    padded[: arr.shape[0]] = arr
    return padded


def _l2_normalize(vec: list[float] | np.ndarray, dim: int | None = None) -> list[float]:
    arr = _align_dim(vec, dim) if dim is not None else np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm > 1e-12:
        arr = arr / norm
    return arr.tolist()


def structural_hash_embedding(code: str, dim: int = EMBED_DIM) -> list[float]:
    """
    Deterministic offline 768-dim feature-hashed embedding over normalized AST/code token 1..3-grams.
    Allows zero-GPU local runs and Type-1/2/3 clone detection while preserving 768-dim LanceDB schema.
    """
    tokens = _TOKEN_RE.findall(code)
    vec = np.zeros(dim, dtype=np.float32)
    if not tokens:
        vec[0] = 1.0
        return vec.tolist()

    # Normalize identifiers lightly while retaining keywords/operators/structure
    keywords = {
        "if", "else", "for", "while", "loop", "match", "switch", "case", "return",
        "fn", "pub", "impl", "struct", "enum", "trait", "async", "await",
        "public", "private", "class", "void", "try", "catch", "throw", "new",
        "func", "defer", "go", "select", "chan", "package", "err", "nil", "Ok", "Err",
    }
    norm_tokens = [t if t in keywords or not t[0].isalpha() else f"id:{t.lower()}" for t in tokens]

    for n in (1, 2, 3):
        for i in range(max(0, len(norm_tokens) - n + 1)):
            gram = " ".join(norm_tokens[i : i + n])
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            val = int.from_bytes(digest, "little")
            idx = val % dim
            sign = 1.0 if (val >> 63) == 0 else -1.0
            vec[idx] += sign * (1.0 if n == 1 else 1.4)

    return _l2_normalize(vec)


def _try_tei_embed(texts: list[str], embed_url: str) -> list[list[float]] | None:
    batch_size = 32
    vectors: list[list[float]] = []
    try:
        with httpx.Client(timeout=60.0) as client:
            for i in range(0, len(texts), batch_size):
                batch = [t[:6000] for t in texts[i : i + batch_size]]
                resp = client.post(embed_url, json={"inputs": batch, "truncate": True})
                resp.raise_for_status()
                raw_vecs = resp.json()
                if not isinstance(raw_vecs, list) or len(raw_vecs) != len(batch):
                    return None
                for v in raw_vecs:
                    vectors.append(_l2_normalize(v, dim=EMBED_DIM))
        return vectors
    except Exception:
        return None


def compute_embeddings(
    texts: list[str],
    embed_url: str | None = None,
    backend: str = "auto",
) -> tuple[list[list[float]], str]:
    url = embed_url or os.getenv("EMBED_API_URL", "http://127.0.0.1:8081/embed")
    model_id = os.getenv("TEI_MODEL_ID", DEFAULT_TEI_MODEL_ID)
    if backend in ("auto", "tei") and texts:
        tei_vecs = _try_tei_embed(texts, url)
        if tei_vecs is not None:
            return tei_vecs, f"tei:{model_id}"
        if backend == "tei":
            raise RuntimeError(f"TEI embedding endpoint unreachable: {url}")
    return [structural_hash_embedding(t) for t in texts], "offline:structural-768d"


def find_cosine_clones(
    records: list[dict[str, Any]],
    threshold: float = 0.88,
    cross_service_only: bool = False,
) -> list[SemanticClonePair]:
    if len(records) < 2:
        return []
    mat = np.asarray([r["vector"] for r in records], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms < 1e-12] = 1.0
    mat = mat / norms
    sim_matrix = mat @ mat.T

    clones: list[SemanticClonePair] = []
    n = len(records)
    for i in range(n):
        row_sims = sim_matrix[i]
        for j in range(i + 1, n):
            sim = float(row_sims[j])
            if math.isnan(sim) or sim < threshold:
                continue
            ra, rb = records[i], records[j]
            if cross_service_only and ra["service_name"] == rb["service_name"]:
                continue
            clones.append(
                SemanticClonePair(
                    a_id=ra["symbol_id"],
                    a_name=f"{ra['file_path']}:{ra['name']}",
                    a_service=ra["service_name"],
                    a_lang=ra["language"],
                    b_id=rb["symbol_id"],
                    b_name=f"{rb['file_path']}:{rb['name']}",
                    b_service=rb["service_name"],
                    b_lang=rb["language"],
                    similarity=round(min(1.0, sim), 4),
                )
            )
    clones.sort(key=lambda c: c.similarity, reverse=True)
    return clones


def embed_and_store_lancedb(
    capsule_dir: Path,
    embed_url: str | None = None,
    backend: str = "auto",
    min_body_len: int = 80,
    clone_threshold: float = 0.88,
) -> tuple[list[SemanticClonePair], str]:
    nodes_pq = capsule_dir / "nodes.parquet"
    if not nodes_pq.is_file():
        return [], "offline:structural-768d"

    con = duckdb.connect()
    rows = con.execute(
        """
        SELECT symbol_id, service_name, language, name, file_path, start_line, clean_body
        FROM read_parquet(?)
        WHERE length(clean_body) >= ?
        """,
        [str(nodes_pq), min_body_len],
    ).fetchall()

    if not rows:
        (capsule_dir / "semantic_clones.json").write_text("[]", encoding="utf-8")
        return [], "offline:structural-768d"

    vectors, used_backend = compute_embeddings(
        [r[6] for r in rows], embed_url=embed_url, backend=backend
    )
    records = [
        {
            "symbol_id": r[0],
            "service_name": r[1],
            "language": r[2],
            "name": r[3],
            "file_path": r[4],
            "start_line": int(r[5]),
            "clean_body": r[6],
            "vector": vec,
        }
        for r, vec in zip(rows, vectors)
    ]

    db = lancedb.connect(str(capsule_dir / "vectors.lance"))
    db.create_table("functions", data=records, mode="overwrite")

    clones = find_cosine_clones(records, threshold=clone_threshold, cross_service_only=False)
    (capsule_dir / "semantic_clones.json").write_text(
        json.dumps([c.model_dump() for c in clones], indent=2),
        encoding="utf-8",
    )
    return clones, used_backend

