#!/usr/bin/env python3
"""Unit tests for Unified ModelRouter (`model_router.py`) and `sdk_orchestrator` wiring."""

from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from kaizen_kit import bedrock_bridge, gemini_cloud_client, model_router, sdk_orchestrator


def _start_oss_mock_server() -> tuple[HTTPServer, str, List[Dict[str, Any]]]:
    received: List[Dict[str, Any]] = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            received.append({"path": self.path, "body": body})
            resp = {
                "id": "chatcmpl-test",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": f"oss:{body['model']}"}}],
            }
            raw = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}", received


class TestModelRouter(unittest.TestCase):
    def test_bedrock_claude_5_5_aliases_route_to_local_bedrock(self) -> None:
        router = model_router.ModelRouter()
        for alias, expected in (
            ("haiku", "claude-haiku-5-5"),
            ("claude-haiku-5-5", "claude-haiku-5-5"),
            ("sonnet", "claude-sonnet-5-5"),
            ("claude-sonnet-5-5", "claude-sonnet-5-5"),
            ("opus", "claude-opus-5-5"),
            ("claude-opus-5-5", "claude-opus-5-5"),
            ("fable", "claude-fable-5"),
            ("claude-fable-5", "claude-fable-5"),
        ):
            target = router.resolve(alias)
            self.assertEqual(target.backend, "bedrock")
            self.assertEqual(target.canonical_model, expected)
            self.assertTrue(target.requires_local_aws_profile)

    def test_oss_aliases_route_to_ec2_litellm_or_gateway(self) -> None:
        with patch.object(model_router, "_port_open", return_value=True):
            router = model_router.ModelRouter()
            # Active Sparse-MoE & micro aliases
            for alias, canonical in (
                ("qwen3.5-4b", "qwen3.5-4b"),
                ("bitnet", "bitnet-2b"),
                ("bitnet-2b", "bitnet-2b"),
                ("qwen3-30b-a3b", "qwen3-coder-30b-a3b"),
                ("qwen3.5-35b-a3b", "qwen3.5-35b-a3b"),
                ("glm-4.7-flash", "glm-4.7-flash"),
                ("ling-mini-2.0", "ling-mini-2.0"),
                ("gemma-4-26b-a4b", "gemma-4-26b-a4b"),
                ("gemma-4-12b-unified", "gemma-4-12b-unified"),
                ("granite-4-h-tiny", "granite-4-h-tiny"),
                ("granite-4.0-h-tiny", "granite-4-h-tiny"),
                ("qwen3-guard-stream-0.6b", "qwen3-guard-stream-0.6b"),
            ):
                target = router.resolve(alias)
                self.assertEqual(target.backend, "ec2_litellm")
                self.assertEqual(target.canonical_model, canonical)
                self.assertFalse(target.requires_local_aws_profile)

            # Deprecated CPU dense aliases redirect cleanly & never pollute list_models()
            for dep_alias, expected_redirect in (
                ("qwen3b", "qwen3.5-4b"),
                ("qwen2.5-coder-3b", "qwen3.5-4b"),
                ("qwen-coder", "qwen3-coder-30b-a3b"),
                ("phi4", "qwen3.5-4b"),
                ("phi-4-mini", "qwen3.5-4b"),
                ("phi-4-reasoning-plus", "qwen3.5-35b-a3b"),
                ("gemma3", "gemma-4-e2b"),
                ("nemotron", "granite-4-h-tiny"),
                ("deepseek-r1-14b", "qwen3.5-35b-a3b"),
            ):
                self.assertNotIn(dep_alias, model_router.OSS_ALIASES)
                target = router.resolve(dep_alias)
                self.assertEqual(target.canonical_model, expected_redirect)

            listed_ids = {entry["id"] for entry in router.list_models()}
            for retired_id in ("qwen2.5-coder-3b", "phi-4-mini", "gemma-3", "nemotron-mini", "gemini-flash-latest"):
                self.assertNotIn(retired_id, listed_ids)

        with patch.object(model_router, "_port_open", return_value=False):
            target = model_router.ModelRouter().resolve("phi4")
            self.assertEqual(target.backend, "ec2_gateway")
            self.assertEqual(target.canonical_model, "qwen3.5-4b")

    def test_non_loopback_rejected(self) -> None:
        with self.assertRaises(ValueError):
            model_router.ModelRouter(litellm_url="http://192.168.1.50:4000")
        with self.assertRaises(ValueError):
            model_router.ModelRouter(gateway_url="https://api.openai.com")
        with self.assertRaises(ValueError):
            model_router.ModelRouter(litellm_url="file://127.0.0.1/tmp")
        with self.assertRaises(ValueError):
            model_router.ModelRouter(bridge_url="ftp://localhost:18889")

    def test_local_bedrock_isolation_guard(self) -> None:
        router = model_router.ModelRouter()
        with patch.dict(os.environ, {"KAIZEN_DISABLE_LOCAL_BEDROCK": "1"}):
            with self.assertRaises(RuntimeError) as ctx:
                router.chat_text("hello", model="sonnet")
            self.assertIn("isolated", str(ctx.exception).lower())

    def test_invoke_bedrock_and_oss_loopback_server(self) -> None:
        srv, base_url, received = _start_oss_mock_server()
        try:
            router = model_router.ModelRouter(litellm_url=base_url, gateway_url="http://127.0.0.1:59997")
            oss_reply = router.chat_text(
                "ping",
                system="sys",
                model="qwen3.5-4b",
                json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
            )
            self.assertEqual(oss_reply, "oss:qwen3.5-4b")
            self.assertEqual(received[0]["path"], "/v1/chat/completions")
            self.assertEqual(received[0]["body"]["model"], "qwen3.5-4b")
            self.assertEqual(received[0]["body"]["response_format"]["type"], "json_schema")
            self.assertTrue(received[0]["body"]["response_format"]["json_schema"]["strict"])

            with patch.object(
                bedrock_bridge,
                "invoke_bedrock",
                return_value={"choices": [{"message": {"content": "bedrock:claude-opus-5-5"}}]},
            ) as mock_bedrock:
                reply = router.chat_text("audit", model="opus")
                self.assertEqual(reply, "bedrock:claude-opus-5-5")
                mock_bedrock.assert_called_once()
                self.assertEqual(mock_bedrock.call_args.kwargs["model_alias"], "claude-opus-5-5")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_sdk_orchestrator_uses_model_router(self) -> None:
        srv, base_url, _ = _start_oss_mock_server()
        try:
            custom_router = model_router.ModelRouter(litellm_url=base_url, gateway_url="http://127.0.0.1:59997")
            with patch.object(model_router, "get_default_router", return_value=custom_router):
                rc, out = sdk_orchestrator._invoke_specialist_text("systems-architect", "phi4", "check")
            self.assertEqual(rc, 0)
            self.assertEqual(out, "oss:qwen3.5-4b")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_sdk_orchestrator_blocks_claude_fallback_when_local_bedrock_disabled(self) -> None:
        with (
            patch.dict(os.environ, {"KAIZEN_DISABLE_LOCAL_BEDROCK": "1"}),
            patch.object(sdk_orchestrator.shutil, "which", return_value="claude"),
            patch.object(sdk_orchestrator.subprocess, "run") as mock_run,
        ):
            self.assertIsNone(sdk_orchestrator._run_claude_cli_fallback("hello", "sys"))
            rc, out = sdk_orchestrator._invoke_specialist_text("systems-architect", "sonnet", "hello")
        self.assertNotEqual(rc, 0)
        self.assertIn("disabled", out.lower())
        mock_run.assert_not_called()

    def test_oss_http_5xx_falls_back_from_litellm_to_gateway(self) -> None:
        class _Fail500Handler(BaseHTTPRequestHandler):
            requests = 0

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 0:
                    self.rfile.read(length)
                _Fail500Handler.requests += 1
                raw = b'{"error":"litellm overloaded"}'
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        fail_srv = HTTPServer(("127.0.0.1", 0), _Fail500Handler)
        fail_thread = threading.Thread(target=fail_srv.serve_forever, daemon=True)
        fail_thread.start()
        fail_url = f"http://127.0.0.1:{fail_srv.server_address[1]}"
        ok_srv, ok_url, received = _start_oss_mock_server()
        try:
            router = model_router.ModelRouter(litellm_url=fail_url, gateway_url=ok_url)
            reply = router.chat_text("fallback test", model="qwen3.5-4b")
            self.assertEqual(reply, "oss:qwen3.5-4b")
            self.assertEqual(len(received), 1)
            self.assertEqual(_Fail500Handler.requests, 1)

            both_fail = model_router.ModelRouter(litellm_url=fail_url, gateway_url=fail_url)
            with self.assertRaises(RuntimeError) as ctx:
                both_fail.chat_text("fail both", model="qwen3.5-4b")
            self.assertIn("Upstream HTTP 500", str(ctx.exception))
            self.assertEqual(_Fail500Handler.requests, 3)
        finally:
            fail_srv.shutdown()
            fail_srv.server_close()
            ok_srv.shutdown()
            ok_srv.server_close()

    def test_ephemeral_gpu_burst_backend_open_and_closed(self) -> None:
        srv, burst_url, received = _start_oss_mock_server()
        try:
            router = model_router.ModelRouter(burst_url=burst_url)
            target = router.resolve("qwen3-32b-awq")
            self.assertEqual(target.backend, "ephemeral_gpu_burst")
            self.assertEqual(target.canonical_model, "Qwen/Qwen3-32B-AWQ")
            self.assertFalse(target.requires_local_aws_profile)
            out = router.chat_text("review diff", model="qwen3-32b-awq")
            self.assertEqual(out, "oss:Qwen/Qwen3-32B-AWQ")
            self.assertEqual(received[0]["body"]["model"], "Qwen/Qwen3-32B-AWQ")
        finally:
            srv.shutdown()
            srv.server_close()

        closed_router = model_router.ModelRouter(burst_url="http://127.0.0.1:59996")
        with self.assertRaises(RuntimeError) as ctx:
            closed_router.chat_text("review diff", model="qwen3-32b-awq")
        self.assertIn("STOPPED", str(ctx.exception))

    def test_gemini_aliases_fail_closed_and_flex_dispatch(self) -> None:
        expected_aliases = {
            "gemini-4-neon": "gemini-4-neon",
            "gemini-neon": "gemini-4-neon",
            "gemini-4-carbon": "gemini-4-carbon",
            "gemini-4-argon": "gemini-4-argon",
            "gemini-4-hydrogen": "gemini-4-hydrogen",
            "gemini-3.5-flash-lite": "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite": "gemini-3.1-flash-lite",
            "gemma-4-31b-it": "gemma-4-31b-it",
            "gemma-4-26b-a4b-it": "gemma-4-26b-a4b-it",
            "gemini-decision-1": "gemini-decision-1",
            "gemini-flex": "gemini-4-hydrogen",
        }
        self.assertEqual(model_router.GEMINI_ALIASES, expected_aliases)
        router = model_router.ModelRouter()
        for alias, canonical in expected_aliases.items():
            target = router.resolve(alias)
            self.assertEqual(target.backend, "gemini_public_api")
            self.assertEqual(target.canonical_model, canonical)
            self.assertEqual(target.endpoint, "https://generativelanguage.googleapis.com/v1beta")
            self.assertEqual(target.provider_hint, "google-gemini-developer-api")
            self.assertFalse(target.requires_local_aws_profile)

        # Default corporate_proprietary MUST fail closed before calling GeminiCloudClient.generate
        mock_client = MagicMock()
        with patch.object(gemini_cloud_client, "GeminiCloudClient", return_value=mock_client):
            with self.assertRaises(PermissionError) as ctx_block:
                router.chat_text("Summarize public algorithm", model="gemini-4-neon")
            self.assertIn("DATA_RESIDENCY_BLOCKED", str(ctx_block.exception))
            mock_client.generate.assert_not_called()

            mock_client.generate.return_value = {
                "candidates": [{"content": {"role": "model", "parts": [{"text": "gemini:flex:ok"}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3},
            }
            # Deprecated gemini-flash-latest still normalizes cleanly to gemini-4-hydrogen
            resp = router.invoke(
                [{"role": "system", "content": "Be brief"}, {"role": "user", "content": "Summarize merge sort"}],
                model="gemini-flash-latest",
                data_classification="public_sanitized",
                service_tier="flex",
            )
            self.assertEqual(resp["choices"][0]["message"]["role"], "assistant")
            self.assertEqual(resp["choices"][0]["message"]["content"], "gemini:flex:ok")
            mock_client.generate.assert_called_once()
            self.assertEqual(mock_client.generate.call_args.kwargs["model"], "gemini-4-hydrogen")
            self.assertEqual(mock_client.generate.call_args.kwargs["service_tier"], "flex")
            self.assertEqual(mock_client.generate.call_args.kwargs["data_classification"], "public_sanitized")

            schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
            text = router.chat_text(
                "Explain binary search",
                model="gemini-4-neon",
                data_classification="public_sanitized",
                service_tier="flex",
                json_schema=schema,
                effort="high",
            )
            self.assertEqual(text, "gemini:flex:ok")
            self.assertEqual(mock_client.generate.call_args.kwargs["model"], "gemini-4-neon")
            self.assertEqual(mock_client.generate.call_args.kwargs["thinking_level"], "HIGH")
            self.assertEqual(mock_client.generate.call_args.kwargs["response_json_schema"], schema)

    def test_capc_prefix_ordering_and_dynamic_context_compression(self) -> None:
        srv, base_url, received = _start_oss_mock_server()
        try:
            router = model_router.ModelRouter(litellm_url=base_url, gateway_url="http://127.0.0.1:59997")
            immutable_prefix = "IMMUTABLE_SYSTEM_SPEC_V1::DO_NOT_MUTATE"
            huge_dynamic = "line_data\n" * 3000
            orig_messages = [{"role": "user", "content": huge_dynamic}]
            router.invoke(
                orig_messages,
                model="qwen3.5-4b",
                capc_static_prefix=immutable_prefix,
                auto_compress_context=True,
            )
            self.assertEqual(orig_messages[0]["content"], huge_dynamic)
            sent_content = received[0]["body"]["messages"][0]["content"]
            self.assertTrue(sent_content.startswith(immutable_prefix))
            self.assertLess(len(sent_content), len(huge_dynamic))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_openai_anthropic_ollama_and_compat_routing_without_gemini(self) -> None:
        clean_env = {
            "OPENAI_API_KEY": "sk-test-openai",
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }
        with patch.dict(os.environ, clean_env, clear=True):
            router = model_router.ModelRouter()
            for alias in ("gpt-5.4", "gpt-5.3-codex", "gpt-5", "gpt-4.1", "gpt-4.1-mini", "o3", "o4-mini", "codex-mini-latest"):
                target = router.resolve(alias)
                self.assertEqual(target.backend, "openai")
                self.assertEqual(target.canonical_model, alias)
                self.assertEqual(target.endpoint, "https://api.openai.com/v1")
                self.assertFalse(target.requires_local_aws_profile)

            for alias in ("anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5", "claude-sonnet-4", "claude-opus-4", "claude-haiku-4"):
                expected_canonical = alias.split("/", 1)[-1]
                target = router.resolve(alias)
                self.assertEqual(target.backend, "anthropic")
                self.assertEqual(target.canonical_model, expected_canonical)
                self.assertEqual(target.endpoint, "https://api.anthropic.com/v1/messages")
                self.assertFalse(target.requires_local_aws_profile)

            ollama_target = router.resolve("ollama/qwen3.5-4b")
            self.assertEqual(ollama_target.backend, "ollama")
            self.assertEqual(ollama_target.canonical_model, "qwen3.5-4b")
            self.assertEqual(ollama_target.endpoint, "http://127.0.0.1:11434/v1")

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=True):
            or_target = model_router.ModelRouter().resolve("openrouter/deepseek/deepseek-r1")
            self.assertEqual(or_target.backend, "openai_compat")
            self.assertEqual(or_target.canonical_model, "deepseek/deepseek-r1")
            self.assertEqual(or_target.endpoint, "https://openrouter.ai/api/v1")

    def test_invoke_openai_ollama_and_anthropic_end_to_end_without_gemini(self) -> None:
        anth_received: List[Dict[str, Any]] = []

        class _AnthropicHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                anth_received.append({
                    "path": self.path,
                    "api_key": self.headers.get("x-api-key", ""),
                    "version": self.headers.get("anthropic-version", ""),
                    "body": body,
                })
                raw = json.dumps({
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"anthropic:{body['model']}"}],
                    "usage": {"input_tokens": 8, "output_tokens": 4},
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        oai_srv, oai_url, oai_received = _start_oss_mock_server()
        anth_srv = HTTPServer(("127.0.0.1", 0), _AnthropicHandler)
        anth_thread = threading.Thread(target=anth_srv.serve_forever, daemon=True)
        anth_thread.start()
        anth_url = f"http://127.0.0.1:{anth_srv.server_address[1]}"

        try:
            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "sk-oai-unit",
                    "OPENAI_BASE_URL": f"{oai_url}/v1",
                    "ANTHROPIC_API_KEY": "sk-ant-unit",
                    "OLLAMA_HOST": oai_url,
                    "KAIZEN_LLM_BASE_URL": f"{oai_url}/v1",
                },
                clear=True,
            ):
                router = model_router.ModelRouter(anthropic_url=anth_url)
                out_oai = router.chat_text("hello", model="gpt-4.1")
                self.assertEqual(out_oai, "oss:gpt-4.1")
                self.assertEqual(oai_received[-1]["path"], "/v1/chat/completions")

                out_ollama = router.chat_text("local", model="ollama/llama3.3")
                self.assertEqual(out_ollama, "oss:llama3.3")

                out_compat = router.chat_text("vllm", model="vllm/Qwen/Qwen3-8B")
                self.assertEqual(out_compat, "oss:Qwen/Qwen3-8B")

                out_anth = router.chat_text("audit", system="Be concise", model="claude-sonnet-4")
                self.assertEqual(out_anth, "anthropic:claude-sonnet-4")
                self.assertEqual(anth_received[0]["path"], "/v1/messages")
                self.assertEqual(anth_received[0]["api_key"], "sk-ant-unit")
                self.assertEqual(anth_received[0]["version"], "2023-06-01")
                self.assertEqual(anth_received[0]["body"]["system"], "Be concise")
        finally:
            oai_srv.shutdown()
            oai_srv.server_close()
            anth_srv.shutdown()
            anth_srv.server_close()

        with patch.dict(os.environ, {}, clear=True):
            strict_router = model_router.ModelRouter()
            with self.assertRaises(RuntimeError):
                strict_router.chat_text("missing key", model="gpt-5")
            with self.assertRaises(RuntimeError):
                strict_router.chat_text("missing key", model="claude-opus-4")

    def test_detect_available_providers_and_resolve_default_provider(self) -> None:
        env_oai_anth = {"OPENAI_API_KEY": "sk-1", "ANTHROPIC_API_KEY": "sk-2"}
        found = model_router.detect_available_providers(env=env_oai_anth, probe_local_ports=False)
        self.assertEqual(found, ["openai", "anthropic"])
        self.assertNotIn("gemini", found)
        self.assertEqual(
            model_router.resolve_default_provider(env=env_oai_anth, probe_local_ports=False),
            "openai",
        )

        env_anth_only = {"ANTHROPIC_API_KEY": "sk-2", "GEMINI_API_KEY": "AIza-fake"}
        self.assertEqual(
            model_router.resolve_default_provider(env=env_anth_only, probe_local_ports=False),
            "anthropic",
        )

        env_ollama = {"OLLAMA_HOST": "127.0.0.1:11434"}
        self.assertEqual(
            model_router.resolve_default_provider(env=env_ollama, probe_local_ports=False),
            "ollama",
        )

        env_override = {"OPENAI_API_KEY": "sk-1", "KAIZEN_DEFAULT_PROVIDER": "anthropic"}
        self.assertEqual(
            model_router.resolve_default_provider(env=env_override, probe_local_ports=False),
            "anthropic",
        )

        self.assertEqual(
            model_router.resolve_default_provider(env={}, probe_local_ports=False),
            "ollama",
        )

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-default"}, clear=True):
            default_target = model_router.ModelRouter().resolve_default()
            self.assertEqual(default_target.backend, "openai")
            self.assertEqual(default_target.canonical_model, "gpt-5.4")


if __name__ == "__main__":
    unittest.main()
