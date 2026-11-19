#!/usr/bin/env python3
"""Unit and ASGI integration tests for Unified FastMCP Gateway (`fastmcp_gateway.py`)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

from starlette.testclient import TestClient

from kaizen_kit import fastmcp_gateway


class _MockBackendState:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []


def _start_mock_backend() -> tuple[HTTPServer, str, _MockBackendState]:
    state = _MockBackendState()

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            state.calls.append(("GET", self.path, {}))
            if self.path == "/healthz":
                payload = {"status": "ok"}
            elif self.path == "/health/liveliness":
                payload = {"status": "alive"}
            elif self.path == "/v1/models":
                payload = {"object": "list", "data": [{"id": "qwen3.5-4b"}, {"id": "mini-pool"}]}
            elif self.path == "/v1/pool/status":
                payload = {"workers": {"jev": "UP"}}
            else:
                payload = {"status": "ok", "path": self.path}
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body_raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            body = json.loads(body_raw)
            state.calls.append(("POST", self.path, body))
            if self.path == "/api/search":
                resp = {"Result": {"FileMatches": [{"FileName": "src/lib.rs"}]}}
            elif self.path == "/v1/rag/query":
                resp = {"query": body.get("query", ""), "results": [{"path": "src/main.rs", "score": 0.91}]}
            elif self.path == "/v1/systemone":
                resp = {"answers": {"q": {"choice": "rust", "confidence": 0.94}}}
            elif self.path == "/v1/chat/completions":
                resp = {"choices": [{"message": {"content": f"echo:{body.get('model')}"}}]}
            else:
                resp = {"ok": True}
            raw = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}", state


class TestFastMcpGateway(unittest.TestCase):
    def test_non_loopback_urls_rejected(self) -> None:
        with self.assertRaises(ValueError):
            fastmcp_gateway.GatewayConfig(jev_url="http://10.0.0.5:8765")
        with self.assertRaises(ValueError):
            fastmcp_gateway.GatewayConfig(zoekt_url="https://example.com:6070")
        with self.assertRaises(ValueError):
            fastmcp_gateway.GatewayConfig(jev_url="file://127.0.0.1/tmp")
        with self.assertRaises(ValueError):
            fastmcp_gateway.GatewayConfig(litellm_url="ftp://localhost:4000")
        with self.assertRaises(ValueError):
            fastmcp_gateway.GatewayConfig(coderag_url="127.0.0.1:18808")

    def test_runtime_env_override_for_gateway_config(self) -> None:
        with patch.dict(os.environ, {"KAIZEN_JEV_URL": "http://127.0.0.1:19876"}):
            cfg = fastmcp_gateway.GatewayConfig()
            self.assertEqual(cfg.jev_url, "http://127.0.0.1:19876")

    def test_http_json_timeout_reports_non_empty_error(self) -> None:
        release = threading.Event()

        class _SlowHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                release.wait(timeout=5.0)
                self.send_response(200)
                self.end_headers()

        slow_srv = HTTPServer(("127.0.0.1", 0), _SlowHandler)
        t = threading.Thread(target=slow_srv.serve_forever, daemon=True)
        t.start()
        try:
            res = asyncio.run(
                fastmcp_gateway._http_json("GET", f"http://127.0.0.1:{slow_srv.server_address[1]}/slow", timeout=0.05)
            )
            self.assertFalse(res["ok"], f"Expected timeout failure, got {res!r}")
            self.assertEqual(res["status_code"], 0)
            self.assertTrue(bool(res["error"].strip()))
        finally:
            release.set()
            slow_srv.shutdown()
            slow_srv.server_close()

    def test_expected_tools_registered_and_serena_mount_safe(self) -> None:
        srv_no_serena = fastmcp_gateway.build_fastmcp_server(include_serena=False)
        tools = asyncio.run(srv_no_serena.list_tools())
        names = {t.name for t in tools}
        expected = {
            "memory_query",
            "memory_ingest",
            "memory_autodream",
            "memory_stats",
            "gateway_health",
            "coderag_query",
            "coderag_reindex",
            "coderag_hybrid_search",
            "coderag_ocr_probe",
            "zoekt_search",
            "litellm_models",
            "litellm_chat",
            "neon_decide",
            "neon_batch_plan",
            "neon_plan",
            "neon_night_shift",
            "neon_best_of_n",
            "neon_gpu_burst_plan",
            "neon_guardrail_stream",
            "neon_voice_to_action",
            "neon_mask_observation",
            "neon_status",
        }
        self.assertEqual(names, expected)

        srv_with_serena = fastmcp_gateway.build_fastmcp_server(
            config=fastmcp_gateway.GatewayConfig(serena_url="http://127.0.0.1:59998/mcp"),
            include_serena=True,
        )
        self.assertIsNotNone(srv_with_serena)

    def test_mcp_client_end_to_end_against_loopback_backends(self) -> None:
        from fastmcp import Client

        server, base_url, state = _start_mock_backend()
        try:
            cfg = fastmcp_gateway.GatewayConfig(
                jev_url=base_url,
                coderag_url=base_url,
                zoekt_url=base_url,
                serena_url=f"{base_url}/mcp",
                litellm_url=base_url,
            )
            mcp = fastmcp_gateway.build_fastmcp_server(config=cfg, include_serena=False)

            async def _exercise() -> Dict[str, Any]:
                async with Client(mcp) as client:
                    z = await client.call_tool("zoekt_search", {"query": "VectorCube", "limit": 5})
                    r = await client.call_tool("coderag_query", {"query": "semilattice", "limit": 3})
                    c = await client.call_tool("litellm_chat", {"prompt": "hi", "model": "qwen3.5-4b"})
                    h = await client.call_tool("gateway_health", {})
                    cd_corp = await client.call_tool(
                        "neon_decide",
                        {
                            "input_content": "def f(): pass",
                            "instructions": "pick lang",
                            "question_type": "choice",
                            "criteria": {"rust": "Rust", "python": "Python"},
                        },
                    )
                    cd_gem = await client.call_tool(
                        "neon_decide",
                        {
                            "input_content": "def add(a, b): return a + b",
                            "instructions": "Is pure?",
                            "data_classification": "public_sanitized",
                            "dry_run": True,
                        },
                    )
                    cd_blocked = await client.call_tool(
                        "neon_decide",
                        {
                            "input_content": "deploy to kaizen-prod",
                            "instructions": "Is safe?",
                            "data_classification": "public_sanitized",
                            "dry_run": True,
                        },
                    )
                    cbp_blocked = await client.call_tool(
                        "neon_batch_plan",
                        {"prompts": ["Summarize generic graph algorithm"]},
                    )
                    cbp_ok = await client.call_tool(
                        "neon_batch_plan",
                        {
                            "prompts": ["Summarize generic graph algorithm"],
                            "system_instruction": "Be concise",
                            "data_classification": "public_sanitized",
                        },
                    )
                    cnp_corp = await client.call_tool(
                        "neon_plan",
                        {"prompt": "Review VectorCube race", "task_type": "code_review"},
                    )
                    cnp_blocked = await client.call_tool(
                        "neon_plan",
                        {"prompt": "deploy to kaizen-prod", "data_classification": "public_sanitized"},
                    )
                    cnp_pub = await client.call_tool(
                        "neon_plan",
                        {
                            "prompt": "Review generic lock-free queue algorithm",
                            "task_type": "code_review",
                            "data_classification": "public_sanitized",
                        },
                    )
                    cns = await client.call_tool("neon_status", {})
                    nns = await client.call_tool("neon_night_shift", {"execute": False})
                    c_schema = await client.call_tool(
                        "litellm_chat",
                        {
                            "prompt": "hi",
                            "model": "qwen3.5-4b",
                            "json_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                        },
                    )
                    hs = await client.call_tool("coderag_hybrid_search", {"query": "semilattice", "limit": 3})
                    bon = await client.call_tool("neon_best_of_n", {"prompt": "Write pure function", "n": 2, "dry_run": True})
                    gb = await client.call_tool("neon_gpu_burst_plan", {"tier": "distributed_256gb", "nodes": 16})
                    gs = await client.call_tool("neon_guardrail_stream", {"chunks": ["clean output ", "ok"]})
                    vta = await client.call_tool(
                        "neon_voice_to_action",
                        {"transcript": "<voice_instructions>concise</voice_instructions> Fix parity", "dry_run": True},
                    )
                    mo = await client.call_tool(
                        "neon_mask_observation",
                        {
                            "command": "rg foo",
                            "output": "\n".join(f"line_{i}" for i in range(100)) + "\nERROR: bad line",
                            "exit_code": 1,
                        },
                    )
                    return {
                        "z": z.data,
                        "r": r.data,
                        "c": c.data,
                        "h": h.data,
                        "cd_corp": cd_corp.data,
                        "cd_gem": cd_gem.data,
                        "cd_blocked": cd_blocked.data,
                        "cbp_blocked": cbp_blocked.data,
                        "cbp_ok": cbp_ok.data,
                        "cnp_corp": cnp_corp.data,
                        "cnp_blocked": cnp_blocked.data,
                        "cnp_pub": cnp_pub.data,
                        "cns": cns.data,
                        "nns": nns.data,
                        "c_schema": c_schema.data,
                        "hs": hs.data,
                        "bon": bon.data,
                        "gb": gb.data,
                        "gs": gs.data,
                        "vta": vta.data,
                        "mo": mo.data,
                    }

            with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
                out = asyncio.run(_exercise())
            self.assertEqual(out["z"]["Result"]["FileMatches"][0]["FileName"], "src/lib.rs")
            self.assertEqual(out["r"]["results"][0]["path"], "src/main.rs")
            self.assertEqual(out["c"]["choices"][0]["message"]["content"], "echo:qwen3.5-4b")
            self.assertEqual(out["h"]["status"], "ok")
            self.assertEqual(out["h"]["backends"]["jev"], "UP")
            self.assertEqual(out["h"]["pool"]["workers"]["jev"], "UP")
            self.assertEqual(out["cd_corp"]["backend"], "ec2_jev_8765")
            self.assertEqual(out["cd_corp"]["answers"]["q"]["choice"], "rust")
            self.assertEqual(out["cd_gem"]["backend"], "gemini-decision-1")
            self.assertEqual(out["cd_gem"]["expected_output_tokens"], 0)
            self.assertTrue(out["cd_blocked"]["blocked"])
            self.assertTrue(out["cbp_blocked"]["blocked"])
            self.assertTrue(out["cbp_ok"]["ok"])
            self.assertEqual(len(out["cbp_ok"]["jsonl_lines"]), 1)
            self.assertIsNotNone(out["cbp_ok"]["cache_plan"])
            self.assertEqual(out["cnp_corp"]["orchestrator"], "neon")
            self.assertEqual(out["cnp_corp"]["data_classification"], "corporate_proprietary")
            self.assertFalse(out["cnp_corp"]["gemini_exposure"])
            self.assertEqual(out["cnp_corp"]["phase_b_executor"]["primary_model"], "qwen3.5-35b-a3b")
            self.assertTrue(out["cnp_blocked"]["blocked"])
            self.assertFalse(out["cnp_blocked"]["ok"])
            self.assertIn("DATA_RESIDENCY_BLOCKED", out["cnp_blocked"]["error"])
            self.assertEqual(out["cnp_pub"]["data_classification"], "public_sanitized")
            self.assertTrue(out["cnp_pub"]["gemini_exposure"])
            self.assertEqual(out["cnp_pub"]["cascade_decision"]["service_tier"], "flex")
            self.assertEqual(out["cnp_pub"]["cascade_decision"]["initial_model"], "gemini-4-hydrogen")
            self.assertIsInstance(out["cns"]["gemini_api_key_configured"], bool)
            self.assertIsInstance(out["cns"]["local_bedrock_enabled"], bool)
            self.assertEqual(
                set(out["cns"]["services"].keys()),
                {"litellm", "jev", "fastmcp", "zoekt", "coderag", "serena", "claude_ram", "gpu_burst"},
            )
            self.assertEqual(out["nns"]["orchestrator"], "neon-night-shift")
            self.assertEqual(out["nns"]["mode"], "dry_run")
            self.assertEqual(out["c_schema"]["choices"][0]["message"]["content"], "echo:qwen3.5-4b")
            self.assertEqual(out["hs"]["results"][0]["path"], "src/main.rs")
            self.assertTrue(out["bon"]["dry_run"])
            self.assertEqual(out["gb"]["tier"], "distributed_256gb")
            self.assertEqual(out["gb"]["default_state"], "STOPPED")
            self.assertFalse(out["gs"]["halted"])
            self.assertTrue(out["vta"]["ok"])
            self.assertTrue(out["mo"]["masked"])
            self.assertIn("ERROR: bad line", out["mo"]["masked_text"])
            self.assertTrue(any(path == "/api/search" and body.get("Q") == "VectorCube" for _, path, body in state.calls))
            self.assertTrue(
                any(
                    path == "/v1/chat/completions" and (body.get("response_format") or {}).get("type") == "json_schema"
                    for _, path, body in state.calls
                )
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_asgi_app_health_and_mcp_routes(self) -> None:
        server, base_url, _ = _start_mock_backend()
        try:
            cfg = fastmcp_gateway.GatewayConfig(
                jev_url=base_url,
                coderag_url=base_url,
                zoekt_url=base_url,
                serena_url=f"{base_url}/mcp",
                litellm_url=base_url,
            )
            asgi = fastmcp_gateway.create_asgi_app(config=cfg, include_serena=False)
            route_paths = {getattr(r, "path", "") for r in asgi.routes}
            self.assertIn("/mcp", route_paths)
            self.assertIn("/health", route_paths)

            with TestClient(asgi) as tc:
                resp = tc.get("/health")
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertEqual(body["status"], "ok")
                self.assertEqual(body["service"], "kaizen-fastmcp")
                self.assertEqual(body["backends"]["litellm"], "UP")

                headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
                init_resp = tc.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "pytest", "version": "1.0"},
                        },
                    },
                )
                self.assertEqual(init_resp.status_code, 200)
                self.assertIn("kaizen-cloud-brain", init_resp.text)

                list_resp = tc.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                self.assertEqual(list_resp.status_code, 200)
                self.assertIn("gateway_health", list_resp.text)
                self.assertIn("zoekt_search", list_resp.text)
        finally:
            server.shutdown()
            server.server_close()

    def test_mirix_http_routes_budget_zero_and_bad_requests(self) -> None:
        import tempfile
        from pathlib import Path
        from kaizen_kit import mirix_memory

        with tempfile.TemporaryDirectory() as tmp:
            resolved = str(Path(tmp).resolve())
            try:
                with patch.dict(os.environ, {"KAIZEN_MIRIX_DIR": tmp, "KAIZEN_MIRIX_SKIP_WARMUP": "1", "KAIZEN_EMBED_URL": "http://127.0.0.1:19999"}):
                    cfg = fastmcp_gateway.GatewayConfig(mirix_dir=tmp, embed_url="http://127.0.0.1:19999")
                    asgi = fastmcp_gateway.create_asgi_app(config=cfg, include_serena=False)
                    with TestClient(asgi) as tc:
                        r_ing = tc.post("/mirix/ingest", json={"items": [{"mem_type": "core", "summary": "Test", "content": "OnceLock cache"}]})
                        self.assertEqual(r_ing.status_code, 200)

                        r_zero = tc.post("/mirix/query", json={"query": "OnceLock", "budget_tokens": 0})
                        self.assertEqual(r_zero.status_code, 200)
                        self.assertEqual(r_zero.json()["budget_tokens"], 0)
                        self.assertEqual(r_zero.json()["used_tokens"], 0)
                        self.assertEqual(r_zero.json()["count"], 0)

                        r_bad_type = tc.post("/mirix/query", json={"query": "OnceLock", "types": ["nonexistent_type"]})
                        self.assertEqual(r_bad_type.status_code, 400)
                        self.assertFalse(r_bad_type.json()["ok"])

                        r_bad_json = tc.post("/mirix/query", content="{not-json", headers={"Content-Type": "application/json"})
                        self.assertEqual(r_bad_json.status_code, 400)

                        r_non_dict = tc.post("/mirix/query", json=[1, 2])
                        self.assertEqual(r_non_dict.status_code, 400)

                        r_bad_ing = tc.post("/mirix/ingest", json="not_a_list_or_dict")
                        self.assertEqual(r_bad_ing.status_code, 400)
                        self.assertFalse(r_bad_ing.json()["ok"])
            finally:
                st = mirix_memory._STORES.pop(resolved, None)
                if st is not None:
                    st.close()


if __name__ == "__main__":
    unittest.main()
