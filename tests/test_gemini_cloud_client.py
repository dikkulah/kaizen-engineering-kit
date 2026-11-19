#!/usr/bin/env python3
"""Tests for GeminiCloudClient, DataResidencyGate, Decision-1, Caching, Batch, Embeddings & CLI."""

from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
import threading
from typing import Any, Dict, List, Optional, Tuple
import unittest
from unittest.mock import patch

from kaizen_kit import gemini_cloud_client as gcc


class _RecordedRequest:
    def __init__(self, method: str, path: str, headers: Dict[str, str], body: bytes) -> None:
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body

    def json_body(self) -> Dict[str, Any]:
        if not self.body:
            return {}
        return json.loads(self.body.decode("utf-8"))


class _MockGeminiServer:
    """Thread-backed local HTTPServer recording wire requests for Gemini REST verification."""

    def __init__(self, resumable_upload: bool = False) -> None:
        self.requests: List[_RecordedRequest] = []
        self.resumable_upload = resumable_upload
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                lower_hdrs = {k.lower(): v for k, v in self.headers.items()}
                parent.requests.append(_RecordedRequest(method, self.path, lower_hdrs, raw))

                resp_headers: Dict[str, str] = {"Content-Type": "application/json"}
                payload: Dict[str, Any] = {"ok": True}

                if self.path == "/v1beta/decisions":
                    req_json = json.loads(raw.decode("utf-8"))
                    answers: Dict[str, Any] = {}
                    for q_id, q_spec in req_json.get("questions", {}).items():
                        qt = q_spec["type"]
                        if qt == "noul":
                            answers[q_id] = {"type": "noul", "noul": 0.92, "confidence": 0.95}
                        elif qt == "choice":
                            first_key = sorted(q_spec["criteria"].keys())[0]
                            answers[q_id] = {
                                "type": "choice",
                                "choice": first_key,
                                "confidence": 0.91,
                                "probabilities": {k: (0.9 if k == first_key else 0.1) for k in q_spec["criteria"]},
                            }
                        else:
                            answers[q_id] = {
                                "type": "score",
                                "score": 1.75,
                                "confidence": 0.88,
                                "probabilities": [0.1, 0.2, 0.7],
                            }
                    payload = {
                        "answers": answers,
                        "usage": {"total_input_tokens": 64, "total_output_tokens": 0, "total_tokens": 64},
                    }
                elif self.path.endswith(":generateContent"):
                    payload = {
                        "candidates": [{"content": {"role": "model", "parts": [{"text": "Sanitized response"}]}}],
                        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 4},
                    }
                elif self.path == "/v1beta/cachedContents" and method == "POST":
                    payload = {"name": "cachedContents/mock001", "model": "models/gemini-4-hydrogen"}
                elif self.path.startswith("/v1beta/cachedContents/") and method == "GET":
                    payload = {"name": self.path.removeprefix("/v1beta/"), "ttl": "3600s"}
                elif self.path.startswith("/v1beta/cachedContents/") and method == "DELETE":
                    payload = {}
                elif self.path == "/v1beta/files" and method == "POST":
                    if parent.resumable_upload and lower_hdrs.get("x-goog-upload-command") == "start":
                        port = parent.server.server_address[1]
                        resp_headers["x-goog-upload-url"] = f"http://127.0.0.1:{port}/upload-session/s1"
                        payload = {}
                    else:
                        payload = {"file": {"name": "files/batch001", "mimeType": "application/jsonl"}}
                elif self.path == "/upload-session/s1" and method == "POST":
                    payload = {"file": {"name": "files/batch_resumable_001", "sizeBytes": str(len(raw))}}
                elif self.path.endswith(":batchGenerateContent") and method == "POST":
                    payload = {"name": "batches/job001", "state": "JOB_STATE_PENDING"}
                elif self.path == "/v1beta/batches/job001" and method == "GET":
                    payload = {"name": "batches/job001", "state": "JOB_STATE_SUCCEEDED"}
                elif self.path == "/v1beta/batches/job001:cancel" and method == "POST":
                    payload = {"name": "batches/job001", "state": "JOB_STATE_CANCELLED"}
                elif self.path.endswith(":embedContent") and method == "POST":
                    payload = {"embedding": {"values": [0.1, 0.2, 0.3]}}

                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                for hk, hv in resp_headers.items():
                    self.send_header(hk, hv)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def do_DELETE(self) -> None:  # noqa: N802
                self._handle("DELETE")

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_MockGeminiServer":
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    @property
    def base_url(self) -> str:
        port = self.server.server_address[1]
        return f"http://127.0.0.1:{port}/v1beta"


class TestGeminiCloudClient(unittest.TestCase):
    def _synthetic_env_token(self) -> str:
        return "unit-" + "mock-" + "token"

    def test_verified_models_and_deprecated_normalization(self) -> None:
        expected = {
            "gemini-4-neon",
            "gemini-4-carbon",
            "gemini-4-argon",
            "gemini-4-hydrogen",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemma-4-31b-it",
            "gemma-4-26b-a4b-it",
            "gemini-decision-1",
            "gemini-embedding-2",
            "gemini-embedding-001",
        }
        self.assertTrue(expected.issubset(gcc.GEMINI_MODELS.keys()))

        self.assertEqual(gcc.resolve_adaptive_thinking("gemini-4-neon", "minimal"), ("MINIMAL", None))
        self.assertEqual(gcc.resolve_adaptive_thinking("gemini-4-neon", "high"), ("HIGH", None))
        self.assertEqual(gcc.resolve_adaptive_thinking("gemma-4-26b-a4b-it", "medium"), (None, 1024))
        self.assertEqual(gcc.resolve_adaptive_thinking("gemini-decision-1", "high"), (None, None))
        with self.assertRaises(ValueError):
            gcc.resolve_adaptive_thinking("gemini-4-neon", effort_or_level="high", thinking_budget=512)

        cases: List[Tuple[str, Tuple[str, Optional[str]]]] = [
            ("gemini-2.5-flash", ("gemini-4-hydrogen", "gemini-2.5-flash")),
            ("gemini-2.5-pro", ("gemini-4-hydrogen", "gemini-2.5-pro")),
            ("gemini-2.0-flash-lite", ("gemini-4-hydrogen", "gemini-2.0-flash-lite")),
            ("gemini-1.5-pro", ("gemini-4-hydrogen", "gemini-1.5-pro")),
            ("models/gemini-flash-latest", ("gemini-4-hydrogen", "gemini-flash-latest")),
            ("gemini-4-carbon", ("gemini-4-carbon", None)),
            ("gemma-4-31b-it", ("gemma-4-31b-it", None)),
        ]
        for raw, wanted in cases:
            self.assertEqual(gcc.normalize_gemini_model(raw), wanted)

        self.assertEqual(
            gcc.normalize_gemini_model("gemini-2.5-flash", allow_raw_alias=True),
            ("gemini-2.5-flash", None),
        )

    def test_data_residency_gate_fail_closed_and_leak_blocks(self) -> None:
        # 1. Default classification fails closed
        with self.assertRaises(PermissionError) as ctx_def:
            gcc.enforce_data_residency("Hello sanitized prompt")
        self.assertIn("DATA_RESIDENCY_BLOCKED", str(ctx_def.exception))

        # 2. Other non-public classifications fail closed
        for cls_name in ("internal", "confidential", ""):
            with self.assertRaises(PermissionError):
                gcc.DataResidencyGate.enforce("Hello", data_classification=cls_name)

        # 3. Secrets constructed dynamically (zero raw literal secrets in test source)
        secret_samples = [
            "AKIA" + "0123456789ABCDEF",
            "ASIA" + "FEDCBA9876543210",
            "glpat-" + "abcDEF1234567890",
            "AQ." + "Ab8" + "SampleToken123",
            "AIza" + "SySample" + "1234567890",
            "-----BEGIN RSA " + "PRIVATE KEY-----",
        ]
        for secret in secret_samples:
            with self.assertRaises(PermissionError) as ctx_sec:
                gcc.enforce_data_residency(f"debug payload {secret}", data_classification="public_sanitized")
            self.assertIn("DATA_RESIDENCY_BLOCKED", str(ctx_sec.exception))

        # 4. Internal corporate identifiers blocked even under public_sanitized
        corp_samples = [
            "kaizen-prod",
            "kaizen-staging",
            "111122223333",
            "444455556666",
            "kaizen.atlassian.net",
            "gitlab.internal.example",
        ]
        for ident in corp_samples:
            with self.assertRaises(PermissionError) as ctx_corp:
                gcc.enforce_data_residency(f"Target host {ident}", data_classification="public_sanitized")
            self.assertIn("DATA_RESIDENCY_BLOCKED", str(ctx_corp.exception))

        # 5. Guardrail rail triggers on destructive command
        with self.assertRaises(PermissionError):
            gcc.enforce_data_residency("rm -rf /", data_classification="public_sanitized")

        # 6. Clean public_sanitized passes
        gcc.enforce_data_residency("Analyze generic sorting complexity", data_classification="public_sanitized")

    def test_decision_1_noul_choice_score_and_checklist(self) -> None:
        with _MockGeminiServer() as srv, patch.dict(os.environ, {"GEMINI_API_KEY": self._synthetic_env_token()}, clear=True):
            client = gcc.GeminiCloudClient(base_url=srv.base_url)

            # Default corporate_proprietary must block before HTTP
            with self.assertRaises(PermissionError):
                client.decide_noul("code", "Is valid?")
            self.assertEqual(len(srv.requests), 0)

            noul_res = client.decide_noul(
                "def add(a, b): return a + b",
                "Is this function pure?",
                true_description="No side effects",
                false_description="Mutates external state",
                data_classification="public_sanitized",
            )
            self.assertEqual(noul_res["usage"]["total_output_tokens"], 0)
            self.assertAlmostEqual(noul_res["answers"]["decision"]["noul"], 0.92)

            choice_res = client.decide_choice(
                "Connection reset by peer",
                "Select subsystem",
                {"net": "Networking error", "disk": "Storage error"},
                data_classification="public_sanitized",
            )
            self.assertIn(choice_res["answers"]["choice"]["choice"], ("disk", "net"))

            score_res = client.decide_score(
                "Null dereference on startup",
                "Rate severity",
                ["Low impact", "Workaround exists", "Crash on boot"],
                data_classification="public_sanitized",
            )
            self.assertEqual(score_res["answers"]["score"]["score"], 1.75)

            checklist_res = client.decide_code_review_checklist(
                "def safe_div(a, b):\n    if b == 0:\n        return 0\n    return a / b\n",
                [
                    "Uses guard clause for zero divisor",
                    {"id": "no_print", "type": "noul", "instructions": "Avoids stdout printing"},
                ],
                data_classification="public_sanitized",
            )
            self.assertIn("rule_001", checklist_res["answers"])
            self.assertIn("no_print", checklist_res["answers"])

            # Validation bounds (0 questions, >1000 questions, <2 choice options)
            with self.assertRaises(ValueError):
                client.decide("abc", {}, data_classification="public_sanitized")
            too_many = {f"q{i}": {"type": "noul", "instructions": "ok?"} for i in range(1001)}
            with self.assertRaises(ValueError):
                client.decide("abc", too_many, data_classification="public_sanitized")
            with self.assertRaises(ValueError):
                client.decide_choice("abc", "Pick", {"only_one": "single"}, data_classification="public_sanitized")

            self.assertEqual(srv.requests[0].headers.get("x-goog-api-key"), self._synthetic_env_token())
            self.assertEqual(srv.requests[0].path, "/v1beta/decisions")

    def test_generate_wire_format_flex_cache_thinking_schema_tools(self) -> None:
        with _MockGeminiServer() as srv, patch.dict(os.environ, {"GOOGLE_API_KEY": self._synthetic_env_token()}, clear=True):
            client = gcc.GeminiCloudClient(base_url=srv.base_url)
            schema = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
            fn_decl = [{"name": "get_metric", "description": "Fetch metric", "parameters": {"type": "object"}}]

            res = client.generate(
                "Summarize architecture",
                model="gemini-2.5-pro",  # deprecated -> gemini-4-hydrogen
                system_instruction="Be concise",
                service_tier="flex",
                thinking_budget=1024,
                include_thoughts=True,
                cached_content="cachedContents/abc123",
                response_json_schema=schema,
                enable_google_search=True,
                enable_code_execution=True,
                enable_url_context=True,
                function_declarations=fn_decl,
                temperature=0.1,
                max_output_tokens=512,
                data_classification="public_sanitized",
            )
            self.assertIn("candidates", res)
            self.assertEqual(len(srv.requests), 1)
            rec = srv.requests[0]
            self.assertEqual(rec.path, "/v1beta/models/gemini-4-hydrogen:generateContent")
            self.assertEqual(rec.headers.get("x-goog-api-key"), self._synthetic_env_token())

            body = rec.json_body()
            self.assertEqual(body["cachedContent"], "cachedContents/abc123")
            self.assertEqual(body["serviceTier"], "flex")
            self.assertEqual(body["systemInstruction"], {"parts": [{"text": "Be concise"}]})
            self.assertIn({"googleSearch": {}}, body["tools"])
            self.assertIn({"codeExecution": {}}, body["tools"])
            self.assertIn({"urlContext": {}}, body["tools"])
            self.assertIn({"functionDeclarations": fn_decl}, body["tools"])

            gen_cfg = body["generationConfig"]
            self.assertNotIn("cachedContent", gen_cfg)
            self.assertNotIn("serviceTier", gen_cfg)
            self.assertEqual(gen_cfg["responseMimeType"], "application/json")
            self.assertEqual(gen_cfg["responseJsonSchema"], schema)
            self.assertEqual(gen_cfg["thinkingConfig"], {"thinkingBudget": 1024, "includeThoughts": True})
            self.assertEqual(gen_cfg["temperature"], 0.1)
            self.assertEqual(gen_cfg["maxOutputTokens"], 512)

            # Mutually exclusive thinking_budget + thinking_level must raise ValueError
            with self.assertRaises(ValueError):
                client.generate(
                    "Test",
                    thinking_budget=256,
                    thinking_level="HIGH",
                    data_classification="public_sanitized",
                    dry_run=True,
                )

            # thinking_level normalized to uppercase in thinkingConfig
            lvl_plan = client.generate(
                "Test level",
                thinking_level="high",
                data_classification="public_sanitized",
                dry_run=True,
            )
            self.assertEqual(lvl_plan["payload"]["generationConfig"]["thinkingConfig"], {"thinkingLevel": "HIGH"})

            # Fail-closed default blocks generate/create_cache/upload/submit/embed before HTTP
            with self.assertRaises(PermissionError):
                client.generate("Blocked")
            with self.assertRaises(PermissionError):
                client.create_cache(contents="Blocked")
            with self.assertRaises(PermissionError):
                client.upload_jsonl_file('{"key":"k1"}', display_name="blocked")
            with self.assertRaises(PermissionError):
                client.submit_batch_file("gemini-4-hydrogen", "files/x", display_name="blocked")
            with self.assertRaises(PermissionError):
                client.embed("Blocked")

        # Missing API key blocks live HTTP calls while dry_run succeeds
        with _MockGeminiServer() as srv, patch.dict(os.environ, {}, clear=True):
            no_key_client = gcc.GeminiCloudClient(base_url=srv.base_url)
            self.assertFalse(no_key_client.has_api_key())
            dry_res = no_key_client.generate("Dry", data_classification="public_sanitized", dry_run=True)
            self.assertTrue(dry_res["dry_run"])
            with self.assertRaises(RuntimeError):
                no_key_client.generate("Live", data_classification="public_sanitized", dry_run=False)
            self.assertEqual(len(srv.requests), 0)

    def test_direct_single_post_file_upload(self) -> None:
        with _MockGeminiServer(resumable_upload=False) as srv, patch.dict(
            os.environ, {"GEMINI_API_KEY": self._synthetic_env_token()}, clear=True
        ):
            client = gcc.GeminiCloudClient(base_url=srv.base_url)
            jsonl = client.build_batch_jsonl([{"key": "d1", "contents": "Sanitized item"}])
            res = client.upload_jsonl_file(jsonl, display_name="direct", data_classification="public_sanitized")
            self.assertEqual(res["file"]["name"], "files/batch001")
            self.assertEqual([(r.method, r.path) for r in srv.requests], [("POST", "/v1beta/files")])

    def test_caching_batch_jsonl_inline_file_upload_poll_cancel_and_embed(self) -> None:
        with _MockGeminiServer(resumable_upload=True) as srv, patch.dict(
            os.environ, {"GEMINI_API_KEY": self._synthetic_env_token()}, clear=True
        ):
            client = gcc.GeminiCloudClient(base_url=srv.base_url)

            # Gemini 4 blocks explicit cachedContents (Implicit Caching only); Gemini 3.x succeeds
            with self.assertRaises(ValueError) as ctx_g4_cache:
                client.create_cache(
                    model="gemini-4-hydrogen",
                    contents="Shared sanitized reference manual",
                    data_classification="public_sanitized",
                )
            self.assertIn("EXPLICIT_CACHE_UNSUPPORTED", str(ctx_g4_cache.exception))

            created = client.create_cache(
                model="gemini-3.5-flash-lite",
                contents="Shared sanitized reference manual",
                system_instruction="Use manual",
                ttl="1800s",
                display_name="manual-cache",
                data_classification="public_sanitized",
            )
            self.assertEqual(created["name"], "cachedContents/mock001")
            got = client.get_cache("cachedContents/mock001")
            self.assertEqual(got["ttl"], "3600s")
            deleted = client.delete_cache("mock001")
            self.assertEqual(deleted, {})

            # CAPC ordering, count_tokens pre-budget routing & cache telemetry extraction
            capc = gcc.build_capc_ordered_prompt("P" * 16500, "volatile diff", "suffix hook")
            self.assertEqual(capc["section_order"], ["static_prefix", "dynamic_context", "dynamic_hooks"])
            self.assertTrue(capc["implicit_cache_eligible"])
            self.assertTrue(capc["prompt"].startswith("P" * 100))
            self.assertTrue(capc["prompt"].endswith("suffix hook"))

            pub_route = gcc.check_token_budget_and_route(
                "x" * 200000, model="gemini-4-neon", max_sync_tokens=32768, data_classification="public_sanitized"
            )
            self.assertTrue(pub_route["over_budget"])
            self.assertEqual(pub_route["recommended_route"], "gemini_batch")
            corp_route = gcc.check_token_budget_and_route(
                "x" * 200000, model="gemini-4-neon", max_sync_tokens=32768, data_classification="corporate_proprietary"
            )
            self.assertEqual(corp_route["recommended_route"], "ec2_local")

            tel = gcc.extract_cache_usage_telemetry(
                {"usageMetadata": {"promptTokenCount": 5000, "cachedContentTokenCount": 4000, "candidatesTokenCount": 120}}
            )
            self.assertEqual(tel["prompt_token_count"], 5000)
            self.assertEqual(tel["total_cached_tokens"], 4000)
            self.assertEqual(tel["cache_hit_ratio"], 0.8)

            # JSONL batch builder wire placement
            jsonl = client.build_batch_jsonl(
                [{"key": "item-a", "contents": "Explain merge sort"}],
                cached_content="cachedContents/mock001",
                thinking_budget=512,
                response_json_schema={"type": "object"},
            )
            row = json.loads(jsonl.splitlines()[0])
            self.assertEqual(row["key"], "item-a")
            self.assertEqual(row["request"]["cachedContent"], "cachedContents/mock001")
            self.assertNotIn("cachedContent", row["request"]["generationConfig"])
            self.assertEqual(row["request"]["generationConfig"]["thinkingConfig"]["thinkingBudget"], 512)
            self.assertEqual(row["request"]["generationConfig"]["responseJsonSchema"], {"type": "object"})

            # Inline batch (<20 MB) + >20 MB guard
            inline_res = client.submit_batch_inline(
                "gemini-4-argon",
                [{"key": "k1", "contents": "Check invariants"}],
                display_name="inline-demo",
                data_classification="public_sanitized",
            )
            self.assertEqual(inline_res["name"], "batches/job001")

            huge_text = "x" * (20 * 1024 * 1024)
            with self.assertRaises(ValueError) as ctx_huge:
                client.submit_batch_inline(
                    "gemini-4-argon",
                    [{"contents": huge_text}],
                    display_name="too-big",
                    data_classification="public_sanitized",
                )
            self.assertIn("20 MB", str(ctx_huge.exception))

            # Resumable file upload + batch file submit + get/cancel
            uploaded = client.upload_jsonl_file(
                jsonl,
                display_name="batch-input",
                data_classification="public_sanitized",
            )
            self.assertEqual(uploaded["file"]["name"], "files/batch_resumable_001")

            file_batch = client.submit_batch_file(
                "gemini-4-hydrogen",
                uploaded["file"]["name"],
                display_name="file-batch-demo",
                data_classification="public_sanitized",
            )
            self.assertEqual(file_batch["name"], "batches/job001")
            status_res = client.get_batch("batches/job001")
            self.assertEqual(status_res["state"], "JOB_STATE_SUCCEEDED")
            cancel_res = client.cancel_batch("job001")
            self.assertEqual(cancel_res["state"], "JOB_STATE_CANCELLED")

            # Embeddings
            emb = client.embed(
                "Vectorize sanitized summary",
                model="gemini-embedding-2",
                task_type="RETRIEVAL_DOCUMENT",
                title="Summary",
                output_dimensionality=768,
                data_classification="public_sanitized",
            )
            self.assertEqual(emb["embedding"]["values"], [0.1, 0.2, 0.3])

            paths = [(r.method, r.path) for r in srv.requests]
            self.assertIn(("POST", "/v1beta/cachedContents"), paths)
            self.assertIn(("GET", "/v1beta/cachedContents/mock001"), paths)
            self.assertIn(("DELETE", "/v1beta/cachedContents/mock001"), paths)
            self.assertIn(("POST", "/v1beta/models/gemini-4-argon:batchGenerateContent"), paths)
            self.assertIn(("POST", "/v1beta/files"), paths)
            self.assertIn(("POST", "/upload-session/s1"), paths)
            self.assertIn(("GET", "/v1beta/batches/job001"), paths)
            self.assertIn(("POST", "/v1beta/batches/job001:cancel"), paths)
            self.assertIn(("POST", "/v1beta/models/gemini-embedding-2:embedContent"), paths)

    def test_cli_dry_run_and_self_test(self) -> None:
        self.assertEqual(gcc.run_self_test(), 0)

        with patch.dict(os.environ, {}, clear=True):
            # status --json
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = gcc.main(["status", "--json"])
            self.assertEqual(rc, 0)
            status_obj = json.loads(buf.getvalue())
            self.assertFalse(status_obj["api_key_configured"])
            self.assertEqual(status_obj["model_count"], 11)

            # response_json_schema corporate leak blocked by DataResidencyGate
            with self.assertRaises(PermissionError):
                gcc.GeminiCloudClient(api_key="").generate(
                    "Clean prompt",
                    model="gemini-4-neon",
                    response_json_schema={"type": "object", "description": "host kaizen-prod"},
                    data_classification="public_sanitized",
                    dry_run=True,
                )

            # batch-plan with --model gemini-4-neon --thinking-level HIGH
            buf_neon_batch = io.StringIO()
            with contextlib.redirect_stdout(buf_neon_batch):
                rc_nb = gcc.main([
                    "batch-plan",
                    "--model",
                    "gemini-4-neon",
                    "--prompt",
                    "Sanitized check",
                    "--thinking-level",
                    "HIGH",
                    "--tier",
                    "flex",
                    "--data-classification",
                    "public_sanitized",
                    "--json",
                ])
            self.assertEqual(rc_nb, 0)
            nb_obj = json.loads(buf_neon_batch.getvalue())
            self.assertEqual(nb_obj["model"], "gemini-4-neon")
            nb_req = json.loads(nb_obj["jsonl_lines"][0])["request"]
            self.assertEqual(nb_req["generationConfig"]["thinkingConfig"], {"thinkingLevel": "HIGH"})

            # generate dry-run blocked by default data_classification
            buf_blk = io.StringIO()
            with contextlib.redirect_stdout(buf_blk):
                rc_blk = gcc.main(["generate", "--prompt", "hi", "--dry-run", "--json"])
            self.assertEqual(rc_blk, 2)
            self.assertTrue(json.loads(buf_blk.getvalue())["blocked"])

            # generate dry-run allowed with public_sanitized + --tier flex
            buf_gen = io.StringIO()
            with contextlib.redirect_stdout(buf_gen):
                rc_gen = gcc.main([
                    "generate",
                    "--prompt",
                    "Review pure helper",
                    "--dry-run",
                    "--tier",
                    "flex",
                    "--data-classification",
                    "public_sanitized",
                    "--json",
                ])
            self.assertEqual(rc_gen, 0)
            gen_obj = json.loads(buf_gen.getvalue())
            self.assertTrue(gen_obj["dry_run"])
            self.assertEqual(gen_obj["payload"]["serviceTier"], "flex")

            # decide dry-run
            buf_dec = io.StringIO()
            with contextlib.redirect_stdout(buf_dec):
                rc_dec = gcc.main([
                    "decide",
                    "--input",
                    "def f(x): return x + 1",
                    "--question-type",
                    "noul",
                    "--instructions",
                    "Is deterministic?",
                    "--dry-run",
                    "--data-classification",
                    "public_sanitized",
                    "--json",
                ])
            self.assertEqual(rc_dec, 0)
            self.assertEqual(json.loads(buf_dec.getvalue())["expected_output_tokens"], 0)

            # cache-plan + batch-plan + self-test
            for sub_argv in (
                ["cache-plan", "--data-classification", "public_sanitized", "--json"],
                ["batch-plan", "--prompt", "A", "--prompt", "B", "--tier", "flex", "--data-classification", "public_sanitized", "--json"],
                ["self-test", "--json"],
            ):
                b = io.StringIO()
                with contextlib.redirect_stdout(b):
                    rc_sub = gcc.main(sub_argv)
                self.assertEqual(rc_sub, 0)
                self.assertIsInstance(json.loads(b.getvalue()), dict)


if __name__ == "__main__":
    unittest.main()
