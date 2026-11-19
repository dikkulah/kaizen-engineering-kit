#!/usr/bin/env python3
"""Unit tests for MiniPoolClient, JevClient.score/noul/batch, and mini_pool CLI."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kaizen_kit import localmodels, mini_pool


class TestLocalModelsExtensions(unittest.TestCase):
    def test_status_keys_backward_compatible(self) -> None:
        with patch.object(localmodels, "_port_open", return_value=False):
            st = localmodels.status()
        self.assertEqual(set(st.keys()), {"jev-style", "bitnet-embed"})

    def test_non_loopback_rejected(self) -> None:
        with self.assertRaises(ValueError):
            localmodels.MiniPoolClient("http://10.0.0.5:8765")

    def test_jev_score_and_noul(self) -> None:
        jev = localmodels.JevClient("http://127.0.0.1:8765")
        with patch.object(localmodels, "_port_open", return_value=True):
            with patch.object(
                localmodels,
                "_post_json",
                return_value={"answers": {"q": {"score": 0.88, "confidence": 0.91, "reason": "ok"}}},
            ):
                res = jev.score("state", "rate quality")
            self.assertIsNotNone(res)
            assert res is not None
            self.assertAlmostEqual(res.score, 0.88)
            self.assertAlmostEqual(res.confidence, 0.91)

            with patch.object(
                localmodels,
                "_post_json",
                return_value={"answers": {"q": {"passed": True, "confidence": 0.95, "issues": []}}},
            ):
                noul = jev.noul("state", "check rules", ["no secrets"])
            self.assertIsNotNone(noul)
            assert noul is not None
            self.assertTrue(noul.passed)
            self.assertEqual(noul.issues, [])

    def test_mini_pool_chat_and_parallel_map(self) -> None:
        pool = localmodels.MiniPoolClient("http://127.0.0.1:8765")
        with patch.object(localmodels, "_port_open", return_value=True):
            with patch.object(
                localmodels,
                "_post_json",
                side_effect=lambda url, body, timeout: {
                    "choices": [{"message": {"content": f"reply:{body['messages'][-1]['content']}"}}]
                },
            ):
                self.assertEqual(pool.chat("hello"), "reply:hello")
                outs = pool.parallel_map(["a", "b", "c"], max_workers=3)
                self.assertEqual(outs, ["reply:a", "reply:b", "reply:c"])


class TestMiniPoolCli(unittest.TestCase):
    def test_cli_status_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            (state_dir / "mini_pool_tunnels.json").write_text(json.dumps({"8765": 99999999}), encoding="utf-8")
            with (
                patch.object(mini_pool, "resolve_state_dir", return_value=state_dir),
                patch.object(mini_pool, "_port_open", return_value=False),
                patch.object(localmodels, "_port_open", return_value=False),
            ):
                rc = mini_pool.main(["status"])
        self.assertEqual(rc, 0)

    def test_pid_alive_uses_win32_handle_without_os_kill_and_posix_fallback(self) -> None:
        self.assertFalse(mini_pool._pid_alive(0))
        self.assertFalse(mini_pool._pid_alive(-1))
        with (
            patch.object(mini_pool.sys, "platform", "win32"),
            patch("kaizen_kit.mini_pool.os.kill") as mock_kill,
            patch.object(mini_pool, "_win32_pid_alive", side_effect=[True, False]) as mock_w32,
        ):
            self.assertTrue(mini_pool._pid_alive(30788))
            self.assertFalse(mini_pool._pid_alive(99999999))
            mock_kill.assert_not_called()
            self.assertEqual(mock_w32.call_count, 2)
        with patch.object(mini_pool.sys, "platform", "linux"):
            with patch("kaizen_kit.mini_pool.os.kill", side_effect=PermissionError("[Errno 1] Operation not permitted")):
                self.assertTrue(mini_pool._pid_alive(30788))
            with patch("kaizen_kit.mini_pool.os.kill", side_effect=OSError("no such process")):
                self.assertFalse(mini_pool._pid_alive(99999999))

    def test_cli_ask(self) -> None:
        with patch.object(localmodels.MiniPoolClient, "chat", return_value="ok-answer"):
            rc = mini_pool.main(["ask", "ping"])
        self.assertEqual(rc, 0)

    def test_fastmcp_port_in_tunnel_ports(self) -> None:
        self.assertIn(8770, mini_pool.TUNNEL_PORTS)
        self.assertEqual(mini_pool.FASTMCP_PORT, 8770)

    def test_start_tunnels_skips_already_open_local_ports_and_optional_bench_by_default(self) -> None:
        spawned_ports: list[str] = []

        class _FakeProc:
            def __init__(self, pid: int) -> None:
                self.pid = pid

        def _fake_popen(cmd, **kwargs):
            params = json.loads(cmd[cmd.index("--parameters") + 1])
            port = params["localPortNumber"][0]
            spawned_ports.append(port)
            return _FakeProc(50000 + len(spawned_ports))

        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            (state_dir / "mini_pool_tunnels.json").write_text(json.dumps({"24280": 99999999}), encoding="utf-8")
            with (
                patch.object(mini_pool, "resolve_state_dir", return_value=state_dir),
                patch.object(mini_pool, "_port_open", side_effect=lambda url: url.endswith(":24280")),
                patch.object(mini_pool, "_pid_alive", return_value=False),
                patch.object(mini_pool, "_win32_spawn_detached_tunnels", return_value={}),
                patch.object(mini_pool.subprocess, "run") as mock_run,
                patch.object(mini_pool.subprocess, "Popen", side_effect=_fake_popen),
                patch.object(mini_pool.time, "sleep"),
                patch.object(mini_pool.time, "time", side_effect=[0.0, 100.0]),
            ):
                active = mini_pool.start_tunnels("i-demo", "kaizen-u2", "eu-west-1")
            self.assertNotIn("24280", spawned_ports)
            self.assertNotIn("18081", spawned_ports)
            self.assertNotIn("24280", active)
            mock_run.assert_not_called()
            self.assertIn("8770", active)

    def test_win32_spawn_detached_tunnels_parses_wmi_pids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            fake_proc = type("Res", (), {"returncode": 0, "stdout": '{"8765":1111,"8770":2222}'})()
            with (
                patch.object(mini_pool.sys, "platform", "win32"),
                patch.object(mini_pool, "resolve_state_dir", return_value=state_dir),
                patch.object(mini_pool.subprocess, "run", return_value=fake_proc) as mock_run,
            ):
                pids = mini_pool._win32_spawn_detached_tunnels(
                    {8765: ["aws", "ssm"], 8770: ["aws", "ssm"]}
                )
            self.assertEqual(pids, {"8765": 1111, "8770": 2222})
            self.assertTrue((state_dir / "tunnels" / "tunnel_8770.cmd").is_file())
            mock_run.assert_called_once()

    def test_win32_kill_pid_terminates_tree_without_taskkill(self) -> None:
        killed_order: list[int] = []
        with (
            patch.object(mini_pool.sys, "platform", "win32"),
            patch.object(mini_pool, "_pid_alive", return_value=True),
            patch.object(mini_pool, "_win32_collect_descendants", return_value=[300, 200, 100]),
            patch.object(mini_pool, "_win32_terminate_pid", side_effect=killed_order.append),
            patch.object(mini_pool.subprocess, "run") as mock_run,
        ):
            mini_pool._kill_pid(100)
        self.assertEqual(killed_order, [300, 200, 100])
        mock_run.assert_not_called()

    def test_stop_tunnels_kills_alive_pids_and_clears_state(self) -> None:
        killed: list[int] = []
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            (state_dir / "mini_pool_tunnels.json").write_text(
                json.dumps({"8765": 1001, "8770": 1002}),
                encoding="utf-8",
            )
            with (
                patch.object(mini_pool, "resolve_state_dir", return_value=state_dir),
                patch.object(mini_pool, "_pid_alive", side_effect=lambda pid: pid == 1001),
                patch.object(mini_pool, "_kill_pid", side_effect=killed.append),
            ):
                closed = mini_pool.stop_tunnels()
            self.assertEqual(closed, 1)
            self.assertEqual(killed, [1001])
            self.assertEqual(json.loads((state_dir / "mini_pool_tunnels.json").read_text(encoding="utf-8")), {})

    def test_deprecated_pool_model_aliases_normalize_to_active_moe_pool(self) -> None:
        retired = ("qwen2.5-coder-3b", "qwen-coder", "phi-4-mini", "gemma-3", "nemotron-mini", "deepseek-r1-14b")
        for r in retired:
            self.assertNotIn(r, localmodels.AVAILABLE_POOL_MODELS)
            resolved = localmodels.normalize_pool_model(r)
            self.assertIn(resolved, localmodels.AVAILABLE_POOL_MODELS)
        self.assertEqual(localmodels.normalize_pool_model("granite-4.0-h-tiny"), "granite-4-h-tiny")

    def test_lever_a_constrained_decoding_and_schema_validation(self) -> None:
        schema = {
            "type": "object",
            "required": ["verdict", "confidence"],
            "additionalProperties": False,
            "properties": {
                "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
        }
        payload = localmodels.build_constrained_payload(
            json_schema=schema,
            gbnf_grammar="duckdb_sql_select",
            target_engine="vllm",
        )
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(payload["guided_json"], schema)
        self.assertEqual(payload["guided_decoding_backend"], "xgrammar")
        self.assertIn("SELECT ", payload["guided_grammar"])

        ok, parsed, errs = localmodels.validate_against_json_schema('```json\n{"verdict":"PASS","confidence":0.95}\n```', schema)
        self.assertTrue(ok)
        self.assertEqual(errs, [])
        self.assertEqual(parsed, {"verdict": "PASS", "confidence": 0.95})

        bad_ok, bad_parsed, bad_errs = localmodels.validate_against_json_schema('{"verdict":"UNKNOWN","confidence":1.5}', schema)
        self.assertFalse(bad_ok)
        self.assertIsNone(bad_parsed)
        self.assertGreaterEqual(len(bad_errs), 2)

        pool = localmodels.MiniPoolClient("http://127.0.0.1:8765")
        with (
            patch.object(localmodels, "_port_open", return_value=True),
            patch.object(
                localmodels,
                "_post_json",
                return_value={"choices": [{"message": {"content": '{"verdict":"PASS","confidence":0.92}'}}]},
            ) as mock_post,
        ):
            structured = pool.chat_structured("evaluate gate", json_schema=schema, model="phi-4-mini")
        self.assertEqual(structured, {"verdict": "PASS", "confidence": 0.92})
        sent_body = mock_post.call_args.args[1]
        self.assertEqual(sent_body["model"], "qwen3.5-4b")
        self.assertEqual(sent_body["response_format"]["type"], "json_schema")

    def test_lever_b_rrf_hybrid_rag_and_document_ocr_client(self) -> None:
        fused = localmodels.reciprocal_rank_fusion(
            [["sym_a", "sym_b", "sym_c"], ["sym_b", "sym_a", "sym_d"]],
            k=60,
            weights=[1.0, 1.0],
        )
        self.assertEqual(len(fused), 4)
        top_ids = {fused[0][0], fused[1][0]}
        self.assertEqual(top_ids, {"sym_a", "sym_b"})

        with patch.object(localmodels, "_port_open", return_value=False):
            hybrid = localmodels.HybridEmbedRerankClient()
            emb = hybrid.embed_hybrid("VectorCubeSingleflightCoordinator Singleflight")
            self.assertIsNone(emb["dense"])
            self.assertIn("singleflight", emb["sparse"])
            cands = [{"name": "A", "signature": "fn a()"}, {"name": "B", "signature": "fn b()"}]
            self.assertEqual(hybrid.rerank("query", cands, top_k=1), [{"name": "A", "signature": "fn a()"}])

        ocr = localmodels.DocumentOcrClient()
        self.assertEqual(ocr.select_engine("telemetry_dashboard_topology.png").engine, "gemma-4-12b-unified")
        self.assertEqual(ocr.select_engine("scanned_rfc_spec.pdf", doc_type="scanned_pdf").engine, "olmocr-2-7b")
        self.assertEqual(ocr.select_engine("architecture_spec.pdf").engine, "docling-v2")

    def test_stage_to_lora_spec_uses_canonical_5_adapter_portfolio(self) -> None:
        with patch.object(localmodels, "_port_open", return_value=False):
            route = localmodels.route_telemetry_2hop("verify avro allow_keys and vector_cube rollup parity")
        self.assertEqual(route.lora_adapter, "lora-rust-vector-parity")
        self.assertEqual(route.judge_adapter, "lora-system-rca-triage")
        self.assertEqual(route.base_model, "Qwen/Qwen3-Coder-30B-A3B-Instruct")


if __name__ == "__main__":
    unittest.main()
