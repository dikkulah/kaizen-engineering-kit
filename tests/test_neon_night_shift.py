#!/usr/bin/env python3
"""Tests for Neon Night-Shift (`neon_night_shift.py`), residency quarantine, Decision-1/Batch/Flex & CLI."""

from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

from kaizen_kit import cli, gemini_cloud_client as gcc, neon_night_shift as nns


class _NightShiftMockGeminiServer:
    """Mock Gemini REST + EC2 AutoDream loopback server for Night-Shift tests."""

    def __init__(self, *, reject_gate_substring: str = "REJECT_ME") -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.reject_gate_substring = reject_gate_substring
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                parent.calls.append(("GET", self.path, {}))
                if self.path == "/v1beta/batches/night-job-001":
                    payload = {"name": "batches/night-job-001", "state": "JOB_STATE_SUCCEEDED", "stats": {"done": 1}}
                else:
                    payload = {"ok": True}
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                raw_in = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                body = json.loads(raw_in)
                parent.calls.append(("POST", self.path, body))

                if self.path == "/v1beta/decisions":
                    inp = str(body.get("input") or "")
                    rejected = parent.reject_gate_substring in inp
                    answers = {
                        q_id: {"type": "noul", "noul": (0.1 if rejected else 0.94), "confidence": 0.96}
                        for q_id in body.get("questions", {})
                    }
                    payload = {
                        "answers": answers,
                        "usage": {"total_input_tokens": 42, "total_output_tokens": 0, "total_tokens": 42},
                    }
                elif self.path == "/v1beta/cachedContents":
                    payload = {"name": "cachedContents/nightcache001", "model": body.get("model")}
                elif self.path.endswith(":batchGenerateContent"):
                    payload = {"name": "batches/night-job-001", "state": "JOB_STATE_PENDING"}
                elif self.path.endswith(":generateContent"):
                    payload = {
                        "candidates": [{"content": {"role": "model", "parts": [{"text": "Flex overnight synthesis OK"}]}}],
                        "usageMetadata": {"promptTokenCount": 18, "candidatesTokenCount": 5},
                    }
                elif self.path == "/mirix/autodream":
                    payload = {"ok": True, "consolidated": 4, "model": "ling-mini-2.0"}
                else:
                    payload = {"ok": True}

                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_NightShiftMockGeminiServer":
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    @property
    def gemini_base_url(self) -> str:
        port = self.server.server_address[1]
        return f"http://127.0.0.1:{port}/v1beta"

    @property
    def autodream_url(self) -> str:
        port = self.server.server_address[1]
        return f"http://127.0.0.1:{port}/mirix/autodream"


class TestNeonNightShift(unittest.TestCase):
    def test_residency_quarantine_protects_proprietary_and_secrets(self) -> None:
        tasks = [
            nns.NightShiftTask(
                task_id="corp_default",
                title="Proprietary Code Review",
                prompt="Audit VectorCube lock ordering",
                mode="batch",
            ),
            nns.NightShiftTask(
                task_id="misclassified_internal",
                title="Internal Host Leak",
                prompt="Fetch logs from kaizen-prod and kaizen.atlassian.net",
                mode="flex",
                data_classification="public_sanitized",
            ),
            nns.NightShiftTask(
                task_id="secret_leak",
                title="Accidental Token",
                prompt="Summarize token " + ("glpat" + "-abcdef1234567890"),
                mode="decision",
                data_classification="public_sanitized",
            ),
            nns.NightShiftTask(
                task_id="clean_batch",
                title="Clean Public Digest",
                prompt="Compare generic Raft vs Paxos quorum tradeoffs",
                mode="batch",
                data_classification="public_sanitized",
            ),
        ]

        plan = nns.plan_night_shift(tasks, shared_rubric="Compare kaizen-prod rollout")
        self.assertEqual(plan["lanes"]["ec2_local"]["task_count"], 3)
        self.assertEqual(plan["lanes"]["gemini_batch"]["task_count"], 1)
        self.assertEqual(plan["lanes"]["gemini_flex"]["task_count"], 0)
        self.assertEqual(plan["lanes"]["gemini_decision_1"]["task_count"], 0)
        self.assertEqual(plan["quarantined_count"], 3)
        self.assertFalse(plan["context_cache_enabled"])
        self.assertIn("DATA_RESIDENCY_BLOCKED", plan["cache_blocked_reason"])
        self.assertEqual(plan["finops_guard"]["gpu_spot_expected_running"], 0)
        self.assertFalse(plan["finops_guard"]["overnight_polling_allowed"])

    def test_live_three_stage_execution_gating_batch_cache_flex_and_morning(self) -> None:
        tasks = [
            nns.NightShiftTask(
                task_id="ec2_mem",
                title="AutoDream",
                prompt="Consolidate semantic nodes",
                mode="ec2_local",
            ),
            nns.NightShiftTask(
                task_id="dec_screen",
                title="Decision-1 Screen",
                prompt="Evaluate lock-free ring buffer invariants",
                mode="decision",
                data_classification="public_sanitized",
            ),
            nns.NightShiftTask(
                task_id="batch_keep",
                title="Batch Keep",
                prompt="Summarize public Rust async pin safety",
                mode="batch",
                data_classification="public_sanitized",
                model="gemini-3.5-flash-lite",
                decision_gate_instructions="Should this public topic be batched overnight?",
            ),
            nns.NightShiftTask(
                task_id="batch_drop",
                title="Batch Drop",
                prompt="REJECT_ME noisy duplicate topic",
                mode="batch",
                data_classification="public_sanitized",
                decision_gate_instructions="Should this topic be processed?",
            ),
            nns.NightShiftTask(
                task_id="flex_sync",
                title="Flex Synthesis",
                prompt="Synthesize two-phase clean architecture notes",
                mode="flex",
                data_classification="public_sanitized",
                sequential=True,
                decision_gate_instructions="Is sanitized?",
            ),
        ]

        with _NightShiftMockGeminiServer() as srv, tempfile.TemporaryDirectory() as tmp:
            client = gcc.GeminiCloudClient(api_key="mock-key", base_url=srv.gemini_base_url)
            res = nns.run_night_shift(
                tasks,
                execute=True,
                use_context_cache=True,
                shared_rubric="Return concise bullet summaries with clear tradeoffs.",
                state_dir=tmp,
                client=client,
                autodream_url=srv.autodream_url,
            )

            self.assertTrue(res["executed_live"])
            self.assertEqual(res["mode"], "live")
            self.assertEqual(len(res["filtered_out_by_decision_1"]), 1)
            self.assertEqual(res["filtered_out_by_decision_1"][0]["task_id"], "batch_drop")
            self.assertEqual(res["batch_job_names"], ["batches/night-job-001"])
            self.assertEqual(res["batch_submission"]["cache_name"], "cachedContents/nightcache001")
            self.assertEqual(len(res["flex_results"]), 1)
            self.assertEqual(res["flex_results"][0]["text"], "Flex overnight synthesis OK")
            self.assertTrue(res["ec2_local_result"]["triggered"])

            # Check Batch payload never received rejected batch_drop
            batch_posts = [body for _, path, body in srv.calls if path.endswith(":batchGenerateContent")]
            self.assertEqual(len(batch_posts), 1)
            inlined = batch_posts[0]["batch"]["inputConfig"]["requests"]["requests"]
            self.assertEqual([item["metadata"]["key"] for item in inlined], ["batch_keep"])
            self.assertEqual(inlined[0]["request"]["cachedContent"], "cachedContents/nightcache001")

            # Flex call used serviceTier=flex
            flex_posts = [body for _, path, body in srv.calls if path.endswith(":generateContent")]
            self.assertEqual(len(flex_posts), 1)
            self.assertEqual(flex_posts[0]["serviceTier"], "flex")

            # Zero overnight GET polling during run_night_shift
            self.assertFalse(any(method == "GET" for method, _, _ in srv.calls))

            # Morning collection checks batch job once and writes report
            morning = nns.collect_morning_report(execute=True, state_dir=tmp, client=client)
            self.assertTrue(morning["ok"])
            self.assertEqual(morning["batch_count"], 1)
            self.assertEqual(morning["morning_batches"][0]["state"], "JOB_STATE_SUCCEEDED")
            report_text = Path(morning["report_file"]).read_text(encoding="utf-8")
            self.assertIn("JOB_STATE_SUCCEEDED", report_text)
            self.assertIn("Flex overnight synthesis OK", report_text)

    def test_cli_neon_night_shift_dry_run_and_morning_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {"AI_KIT_STATE_DIR": tmp, "GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""},
            ):
                buf1 = io.StringIO()
                with contextlib.redirect_stdout(buf1):
                    rc1 = cli.main(["neon", "night-shift", "--json"])
                self.assertEqual(rc1, 0)
                data1 = json.loads(buf1.getvalue())
                self.assertEqual(data1["orchestrator"], "neon-night-shift")
                self.assertEqual(data1["mode"], "dry_run")
                self.assertEqual(data1["plan"]["lanes"]["ec2_local"]["task_count"], 1)
                self.assertEqual(data1["plan"]["lanes"]["gemini_decision_1"]["task_count"], 1)
                self.assertEqual(data1["plan"]["lanes"]["gemini_batch"]["task_count"], 1)
                self.assertEqual(data1["plan"]["lanes"]["gemini_flex"]["task_count"], 1)

                buf2 = io.StringIO()
                with contextlib.redirect_stdout(buf2):
                    rc2 = cli.main(["neon", "night-shift", "--morning", "--json"])
                self.assertEqual(rc2, 0)
                data2 = json.loads(buf2.getvalue())
                self.assertTrue(data2["ok"])
                self.assertTrue(data2["dry_run"])
                self.assertTrue(Path(data2["report_file"]).is_file())

    def test_best_of_n_lora_synth_distributed_256gb_and_gemini_4_neon_effort(self) -> None:
        tasks = [
            nns.NightShiftTask(
                task_id="bon_rust",
                title="Best-of-N Rust Parity",
                prompt="Implement deterministic VectorCube fold guard clause",
                mode="best_of_n",
                data_classification="corporate_proprietary",
                best_of_n=3,
                verify_cmd="cargo test",
                lora_synthesis=True,
                lora_adapter="lora-avro-c",
            ),
            nns.NightShiftTask(
                task_id="lora_triage",
                title="QLoRA SFT/DPO Triage Pair",
                prompt="Extract UC RCA commit link from sanitized git diff",
                mode="lora_synth",
                data_classification="corporate_proprietary",
                lora_adapter="lora-system-rca-triage",
                allow_distributed_256gb=True,
            ),
            nns.NightShiftTask(
                task_id="neon_flex_ok",
                title="Gemini 4 Neon Adaptive Effort",
                prompt="Compare sparse MoE memory bandwidth tradeoffs",
                mode="flex",
                data_classification="public_sanitized",
                model="gemini-4-neon",
                thinking_level="minimal",
                response_json_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
            ),
            nns.NightShiftTask(
                task_id="neon_flex_quarantine",
                title="Proprietary Gemini 4 Neon Quarantined",
                prompt="Audit kaizen-prod CloudGatewayApi endpoint",
                mode="flex",
                data_classification="public_sanitized",
                model="gemini-4-neon",
                thinking_level="high",
            ),
        ]

        plan = nns.plan_night_shift(tasks)
        self.assertEqual(plan["lanes"]["best_of_n"]["task_count"], 1)
        self.assertEqual(plan["lanes"]["lora_synth"]["task_count"], 1)
        self.assertEqual(plan["lanes"]["gemini_flex"]["task_count"], 1)
        self.assertEqual(plan["quarantined_count"], 1)
        self.assertEqual(plan["finops_guard"]["gpu_spot_expected_running"], 0)
        self.assertEqual(plan["finops_guard"]["gpu_spot_idle_cost_usd"], 0.0)
        self.assertTrue(plan["finops_guard"]["distributed_256gb_enabled"])
        self.assertEqual(plan["finops_guard"]["distributed_256gb_plan"]["tier"], "distributed_256gb")

        with _NightShiftMockGeminiServer() as srv, tempfile.TemporaryDirectory() as tmp:
            client = gcc.GeminiCloudClient(api_key="mock-key", base_url=srv.gemini_base_url)
            res = nns.run_night_shift(
                tasks,
                execute=True,
                state_dir=tmp,
                client=client,
                autodream_url=srv.autodream_url,
            )
            self.assertEqual(len(res["best_of_n_results"]), 1)
            self.assertEqual(res["lora_synth_result"]["gpu_spot_expected_running"], 0)
            self.assertGreaterEqual(res["lora_synth_result"]["record_count"], 1)
            self.assertTrue(Path(res["lora_synth_result"]["jsonl_file"]).is_file())
            self.assertEqual(len(res["flex_results"]), 1)
            flex_posts = [body for _, path, body in srv.calls if path.endswith(":generateContent")]
            self.assertEqual(len(flex_posts), 1)
            gen_cfg = flex_posts[0]["generationConfig"]
            self.assertEqual(gen_cfg["thinkingConfig"]["thinkingLevel"], "MINIMAL")
            self.assertIn("responseJsonSchema", gen_cfg)

    def test_frontier_batch_lanes_openai_and_anthropic(self) -> None:
        tasks = [
            nns.NightShiftTask(
                task_id="openai_task_001",
                title="OpenAI Batch Summarization",
                prompt="Summarize distributed cache invalidation strategies.",
                mode="openai_batch",
                model="gpt-5.4",
                system_instruction="Be concise and return Markdown bullets.",
            ),
            nns.NightShiftTask(
                task_id="anthropic_task_002",
                title="Anthropic Batch Code Review",
                prompt="Review Clean Architecture dependency inversion in Rust.",
                mode="anthropic_batch",
                model="claude-sonnet-5-5",
                max_output_tokens=2048,
            ),
            nns.NightShiftTask(
                task_id="openai_inferred_003",
                title="OpenAI Inferred Batch Task",
                prompt="Analyze lock-free queue benchmarks.",
                mode="batch",
                model="gpt-5",
            ),
        ]
        plan = nns.plan_night_shift(tasks)
        self.assertEqual(plan["lanes"]["openai_batch"]["task_count"], 2)
        self.assertEqual(plan["lanes"]["anthropic_batch"]["task_count"], 1)
        self.assertEqual(plan["lanes"]["gemini_batch"]["task_count"], 0)
        self.assertEqual(len(plan["openai_batch_jsonl_preview"]), 2)
        self.assertEqual(len(plan["anthropic_batch_jsonl_preview"]), 1)

        # Validate OpenAI JSONL structure
        first_openai_line = json.loads(plan["openai_batch_jsonl_preview"][0])
        self.assertEqual(first_openai_line["custom_id"], "openai_task_001")
        self.assertEqual(first_openai_line["method"], "POST")
        self.assertEqual(first_openai_line["url"], "/v1/chat/completions")
        self.assertEqual(first_openai_line["body"]["model"], "gpt-5.4")

        # Validate Anthropic JSONL structure
        first_anthropic_line = json.loads(plan["anthropic_batch_jsonl_preview"][0])
        self.assertEqual(first_anthropic_line["custom_id"], "anthropic_task_002")
        self.assertEqual(first_anthropic_line["params"]["model"], "claude-sonnet-5-5")
        self.assertEqual(first_anthropic_line["params"]["max_tokens"], 2048)


if __name__ == "__main__":
    unittest.main()
