#!/usr/bin/env python3
"""Unit and integration tests for Google ADK Kaizen domain workflows (`adk_agents.py`)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from kaizen_kit import adk_agents, cli, model_router


def _start_map_and_filter_servers() -> tuple[HTTPServer, str, HTTPServer, str, List[Dict[str, Any]]]:
    calls: List[Dict[str, Any]] = []

    class _OssHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            calls.append({"server": "oss", "path": self.path, "body": body})
            user_text = body["messages"][-1]["content"]
            content = "NOISE" if "drop-me" in user_text else f"evidence:{user_text.splitlines()[-1]}"
            resp = {"id": "map-1", "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}
            raw = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    class _ModernBertHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            calls.append({"server": "modernbert", "path": self.path, "body": body})
            state = body.get("state", "")
            score = 0.12 if "NOISE" in state else 0.91
            resp = {"answers": {"relevance": {"score": score, "confidence": 0.95, "reason": "scored"}}}
            raw = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    oss_srv = HTTPServer(("127.0.0.1", 0), _OssHandler)
    bert_srv = HTTPServer(("127.0.0.1", 0), _ModernBertHandler)
    threading.Thread(target=oss_srv.serve_forever, daemon=True).start()
    threading.Thread(target=bert_srv.serve_forever, daemon=True).start()
    return (
        oss_srv,
        f"http://127.0.0.1:{oss_srv.server_address[1]}",
        bert_srv,
        f"http://127.0.0.1:{bert_srv.server_address[1]}",
        calls,
    )


def _tool_loop_message(step: int) -> tuple[Dict[str, Any], str]:
    if step == 1:
        return (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_parity_1",
                        "type": "function",
                        "function": {
                            "name": "run_parity_gate_tool",
                            "arguments": json.dumps({
                                "legacy_json": '{"home": {"id": 1}}',
                                "target_json": '{"home": {"id": 1}}',
                            }),
                        },
                    }
                ],
            },
            "tool_calls",
        )
    if step == 2:
        return (
            {
                "role": "assistant",
                "content": "Parity matched, escalating exit.",
                "tool_calls": [
                    {
                        "id": "call_exit_2",
                        "type": "function",
                        "function": {
                            "name": "exit_parity_loop",
                            "arguments": json.dumps({"reason": "POPULATED_MATCH"}),
                        },
                    }
                ],
            },
            "tool_calls",
        )
    return ({"role": "assistant", "content": "PARITY_DONE"}, "stop")


class _ToolLoopHandler(BaseHTTPRequestHandler):
    captured_requests: List[Dict[str, Any]] = []
    turn_count = 0

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).captured_requests.append(body)
        type(self).turn_count += 1
        step = type(self).turn_count
        msg, finish = _tool_loop_message(step)
        resp = {
            "id": f"tool-turn-{step}",
            "model": "qwen3.5-4b",
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        }
        raw = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _start_tool_loop_server() -> tuple[HTTPServer, str, List[Dict[str, Any]]]:
    captured: List[Dict[str, Any]] = []

    class _BoundToolLoopHandler(_ToolLoopHandler):
        captured_requests = captured
        turn_count = 0

    srv = HTTPServer(("127.0.0.1", 0), _BoundToolLoopHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", captured


async def _run_adk_loop_session(loop_agent: Any) -> Dict[str, Any]:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as genai_types

    svc = InMemorySessionService()
    await svc.create_session(app_name="test-tools", user_id="u1", session_id="s1")
    runner = Runner(app_name="test-tools", agent=loop_agent, session_service=svc)
    msg = genai_types.Content(role="user", parts=[genai_types.Part.from_text(text="run parity check")])
    escalated = False
    texts: List[str] = []
    async for ev in runner.run_async(user_id="u1", session_id="s1", new_message=msg):
        actions = getattr(ev, "actions", None)
        if actions is not None and getattr(actions, "escalate", False):
            escalated = True
        texts.extend(adk_agents._extract_event_texts(ev))
    return {"escalated": escalated, "texts": texts}


def _run_cli_json(argv: List[str]) -> tuple[int, Dict[str, Any]]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, json.loads(buf.getvalue())


def _create_temp_mcp_config(root: Path) -> Path:
    cfg = root / "gemini_cfg"
    (cfg / "plugins" / "context7").mkdir(parents=True, exist_ok=True)
    secret_pat = "glpat-" + "TESTSECRET1234567890"
    secret_key = "AKIA" + "IOSFODNN7EXAMPLE"
    main_cfg = {
        "mcpServers": {
            "serena": {"type": "http", "url": "http://127.0.0.1:24280/mcp"},
            "kaizen-atlassian": {
                "command": "wsl.exe",
                "args": ["-d", "Ubuntu-20.04", "--", "mcp-atlassian"],
                "env": {"JIRA_API_TOKEN": secret_pat, "JIRA_URL": "https://kaizen.atlassian.net"},
            },
            "kaizen-gitlab": {
                "command": "node.exe",
                "args": ["gitlab-mcp/index.js", f"--token={secret_pat}"],
                "env": {"GITLAB_PERSONAL_ACCESS_TOKEN": secret_pat},
            },
            "internal-git-gitlab": {
                "command": "node.exe",
                "args": ["gitlab-mcp/index.js", "--api-url", "https://gitlab.internal.example/api/v4"],
                "env": {"GITLAB_PERSONAL_ACCESS_TOKEN": secret_pat},
            },
            "kaizen-devops": {
                "command": "node.exe",
                "args": ["jenkins-mcp/kaizen-devops-gateway.js"],
                "env": {"JENKINS_TOKEN": secret_key, "GERRIT_CORP_PASSWORD": "pw"},
            },
            "awsknowledge": {"type": "http", "url": "https://knowledge-mcp.global.api.aws"},
        }
    }
    (cfg / "mcp_config.json").write_text(json.dumps(main_cfg), encoding="utf-8")
    (cfg / "plugins" / "context7" / "mcp_config.json").write_text(
        json.dumps({"context7": {"serverUrl": "https://mcp.context7.com/mcp"}}),
        encoding="utf-8",
    )
    return cfg


class TestAdkAgents(unittest.TestCase):
    def test_google_adk_installed_and_workflows_built(self) -> None:
        self.assertTrue(adk_agents.HAS_GOOGLE_ADK)
        from google.adk.agents import LlmAgent, LoopAgent, ParallelAgent, SequentialAgent

        loop_agent = adk_agents.build_workflow("KaizenOndemandParityLoop")
        self.assertIsInstance(loop_agent, LoopAgent)
        self.assertEqual(loop_agent.max_iterations, 3)
        self.assertEqual(len(loop_agent.sub_agents), 3)

        rca_agent = adk_agents.build_workflow("KaizenUcRcaPipeline")
        self.assertIsInstance(rca_agent, SequentialAgent)
        self.assertEqual(len(rca_agent.sub_agents), 4)
        self.assertEqual(rca_agent.sub_agents[-1].model.model, "claude-opus-5-5")

        swarm_agent = adk_agents.build_workflow("KaizenSeniorReviewSwarm")
        self.assertIsInstance(swarm_agent, ParallelAgent)
        self.assertEqual(len(swarm_agent.sub_agents), 8)

        cloud_compose_agent = adk_agents.build_workflow("KaizenCloudComposeOpsParityAgent")
        self.assertIsInstance(cloud_compose_agent, LlmAgent)
        openapi_agent = adk_agents.build_workflow("KaizenOpenApiContractSyncAgent")
        self.assertIsInstance(openapi_agent, LlmAgent)
        mr_agent = adk_agents.build_workflow("LlmArmyMapReduce")
        self.assertIsInstance(mr_agent, SequentialAgent)

    def test_exit_parity_loop_sets_escalate(self) -> None:
        ctx = SimpleNamespace(actions=SimpleNamespace(escalate=False))
        res = adk_agents.exit_parity_loop(ctx, reason="POPULATED_MATCH")
        self.assertTrue(res["escalated"])
        self.assertTrue(ctx.actions.escalate)

    def test_non_secret_7_mcp_discovery_and_toolset_construction(self) -> None:
        from google.adk.tools.mcp_tool import McpToolset

        with tempfile.TemporaryDirectory() as td:
            cfg = _create_temp_mcp_config(Path(td))
            reg = adk_agents.discover_mcp_registry(config_dir=cfg, fastmcp_url="http://127.0.0.1:8770/mcp")
            for required in adk_agents.REQUIRED_MCP_SERVERS:
                self.assertIn(required, reg)
            self.assertIsNotNone(adk_agents.resolve_mcp_spec(reg, "context7_context7"))

            serialized = json.dumps({k: v.to_dict() for k, v in reg.items()})
            self.assertNotIn("TESTSECRET1234567890", serialized)
            self.assertNotIn("IOSFODNN7EXAMPLE", serialized)
            self.assertIn("<REDACTED>", reg["kaizen-gitlab"].args[1])
            self.assertEqual(reg["kaizen-atlassian"].env_keys, ("JIRA_API_TOKEN", "JIRA_URL"))

            swarm = adk_agents.build_workflow("KaizenSeniorReviewSwarm", attach_mcps=True, registry=reg)
            mcp_tools = [t for t in swarm.sub_agents[0].tools if isinstance(t, McpToolset)]
            self.assertEqual(len(mcp_tools), 3)

            cloud_compose = adk_agents.build_workflow("KaizenCloudComposeOpsParityAgent", attach_mcps=True, registry=reg)
            self.assertEqual(len([t for t in cloud_compose.tools if isinstance(t, McpToolset)]), 4)

    def test_adk_runner_executes_model_router_llm_via_oss_server(self) -> None:
        from google.adk.agents import LlmAgent
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types as genai_types

        oss_srv, oss_url, bert_srv, _, _ = _start_map_and_filter_servers()
        try:
            router = model_router.ModelRouter(litellm_url=oss_url, gateway_url=oss_url)
            llm = adk_agents.ModelRouterLlm(model="qwen3.5-4b", router=router)
            agent = LlmAgent(name="TestOssRunner", model=llm, instruction="Summarize input.")

            import asyncio

            async def _exec() -> List[str]:
                svc = InMemorySessionService()
                await svc.create_session(app_name="test", user_id="u1", session_id="s1")
                r = Runner(app_name="test", agent=agent, session_service=svc)
                msg = genai_types.Content(role="user", parts=[genai_types.Part.from_text(text="check-route")])
                out: List[str] = []
                async for ev in r.run_async(user_id="u1", session_id="s1", new_message=msg):
                    out.extend(adk_agents._extract_event_texts(ev))
                return out

            outputs = asyncio.run(_exec())
            self.assertEqual(outputs, ["evidence:check-route"])
        finally:
            oss_srv.shutdown()
            oss_srv.server_close()
            bert_srv.shutdown()
            bert_srv.server_close()

    def test_llm_army_map_reduce_live_loopback_pipeline(self) -> None:
        oss_srv, oss_url, bert_srv, bert_url, calls = _start_map_and_filter_servers()
        try:
            router = model_router.ModelRouter(litellm_url=oss_url, gateway_url=oss_url)
            secret_token = "glpat-" + "1234567890abcdef1234"
            with patch.object(
                adk_agents.model_router.bedrock_bridge,
                "invoke_bedrock",
                return_value={"choices": [{"message": {"content": "REDUCED_OPUS_5_5: valid-item"}}]},
            ) as mock_bedrock:
                pipeline = adk_agents.LlmArmyMapReduce(
                    router=router,
                    map_model="mini-pool",
                    reduce_model="claude-opus-5-5",
                    filter_url=bert_url,
                    fallback_jev_url=bert_url,
                    min_score=0.5,
                )
                res = pipeline.execute(
                    prompt="Audit telemetry findings",
                    items=["valid-item", "drop-me", f"leak {secret_token}"],
                )
            self.assertEqual(res["status"], "OK")
            self.assertEqual(res["mapped_count"], 3)
            self.assertEqual(res["kept_count"], 1)
            self.assertEqual(res["reduce_model"], "claude-opus-5-5")
            self.assertTrue(res["filter_backend"].startswith("modernbert:"))
            self.assertIn("REDUCED_OPUS_5_5", res["output"])
            mock_bedrock.assert_called_once()
            self.assertTrue(any(c["server"] == "modernbert" for c in calls))
        finally:
            oss_srv.shutdown()
            oss_srv.server_close()
            bert_srv.shutdown()
            bert_srv.server_close()

    def test_cli_adk_subcommands(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = _create_temp_mcp_config(Path(td))
            for argv, expected_key in (
                (["adk", "list", "--json"], "workflows"),
                (["adk", "inspect", "KaizenSeniorReviewSwarm", "--json"], "topology"),
                (["adk", "mcp-matrix", "--config-dir", str(cfg), "--json"], "required"),
                (["adk", "map-reduce", "--prompt", "Audit", "--dry-run", "--json"], "stages"),
                (["adk", "run", "uc-rca", "--prompt", "Triage UC-1", "--dry-run", "--json"], "status"),
            ):
                rc, parsed = _run_cli_json(argv)
                self.assertEqual(rc, 0, msg=f"CLI failed for {argv}")
                self.assertIn(expected_key, parsed)

    def test_fallback_adapters_when_google_adk_absent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = _create_temp_mcp_config(Path(td))
            reg = adk_agents.discover_mcp_registry(config_dir=cfg)
            with patch.object(adk_agents, "HAS_GOOGLE_ADK", False):
                loop_agent = adk_agents.build_workflow("KaizenOndemandParityLoop", attach_mcps=True, registry=reg)
                self.assertIsInstance(loop_agent, adk_agents.FallbackBaseAgent)
                self.assertEqual(loop_agent.max_iterations, 3)
                toolsets = [t for t in loop_agent.sub_agents[0].tools if isinstance(t, adk_agents.FallbackMcpToolset)]
                self.assertEqual(len(toolsets), 2)

    def test_map_reduce_no_candidates_kept_and_offline_fallback(self) -> None:
        oss_srv, oss_url, bert_srv, _, _ = _start_map_and_filter_servers()
        try:
            router = model_router.ModelRouter(litellm_url=oss_url, gateway_url=oss_url)
            self._assert_map_reduce_blocked_and_offline(router)
        finally:
            oss_srv.shutdown()
            oss_srv.server_close()
            bert_srv.shutdown()
            bert_srv.server_close()

    def _assert_map_reduce_blocked_and_offline(self, router: model_router.ModelRouter) -> None:
        with patch.object(adk_agents, "_port_open", return_value=False):
            pipeline = adk_agents.LlmArmyMapReduce(
                router=router,
                map_model="mini-pool",
                reduce_model="claude-opus-5-5",
                safety_rail_fn=lambda _t: (False, "blocked_by_rail"),
            )
            blocked = pipeline.execute(prompt="Audit", items=["item-1"])
            self.assertEqual(blocked["status"], "NO_CANDIDATES_KEPT")
            self.assertEqual(blocked["kept_count"], 0)

        with (
            patch.object(adk_agents, "_port_open", return_value=False),
            patch.object(
                adk_agents.model_router.bedrock_bridge,
                "invoke_bedrock",
                return_value={"choices": [{"message": {"content": "OFFLINE_FILTER_OK"}}]},
            ),
        ):
            ok_pipeline = adk_agents.LlmArmyMapReduce(
                router=router,
                map_model="mini-pool",
                reduce_model="claude-opus-5-5",
            )
            res = ok_pipeline.execute(prompt="Audit", items=["valid-item-123456"])
        self.assertEqual(res["status"], "OK")
        self.assertEqual(res["filter_backend"], "offline_heuristic")
        self.assertEqual(res["output"], "OFFLINE_FILTER_OK")

    def test_adk_loop_agent_tool_calling_roundtrip_and_early_exit(self) -> None:
        import asyncio
        from google.adk.agents import LlmAgent, LoopAgent

        srv, url, captured_requests = _start_tool_loop_server()
        try:
            router = model_router.ModelRouter(litellm_url=url, gateway_url=url)
            llm = adk_agents.ModelRouterLlm(model="qwen3.5-4b", router=router)
            parity_agent = LlmAgent(
                name="ParityToolLoopProbe",
                model=llm,
                instruction="Verify parity then exit.",
                tools=[adk_agents.run_parity_gate_tool, adk_agents.exit_parity_loop],
            )
            loop_agent = LoopAgent(name="ProbeParityLoop", sub_agents=[parity_agent], max_iterations=4)
            result = asyncio.run(_run_adk_loop_session(loop_agent))
            self.assertTrue(result["escalated"])
            self.assertIn("PARITY_DONE", result["texts"])
            self.assertEqual(len(captured_requests), 3)

            tools_sent = captured_requests[0].get("tools") or []
            tool_names = [t["function"]["name"] for t in tools_sent]
            self.assertIn("run_parity_gate_tool", tool_names)
            self.assertIn("exit_parity_loop", tool_names)

            turn2_roles = [m["role"] for m in captured_requests[1]["messages"]]
            self.assertIn("tool", turn2_roles)
            tool_msg = next(m for m in captured_requests[1]["messages"] if m["role"] == "tool")
            self.assertEqual(tool_msg["name"], "run_parity_gate_tool")
            self.assertIn("POPULATED_MATCH", tool_msg["content"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_model_router_bedrock_tool_calling_roundtrip(self) -> None:
        mock_client = MagicMock()
        mock_client.converse.side_effect = [
            {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "tu_1",
                                    "name": "run_parity_gate_tool",
                                    "input": {"legacy_json": "{}", "target_json": "{}"},
                                }
                            }
                        ],
                    }
                },
                "stopReason": "tool_use",
                "usage": {"inputTokens": 10, "outputTokens": 5},
            },
            {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [{"text": "VERIFIED_BY_BEDROCK"}],
                    }
                },
                "stopReason": "end_turn",
                "usage": {"inputTokens": 15, "outputTokens": 4},
            },
        ]
        router = model_router.ModelRouter()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "run_parity_gate_tool",
                    "description": "Evaluate parity",
                    "parameters": {"type": "object", "properties": {"legacy_json": {"type": "string"}}},
                },
            }
        ]
        with patch.object(adk_agents.model_router.bedrock_bridge.CLIENT_HOLDER, "get_client", return_value=mock_client):
            resp1 = router.invoke(
                [{"role": "user", "content": "Check parity"}],
                model="sonnet",
                tools=tools,
            )
            tc = resp1["choices"][0]["message"]["tool_calls"][0]
            self.assertEqual(tc["id"], "tu_1")
            self.assertEqual(tc["function"]["name"], "run_parity_gate_tool")

            resp2 = router.invoke(
                [
                    {"role": "user", "content": "Check parity"},
                    {"role": "assistant", "content": None, "tool_calls": [tc]},
                    {"role": "tool", "tool_call_id": "tu_1", "name": "run_parity_gate_tool", "content": {"passed": True}},
                ],
                model="sonnet",
                tools=tools,
            )
            self.assertEqual(resp2["choices"][0]["message"]["content"], "VERIFIED_BY_BEDROCK")

        call1_kwargs = mock_client.converse.call_args_list[0].kwargs
        self.assertIn("toolConfig", call1_kwargs)
        self.assertEqual(call1_kwargs["toolConfig"]["tools"][0]["toolSpec"]["name"], "run_parity_gate_tool")

        call2_msgs = mock_client.converse.call_args_list[1].kwargs["messages"]
        self.assertEqual(call2_msgs[1]["content"][0]["toolUse"]["toolUseId"], "tu_1")
        self.assertEqual(call2_msgs[2]["content"][0]["toolResult"]["toolUseId"], "tu_1")
        self.assertIn('"passed": true', call2_msgs[2]["content"][0]["toolResult"]["content"][0]["text"])

    def test_mcp_secret_redaction_url_and_space_flags(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td)
            (cfg / "mcp_config.json").write_text(
                json.dumps({
                    "mcpServers": {
                        "cli-secrets": {
                            "command": "node.exe",
                            "args": [
                                "srv.js",
                                "-p",
                                "PASS_SPACE_1",
                                "-p=SHORT_EQ_P_VAL",
                                "--TOKEN",
                                "TOK_SPACE_2",
                                "--password",
                                "PW_SPACE_3",
                                "--secret",
                                "SEC_SPACE_4",
                                "--API-KEY",
                                "KEY_SPACE_5",
                                "--auth",
                                "AUTH_SPACE_6",
                                "--token=TOK_EQ_7",
                                "https://ARG_URL_USER_VAL:ARG_URL_PASS_VAL@host.example.com/mcp?token=ARG_URL_QUERY_VAL",
                                "--url=https://FLAG_USER_VAL:FLAG_PASS_VAL@host.example.com/mcp?api_key=FLAG_KEY_VAL&ok=1",
                            ],
                        },
                        "url-secrets": {
                            "url": "https://USER_SECRET_A:PASS_SECRET_B@mcp.example.com:8443/mcp?token=QTOK_C&api_key=QKEY_D&mode=readonly",
                        },
                    }
                }),
                encoding="utf-8",
            )
            reg = adk_agents.discover_mcp_registry(config_dir=cfg)
            serialized = json.dumps({k: v.to_dict() for k, v in reg.items()})
            for secret in (
                "PASS_SPACE_1",
                "SHORT_EQ_P_VAL",
                "TOK_SPACE_2",
                "PW_SPACE_3",
                "SEC_SPACE_4",
                "KEY_SPACE_5",
                "AUTH_SPACE_6",
                "TOK_EQ_7",
                "ARG_URL_USER_VAL",
                "ARG_URL_PASS_VAL",
                "ARG_URL_QUERY_VAL",
                "FLAG_USER_VAL",
                "FLAG_PASS_VAL",
                "FLAG_KEY_VAL",
                "USER_SECRET_A",
                "PASS_SECRET_B",
                "QTOK_C",
                "QKEY_D",
            ):
                self.assertNotIn(secret, serialized)
            self.assertIn("-p=<REDACTED>", reg["cli-secrets"].args)
            self.assertIn(
                "https://<REDACTED>:<REDACTED>@host.example.com/mcp?token=<REDACTED>",
                reg["cli-secrets"].args,
            )
            self.assertIn(
                "--url=https://<REDACTED>:<REDACTED>@host.example.com/mcp?api_key=<REDACTED>&ok=1",
                reg["cli-secrets"].args,
            )
            self.assertIn("<REDACTED>:<REDACTED>@mcp.example.com:8443", reg["url-secrets"].url)
            self.assertIn("token=<REDACTED>", reg["url-secrets"].url)
            self.assertIn("api_key=<REDACTED>", reg["url-secrets"].url)
            self.assertIn("mode=readonly", reg["url-secrets"].url)

    def test_fastmcp_config_override_non_loopback_rejected_and_url_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td)
            (cfg / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {"fastmcp": {"url": "https://evil.example.com/mcp"}}}),
                encoding="utf-8",
            )
            self.assertRaises(ValueError, adk_agents.discover_mcp_registry, config_dir=cfg)

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td)
            reg = self._discover_with_env_fastmcp(
                cfg,
                "http://FASTMCP_USER:FASTMCP_PASS@127.0.0.1:8770/mcp?token=FASTMCP_TOK&keep=yes",
            )
            self.assertEqual(
                reg["fastmcp"].url,
                "http://<REDACTED>:<REDACTED>@127.0.0.1:8770/mcp?token=<REDACTED>&keep=yes",
            )

    def test_fastmcp_non_loopback_rejected_and_forward_env_opt_in(self) -> None:
        with patch.dict(os.environ, {"KAIZEN_FASTMCP_URL": "https://evil.example.com/mcp"}):
            with self.assertRaises(ValueError):
                adk_agents.describe_runtime_capabilities()

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td)
            (cfg / "mcp_config.json").write_text(
                json.dumps({
                    "mcpServers": {
                        "stdio-srv": {
                            "command": "node.exe",
                            "args": ["srv.js"],
                            "env": {"JIRA_API_TOKEN": "raw-ignored-value", "UNSET_VAR": "x"},
                        }
                    }
                }),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                adk_agents.discover_mcp_registry(config_dir=cfg, fastmcp_url="https://evil.example.com/mcp")
            self.assertRaises(
                ValueError,
                self._discover_with_env_fastmcp,
                cfg,
                "https://evil.example.com/mcp",
            )
            reg = adk_agents.discover_mcp_registry(config_dir=cfg)

        spec = reg["stdio-srv"]
        with patch.dict(os.environ, {"JIRA_API_TOKEN": "live-env-secret"}, clear=False):
            os.environ.pop("UNSET_VAR", None)
            default_ts = adk_agents.build_mcp_toolset(spec)
            self.assertIsNone(default_ts._connection_params.server_params.env)
            live_ts = adk_agents.build_mcp_toolset(spec, forward_env=True)
            self.assertEqual(live_ts._connection_params.server_params.env, {"JIRA_API_TOKEN": "live-env-secret"})
            live_list = adk_agents.build_mcp_toolsets(["stdio-srv"], registry=reg, forward_env=True)
            self.assertEqual(
                live_list[0]._connection_params.server_params.env,
                {"JIRA_API_TOKEN": "live-env-secret"},
            )

    @staticmethod
    def _discover_with_env_fastmcp(cfg: Path, url: str) -> Any:
        with patch.dict(os.environ, {"KAIZEN_FASTMCP_URL": url}):
            return adk_agents.discover_mcp_registry(config_dir=cfg)

    def test_has_antigravity_sdk_handles_missing_google_namespace(self) -> None:
        with patch.object(adk_agents.importlib.util, "find_spec", side_effect=ModuleNotFoundError("No module named 'google'")):
            self.assertFalse(adk_agents.has_antigravity_sdk())

    def test_phase2_constrained_decoding_distributed_256gb_and_tool_hooks(self) -> None:
        schema = {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}
        fake_req = SimpleNamespace(
            config=SimpleNamespace(
                max_output_tokens=512,
                temperature=0.0,
                response_json_schema=schema,
                response_mime_type="application/json",
            )
        )
        resp_fmt, parsed_schema = adk_agents._extract_response_format(fake_req)
        self.assertEqual(resp_fmt["type"], "json_schema")
        self.assertEqual(parsed_schema["type"], "object")

        rc, mr_out = _run_cli_json(
            [
                "adk",
                "map-reduce",
                "--prompt",
                "Evaluate distributed cluster",
                "--spot-tier",
                "distributed_256gb",
                "--json-schema",
                json.dumps(schema),
                "--best-of-n",
                "2",
                "--effort",
                "medium",
                "--dry-run",
                "--json",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mr_out["status"], "DRY_RUN")
        self.assertEqual(mr_out["response_format"]["type"], "json_schema")
        self.assertEqual(mr_out["best_of_n"], 2)
        self.assertEqual(mr_out["effort"], "medium")
        self.assertEqual(mr_out["spot_burst_plan"]["tier"], "distributed_256gb")

        guard_res = adk_agents.run_streaming_guardrail_tool("pub fn ok() {}\n")
        self.assertTrue(guard_res.get("allowed", True))

        voice_res = adk_agents.run_voice_to_action_tool(
            "<voice_instructions>be brief</voice_instructions> Check VectorCube parity",
            dry_run=True,
        )
        self.assertTrue(voice_res.get("ok", True))

        with tempfile.TemporaryDirectory() as td:
            ws_root = Path(td) / "ws"
            repo = ws_root / "demo"
            (repo / ".git").mkdir(parents=True)
            (repo / "src").mkdir(parents=True)
            (repo / "src" / "lib.rs").write_text("pub fn vector_cube_fold() {}\n", encoding="utf-8")
            state_dir = Path(td) / "state"
            with patch.dict(os.environ, {"AI_KIT_STATE_DIR": str(state_dir)}):
                rag_res = adk_agents.run_hybrid_rag_ocr_tool("vector_cube_fold", workspace=str(ws_root))
            self.assertTrue(rag_res["ok"])
            self.assertGreaterEqual(len(rag_res["hits"]), 1)

    def test_phase2b_adk_stale_turn_masking_and_code_agent_batching(self) -> None:
        from kaizen_kit.hooks import lossless_test_distiller

        stale_history = [
            {"role": "tool", "name": "zoekt", "tool_call_id": f"tc-{i}", "content": f"match line {i}"}
            for i in range(6)
        ]
        masked = lossless_test_distiller.mask_stale_turn_observations(stale_history, keep_recent_turns=4)
        self.assertIn("<Observation masked:", masked[0]["content"])
        self.assertIn("<Observation masked:", masked[1]["content"])
        self.assertEqual(masked[2]["content"], "match line 2")

        oss_srv, oss_url, bert_srv, bert_url, calls = _start_map_and_filter_servers()
        try:
            router = model_router.ModelRouter(litellm_url=oss_url, gateway_url=oss_url)
            pipe = adk_agents.LlmArmyMapReduce(
                router=router,
                map_model="mini-pool",
                reduce_model="mini-pool",
                filter_url=bert_url,
                fallback_jev_url=bert_url,
                min_score=0.45,
                code_agent_batch_mode=True,
                compact_report_max_lines=3,
            )
            res = pipe.execute("Audit items", ["item-alpha", "item-beta", "item-gamma"])
            self.assertEqual(res["status"], "OK")
            self.assertEqual(res["mapped_count"], 1)
            oss_calls = [c for c in calls if c["server"] == "oss"]
            self.assertEqual(len(oss_calls), 2)
        finally:
            oss_srv.shutdown()
            oss_srv.server_close()
            bert_srv.shutdown()
            bert_srv.server_close()

        rc, dry_batch = _run_cli_json(
            [
                "adk",
                "map-reduce",
                "--prompt",
                "Batch check",
                "--item",
                "a",
                "--item",
                "b",
                "--batch-mode",
                "--compact-lines",
                "12",
                "--dry-run",
                "--json",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue(dry_batch["code_agent_batch_mode"])
        self.assertEqual(dry_batch["compact_report_max_lines"], 12)


if __name__ == "__main__":
    unittest.main()
