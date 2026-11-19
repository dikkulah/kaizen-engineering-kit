#!/usr/bin/env python3
"""Comprehensive tests for Carbon 6-Layer LLM Army Orchestrator, CascadeRouter, PoLL Jury & Guards."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from kaizen_kit import adk_agents, carbon_orchestrator, cli, gemini_cloud_client, gpu_burst_orchestrator, mini_pool, model_router
from kaizen_kit.radar import ec2_spot_batch


class TestCarbonOrchestrator(unittest.TestCase):
    def test_six_layer_catalog_and_verified_repo_ids(self) -> None:
        models = carbon_orchestrator.ARMY_MODELS
        self.assertEqual({m.layer for m in models.values()}, {0, 1, 2, 3, 4, 5})
        bad_ids = {
            "Qwen/Qwen3-Coder-480B-A35B-Q2",
            "moonshotai/Kimi-K2.5-UD-TQ1_0",
            "moonshotai/Kimi-K3.1-Pro-REAP55",
            "deepseek-ai/DeepSeek-R1-Distill-32B",
        }
        all_repo_ids = {m.hf_repo_id for m in models.values()}
        for tier in gpu_burst_orchestrator.GPU_BURST_TIERS.values():
            all_repo_ids.update(tier.models)
        self.assertTrue(bad_ids.isdisjoint(all_repo_ids))

    def test_commercial_license_gate_blocks_nc_and_restricted_models(self) -> None:
        for blocked in (
            "Salesforce/xLAM-2-1b-fc-r",
            "jinaai/jina-code-embeddings-0.5b",
            "LiquidAI/LFM2.5-1.2B-Instruct",
            "MiniMaxAI/MiniMax-M2.7",
            "katanemo/Arch-Router-1.5B",
        ):
            res = carbon_orchestrator.validate_commercial_license(blocked)
            self.assertFalse(res["allowed"], f"Expected license gate to block {blocked}")
        self.assertTrue(carbon_orchestrator.validate_commercial_license("Qwen/Qwen3.5-35B-A3B")["allowed"])
        self.assertTrue(carbon_orchestrator.validate_commercial_license("unsloth/Kimi-K2.5-GGUF")["allowed"])

    def test_hardware_physics_sm75_arm64_quota_and_dead_end_guards(self) -> None:
        # SM75 Turing T4 blocks MXFP4 & SM89-required models, allows AWQ-Int4 32B
        self.assertFalse(carbon_orchestrator.validate_deployment_fit("gpt-oss-120b", "g4dn.12xlarge")["ok"])
        self.assertFalse(carbon_orchestrator.validate_deployment_fit("qwen3.5-122b-a10b", "g4dn.12xlarge")["ok"])
        self.assertTrue(carbon_orchestrator.validate_deployment_fit("qwen3-32b-awq", "g4dn.12xlarge")["ok"])
        self.assertTrue(carbon_orchestrator.validate_deployment_fit("qwen3-235b-a22b-awq", "g6e.12xlarge")["ok"])

        # ARM64 Graviton3 blocks KTransformers and DE-006 small-draft CPU spec-decoding
        kt_arm = carbon_orchestrator.validate_deployment_fit("qwen3-coder-480b", "r7g.16xlarge", use_ktransformers=True)
        self.assertFalse(kt_arm["ok"])
        de006 = carbon_orchestrator.validate_deployment_fit("qwen3-coder-30b-a3b", "m7g.8xlarge", use_cpu_draft_speculative=True)
        self.assertFalse(de006["ok"])

        # Over-64 vCPU G-family quota violation blocked
        over_quota = carbon_orchestrator.validate_deployment_fit("qwen3-235b-a22b-awq", "g6.48xlarge")
        self.assertFalse(over_quota["ok"])

        # AWS Batch plan never emits over-quota G-family instances
        batch_plan = gpu_burst_orchestrator.build_aws_batch_plan(tier="ultra_frontier_2_8t", region="eu-west-1")
        self.assertTrue(all(not gpu_burst_orchestrator._is_g_family(t) for t in batch_plan["instance_types"]))

    def test_cascade_router_fast_path_escalation_and_input_rail(self) -> None:
        # High-confidence CPU response avoids Bedrock call
        fast_router = MagicMock(spec=model_router.ModelRouter)
        fast_router.chat_text.return_value = "Here is the verified fix using OnceLock and single-origin CORS."
        fast_res = carbon_orchestrator.CascadeRouter(router=fast_router).execute_cascade(
            "Write a clean Rust handler helper", task_type="code_write"
        )
        self.assertEqual(fast_res["status"], "OK")
        self.assertFalse(fast_res["escalated"])
        self.assertEqual(fast_res["final_model"], "qwen3-coder-30b-a3b")
        fast_router.chat_text.assert_called_once()

        # Low-confidence uncertainty escalates to Bedrock
        esc_router = MagicMock(spec=model_router.ModelRouter)
        esc_router.chat_text.side_effect = [
            "I cannot verify this concurrency race without more context.",
            "I am not sure about the lock order.",
            "Confirmed deadlock between cache lock and singleflight map; acquire in canonical order.",
        ]
        esc_res = carbon_orchestrator.CascadeRouter(router=esc_router).execute_cascade(
            "Review VectorCube concurrency lock ordering", task_type="code_review"
        )
        self.assertEqual(esc_res["status"], "OK")
        self.assertTrue(esc_res["escalated"])
        self.assertEqual(esc_res["final_model"], "claude-opus-5-5")
        self.assertEqual(len(esc_res["attempts"]), 3)

        # Destructive command blocked by input rail before model invocation
        blocked = carbon_orchestrator.CascadeRouter(router=esc_router).execute_cascade("rm -rf /")
        self.assertEqual(blocked["status"], "BLOCKED_BY_INPUT_RAIL")

    def test_poll_review_jury_consensus_and_disagreement(self) -> None:
        mock_router = MagicMock(spec=model_router.ModelRouter)
        mock_router.chat_text.side_effect = [
            "APPROVE\n- Clean guard clauses and tests.",
            "NEEDS_WORK\n- Missing null guard on optional field.",
            "APPROVE\n- Looks safe.",
        ]
        res = carbon_orchestrator.run_poll_review_jury("fn foo() {}", router=mock_router)
        self.assertEqual(res["consensus_verdict"], "APPROVE")
        self.assertTrue(res["disagreement"])
        self.assertEqual(res["votes"]["APPROVE"], 2)
        self.assertEqual(res["votes"]["NEEDS_WORK"], 1)
        self.assertEqual(res["tie_breaker_model"], "claude-opus-5-5")

    def test_litellm_config_provision_plan_tunnels_and_cli(self) -> None:
        cfg = carbon_orchestrator.generate_m7g_litellm_config()
        self.assertLess(cfg["resident_weights_gb"], carbon_orchestrator.M7G_SAFE_WEIGHT_CEILING_GIB)
        self.assertIn("qwen3.5-35b-a3b", cfg["litellm_yaml"])
        self.assertIn("ling-mini-2.0", cfg["litellm_yaml"])

        plan = carbon_orchestrator.build_provision_plan()
        self.assertFalse(plan["aws_mutated"])
        self.assertTrue(plan["requires_explicit_user_approval"])
        self.assertEqual(plan["phase3_spot_smoke_test"]["target_instance"], "g4dn.12xlarge")
        self.assertIn("--kv-cache-dtype float16", plan["phase3_spot_smoke_test"]["vllm_command"])

        # Base TUNNEL_PORTS uses 7 core gateway ports while BURST_TUNNEL_PORTS carries 8000..8002
        self.assertEqual(len(mini_pool.TUNNEL_PORTS), 7)
        for p in (4000, 8765, 8770, 24280):
            self.assertIn(p, mini_pool.TUNNEL_PORTS)
        self.assertEqual(mini_pool.BURST_TUNNEL_PORTS, (8000, 8001, 8002))

        # ec2_spot_batch live path blocks on SPS gate before calling launch
        with patch.object(ec2_spot_batch.boto3, "Session") as mock_sess:
            mock_ec2 = MagicMock()
            mock_ec2.describe_instance_type_offerings.return_value = {
                "InstanceTypeOfferings": [{"InstanceType": "g4dn.12xlarge", "Location": "eu-west-1a"}]
            }
            mock_ec2.describe_availability_zones.return_value = {
                "AvailabilityZones": [{"ZoneId": "euw1-az1", "ZoneName": "eu-west-1a"}]
            }
            mock_ec2.get_spot_placement_scores.return_value = {
                "SpotPlacementScores": [{"Region": "eu-west-1", "AvailabilityZoneId": "euw1-az1", "Score": 1}]
            }
            mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
            mock_sq = MagicMock()
            mock_sq.get_service_quota.return_value = {"Quota": {"Value": 64.0}}
            mock_sess.return_value.client.side_effect = lambda svc: {
                "s3": MagicMock(),
                "ec2": mock_ec2,
                "ssm": MagicMock(),
                "service-quotas": mock_sq,
            }[svc]
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(RuntimeError) as ctx:
                    ec2_spot_batch.run_spot_batch([("svc", Path(td))], Path(td) / "out", dry_run=False)
                self.assertIn("SPS gate", str(ctx.exception))

        # CLI & ADK dry-run wiring
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["carbon", "route", "--prompt", "Review Rust PR", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["initial_model"], "qwen3.5-35b-a3b")
        self.assertEqual(cli.main(["carbon", "self-test"]), 0)

        # Invalid burst URL fails closed
        with self.assertRaises(ValueError):
            adk_agents.LlmArmyMapReduce(burst_url="https://remote.example.com:8000")

    def test_layer3b_gemini_cascade_decision_rubric_and_single_node_vram(self) -> None:
        models = carbon_orchestrator.ARMY_MODELS
        for alias in (
            "gemini-decision-1",
            "gemini-4-hydrogen",
            "gemini-4-carbon",
            "gemini-3.5-flash-lite",
            "gemma-4-26b-a4b-it",
        ):
            self.assertIn(alias, models)
            self.assertEqual(models[alias].layer, 3)
            self.assertEqual(models[alias].target_hardware, "google-gemini-api-public-sanitized")
        self.assertEqual({m.layer for m in models.values()}, {0, 1, 2, 3, 4, 5})

        # Corporate default stays 100% EC2/Bedrock even when prefer_gemini_flex=True (zero Gemini calls)
        mock_gem = MagicMock(spec=gemini_cloud_client.GeminiCloudClient)
        corp_dec = carbon_orchestrator.CascadeRouter().route_task(
            "Review VectorCube concurrency race",
            task_type="code_review",
            data_classification="corporate_proprietary",
            prefer_gemini_flex=True,
            gemini_client=mock_gem,
        )
        self.assertEqual(corp_dec.initial_model, "qwen3.5-35b-a3b")
        self.assertIsNone(corp_dec.decision_gate_model)
        mock_gem.decide_choice.assert_not_called()

        # Public-sanitized uses gemini-decision-1 (0 output tokens) and Flex -> Carbon escalation
        mock_gem.has_api_key.return_value = True
        mock_gem.decide_choice.return_value = {
            "answers": {"task_type": {"type": "choice", "choice": "code_review", "confidence": 0.94}}
        }
        mock_gem.decide_noul.return_value = {
            "answers": {"frontier_needed": {"type": "noul", "noul": 0.88, "confidence": 0.91}}
        }
        pub_router = MagicMock(spec=model_router.ModelRouter)
        pub_router.chat_text.side_effect = [
            "I cannot verify edge case without deeper check.",
            "Confirmed thread-safe lock ordering across shards.",
        ]
        cascade_res = carbon_orchestrator.CascadeRouter(router=pub_router).execute_cascade(
            "Review generic lock-free queue algorithm",
            data_classification="public_sanitized",
            prefer_gemini_flex=True,
            gemini_client=mock_gem,
        )
        self.assertEqual(cascade_res["status"], "OK")
        self.assertEqual(cascade_res["decision"]["decision_gate_model"], "gemini-decision-1")
        self.assertEqual(cascade_res["decision"]["service_tier"], "flex")
        self.assertEqual(cascade_res["decision"]["initial_model"], "gemini-4-hydrogen")
        self.assertIn("gemini-4-carbon", cascade_res["decision"]["escalation_chain"])
        self.assertEqual(cascade_res["final_model"], "gemini-4-carbon")
        mock_gem.decide_choice.assert_called_once()
        mock_gem.decide_noul.assert_called_once()

        # Offline fallback when API key absent keeps routing working cleanly
        no_key_client = gemini_cloud_client.GeminiCloudClient(api_key="")
        fallback_dec = carbon_orchestrator.CascadeRouter().route_task(
            "Classify sanitized release note",
            task_type="classify",
            data_classification="public_sanitized",
            prefer_gemini_flex=True,
            gemini_client=no_key_client,
        )
        self.assertEqual(fallback_dec.decision_gate_model, "local-heuristic")
        self.assertEqual(fallback_dec.initial_model, "gemini-3.5-flash-lite")
        self.assertEqual(fallback_dec.service_tier, "flex")

        # run_decision_rubric_gate packs N rules into ONE Decision-1 call
        mock_gem.decide_code_review_checklist.return_value = {
            "answers": {
                "rule_001": {"type": "noul", "noul": 0.95, "confidence": 0.92},
                "rule_002": {"type": "noul", "noul": 0.87, "confidence": 0.89},
            },
            "usage": {"total_input_tokens": 30, "total_output_tokens": 0, "total_tokens": 30},
        }
        rubric = carbon_orchestrator.run_decision_rubric_gate(
            "def add(a, b): return a + b",
            ["Uses pure arithmetic", "Avoids global state"],
            data_classification="public_sanitized",
            client=mock_gem,
        )
        self.assertTrue(rubric["passed"])
        self.assertEqual(rubric["expected_output_tokens"], 0)
        self.assertEqual(rubric["rule_count"], 2)
        mock_gem.decide_code_review_checklist.assert_called_once()

        # PoLL jury attaches decision_rubric when use_decision_gate=True
        jury_router = MagicMock(spec=model_router.ModelRouter)
        jury_router.chat_text.return_value = "APPROVE\n- Clean helper."
        jury = carbon_orchestrator.run_poll_review_jury(
            "def add(a, b): return a + b",
            router=jury_router,
            data_classification="public_sanitized",
            use_decision_gate=True,
            gemini_client=mock_gem,
        )
        self.assertEqual(jury["consensus_verdict"], "APPROVE")
        self.assertIn("decision_rubric", jury)
        self.assertTrue(jury["decision_rubric"]["passed"])

        # Single-node large plan_gpu_burst enforces min_vram_gb=64 -> g4dn.12xlarge
        class _MultiPriceEc2:
            def describe_instance_type_offerings(self, **kwargs):
                vals = kwargs.get("Filters", [{}])[0].get("Values", [])
                return {"InstanceTypeOfferings": [{"InstanceType": it, "Location": "eu-west-1a"} for it in vals if it in {"g4dn.12xlarge", "g5.12xlarge", "g5.2xlarge", "g5.xlarge"}]}

            def describe_availability_zones(self, **kwargs):
                return {"AvailabilityZones": [{"ZoneId": "euw1-az1", "ZoneName": "eu-west-1a"}]}

            def get_spot_placement_scores(self, **kwargs):
                return {"SpotPlacementScores": [{"Region": "eu-west-1", "AvailabilityZoneId": "euw1-az1", "Score": 9}]}

            def describe_spot_price_history(self, **kwargs):
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                return {
                    "SpotPriceHistory": [
                        {"InstanceType": "g5.xlarge", "AvailabilityZone": "eu-west-1a", "SpotPrice": "0.5000", "Timestamp": now},
                        {"InstanceType": "g4dn.12xlarge", "AvailabilityZone": "eu-west-1a", "SpotPrice": "1.9612", "Timestamp": now},
                        {"InstanceType": "g5.12xlarge", "AvailabilityZone": "eu-west-1a", "SpotPrice": "2.4500", "Timestamp": now},
                    ]
                }

        class _Sq:
            def get_service_quota(self, **kwargs):
                return {"Quota": {"Value": 64.0}}

        single_plan = gpu_burst_orchestrator.plan_gpu_burst(
            tier="large", nodes=1, region="eu-west-1", ec2_client=_MultiPriceEc2(), sq_client=_Sq()
        )
        self.assertEqual(single_plan["spot_placement"]["min_vram_gb"], 64)
        self.assertEqual(single_plan["spot_placement"]["ranked_azs"][0]["recommended_instance_type"], "g4dn.12xlarge")

    def test_neon_two_phase_plan_live_status_and_cli(self) -> None:
        self.assertEqual(
            {"phase_a_planner", "phase_b_executor", "phase_c_verifier"}.issubset(
                carbon_orchestrator.NEON_TWO_PHASE_PLAYBOOK.keys()
            ),
            True,
        )

        # Corporate proprietary Neon plan keeps zero Gemini exposure
        corp_plan = carbon_orchestrator.build_neon_phase_plan(
            "Review VectorCube concurrency race",
            task_type="code_review",
            data_classification="corporate_proprietary",
        )
        self.assertEqual(corp_plan["orchestrator"], "neon")
        self.assertEqual(corp_plan["data_classification"], "corporate_proprietary")
        self.assertFalse(corp_plan["gemini_exposure"])
        self.assertIsNone(corp_plan["cascade_decision"]["decision_gate_model"])
        self.assertIsNone(corp_plan["cascade_decision"]["service_tier"])
        self.assertEqual(corp_plan["cascade_decision"]["initial_model"], "qwen3.5-35b-a3b")
        self.assertEqual(
            corp_plan["phase_a_planner"]["mcp_tools"],
            ["serena", "zoekt_search", "coderag_query", "neon_decide"],
        )
        self.assertEqual(corp_plan["phase_a_planner"]["recommended_decision_backend"], "ec2_jev_8765")
        for model_name in (
            "qwen3.5-35b-a3b",
            "qwen3-coder-30b-a3b",
            "ling-mini-2.0",
            "gemma-4-26b-a4b",
            "claude-sonnet-5-5",
            "claude-opus-5-5",
        ):
            self.assertIn(model_name, corp_plan["phase_b_executor"]["recommended_offload_models"])
        self.assertTrue(any("claude-ram" in cmd for cmd in corp_plan["phase_b_executor"]["commands"]))
        self.assertIn("poll_review_jury", corp_plan["phase_c_verifier"]["gates"])
        self.assertIn("doctor --self-test", corp_plan["phase_c_verifier"]["gates"])
        self.assertIn("senior_review_lint", corp_plan["phase_c_verifier"]["gates"])

        # Public-sanitized Neon plan blocks corporate identifiers and activates Decision-1 + Flex
        with self.assertRaises(PermissionError):
            carbon_orchestrator.build_neon_phase_plan(
                "Deploy to kaizen-prod",
                data_classification="public_sanitized",
            )

        mock_gem = MagicMock(spec=gemini_cloud_client.GeminiCloudClient)
        mock_gem.has_api_key.return_value = True
        mock_gem.decide_choice.return_value = {
            "answers": {"task_type": {"type": "choice", "choice": "code_review", "confidence": 0.96}}
        }
        mock_gem.decide_noul.return_value = {
            "answers": {"frontier_needed": {"type": "noul", "noul": 0.89, "confidence": 0.93}}
        }
        pub_plan = carbon_orchestrator.build_neon_phase_plan(
            "Review generic lock-free queue algorithm",
            task_type="code_review",
            data_classification="public_sanitized",
            gemini_client=mock_gem,
        )
        self.assertTrue(pub_plan["gemini_exposure"])
        self.assertEqual(pub_plan["phase_a_planner"]["recommended_decision_backend"], "gemini-decision-1")
        self.assertEqual(pub_plan["cascade_decision"]["decision_gate_model"], "gemini-decision-1")
        self.assertEqual(pub_plan["cascade_decision"]["service_tier"], "flex")
        self.assertEqual(pub_plan["cascade_decision"]["initial_model"], "gemini-4-hydrogen")
        self.assertIn("gemini-4-hydrogen", pub_plan["phase_b_executor"]["recommended_offload_models"])
        self.assertIn("gemini-decision-1", pub_plan["phase_b_executor"]["recommended_offload_models"])

        # Live loopback port probe & safe boolean flags (never echoes API key)
        seen_timeouts = []

        class _DummyConn:
            def __enter__(self) -> "_DummyConn":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        def _fake_connect(addr, timeout=0.25):
            seen_timeouts.append(timeout)
            host, port = addr
            if host == "127.0.0.1" and port in (4000, 24302):
                return _DummyConn()
            raise OSError("connection refused")

        with patch.object(carbon_orchestrator.socket, "create_connection", side_effect=_fake_connect):
            with patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "synthetic-test-marker", "KAIZEN_DISABLE_LOCAL_BEDROCK": "1"},
                clear=False,
            ):
                status = carbon_orchestrator.get_neon_live_status()

        self.assertTrue(all(t == 0.25 for t in seen_timeouts))
        self.assertEqual(len(seen_timeouts), 8)
        self.assertTrue(status["gemini_api_key_configured"])
        self.assertFalse(status["local_bedrock_enabled"])
        self.assertNotIn("synthetic-test-marker", json.dumps(status))
        self.assertEqual(status["up_count"], 2)
        self.assertEqual(status["total_services"], 8)
        self.assertTrue(status["ports_up"]["litellm"])
        self.assertTrue(status["ports_up"]["claude_ram"])
        self.assertFalse(status["ports_up"]["jev"])
        expected_ports = {
            "litellm": 4000,
            "jev": 8765,
            "fastmcp": 8770,
            "zoekt": 6070,
            "coderag": 18808,
            "serena": 24280,
            "claude_ram": 24302,
            "gpu_burst": 8000,
        }
        for svc, port in expected_ports.items():
            self.assertEqual(status["services"][svc]["port"], port)

        # CLI dispatch via `kaizen-kit neon ...`
        buf_plan = io.StringIO()
        with contextlib.redirect_stdout(buf_plan):
            rc_plan = cli.main(
                [
                    "neon",
                    "neon-plan",
                    "--prompt",
                    "Review VectorCube race",
                    "--task-type",
                    "code_review",
                    "--json",
                ]
            )
        self.assertEqual(rc_plan, 0)
        cli_plan = json.loads(buf_plan.getvalue())
        self.assertEqual(cli_plan["orchestrator"], "neon")
        self.assertEqual(cli_plan["cascade_decision"]["initial_model"], "qwen3.5-35b-a3b")

        buf_blocked = io.StringIO()
        with contextlib.redirect_stdout(buf_blocked):
            rc_blocked = cli.main(
                [
                    "neon",
                    "neon-plan",
                    "--prompt",
                    "deploy to kaizen-prod",
                    "--data-classification",
                    "public_sanitized",
                    "--json",
                ]
            )
        self.assertEqual(rc_blocked, 1)
        self.assertTrue(json.loads(buf_blocked.getvalue())["blocked"])

        buf_status = io.StringIO()
        with patch.object(carbon_orchestrator.socket, "create_connection", side_effect=_fake_connect):
            with patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": "", "KAIZEN_DISABLE_LOCAL_BEDROCK": ""},
                clear=False,
            ):
                with contextlib.redirect_stdout(buf_status):
                    rc_status = cli.main(["neon", "status", "--json"])
        self.assertEqual(rc_status, 0)
        cli_status = json.loads(buf_status.getvalue())
        self.assertEqual(cli_status["orchestrator"], "neon")
        self.assertFalse(cli_status["gemini_api_key_configured"])
        self.assertTrue(cli_status["local_bedrock_enabled"])

    def test_phase2_levers_execution_gate_best_of_n_distributed_rpc_and_neon(self) -> None:
        models = carbon_orchestrator.ARMY_MODELS
        for required_alias in (
            "gemini-4-neon",
            "gemini-4-argon",
            "gemini-3.1-flash-lite",
            "bge-m3",
            "qwen3-reranker-0.6b",
            "granite-docling-258m",
            "gemma-4-12b-unified",
            "olmocr-2-7b",
            "qwen3-235b-a22b-rpc",
            "qwen3-guard-stream-0.6b",
            "whisper-large-v3-turbo",
            "kokoro-82m",
        ):
            self.assertIn(required_alias, models)
            self.assertTrue(models[required_alias].commercial_safe)

        # RAM ceiling math + LiteLLM YAML completeness
        m7g_cfg = carbon_orchestrator.generate_m7g_litellm_config()
        self.assertEqual(m7g_cfg["safe_weight_ceiling_gib"], 72.0)
        self.assertEqual(m7g_cfg["safe_weight_headroom_gb"], round(72.0 - m7g_cfg["resident_weights_gb"], 2))
        for model_name in ("gemma-4-26b-a4b", "granite-4-h-tiny", "granite-4.0-h-tiny", "bitnet-2b", "bitnet"):
            self.assertIn(f"model_name: {model_name}", m7g_cfg["litellm_yaml"])

        # Lever C: 16x g4dn.xlarge = 64 vCPU (L-3819A6DF) + 256 GB VRAM passes only with distributed_rpc=True
        fit_ok = carbon_orchestrator.validate_deployment_fit(
            "qwen3-235b-a22b-rpc", "g4dn.xlarge", nodes=16, distributed_rpc=True
        )
        self.assertTrue(fit_ok["ok"])
        fit_over = carbon_orchestrator.validate_deployment_fit(
            "qwen3-235b-a22b-rpc", "g4dn.xlarge", nodes=17, distributed_rpc=True
        )
        self.assertFalse(fit_over["ok"])
        fit_no_rpc = carbon_orchestrator.validate_deployment_fit(
            "qwen3-235b-a22b-rpc", "g4dn.xlarge", nodes=16, distributed_rpc=False
        )
        self.assertFalse(fit_no_rpc["ok"])
        prov = carbon_orchestrator.build_provision_plan()
        self.assertTrue(prov["phase4_distributed_256gb_rpc"]["fit_verified"])
        self.assertEqual(prov["phase4_distributed_256gb_rpc"]["idle_cost_usd"], 0.0)

        # ExecutionGateSpec escalates confident-sounding invalid JSON / broken Python AST
        schema = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}}
        gate = carbon_orchestrator.ExecutionGateSpec(json_schema=schema)
        self.assertFalse(
            carbon_orchestrator.CascadeRouter.evaluate_execution_gate(
                "Here is a confident prose answer with no JSON at all.", gate
            )["passed"]
        )
        self.assertTrue(
            carbon_orchestrator.CascadeRouter.evaluate_execution_gate('{"verdict": "APPROVE"}', gate)["passed"]
        )

        schema_router = MagicMock(spec=model_router.ModelRouter)
        schema_router.chat_text.side_effect = [
            "Confident hallucinated prose missing JSON schema completely.",
            '{"verdict": "APPROVE"}',
        ]
        gated_cascade = carbon_orchestrator.CascadeRouter(router=schema_router).execute_cascade(
            "Emit structured review verdict",
            task_type="code_write",
            gate=gate,
        )
        self.assertTrue(gated_cascade["escalated"])
        self.assertEqual(gated_cascade["final_model"], "qwen3.5-35b-a3b")
        self.assertEqual(gated_cascade["output"], '{"verdict": "APPROVE"}')

        # Lever D: Execution-Verified Best-of-N dry_run, candidate filtering, SFT/DPO pairs & escalation
        bon_dry = carbon_orchestrator.CascadeRouter().execute_best_of_n(
            "Write pure Python helper", n=3, dry_run=True
        )
        self.assertEqual(bon_dry["status"], "DRY_RUN")
        self.assertEqual(bon_dry["total_candidates"], 3)
        self.assertFalse(bon_dry["escalated"])

        py_gate = carbon_orchestrator.ExecutionGateSpec(
            ast_lang="python",
            verifier_fn=lambda code: ("def add(" in code, "must define add()"),
        )
        bon_offline = carbon_orchestrator.CascadeRouter().execute_best_of_n(
            "Implement add(a, b)",
            gate=py_gate,
            candidates=[
                "def broken(:\n    pass",
                "def other(x):\n    return x",
                "def add(a: int, b: int) -> int:\n    return a + b",
            ],
        )
        self.assertEqual(bon_offline["status"], "OK")
        self.assertEqual(bon_offline["passed_count"], 1)
        self.assertEqual(bon_offline["total_candidates"], 3)
        self.assertFalse(bon_offline["escalated"])
        self.assertIn("def add", bon_offline["winner"]["output"])
        self.assertEqual(len(bon_offline["sft_dpo_pairs"]), 2)

        # Best-of-N escalates to Layer 3 when 0 of N Layer-2 candidates pass execution gate
        bon_router = MagicMock(spec=model_router.ModelRouter)
        bon_router.chat_text.side_effect = [
            "def bad1(:",
            "def bad2(:",
            "def add(a, b):\n    return a + b",
        ]
        bon_esc = carbon_orchestrator.CascadeRouter(router=bon_router).execute_best_of_n(
            "Implement add(a, b)",
            n=2,
            gate=py_gate,
        )
        self.assertTrue(bon_esc["escalated"])
        self.assertEqual(bon_esc["escalation_model"], "claude-sonnet-5-5")
        self.assertTrue(bon_esc["winner"]["passed"])

        # DataResidencyGate blocks corporate identifiers on public_sanitized Best-of-N
        with self.assertRaises(PermissionError):
            carbon_orchestrator.CascadeRouter().execute_best_of_n(
                "Inspect kaizen-prod logs",
                data_classification="public_sanitized",
                dry_run=True,
            )

        # Closed loopback / upstream error returns structured ERROR without TypeError
        err_router = MagicMock(spec=model_router.ModelRouter)
        err_router.chat_text.side_effect = RuntimeError("EC2 OSS endpoints unreachable")
        bon_err = carbon_orchestrator.CascadeRouter(router=err_router).execute_best_of_n(
            "Implement add(a, b)",
            n=2,
            gate=py_gate,
        )
        self.assertEqual(bon_err["status"], "ERROR")
        self.assertTrue(bon_err["escalated"])
        self.assertEqual(bon_err["passed_count"], 0)

        # Phase 2B: TALE dynamic token-budget policy & CAPC byte-exact prefix preservation
        triage_budget = carbon_orchestrator.compute_tale_budget("file_selection", "high", prompt_tokens=5000)
        self.assertEqual(triage_budget["recommended_public_model"], "gemini-decision-1")
        self.assertEqual(triage_budget["recommended_corporate_model"], "qwen3.5-4b")
        self.assertEqual(triage_budget["thinking_level"], "MINIMAL")
        self.assertTrue(triage_budget["decision_zero_output_tokens"])
        self.assertTrue(triage_budget["implicit_cache_eligible"])

        low_budget = carbon_orchestrator.compute_tale_budget("code_write", "low", prompt_tokens=512)
        high_budget = carbon_orchestrator.compute_tale_budget("code_review", "high", prompt_tokens=8192)
        self.assertLess(low_budget["max_output_tokens"], high_budget["max_output_tokens"])
        self.assertEqual(low_budget["thinking_level"], "LOW")
        self.assertEqual(high_budget["thinking_level"], "HIGH")

        static_prefix = "=== IMMUTABLE_ARCHITECTURE_CONSTITUTION ===\nRule 1: Zero byte drift.\n"
        volatile_ctx = ("info: routine line\n" * 1200) + "ERROR: critical lock inversion\n" + ("tail: end\n" * 400)
        capc_res = carbon_orchestrator.compress_dynamic_context_capc(
            static_prefix, volatile_ctx, max_dynamic_chars=2000
        )
        self.assertEqual(capc_res["static_prefix"], static_prefix)
        self.assertTrue(capc_res["prefix_preserved"])
        self.assertTrue(capc_res["was_compressed"])
        self.assertLessEqual(capc_res["compressed_dynamic_chars"], 2000)
        self.assertGreater(capc_res["tokens_saved_estimate"], 0)
        self.assertTrue(capc_res["combined_prompt"].startswith(static_prefix))


if __name__ == "__main__":
    unittest.main()
