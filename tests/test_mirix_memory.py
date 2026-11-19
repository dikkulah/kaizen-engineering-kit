#!/usr/bin/env python3
"""Unit tests for Kaizen-Native MIRIX-2.0 Lite, AutoDream, FastMCP tools, and zero-tax hooks."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from kaizen_kit import fastmcp_gateway, mirix_memory  # noqa: E402
from kaizen_kit.hooks import compact_state_guard  # noqa: E402


class _FakeResp:
    def __init__(self, status_code: int, payload: Dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


class _FakeHttpClient:
    def __init__(self, handler: Any) -> None:
        self._handler = handler

    def post(self, url: str, json: Dict[str, Any], **_kwargs: Any) -> _FakeResp:
        return self._handler(url, json)


def test_mirix_core_line_limit_and_screen_capture_disabled() -> None:
    core_path = SRC_DIR / "kaizen_kit" / "mirix_memory.py"
    line_count = len(core_path.read_text(encoding="utf-8").splitlines())
    assert line_count <= 500, f"mirix_memory.py exceeded 500 lines: {line_count}"
    assert mirix_memory.SCREEN_CAPTURE_ENABLED is False


def test_six_memory_types_and_nary_provenance_hypergraph(tmp_path: Path) -> None:
    store = mirix_memory.MirixStore(data_dir=tmp_path, embed_url="")
    try:
        store.warmup()
        items = [
            {
                "mem_type": mtype,
                "summary": f"Entry for {mtype}",
                "content": f"Detailed payload for {mtype} memory verification",
                "tags": f"tag_{mtype}",
                "provenance": {
                    "olay": f"event_{mtype}",
                    "bileşen": "vector_cube",
                    "kök_neden": "cold_start",
                    "çözüm": "warmup",
                    "jira_veya_mr": "UC-59335",
                    "dosya_yolu": "src/main.rs",
                    "satır_aralığı": "10-42",
                    "tarih": "2026-11-17",
                    "raw_source_ptr": "git:fd9c7c7",
                    "raw_source_type": "git commit",
                },
            }
            for mtype in mirix_memory.MEMORY_TYPES
        ]
        res = store.ingest(items)
        assert res["ok"] is True and res["ingested"] == 6
        bad = store.ingest([{"mem_type": "invalid_kind", "summary": "x", "content": "y"}])
        assert bad["ok"] is False and bad["rejected"] == 1

        stats = store.stats()
        assert stats["total_memories"] == 6
        assert stats["hypergraph_edges"] == 6
        assert stats["screen_capture"] is False
        assert all(stats["counts_by_type"][t] == 1 for t in mirix_memory.MEMORY_TYPES)

        q = store.query("vector_cube warmup", types=["procedural", "core"], budget_tokens=600)
        assert q["ok"] is True and q["count"] == 2
        assert {it["mem_type"] for it in q["items"]} == {"procedural", "core"}
        prov = q["items"][0]["provenance"][0]
        assert prov["bilesen"] == "vector_cube"
        assert prov["kok_neden"] == "cold_start"
        assert prov["cozum"] == "warmup"
        assert prov["satir_araligi"] == "10-42"
        assert prov["raw_source_type"] == "git commit"
    finally:
        store.close()


def test_secret_rejection_and_deterministic_vault_pseudonymization(tmp_path: Path) -> None:
    store = mirix_memory.MirixStore(data_dir=tmp_path, embed_url="")
    try:
        secret_tok = "AKIA" + "IOSFODNN7EXAMPLE"
        jwt_tok = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        res = store.ingest([
            {"mem_type": "episodic", "summary": "leak1", "content": f"key={secret_tok}"},
            {"mem_type": "episodic", "summary": "leak2", "content": f"jwt={jwt_tok}"},
            {"mem_type": "vault", "summary": "customer CPE", "content": "CPE AA:BB:CC:11:22:33 ip 10.44.55.66"},
            {"mem_type": "core", "summary": "u2 testbed", "content": "kaizen-u2 CPE 11:22:33:44:55:66 at 10.99.88.77", "scope": "kaizen-u2"},
        ])
        assert res["ingested"] == 2 and res["rejected"] == 2
        assert all("SECRET_DETECTED" in r["reason"] for r in res["rejected_reasons"])

        q_cust = store.query("customer CPE", types=["vault"], budget_tokens=400)
        assert q_cust["count"] == 1
        assert "AA:BB:CC:11:22:33" not in q_cust["items"][0]["content"]
        assert "10.44.55.66" not in q_cust["items"][0]["content"]
        assert "00:50:C2:" in q_cust["items"][0]["content"]
        assert "198.51.100." in q_cust["items"][0]["content"]

        q_u2 = store.query("kaizen-u2", types=["core"], budget_tokens=400)
        assert q_u2["count"] == 1
        assert "11:22:33:44:55:66" in q_u2["items"][0]["content"]
    finally:
        store.close()


def test_budget_packing_and_rrf_hybrid_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = mirix_memory.MirixStore(data_dir=tmp_path, embed_url="http://127.0.0.1:8766")
    try:
        def fake_embed(text: str) -> List[float]:
            v = [0.0] * mirix_memory.EMBED_DIM
            v[0] = 1.0 if "alpha" in text.lower() else 0.1
            return v

        monkeypatch.setattr(store, "embed_text", fake_embed)
        store.warmup()
        big_items = [
            {"mem_type": "semantic", "summary": f"Alpha doc {i}", "content": ("alpha " * 90) + f"item_{i}"}
            for i in range(6)
        ]
        res = store.ingest(big_items)
        assert res["ingested"] == 6

        q400 = store.query("alpha", budget_tokens=400, limit=10)
        assert q400["vector_used"] is True and q400["fts_used"] is True
        assert 0 < q400["used_tokens"] <= 400 and q400["count"] < 6

        q600 = store.query("alpha", budget_tokens=600, limit=10)
        assert q600["used_tokens"] <= 600 and q600["used_tokens"] >= q400["used_tokens"]

        q_tiny = store.query("alpha", budget_tokens=20, limit=5)
        assert q_tiny["used_tokens"] <= 20 and q_tiny["count"] == 1
    finally:
        store.close()


def test_autodream_consolidation_preserves_raw_and_rescrubs(tmp_path: Path) -> None:
    store = mirix_memory.MirixStore(data_dir=tmp_path, embed_url="")
    try:
        ing = store.ingest([
            {"id": "raw-ep-1", "mem_type": "episodic", "summary": "ALB 502 error", "content": "Uncompressed payload >1MB failed", "consolidated": 0},
            {"id": "raw-res-1", "mem_type": "resource", "summary": "Gzip fix trace", "content": "Added gzip + base64 framing", "consolidated": 0},
        ])
        assert ing["ingested"] == 2

        syn_leak = "AKIA" + "1234567890ABCDEF"
        llm_json = {
            "anchors": [{"summary": "ALB gzip anchor", "content": f"Always gzip payloads >900KB; leak {syn_leak}", "source_ids": ["raw-ep-1", "raw-res-1"], "provenance": {"olay": "502", "cozum": "gzip"}}],
            "procedural": [{"summary": "Runbook ALB framing", "content": "1. Enable gzip 2. Verify <1MB", "source_ids": ["raw-res-1"]}],
            "state_updates": [{"summary": "State WS-3 active", "content": "MIRIX + AutoDream wired on EC2", "source_ids": ["raw-ep-1"]}],
        }
        fake_client = _FakeHttpClient(lambda _u, _j: _FakeResp(200, {"choices": [{"message": {"content": json.dumps(llm_json)}}]}))
        out = mirix_memory.run_autodream(store=store, max_records=10, http_client=fake_client)
        assert out["ok"] is True and out["processed"] == 2
        assert out["anchors"] == 1 and out["procedural"] == 1 and out["state_updates"] == 1

        stats = store.stats()
        assert stats["unconsolidated"] == 0
        assert stats["counts_by_type"]["semantic"] == 1
        assert stats["counts_by_type"]["procedural"] == 1
        assert stats["counts_by_type"]["core"] == 1
        raw_row = store._conn.execute("SELECT content, consolidated, anchor_id FROM memories WHERE id = 'raw-ep-1'").fetchone()
        assert raw_row["content"] == "Uncompressed payload >1MB failed"
        assert raw_row["consolidated"] == 1 and raw_row["anchor_id"] != ""
        sem_row = store._conn.execute("SELECT content FROM memories WHERE mem_type = 'semantic'").fetchone()
        assert syn_leak not in sem_row["content"]
    finally:
        store.close()


def test_fastmcp_gateway_tools_and_http_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.testclient import TestClient

    monkeypatch.setenv("KAIZEN_MIRIX_DIR", str(tmp_path))
    monkeypatch.setenv("KAIZEN_EMBED_URL", "http://127.0.0.1:19999")
    monkeypatch.setenv("KAIZEN_MIRIX_SKIP_WARMUP", "1")
    cfg = fastmcp_gateway.GatewayConfig(mirix_dir=str(tmp_path), embed_url="http://127.0.0.1:19999")
    srv = fastmcp_gateway.build_fastmcp_server(config=cfg, include_serena=False)

    tool_names = [t.name for t in asyncio.run(srv.list_tools())]
    for expected in ("memory_query", "memory_ingest", "memory_autodream", "memory_stats"):
        assert expected in tool_names

    asgi = fastmcp_gateway.create_asgi_app(config=cfg, include_serena=False)
    with TestClient(asgi) as client:
        r_ing = client.post("/mirix/ingest", json={"items": [{"mem_type": "core", "summary": "FastMCP route test", "content": "MIRIX route works cleanly"}]})
        assert r_ing.status_code == 200 and r_ing.json()["ingested"] == 1
        r_q = client.post("/mirix/query", json={"query": "FastMCP route", "budget_tokens": 400})
        assert r_q.status_code == 200 and r_q.json()["count"] == 1
        assert r_q.json()["used_tokens"] <= 400


def test_zero_tax_hook_enqueue_budget_and_offline_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    qpath = tmp_path / "mirix_queue.jsonl"
    monkeypatch.setattr(compact_state_guard, "_MIRIX_VAULT", None)
    monkeypatch.setenv("AI_KIT_STATE_DIR", str(tmp_path))

    t0 = time.perf_counter()
    ok = compact_state_guard.enqueue_mirix_event(
        {"mem_type": "episodic", "summary": "Edit fold_avro", "content": "Customer 10.22.33.44 MAC AA:BB:CC:00:11:22"},
        queue_path=qpath,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert ok is True and elapsed_ms < 50.0
    blocked = compact_state_guard.enqueue_mirix_event(
        {"mem_type": "episodic", "summary": "bad", "content": "AKIA" + "9999888877776666"},
        queue_path=qpath,
    )
    assert blocked is False

    lines = qpath.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "AA:BB:CC:00:11:22" not in lines[0]

    offline = compact_state_guard.fetch_mirix_context("fold_avro", budget_tokens=400, timeout_s=0.1, gateway_url="http://127.0.0.1:19998")
    assert offline == ""

    import httpx

    class _MockClient:
        def __init__(self, **_kw: Any) -> None:
            pass

        def __enter__(self) -> "_MockClient":
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

        def post(self, url: str, json: Dict[str, Any]) -> _FakeResp:
            if "/mirix/ingest" in url:
                return _FakeResp(200, {"ok": True, "ingested": len(json.get("items") or []), "rejected": 0})
            assert json["budget_tokens"] <= 400
            return _FakeResp(200, {"items": [{"mem_type": "semantic", "summary": f"S{i}", "content": "word " * 80} for i in range(10)]})

    monkeypatch.setattr(httpx, "Client", _MockClient)
    ctx = compact_state_guard.fetch_mirix_context("fold_avro", budget_tokens=999, timeout_s=0.25)
    assert ctx.startswith("[MIRIX MEMORY CONTEXT") and len(ctx) <= 400 * 4

    flushed = compact_state_guard.flush_mirix_queue(queue_path=qpath, gateway_url="http://127.0.0.1:8770")
    assert flushed["ok"] is True and flushed["flushed"] == 1 and flushed["remaining"] == 0

    assert mirix_memory.run_self_test() == 0
    assert compact_state_guard.run_self_test() == 0


def test_strict_budget_tokens_oversized_and_source_ref_vault(tmp_path: Path) -> None:
    store = mirix_memory.MirixStore(data_dir=tmp_path, embed_url="")
    try:
        store.warmup()
        res = store.ingest([
            {
                "mem_type": "semantic",
                "summary": "S" * 800,
                "content": "C" * 5000,
                "source_ref": "device DE:AD:BE:EF:00:11 log",
                "provenance": {"olay": "O" * 200, "cozum": "Z" * 1000, "dosya_yolu": "src/big.rs"},
            }
        ])
        assert res["ingested"] == 1
        src_row = store._conn.execute("SELECT source_ref FROM memories").fetchone()
        assert "DE:AD:BE:EF:00:11" not in src_row["source_ref"]

        for budget in (0, 5, 15, 120, 400, 600):
            q = store.query("S", budget_tokens=budget)
            assert q["used_tokens"] <= budget
            if budget == 0:
                assert q["count"] == 0 and q["used_tokens"] == 0
            else:
                actual = sum(
                    mirix_memory._pack_cost(it["mem_type"], it["summary"], it["content"], it["provenance"])
                    for it in q["items"]
                )
                assert actual == q["used_tokens"] <= budget
    finally:
        store.close()


def test_turkish_diacritic_fts5_and_injection_safety(tmp_path: Path) -> None:
    store = mirix_memory.MirixStore(data_dir=tmp_path, embed_url="")
    try:
        store.ingest([
            {"id": "en1", "mem_type": "core", "summary": "English baseline", "content": "Unrelated latency metrics"},
            {"id": "tr1", "mem_type": "procedural", "summary": "Türkçe bileşen kılavuzu", "content": "Kök neden analizi ve çözüm satır aralığı doğrulandı"},
        ])
        q1 = store.query("çözüm", budget_tokens=400)
        assert q1["fts_used"] is True and q1["items"][0]["id"] == "tr1"

        q2 = store.query("bileşen satır_aralığı", budget_tokens=400)
        assert q2["fts_used"] is True and q2["items"][0]["id"] == "tr1"

        q_inj = store.query('ALB "gzip" OR (1=1); -- DROP TABLE memories;', budget_tokens=400)
        assert q_inj["ok"] is True
    finally:
        store.close()


def test_autodream_empty_distillation_and_tags_normalization(tmp_path: Path) -> None:
    store = mirix_memory.MirixStore(data_dir=tmp_path, embed_url="")
    try:
        store.ingest([{"id": "ep_keep", "mem_type": "episodic", "summary": "Raw log", "content": "Must stay unconsolidated on empty dream", "consolidated": 0}])
        empty_client = _FakeHttpClient(lambda _u, _j: _FakeResp(200, {"choices": [{"message": {"content": json.dumps({"anchors": [], "procedural": [], "state_updates": []})}}]}))
        out_empty = mirix_memory.run_autodream(store=store, max_records=10, http_client=empty_client)
        assert out_empty["ok"] is False and out_empty["created_ids"] == []
        raw_row = store._conn.execute("SELECT consolidated, anchor_id FROM memories WHERE id = 'ep_keep'").fetchone()
        assert raw_row["consolidated"] == 0 and raw_row["anchor_id"] == ""

        good_llm = {
            "anchors": [{"summary": "Dream anchor", "content": "Merged insight", "tags": ["lambda", "singleton", "performance"], "source_ids": ["ep_keep"]}],
            "procedural": [],
            "state_updates": [],
        }
        good_client = _FakeHttpClient(lambda _u, _j: _FakeResp(200, {"choices": [{"message": {"content": json.dumps(good_llm)}}]}))
        out_good = mirix_memory.run_autodream(store=store, max_records=10, http_client=good_client)
        assert out_good["ok"] is True and len(out_good["created_ids"]) == 1
        sem_row = store._conn.execute("SELECT tags FROM memories WHERE mem_type = 'semantic'").fetchone()
        assert sem_row["tags"] == "lambda,singleton,performance"
    finally:
        store.close()


def test_closed_port_fallback_new_mac_enqueue_and_concurrent_flush(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()

    for _ in range(3):
        t0 = time.perf_counter()
        res = compact_state_guard.fetch_mirix_context("SQLite", gateway_url=f"http://127.0.0.1:{closed_port}", timeout_s=0.25)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert res == "" and elapsed_ms <= 300.0

    qpath = tmp_path / "mirix_queue.jsonl"
    monkeypatch.setattr(compact_state_guard, "_MIRIX_VAULT", None)
    monkeypatch.setenv("AI_KIT_STATE_DIR", str(tmp_path))
    assert compact_state_guard.enqueue_mirix_event({"mem_type": "episodic", "summary": "warmup", "content": "init"}, queue_path=qpath, scope="prod") is True

    timings: List[float] = []
    for idx in range(10):
        t0 = time.perf_counter()
        ok = compact_state_guard.enqueue_mirix_event(
            {"mem_type": "episodic", "summary": f"ev{idx}", "content": f"MAC AA:BB:CC:00:01:{idx:02X} IP 10.55.1.{idx + 10}"},
            queue_path=qpath,
            scope="prod",
        )
        timings.append((time.perf_counter() - t0) * 1000.0)
        assert ok is True
    assert (sum(timings) / len(timings)) < 5.0

    import httpx

    class _ConcurrentPostClient:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail

        def __enter__(self) -> "_ConcurrentPostClient":
            return self

        def __exit__(self, *_a: Any) -> None:
            pass

        def post(self, _u: str, json: Dict[str, Any]) -> _FakeResp:
            compact_state_guard.enqueue_mirix_event({"mem_type": "episodic", "summary": "concurrent", "content": "arrived during post"}, queue_path=qpath, scope="prod")
            if self.fail:
                return _FakeResp(500, {"ok": False})
            return _FakeResp(200, {"ok": True, "ingested": len(json.get("items") or [])})

    monkeypatch.setattr(httpx, "Client", lambda **_kw: _ConcurrentPostClient(fail=False))
    fl_ok = compact_state_guard.flush_mirix_queue(queue_path=qpath, gateway_url="http://127.0.0.1:8770")
    assert fl_ok["ok"] is True and fl_ok["flushed"] == 11 and fl_ok["remaining"] == 1
    assert "arrived during post" in qpath.read_text(encoding="utf-8")

    monkeypatch.setattr(httpx, "Client", lambda **_kw: _ConcurrentPostClient(fail=True))
    fl_fail = compact_state_guard.flush_mirix_queue(queue_path=qpath, gateway_url="http://127.0.0.1:8770")
    assert fl_fail["ok"] is False and fl_fail["remaining"] == 2
    assert len(qpath.read_text(encoding="utf-8").splitlines()) == 2
