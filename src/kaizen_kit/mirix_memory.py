#!/usr/bin/env python3
"""Kaizen-Native MIRIX-2.0 Lite + Async AutoDream (Option A: SQLite FTS5 + LanceDB + LiteLLM)."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

_LOG = logging.getLogger("kaizen_kit.mirix_memory")
SCREEN_CAPTURE_ENABLED = False
MEMORY_TYPES = ("core", "episodic", "semantic", "procedural", "resource", "vault")
EMBED_DIM = 768
DEFAULT_EMBED_URL = os.environ.get("KAIZEN_EMBED_URL", "http://127.0.0.1:8766")
DEFAULT_LITELLM_URL = os.environ.get("KAIZEN_LITELLM_URL", "http://127.0.0.1:4000")
DEFAULT_AUTODREAM_MODEL = os.environ.get("KAIZEN_AUTODREAM_MODEL", "qwen3-30b-a3b")

try:
    from kaizen_kit.gates.deterministic_vault import DeterministicVault, check_for_secrets
    from kaizen_kit.paths import resolve_state_dir
except ImportError:
    _MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})\b")
    _IPV4_RE = re.compile(r"\b((?!127\.0\.0\.1|0\.0\.0\.0|255\.255\.255\.255)(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?))\b")
    _EXEMPT_RE = re.compile(r"(?i)\b(?:u2|uca|kaizen-u2|kaizen-staging|u2_all_parity|u2-parity|telemetry_dashboard-uca|staging)\b")
    _SEC_PATS = (
        ("AWS_ACCESS_KEY", re.compile(r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b")),
        ("AWS_SECRET_KEY", re.compile(r"(?i)(?:aws_secret_access_key|secret_key|secretaccesskey)[\s:='\"]+([0-9a-zA-Z/+]{40})")),
        ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
        ("JWT_TOKEN", re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b")),
    )

    def resolve_state_dir() -> Path:
        env = os.environ.get("AI_KIT_STATE_DIR", "").strip()
        p = Path(env).expanduser().resolve() if env else (Path.home() / ".kaizen-kit")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def check_for_secrets(text: str) -> Optional[Tuple[str, str]]:
        return next(((k, "[REDACTED]") for k, pat in _SEC_PATS if text and pat.search(text)), None)

    class DeterministicVault:  # type: ignore[no-redef]
        def __init__(self, vault_path: Optional[Path] = None) -> None:
            self.vault_path = vault_path or (resolve_state_dir() / "vault" / "anonymizer_vault.json")
            self._lock, self._fwd, self._cnt = threading.Lock(), {"mac": {}, "ipv4": {}}, {"mac": 0, "ipv4": 0}
            if self.vault_path.is_file():
                try:
                    d = json.loads(self.vault_path.read_text(encoding="utf-8"))
                    self._fwd.update(d.get("forward", {}))
                    self._cnt.update(d.get("counters", {}))
                except Exception:
                    pass

        def anonymize_text(self, text: str, scope: str = "") -> str:
            if not text or _EXEMPT_RE.search(scope) or _EXEMPT_RE.search(text):
                return text
            def _sub(kind: str, raw: str, fmt: Any) -> str:
                with self._lock:
                    if raw not in self._fwd[kind]:
                        self._cnt[kind] += 1
                        self._fwd[kind][raw] = fmt(self._cnt[kind])
                    return self._fwd[kind][raw]
            t = _MAC_RE.sub(lambda m: m.group(1) if m.group(1).upper() in ("FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00") else _sub("mac", m.group(1).upper().replace("-", ":"), lambda i: f"00:50:C2:{(i>>16)&0xFF:02X}:{(i>>8)&0xFF:02X}:{i&0xFF:02X}"), text)
            return _IPV4_RE.sub(lambda m: m.group(1) if m.group(1).startswith("127.") or m.group(1) == "0.0.0.0" else _sub("ipv4", m.group(1), lambda i: f"198.51.100.{(i%250)+1}"), t)


_STORE_LOCK = threading.RLock()
_STORES: Dict[str, "MirixStore"] = {}
_PROV_FIELDS = ("olay", "bilesen", "kok_neden", "cozum", "jira_veya_mr", "dosya_yolu", "satir_araligi", "tarih", "raw_source_ptr", "raw_source_type")
_SCHEMA_SQL = """
PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, mem_type TEXT NOT NULL CHECK(mem_type IN ('core','episodic','semantic','procedural','resource','vault')),
    summary TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '', source_ref TEXT NOT NULL DEFAULT '',
    anchor_id TEXT NOT NULL DEFAULT '', consolidated INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, token_count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS provenance_hypergraph (
    id TEXT PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    olay TEXT NOT NULL DEFAULT '', bilesen TEXT NOT NULL DEFAULT '', kok_neden TEXT NOT NULL DEFAULT '', cozum TEXT NOT NULL DEFAULT '',
    jira_veya_mr TEXT NOT NULL DEFAULT '', dosya_yolu TEXT NOT NULL DEFAULT '', satir_araligi TEXT NOT NULL DEFAULT '',
    tarih TEXT NOT NULL DEFAULT '', raw_source_ptr TEXT NOT NULL DEFAULT '', raw_source_type TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(mem_type, consolidated);
CREATE INDEX IF NOT EXISTS idx_prov_mem ON provenance_hypergraph(memory_id);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(id UNINDEXED, mem_type, summary, content, tags, tokenize='porter unicode61');
"""

def default_mirix_dir() -> Path:
    env = os.environ.get("KAIZEN_MIRIX_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    ec2_dir = Path("/opt/kaizen-mini-pool")
    return (ec2_dir / "mirix_data") if ec2_dir.is_dir() else (resolve_state_dir() / "mirix_data")

def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0

def _pack_cost(mtype: str, summary: str, content: str, provs: Sequence[Dict[str, Any]]) -> int:
    base = f"[{mtype}] {summary}: {content}"
    return estimate_tokens(f"{base} {json.dumps(provs, ensure_ascii=False)}" if provs else base)

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _process_rss_mb() -> Optional[float]:
    try:
        import psutil  # type: ignore[import-not-found]
        return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 2)
    except ImportError:
        proc_status = Path("/proc/self/status")
        if proc_status.is_file():
            for line in proc_status.read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.split()
                if line.startswith("VmRSS:") and len(parts) >= 2 and parts[1].isdigit():
                    return round(int(parts[1]) / 1024.0, 2)
        return None

def _normalize_provenance(raw: Optional[Dict[str, Any]], source_ref: str, default_date: str) -> Dict[str, str]:
    p = raw if isinstance(raw, dict) else {}
    return {
        "olay": str(p.get("olay") or ""), "bilesen": str(p.get("bilesen") or p.get("bileşen") or ""),
        "kok_neden": str(p.get("kok_neden") or p.get("kök_neden") or ""), "cozum": str(p.get("cozum") or p.get("çözüm") or ""),
        "jira_veya_mr": str(p.get("jira_veya_mr") or ""), "dosya_yolu": str(p.get("dosya_yolu") or ""),
        "satir_araligi": str(p.get("satir_araligi") or p.get("satır_aralığı") or ""), "tarih": str(p.get("tarih") or default_date),
        "raw_source_ptr": str(p.get("raw_source_ptr") or source_ref or ""), "raw_source_type": str(p.get("raw_source_type") or ""),
    }

def _scrub_secret_text(text: str) -> str:
    found = check_for_secrets(text) if text else None
    return f"[REDACTED_SECRET:{found[0]}]" if found else text

def _validate_types(types: Optional[Sequence[str]]) -> Optional[Tuple[str, ...]]:
    if not types:
        return None
    cleaned = tuple(str(t).strip().lower() for t in types if str(t).strip())
    bad = [t for t in cleaned if t not in MEMORY_TYPES]
    if bad:
        raise ValueError(f"Unsupported memory type(s): {bad}; allowed={MEMORY_TYPES}")
    return cleaned or None

class MirixStore:
    """Thread-safe Option-A MIRIX-2.0 Lite store (SQLite FTS5 + LanceDB + nomic-embed)."""

    def __init__(self, data_dir: Optional[Path] = None, embed_url: Optional[str] = None, litellm_url: Optional[str] = None) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve() if data_dir else default_mirix_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path, self.lance_dir = self.data_dir / "mirix.sqlite", self.data_dir / "lancedb"
        self.embed_url = (DEFAULT_EMBED_URL if embed_url is None else embed_url).rstrip("/")
        self.litellm_url = (DEFAULT_LITELLM_URL if litellm_url is None else litellm_url).rstrip("/")
        self.vault, self._lock = DeterministicVault(self.data_dir / "mirix_vault.json"), threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA_SQL)
        self._http, self._embed_cache, self._lance_tbl, self._warmed = httpx.Client(timeout=1.2), OrderedDict(), None, False

    def close(self) -> None:
        with self._lock:
            self._lance_tbl = None
            for obj in (self._conn, self._http):
                try:
                    obj.close()
                except Exception:
                    pass

    def _ensure_lance(self) -> Any:
        if self._lance_tbl is None:
            import lancedb
            import pyarrow as pa
            schema = pa.schema([pa.field("id", pa.string()), pa.field("mem_type", pa.string()), pa.field("summary", pa.string()), pa.field("updated_at", pa.string()), pa.field("vector", pa.list_(pa.float32(), EMBED_DIM))])
            self._lance_tbl = lancedb.connect(str(self.lance_dir)).create_table("kaizen_memory", schema=schema, exist_ok=True)
        return self._lance_tbl

    def warmup(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        with self._lock:
            self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            self._ensure_lance().search([0.0] * EMBED_DIM).limit(1).to_list()
            self._warmed = True
        return {"ok": True, "warmup_ms": round((time.perf_counter() - t0) * 1000.0, 2), "screen_capture": SCREEN_CAPTURE_ENABLED}

    def embed_text(self, text: str) -> Optional[List[float]]:
        if not self.embed_url or not text.strip():
            return None
        key = hashlib.sha1(text[:2000].encode("utf-8", errors="replace")).hexdigest()
        with self._lock:
            cached = self._embed_cache.get(key)
            if cached is not None:
                self._embed_cache.move_to_end(key)
                return cached
        try:
            resp = self._http.post(f"{self.embed_url}/v1/embeddings", json={"input": text[:2000], "model": "nomic-embed"})
            vec = resp.json()["data"][0]["embedding"] if resp.status_code == 200 else None
            if not isinstance(vec, list) or len(vec) != EMBED_DIM:
                return None
            out = [float(x) for x in vec]
        except Exception:
            return None
        with self._lock:
            self._embed_cache[key] = out
            if len(self._embed_cache) > 256:
                self._embed_cache.popitem(last=False)
        return out

    def ingest(self, items: Sequence[Dict[str, Any]], scope: str = "") -> Dict[str, Any]:
        ingested_ids, rejected, lance_rows, now = [], [], [], _now_iso()
        with self._lock:
            cur = self._conn.cursor()
            for idx, item in enumerate(items):
                mem_type = str(item.get("mem_type") or item.get("type") or "episodic").strip().lower()
                if mem_type not in MEMORY_TYPES:
                    rejected.append({"index": str(idx), "reason": f"INVALID_TYPE:{mem_type}"})
                    continue
                summary, content = str(item.get("summary") or "").strip(), str(item.get("content") or item.get("summary") or "").strip()
                if not content:
                    rejected.append({"index": str(idx), "reason": "EMPTY_CONTENT"})
                    continue
                tags, source_ref = str(item.get("tags") or ""), str(item.get("source_ref") or "")
                prov = _normalize_provenance(item.get("provenance"), source_ref, now[:10])
                secret_hit = check_for_secrets("\n".join([summary, content, tags, source_ref, *prov.values()]))
                if secret_hit:
                    rejected.append({"index": str(idx), "reason": f"SECRET_DETECTED:{secret_hit[0]}"})
                    continue
                sc = str(item.get("scope") or scope or "")
                summary, content, tags, source_ref = self.vault.anonymize_text(summary or content[:140], scope=sc), self.vault.anonymize_text(content, scope=sc), self.vault.anonymize_text(tags, scope=sc), self.vault.anonymize_text(source_ref, scope=sc)
                prov = {k: self.vault.anonymize_text(v, scope=sc) for k, v in prov.items()}
                rec_id = str(item.get("id") or hashlib.sha1(f"{mem_type}:{summary}:{content}".encode("utf-8")).hexdigest()[:16])
                cur.execute(
                    "INSERT INTO memories(id,mem_type,summary,content,tags,source_ref,anchor_id,consolidated,created_at,updated_at,token_count) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET mem_type=excluded.mem_type,summary=excluded.summary,content=excluded.content,"
                    "tags=excluded.tags,source_ref=excluded.source_ref,anchor_id=excluded.anchor_id,consolidated=excluded.consolidated,updated_at=excluded.updated_at,token_count=excluded.token_count",
                    (rec_id, mem_type, summary, content, tags, source_ref, str(item.get("anchor_id") or ""), int(bool(item.get("consolidated", False))), str(item.get("created_at") or now), now, estimate_tokens(f"{summary}\n{content}")),
                )
                cur.execute("DELETE FROM memories_fts WHERE id = ?", (rec_id,))
                cur.execute("INSERT INTO memories_fts(id,mem_type,summary,content,tags) VALUES(?,?,?,?,?)", (rec_id, mem_type, summary, content, tags))
                if any(prov.get(k) for k in ("olay", "bilesen", "kok_neden", "cozum", "jira_veya_mr", "dosya_yolu", "raw_source_ptr")):
                    cur.execute("INSERT OR REPLACE INTO provenance_hypergraph(id,memory_id," + ",".join(_PROV_FIELDS) + ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [str(item.get("prov_id") or f"prov-{rec_id}"), rec_id, *(prov[k] for k in _PROV_FIELDS)])
                vec = self.embed_text(f"[{mem_type}] {summary}\n{content}")
                if vec is not None:
                    lance_rows.append({"id": rec_id, "mem_type": mem_type, "summary": summary[:240], "updated_at": now, "vector": vec})
                ingested_ids.append(rec_id)
            self._conn.commit()
            if lance_rows:
                tbl = self._ensure_lance()
                try:
                    tbl.delete("id IN (" + ", ".join(f"'{r['id'].replace(chr(39), '')}'" for r in lance_rows) + ")")
                except Exception:
                    pass
                tbl.add(lance_rows)
        return {"ok": not rejected, "ingested": len(ingested_ids), "rejected": len(rejected), "ids": ingested_ids, "rejected_reasons": rejected, "screen_capture": SCREEN_CAPTURE_ENABLED}

    def ingest_jsonl(self, jsonl_path: Path, scope: str = "") -> Dict[str, Any]:
        if not jsonl_path.is_file():
            return {"ok": True, "ingested": 0, "rejected": 0, "ids": [], "rejected_reasons": []}
        items: List[Dict[str, Any]] = []
        for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        items.append(obj)
                except json.JSONDecodeError:
                    continue
        return self.ingest(items, scope=scope) if items else {"ok": True, "ingested": 0, "rejected": 0, "ids": [], "rejected_reasons": []}

    def _search_vector(self, vec: Optional[List[float]], valid_types: Optional[Tuple[str, ...]], top_k: int, scores: Dict[str, float]) -> bool:
        if vec is None:
            return False
        try:
            qb = self._ensure_lance().search(vec)
            if valid_types:
                qb = qb.where(f"mem_type IN ({', '.join(repr(t) for t in valid_types)})", prefilter=True)
            for rank, row in enumerate(qb.limit(top_k * 2).to_list(), start=1):
                scores[str(row["id"])] = scores.get(str(row["id"]), 0.0) + 1.0 / (60.0 + rank)
            return True
        except Exception as exc:
            _LOG.warning("LanceDB search fallback to FTS5: %s", exc)
            return False

    def _search_fts(self, query: str, valid_types: Optional[Tuple[str, ...]], top_k: int, scores: Dict[str, float]) -> bool:
        tokens = [t.replace('"', '""') for t in re.findall(r"[^\W_]{2,}", query or "", flags=re.UNICODE)[:12]]
        if not tokens:
            return False
        clauses = [('"' + " ".join(tokens) + '"', 3.0), (" AND ".join(f'"{t}"' for t in tokens), 2.0)] if len(tokens) >= 2 else []
        t_sql = f" AND mem_type IN ({','.join('?' for _ in valid_types)})" if valid_types else ""
        sql, base_p, hit = f"SELECT id FROM memories_fts WHERE memories_fts MATCH ?{t_sql} ORDER BY bm25(memories_fts) LIMIT ?", [*(valid_types or ()), top_k * 2], False
        for expr, wt in [*clauses, (" OR ".join(f'"{t}"' for t in tokens), 1.0)]:
            if hit and wt == 1.0 and clauses:
                break
            for rank, row in enumerate(self._conn.execute(sql, [expr, *base_p]).fetchall(), start=1):
                hit, scores[str(row["id"])] = True, scores.get(str(row["id"]), 0.0) + wt / (60.0 + rank)
        return hit

    def query(self, query: str, types: Optional[Sequence[str]] = None, budget_tokens: int = 600, limit: int = 12) -> Dict[str, Any]:
        t0, valid_types, budget, top_k = time.perf_counter(), _validate_types(types), max(0, int(budget_tokens)), max(1, min(int(limit), 50))
        if budget <= 0:
            return {"ok": True, "query": query, "budget_tokens": budget, "used_tokens": 0, "count": 0, "vector_used": False, "fts_used": False, "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2), "screen_capture": SCREEN_CAPTURE_ENABLED, "items": []}
        scores, vec, q_low = {}, self.embed_text(query), (query or "").strip().lower()
        with self._lock:
            vector_used, fts_used = self._search_vector(vec, valid_types, top_k, scores), self._search_fts(query, valid_types, top_k, scores)
            if len(q_low) >= 2:
                t_sql = f" AND mem_type IN ({','.join('?' for _ in valid_types)})" if valid_types else ""
                for row in self._conn.execute(f"SELECT id, tags, summary FROM memories WHERE (instr(lower(id), ?) > 0 OR instr(lower(tags), ?) > 0 OR instr(lower(summary), ?) > 0){t_sql} LIMIT ?", [q_low, q_low, q_low, *(valid_types or ()), top_k * 2]).fetchall():
                    rid, rtags = str(row["id"]), {t.strip().lower() for t in str(row["tags"]).split(",") if t.strip()}
                    exact = rid.lower() in (q_low, f"seed-{q_low}") or q_low in rtags or str(row["summary"]).lower().startswith(f"{q_low}:")
                    scores[rid] = scores.get(rid, 0.0) + (0.25 if exact else 0.1)
            if not scores:
                sql = "SELECT id FROM memories" + (f" WHERE mem_type IN ({','.join('?' for _ in valid_types)})" if valid_types else "") + " ORDER BY updated_at DESC LIMIT ?"
                for rank, row in enumerate(self._conn.execute(sql, [*(valid_types or ()), top_k]).fetchall(), start=1):
                    scores[str(row["id"])] = 1.0 / (120.0 + rank)
            ranked_ids = [rid for rid, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]]
            if not ranked_ids:
                return {"ok": True, "query": query, "budget_tokens": budget, "used_tokens": 0, "count": 0, "vector_used": vector_used, "fts_used": fts_used, "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2), "screen_capture": SCREEN_CAPTURE_ENABLED, "items": []}
            ph = ",".join("?" for _ in ranked_ids)
            mem_rows = {r["id"]: dict(r) for r in self._conn.execute(f"SELECT * FROM memories WHERE id IN ({ph})", ranked_ids).fetchall()}
            prov_map: Dict[str, List[Dict[str, Any]]] = {}
            for pr in self._conn.execute(f"SELECT * FROM provenance_hypergraph WHERE memory_id IN ({ph})", ranked_ids).fetchall():
                prov_map.setdefault(pr["memory_id"], []).append({k: pr[k] for k in _PROV_FIELDS if pr[k]})
        packed, used_tokens = [], 0
        for rid in ranked_ids:
            rem = budget - used_tokens
            row = mem_rows.get(rid)
            if not row or rem <= 0:
                break
            mtype, s, c, provs = row["mem_type"], row["summary"], row["content"], prov_map.get(rid, [])
            cost = _pack_cost(mtype, s, c, provs)
            if cost > rem:
                provs, cost = [], _pack_cost(mtype, s, c, [])
            if cost > rem:
                avail = rem * 4 - (len(mtype) + 5)
                if avail < 2 or (packed and rem < 8):
                    continue
                s_cap = min(len(s), max(1, avail // 2))
                c = c[: max(1, avail - s_cap)]
                s = s[: max(1, avail - len(c))]
                cost = _pack_cost(mtype, s, c, [])
                if cost > rem:
                    continue
            used_tokens += cost
            packed.append({"id": rid, "mem_type": mtype, "summary": s, "content": c, "tags": row["tags"], "anchor_id": row["anchor_id"], "score": round(scores[rid], 6), "tokens": cost, "provenance": provs})
        return {"ok": True, "query": query, "budget_tokens": budget, "used_tokens": used_tokens, "count": len(packed), "vector_used": vector_used, "fts_used": fts_used, "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2), "screen_capture": SCREEN_CAPTURE_ENABLED, "items": packed}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            counts = {t: 0 for t in MEMORY_TYPES}
            for r in self._conn.execute("SELECT mem_type, COUNT(*) AS c FROM memories GROUP BY mem_type").fetchall():
                counts[str(r["mem_type"])] = int(r["c"])
            unconsolidated = int(self._conn.execute("SELECT COUNT(*) FROM memories WHERE consolidated = 0 AND mem_type IN ('episodic','resource')").fetchone()[0])
            edges = int(self._conn.execute("SELECT COUNT(*) FROM provenance_hypergraph").fetchone()[0])
        return {"ok": True, "data_dir": str(self.data_dir), "total_memories": sum(counts.values()), "counts_by_type": counts, "unconsolidated": unconsolidated, "hypergraph_edges": edges, "rss_mb": _process_rss_mb(), "screen_capture": SCREEN_CAPTURE_ENABLED, "warmed": self._warmed}


def _extract_json_obj(raw: str) -> Dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL).strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, flags=re.DOTALL)
    candidate = fence.group(1) if fence else cleaned
    start, end = candidate.find("{"), candidate.rfind("}")
    return json.loads(candidate[start : end + 1] if start >= 0 and end > start else candidate)


def run_autodream(store: Optional[MirixStore] = None, max_records: int = 30, litellm_url: Optional[str] = None, model: str = DEFAULT_AUTODREAM_MODEL, scope: str = "", http_client: Any = None) -> Dict[str, Any]:
    st, url = store or get_store(), (litellm_url or (store or get_store()).litellm_url).rstrip("/")
    with st._lock:
        rows = [dict(r) for r in st._conn.execute("SELECT id, mem_type, summary, content, tags, source_ref, created_at FROM memories WHERE consolidated = 0 AND mem_type IN ('episodic','resource') ORDER BY created_at ASC LIMIT ?", (max(1, int(max_records)),)).fetchall()]
    if not rows:
        return {"ok": True, "processed": 0, "anchors": 0, "procedural": 0, "state_updates": 0, "screen_capture": SCREEN_CAPTURE_ENABLED}
    compact_rows = [{"id": r["id"], "type": r["mem_type"], "summary": r["summary"][:120], "content": r["content"][:220]} for r in rows]
    prompt = "Consolidate these events into concise JSON (max 2 anchors, 2 procedural, 1 state_updates) with keys 'anchors' (semantic), 'procedural' (runbooks), and 'state_updates' (core). Each item: summary, content, tags, source_ids, provenance(olay,bilesen,kok_neden,cozum,jira_veya_mr,dosya_yolu,tarih). Events:\n" + json.dumps(compact_rows, ensure_ascii=False)
    payload = {"model": model, "messages": [{"role": "system", "content": "Output compact valid JSON only. No markdown or thinking."}, {"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 2048, "response_format": {"type": "json_object"}}
    try:
        with contextlib.nullcontext(http_client) if http_client is not None else httpx.Client(timeout=120.0) as client:
            resp = client.post(f"{url}/v1/chat/completions", json=payload)
        if resp.status_code != 200:
            return {"ok": False, "processed": 0, "error": f"LITELLM_HTTP_{resp.status_code}", "screen_capture": SCREEN_CAPTURE_ENABLED}
        parsed = _extract_json_obj(resp.json()["choices"][0]["message"]["content"])
    except Exception as exc:
        return {"ok": False, "processed": 0, "error": str(exc), "screen_capture": SCREEN_CAPTURE_ENABLED}
    new_items, counts, raw_ids = [], {"anchors": 0, "procedural": 0, "state_updates": 0}, [r["id"] for r in rows]
    for bucket, mtype in (("anchors", "semantic"), ("procedural", "procedural"), ("state_updates", "core")):
        for entry in parsed.get(bucket) or []:
            if not isinstance(entry, dict) or not (entry.get("summary") or entry.get("content")):
                continue
            src_ids = [str(s) for s in (entry.get("source_ids") or raw_ids[:3])]
            raw_tags = entry.get("tags")
            tags_str = ",".join(str(x).strip() for x in raw_tags if str(x).strip()) if isinstance(raw_tags, list) else str(raw_tags or f"autodream,{mtype}")
            prov = {**dict(entry.get("provenance") or {}), "raw_source_ptr": ",".join(src_ids), "raw_source_type": "autodream_consolidation"}
            new_items.append({"mem_type": mtype, "summary": _scrub_secret_text(str(entry.get("summary") or "")), "content": _scrub_secret_text(str(entry.get("content") or "")), "tags": _scrub_secret_text(tags_str), "source_ref": ",".join(src_ids), "anchor_id": src_ids[0] if src_ids else "", "consolidated": 1, "provenance": {k: _scrub_secret_text(str(v)) for k, v in prov.items()}})
            counts[bucket] += 1
    ing_res = st.ingest(new_items, scope=scope) if new_items else {"ids": []}
    created_ids = list(ing_res.get("ids") or [])
    if not created_ids:
        return {"ok": False, "processed": len(rows), "model": model, **counts, "created_ids": [], "error": "EMPTY_DISTILLATION", "screen_capture": SCREEN_CAPTURE_ENABLED}
    first_anchor = created_ids[0]
    with st._lock:
        ph = ",".join("?" for _ in raw_ids)
        st._conn.execute(f"UPDATE memories SET consolidated = 1, anchor_id = CASE WHEN anchor_id = '' THEN ? ELSE anchor_id END, updated_at = ? WHERE id IN ({ph})", [first_anchor, _now_iso(), *raw_ids])
        st._conn.commit()
    return {"ok": True, "processed": len(rows), "model": model, **counts, "created_ids": created_ids, "screen_capture": SCREEN_CAPTURE_ENABLED}


def get_store(data_dir: Optional[Path] = None, embed_url: Optional[str] = None, litellm_url: Optional[str] = None) -> MirixStore:
    resolved = str(Path(data_dir).expanduser().resolve() if data_dir else default_mirix_dir())
    with _STORE_LOCK:
        if resolved not in _STORES:
            _STORES[resolved] = MirixStore(data_dir=Path(resolved), embed_url=embed_url, litellm_url=litellm_url)
        return _STORES[resolved]


def build_historical_seed_items(state_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    sdir, items = Path(state_dir) if state_dir else resolve_state_dir(), []
    ledger_p = sdir / "kaizen_ledger.json"
    if ledger_p.is_file():
        ledger = json.loads(ledger_p.read_text(encoding="utf-8"))
        for adr in ledger.get("decisions", []):
            aid = str(adr.get("id") or "")
            items.append({"id": f"seed-{aid.lower()}", "mem_type": "semantic", "summary": f"{aid}: {adr.get('title', '')}", "content": str(adr.get("summary") or ""), "tags": f"{aid},{adr.get('scope', '')}", "source_ref": str(ledger_p), "consolidated": 1, "provenance": {"olay": adr.get("title", ""), "bilesen": adr.get("scope", ""), "cozum": adr.get("summary", ""), "jira_veya_mr": aid, "dosya_yolu": "kaizen_ledger.json", "tarih": str(adr.get("date", ""))[:10], "raw_source_ptr": str(ledger_p), "raw_source_type": "ledger_adr"}})
        for de in ledger.get("dead_ends", []):
            did = str(de.get("id") or "")
            items.append({"id": f"seed-{did.lower()}", "mem_type": "episodic", "summary": f"{did}: {de.get('symptom', '')}", "content": f"Root cause: {de.get('root_cause', '')}. Guard: {de.get('guard', '')}", "tags": f"{did},dead_end", "source_ref": str(sdir / "DEAD_ENDS.md"), "consolidated": 0, "provenance": {"olay": de.get("symptom", ""), "bilesen": "kaizen-guard", "kok_neden": de.get("root_cause", ""), "cozum": de.get("guard", ""), "jira_veya_mr": did, "dosya_yolu": "DEAD_ENDS.md", "tarih": str(de.get("date", ""))[:10], "raw_source_ptr": str(sdir / "DEAD_ENDS.md"), "raw_source_type": "dead_ends_vault"}})
    lin_p = sdir / "telemetry_lineage.sqlite"
    if lin_p.is_file():
        with contextlib.closing(sqlite3.connect(str(lin_p))) as con:
            for role, stage, root, sha, rcnt in con.execute("SELECT repo_role, stage, repo_root, head_sha, row_count FROM repo_state").fetchall():
                items.append({"id": f"seed-repo-{role}", "mem_type": "core", "summary": f"Repo lineage {role} ({stage})", "content": f"Repo {role} at {root} HEAD={sha[:12]} indexed_symbols={rcnt}", "tags": f"lineage,{role},{stage}", "source_ref": str(lin_p), "consolidated": 1, "provenance": {"olay": f"Lineage index {role}", "bilesen": role, "dosya_yolu": root, "jira_veya_mr": sha[:12], "raw_source_ptr": str(lin_p), "raw_source_type": "telemetry_lineage"}})
    for p in sorted((sdir / "compact_checkpoints").glob("compact_checkpoint_*.json"))[:6]:
        try:
            ck = json.loads(p.read_text(encoding="utf-8"))
            goal = str(ck.get("last_goal") or "").strip()
            if goal:
                items.append({"id": f"seed-ckpt-{p.stem[-12:]}", "mem_type": "resource", "summary": f"Checkpoint: {goal[:100]}", "content": f"Files={ck.get('edited_files', [])[:6]} Symbols={ck.get('verified_serena_symbols', [])[:6]} Err={ck.get('unresolved_error', '')}", "tags": "compact_checkpoint", "source_ref": str(p), "consolidated": 0, "provenance": {"olay": goal[:120], "bilesen": "compact_state_guard", "dosya_yolu": ",".join(ck.get("edited_files", [])[:3]), "raw_source_ptr": str(p), "raw_source_type": "compact_checkpoint"}})
        except Exception:
            continue
    return items


def run_self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="kaizen_mirix_test_") as tmp:
        st = MirixStore(data_dir=Path(tmp), embed_url="")
        try:
            st.warmup()
            res = st.ingest([
                {"mem_type": "core", "summary": "MIRIX core config", "content": "Option A SQLite FTS5 + LanceDB kaizen_memory", "provenance": {"olay": "Init", "bileşen": "mirix", "çözüm": "SQLite+LanceDB", "dosya_yolu": "mirix_memory.py", "satır_aralığı": "1-100"}},
                {"mem_type": "vault", "summary": "Customer MAC test", "content": "Device AA:BB:CC:DD:EE:FF at 10.20.30.40"},
                {"mem_type": "episodic", "summary": "Secret leak attempt", "content": "AKIA" + "1234567890ABCDEF"},
            ])
            q, stats, qv = st.query("SQLite LanceDB", budget_tokens=400), st.stats(), st.query("Customer", budget_tokens=400)
            if res["ingested"] != 2 or res["rejected"] != 1 or not q["ok"] or q["used_tokens"] > 400 or stats["screen_capture"] is not False or stats["counts_by_type"]["vault"] != 1 or any("AA:BB:CC:DD:EE:FF" in it["content"] for it in qv["items"]):
                sys.stderr.write(f"[mirix_memory FAIL] res={res} q={q} stats={stats}\n")
                return 1
        finally:
            st.close()
    print("  [PASS] mirix_memory -- 6-type SQLite+FTS5+LanceDB, N-ary provenance, vault/secret guard & budget verified")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kaizen-Native MIRIX-2.0 Lite + AutoDream CLI")
    for name, kw in (("--data-dir", {"default": ""}), ("--init-seed", {"action": "store_true"}), ("--query", {"default": ""}), ("--types", {"default": ""}), ("--budget", {"type": int, "default": 600}), ("--ingest-jsonl", {"default": ""}), ("--autodream", {"action": "store_true"}), ("--max-records", {"type": int, "default": 30}), ("--stats", {"action": "store_true"}), ("--self-test", {"action": "store_true"})):
        ap.add_argument(name, **kw)
    args = ap.parse_args(argv)
    if args.self_test:
        return run_self_test()
    st = get_store(Path(args.data_dir) if args.data_dir else None)
    st.warmup()
    if args.init_seed:
        items = build_historical_seed_items()
        print(json.dumps(st.ingest(items, scope="kaizen-u2") if items else {"ok": True, "ingested": 0}, indent=2, ensure_ascii=False))
    elif args.ingest_jsonl:
        print(json.dumps(st.ingest_jsonl(Path(args.ingest_jsonl)), indent=2, ensure_ascii=False))
    elif args.query:
        tlist = [t.strip() for t in args.types.split(",") if t.strip()] if args.types else None
        print(json.dumps(st.query(args.query, types=tlist, budget_tokens=args.budget), indent=2, ensure_ascii=False))
    elif args.autodream:
        res = run_autodream(st, max_records=args.max_records)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    elif args.stats:
        print(json.dumps(st.stats(), indent=2, ensure_ascii=False))
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
