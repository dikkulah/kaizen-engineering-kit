#!/usr/bin/env python3
"""Comprehensive unit and integration tests for M4 GPU Burst Orchestrator, Shims, Guardrails & ADK wiring."""

from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
from typing import Any, Dict, List
import unittest
from unittest.mock import MagicMock, patch

from kaizen_kit import adk_agents, cli, gpu_burst_orchestrator, guardrails, model_router
from kaizen_kit.qlora_pipeline import plan_or_launch_cluster
from kaizen_kit.radar import ec2_spot_batch


def _capture_cli(argv: List[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


class _FakeEc2Client:
    """Mock EC2 client returning deterministic offerings, AZs, placement scores, and Spot prices."""

    def __init__(self, region: str, sps_score: int = 1) -> None:
        self.region = region
        self.sps_score = sps_score

    def describe_instance_type_offerings(self, **kwargs: Any) -> Dict[str, Any]:
        values = kwargs.get("Filters", [{}])[0].get("Values", [])
        if self.region == "eu-west-1":
            allowed = {
                "g4dn.xlarge",
                "g4dn.2xlarge",
                "g4dn.12xlarge",
                "g5.xlarge",
                "g5.2xlarge",
                "g5.12xlarge",
                "g5.24xlarge",
                "g5.48xlarge",
                "r7g.16xlarge",
                "r7i.24xlarge",
            }
            azs = ["eu-west-1a", "eu-west-1b", "eu-west-1c"]
        else:
            allowed = set(values)
            azs = [f"{self.region}a", f"{self.region}b", f"{self.region}c"]
        rows = [
            {"InstanceType": itype, "Location": az}
            for itype in values
            if itype in allowed
            for az in azs
        ]
        return {"InstanceTypeOfferings": rows}

    def describe_availability_zones(self, **kwargs: Any) -> Dict[str, Any]:
        if self.region == "eu-west-1":
            return {
                "AvailabilityZones": [
                    {"ZoneId": "euw1-az1", "ZoneName": "eu-west-1a"},
                    {"ZoneId": "euw1-az2", "ZoneName": "eu-west-1b"},
                    {"ZoneId": "euw1-az3", "ZoneName": "eu-west-1c"},
                ]
            }
        return {
            "AvailabilityZones": [
                {"ZoneId": "euc1-az1", "ZoneName": f"{self.region}a"},
                {"ZoneId": "euc1-az2", "ZoneName": f"{self.region}b"},
                {"ZoneId": "euc1-az3", "ZoneName": f"{self.region}c"},
            ]
        }

    def get_spot_placement_scores(self, **kwargs: Any) -> Dict[str, Any]:
        if self.region == "eu-west-1":
            return {
                "SpotPlacementScores": [
                    {"Region": "eu-west-1", "AvailabilityZoneId": "euw1-az1", "Score": self.sps_score},
                    {"Region": "eu-west-1", "AvailabilityZoneId": "euw1-az2", "Score": max(1, self.sps_score - 1)},
                    {"Region": "eu-west-1", "AvailabilityZoneId": "euw1-az3", "Score": self.sps_score},
                ]
            }
        return {
            "SpotPlacementScores": [
                {"Region": self.region, "AvailabilityZoneId": "euc1-az1", "Score": self.sps_score},
                {"Region": self.region, "AvailabilityZoneId": "euc1-az3", "Score": self.sps_score},
            ]
        }

    def describe_spot_price_history(self, **kwargs: Any) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        itypes = kwargs.get("InstanceTypes", ["g5.12xlarge"])
        primary = itypes[0]
        if self.region == "eu-west-1":
            return {
                "SpotPriceHistory": [
                    {"InstanceType": primary, "AvailabilityZone": "eu-west-1a", "SpotPrice": "2.4500", "Timestamp": now},
                    {"InstanceType": primary, "AvailabilityZone": "eu-west-1b", "SpotPrice": "3.2700", "Timestamp": now},
                    {"InstanceType": primary, "AvailabilityZone": "eu-west-1c", "SpotPrice": "3.1400", "Timestamp": now},
                ]
            }
        return {
            "SpotPriceHistory": [
                {"InstanceType": primary, "AvailabilityZone": f"{self.region}a", "SpotPrice": "2.6000", "Timestamp": now},
                {"InstanceType": primary, "AvailabilityZone": f"{self.region}c", "SpotPrice": "2.4900", "Timestamp": now},
            ]
        }


class _FakeServiceQuotasClient:
    def __init__(self, quota_value: float = 64.0) -> None:
        self.quota_value = quota_value

    def get_service_quota(self, ServiceCode: str, QuotaCode: str) -> Dict[str, Any]:
        return {"Quota": {"QuotaCode": QuotaCode, "Value": self.quota_value}}


class TestGpuBurstOrchestrator(unittest.TestCase):
    def test_safe_defaults_and_tenancy_isolation(self) -> None:
        self.assertEqual(gpu_burst_orchestrator.DEFAULT_STATE, "STOPPED")
        self.assertTrue(gpu_burst_orchestrator.DEFAULT_DRY_RUN)
        self.assertFalse(gpu_burst_orchestrator.DEFAULT_EXECUTE_APPROVED_U2)
        self.assertEqual(gpu_burst_orchestrator.DEFAULT_IDLE_STOP_MINUTES, 15)
        self.assertEqual(gpu_burst_orchestrator.DEFAULT_MIN_SPS, 7)
        self.assertEqual(gpu_burst_orchestrator.TIER_FLEET_MIN_NODES, 2)
        self.assertEqual(gpu_burst_orchestrator.TIER_FLEET_MAX_NODES, 16)

        with self.assertRaises(ValueError):
            gpu_burst_orchestrator.plan_gpu_burst(profile="production", check_aws=False)
        with self.assertRaises(ValueError):
            gpu_burst_orchestrator.plan_gpu_burst(profile="kaizen-prod", check_aws=False)

    def test_four_tiers_and_2d_sparsity_configs(self) -> None:
        tiers = gpu_burst_orchestrator.GPU_BURST_TIERS
        self.assertEqual(
            set(tiers.keys()),
            {"daytime_bedrock", "large", "distributed_256gb", "frontier_1t", "ultra_frontier_2_8t"},
        )

        day = gpu_burst_orchestrator.plan_gpu_burst(tier="daytime_bedrock", check_aws=False)
        self.assertFalse(day["region_mapping"]["ec2_required"])
        self.assertEqual(day["finops_estimate"]["estimated_total_usd"], 0.0)
        self.assertIn("claude-opus-5-5", tiers["daytime_bedrock"].models)

        large = gpu_burst_orchestrator.plan_gpu_burst(tier="large", check_aws=False)
        self.assertEqual(large["sparsity_config"]["runai_streamer"]["load_format"], "runai_streamer")
        self.assertIn(
            "s3://kaizen-u2-dev-ncv-duckdb/kaizen-cloud-brain/models/",
            large["sparsity_config"]["runai_streamer"]["s3_model_uri"],
        )
        self.assertIn("MTP", large["sparsity_config"]["depth_axis"]["supported"])
        self.assertIn("REAP", large["sparsity_config"]["width_axis"]["supported"])

        f1t = gpu_burst_orchestrator.plan_gpu_burst(tier="frontier_1t", check_aws=False)
        self.assertIn("KTransformers", f1t["sparsity_config"]["width_axis"]["supported"])
        self.assertIn("r7g.16xlarge", f1t["region_mapping"]["selected_instance_types"])

        uf = gpu_burst_orchestrator.plan_gpu_burst(tier="ultra_frontier_2_8t", check_aws=False)
        self.assertEqual(uf["aws_batch_plan"]["allocation_strategy"], "SPOT_CAPACITY_OPTIMIZED")
        self.assertIn("REAP55 (319 GiB)", uf["sparsity_config"]["width_axis"]["supported"])

    def test_eu_west_1_maps_96gb_large_to_g5_and_preserves_g6_in_eu_central_1(self) -> None:
        eu_west = gpu_burst_orchestrator.resolve_region_instance_types(
            "large", region="eu-west-1", ec2_client=_FakeEc2Client("eu-west-1")
        )
        self.assertEqual(eu_west["selected_instance_types"][0], "g4dn.12xlarge")
        self.assertIn("g5.12xlarge", eu_west["selected_instance_types"])
        excluded = {item["instance_type"]: item["reason"] for item in eu_west["excluded_types"]}
        self.assertIn("g6.12xlarge", excluded)
        self.assertIn("g6e.12xlarge", excluded)
        self.assertIn("g4dn.12xlarge", excluded["g6.12xlarge"])

        eu_central = gpu_burst_orchestrator.resolve_region_instance_types(
            "large", region="eu-central-1", ec2_client=_FakeEc2Client("eu-central-1")
        )
        self.assertEqual(eu_central["selected_instance_types"][:2], ["g6.12xlarge", "g6e.12xlarge"])
        self.assertEqual(eu_central["excluded_types"], [])

    def test_tier2_spot_fleet_bounds_and_64_vcpu_quota_guard(self) -> None:
        with self.assertRaises(ValueError):
            gpu_burst_orchestrator.plan_spot_fleet(nodes=1, check_aws=False)
        with self.assertRaises(ValueError):
            gpu_burst_orchestrator.plan_spot_fleet(nodes=17, check_aws=False)

        sq = _FakeServiceQuotasClient(64.0)
        q_fail = gpu_burst_orchestrator.check_g_spot_quota(["g5.12xlarge"], nodes=2, sq_client=sq)
        self.assertFalse(q_fail["passed"])
        self.assertEqual(q_fail["requested_vcpus"], 96)
        self.assertEqual(q_fail["quota_vcpus"], 64.0)

        q_ok = gpu_burst_orchestrator.check_g_spot_quota(["g5.xlarge"], nodes=10, sq_client=sq)
        self.assertTrue(q_ok["passed"])
        self.assertEqual(q_ok["requested_vcpus"], 40)

        fleet_10 = gpu_burst_orchestrator.plan_spot_fleet(
            nodes=10,
            tier="large",
            candidate_types=["g5.xlarge"],
            ec2_client=_FakeEc2Client("eu-west-1", sps_score=8),
            sq_client=sq,
        )
        self.assertEqual(fleet_10["nodes"], 10)
        self.assertTrue(fleet_10["quota_check"]["passed"])
        self.assertEqual(fleet_10["default_state"], "STOPPED")
        self.assertFalse(fleet_10["aws_mutated"])

    def test_spot_placement_sps_gate_and_az_price_ranking(self) -> None:
        low_sps = gpu_burst_orchestrator.plan_gpu_burst(
            tier="large",
            region="eu-west-1",
            dry_run=False,
            execute_approved_u2=True,
            ec2_client=_FakeEc2Client("eu-west-1", sps_score=1),
            sq_client=_FakeServiceQuotasClient(64.0),
        )
        self.assertFalse(low_sps["spot_placement"]["sps_gate_passed"])
        self.assertFalse(low_sps["launch_eligible"])
        self.assertEqual(low_sps["mode"], "BLOCKED_BY_GUARD")
        self.assertIn("max_sps=1 < required 7", low_sps["spot_placement"]["sps_diagnostic"])

        # Tie on Score=8 between eu-west-1a ($2.45) and eu-west-1c ($3.14) -> 1a ranks first.
        high_sps = gpu_burst_orchestrator.plan_gpu_burst(
            tier="large",
            region="eu-west-1",
            dry_run=False,
            execute_approved_u2=True,
            ec2_client=_FakeEc2Client("eu-west-1", sps_score=8),
            sq_client=_FakeServiceQuotasClient(64.0),
        )
        self.assertTrue(high_sps["spot_placement"]["sps_gate_passed"])
        self.assertTrue(high_sps["launch_eligible"])
        self.assertEqual(high_sps["spot_placement"]["ranked_azs"][0]["availability_zone"], "eu-west-1a")
        self.assertEqual(high_sps["spot_placement"]["ranked_azs"][0]["spot_price_usd_hr"], 2.45)

    def test_imdsv2_interruption_checkpoint_and_15min_idle_stop_userdata(self) -> None:
        ud = gpu_burst_orchestrator.build_spot_userdata(run_id="m4-test-01")
        self.assertIn("http://169.254.169.254/latest/api/token", ud)
        self.assertIn("X-aws-ec2-metadata-token-ttl-seconds: 21600", ud)
        self.assertIn("http://169.254.169.254/latest/meta-data/spot/instance-action", ud)
        self.assertIn("s3://kaizen-u2-dev-ncv-duckdb/kaizen-cloud-brain/spot-checkpoints/m4-test-01", ud)
        self.assertIn("trap sync_spot_checkpoints EXIT INT TERM", ud)
        self.assertIn("--on-active=15m", ud)

        b64 = ec2_spot_batch._build_userdata(15, run_id="shim-test")
        decoded = base64.b64decode(b64).decode("utf-8")
        self.assertIn("spot/instance-action", decoded)
        self.assertIn("--on-active=15m", decoded)

    def test_backwards_compatible_shims_delegate_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.object(
                gpu_burst_orchestrator,
                "_resolve_live_or_offline_clients",
                return_value=(_FakeEc2Client("eu-west-1", sps_score=7), _FakeServiceQuotasClient(64.0)),
            ):
                summary = ec2_spot_batch.run_spot_batch(
                    repos=[("kit", Path(td))],
                    out_dir=Path(td) / "out",
                    dry_run=True,
                )
            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["default_state"], "STOPPED")
            self.assertEqual(summary["idle_stop_minutes"], 15)
            self.assertEqual(summary["runai_streamer"]["load_format"], "runai_streamer")

        cluster = plan_or_launch_cluster(execute_approved_u2=False)
        self.assertEqual(cluster["mode"], "DRY_RUN_PLAN_ONLY")
        self.assertEqual(cluster["default_state"], "STOPPED")
        self.assertEqual(cluster["region_mapping"]["selected_instance_types"][0], "g4dn.12xlarge")
        self.assertEqual(len(cluster["lora_portfolio"]), 5)
        self.assertIn("spot/instance-action", cluster["gpu_userdata_preview"])

    def test_guardrails_filter_and_adk_map_reduce_wiring(self) -> None:
        # Start a local mock ModernBERT /classify + /v1/systemone server
        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/classify":
                    resp = {"blocked": False}
                else:
                    state = body.get("state", "")
                    score = 0.15 if "noise" in state.lower() else 0.92
                    resp = {"answers": {"relevance": {"score": score}}}
                raw = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        bert_url = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            kept = guardrails.filter_mapped_candidate(
                "Audit parity", "Verified 14 endpoint payloads", bert_url=bert_url, fallback_jev_url=bert_url
            )
            self.assertTrue(kept["kept"])
            self.assertGreaterEqual(kept["score"], 0.9)

            dropped = guardrails.filter_mapped_candidate(
                "Audit parity", "pure noise candidate", bert_url=bert_url, fallback_jev_url=bert_url
            )
            self.assertFalse(dropped["kept"])

            leaked = guardrails.filter_mapped_candidate(
                "Audit parity", "token glpat-" + "abcdef1234567890abcd", bert_url=bert_url, fallback_jev_url=bert_url
            )
            self.assertFalse(leaked["kept"])
            self.assertEqual(leaked["rail"], "secret")

            mock_router = MagicMock(spec=model_router.ModelRouter)
            mock_router.resolve.side_effect = lambda name: model_router.RouteTarget(
                requested_model=name,
                canonical_model=name,
                backend="bedrock" if "claude" in name else "ec2_litellm",
                endpoint="bedrock://kaizen-claude" if "claude" in name else "http://127.0.0.1:4000",
                provider_hint="bedrock" if "claude" in name else "litellm",
                requires_local_aws_profile="claude" in name,
            )
            mock_router.chat_text.side_effect = [
                "Verified VectorCube parity evidence",
                "noise item",
                "REDUCED_CLAUDE_5_5: VectorCube parity verified",
            ]
            mr = adk_agents.LlmArmyMapReduce(
                router=mock_router,
                filter_url=bert_url,
                fallback_jev_url=bert_url,
                spot_tier="large",
                spot_nodes=2,
            )
            out = mr.execute(prompt="Verify VectorCube parity", items=["shard-1", "shard-2"])
            self.assertEqual(out["status"], "OK")
            self.assertEqual(out["mapped_count"], 2)
            self.assertEqual(out["kept_count"], 1)
            self.assertEqual(out["filtered_out_count"], 1)
            self.assertEqual(out["noise_reduction_ratio"], 0.5)
            self.assertTrue(out["input_rail"]["allowed"])
            self.assertEqual(out["spot_burst_plan"]["nodes"], 2)

            blocked_out = mr.execute(
                prompt="aws lambda update-function-code --profile kaizen-prod",
                items=["shard-1"],
            )
            self.assertEqual(blocked_out["status"], "BLOCKED_BY_INPUT_RAIL")
            self.assertFalse(blocked_out["input_rail"]["allowed"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_cli_gpu_burst_and_adk_flags(self) -> None:
        rc1, out1 = _capture_cli(["gpu-burst", "tiers", "--json"])
        self.assertEqual(rc1, 0)
        self.assertEqual(json.loads(out1)["default_state"], "STOPPED")

        rc2, out2 = _capture_cli(["gpu-burst", "fleet", "--nodes", "4", "--no-aws", "--json"])
        self.assertEqual(rc2, 0)
        parsed2 = json.loads(out2)
        self.assertEqual(parsed2["nodes"], 4)
        self.assertEqual(parsed2["default_state"], "STOPPED")

        rc3, out3 = _capture_cli(
            [
                "adk",
                "map-reduce",
                "--prompt",
                "Verify M4",
                "--spot-tier",
                "large",
                "--spot-nodes",
                "2",
                "--dry-run",
                "--json",
            ]
        )
        self.assertEqual(rc3, 0)
        parsed3 = json.loads(out3)
        self.assertEqual(parsed3["status"], "DRY_RUN")
        self.assertEqual(parsed3["spot_burst_plan"]["nodes"], 2)

    def test_m4_r2_quota_filtered_spot_placement_finops_and_pagination(self) -> None:
        class _MultiPricePaginatedEc2(_FakeEc2Client):
            def __init__(self, region: str = "eu-west-1", sps_score: int = 8) -> None:
                super().__init__(region=region, sps_score=sps_score)
                self.sps_queries: List[List[str]] = []

            def get_spot_placement_scores(self, **kwargs: Any) -> Dict[str, Any]:
                self.sps_queries.append(list(kwargs.get("InstanceTypes", [])))
                return super().get_spot_placement_scores(**kwargs)

            def describe_spot_price_history(self, **kwargs: Any) -> Dict[str, Any]:
                now = datetime.now(timezone.utc)
                itypes = list(kwargs.get("InstanceTypes", []))
                token = kwargs.get("NextToken")
                if not token:
                    rows = []
                    if "g5.12xlarge" in itypes:
                        rows.append(
                            {"InstanceType": "g5.12xlarge", "AvailabilityZone": "eu-west-1a", "SpotPrice": "2.4500", "Timestamp": now}
                        )
                    if "g5.2xlarge" in itypes:
                        rows.append(
                            {"InstanceType": "g5.2xlarge", "AvailabilityZone": "eu-west-1a", "SpotPrice": "0.8602", "Timestamp": now}
                        )
                    return {"SpotPriceHistory": rows, "NextToken": "page-2"}
                rows = []
                if "g5.xlarge" in itypes:
                    rows.append(
                        {"InstanceType": "g5.xlarge", "AvailabilityZone": "eu-west-1a", "SpotPrice": "0.6680", "Timestamp": now}
                    )
                if "g4dn.xlarge" in itypes:
                    rows.append(
                        {"InstanceType": "g4dn.xlarge", "AvailabilityZone": "eu-west-1a", "SpotPrice": "0.2150", "Timestamp": now}
                    )
                return {"SpotPriceHistory": rows}

        sq = _FakeServiceQuotasClient(64.0)
        for node_count in (2, 4):
            ec2_mock = _MultiPricePaginatedEc2("eu-west-1", sps_score=8)
            plan = gpu_burst_orchestrator.plan_spot_fleet(
                nodes=node_count,
                tier="large",
                region="eu-west-1",
                dry_run=False,
                execute_approved_u2=True,
                ec2_client=ec2_mock,
                sq_client=sq,
            )
            self.assertEqual(ec2_mock.sps_queries, [["g4dn.2xlarge", "g4dn.xlarge", "g5.2xlarge", "g5.xlarge"]])
            self.assertNotIn("g4dn.12xlarge", plan["spot_placement"]["sps_diagnostic"])
            self.assertNotIn("g5.12xlarge", plan["spot_placement"]["sps_diagnostic"])
            self.assertNotIn("g5.12xlarge", {r["recommended_instance_type"] for r in plan["spot_placement"]["ranked_azs"]})
            self.assertIn("g5.xlarge@eu-west-1a", plan["spot_placement"]["spot_prices_usd"])
            self.assertIn("g4dn.xlarge@eu-west-1a", plan["spot_placement"]["spot_prices_usd"])
            top_az = plan["spot_placement"]["ranked_azs"][0]
            self.assertEqual(top_az["recommended_instance_type"], "g4dn.xlarge")
            self.assertEqual(top_az["spot_price_usd_hr"], 0.215)
            self.assertEqual(plan["finops_estimate"]["primary_instance_type"], "g4dn.xlarge")
            self.assertEqual(plan["finops_estimate"]["hourly_spot_usd_per_node"], 0.215)

        # Over-quota single candidate must never recommend or set launch_eligible=True
        ec2_over = _MultiPricePaginatedEc2("eu-west-1", sps_score=9)
        blocked_over = gpu_burst_orchestrator.plan_spot_fleet(
            nodes=2,
            tier="large",
            region="eu-west-1",
            candidate_types=["g5.12xlarge"],
            dry_run=False,
            execute_approved_u2=True,
            ec2_client=ec2_over,
            sq_client=sq,
        )
        self.assertFalse(blocked_over["quota_check"]["passed"])
        self.assertFalse(blocked_over["spot_placement"]["sps_gate_passed"])
        self.assertEqual(blocked_over["spot_placement"]["ranked_azs"], [])
        self.assertFalse(blocked_over["launch_eligible"])
        self.assertEqual(ec2_over.sps_queries, [])

    def test_m4_r2_vcpu_size_suffix_vt1_sparsity_and_validation(self) -> None:
        self.assertEqual(gpu_burst_orchestrator._vcpus_for("g4dn.xlarge"), 4)
        self.assertEqual(gpu_burst_orchestrator._vcpus_for("g5.4xlarge"), 16)
        self.assertEqual(gpu_burst_orchestrator._vcpus_for("g5.metal"), 96)
        self.assertTrue(gpu_burst_orchestrator._is_g_family("vt1.3xlarge"))

        q_g4dn = gpu_burst_orchestrator.check_g_spot_quota(
            ["g4dn.xlarge"], nodes=2, sq_client=_FakeServiceQuotasClient(64.0)
        )
        self.assertTrue(q_g4dn["passed"])
        self.assertEqual(q_g4dn["requested_vcpus"], 8)

        self.assertIsNone(gpu_burst_orchestrator.build_2d_sparsity_config("frontier_1t")["runai_streamer"])
        self.assertIsNone(gpu_burst_orchestrator.build_2d_sparsity_config("ultra_frontier_2_8t")["runai_streamer"])

        with self.assertRaises(ValueError):
            gpu_burst_orchestrator.plan_spot_fleet(tier="daytime_bedrock", nodes=3, check_aws=False)
        with self.assertRaises(ValueError):
            gpu_burst_orchestrator.resolve_region_instance_types("large", region="invalid-region-1", ec2_client=None)
        with self.assertRaises(ValueError):
            gpu_burst_orchestrator.plan_gpu_burst(tier="large", region="invalid-region-1", check_aws=False)

    def test_m4_r2_training_cluster_shim_checkpoint_uri_and_region_propagation(self) -> None:
        plan = plan_or_launch_cluster(region="eu-central-1", execute_approved_u2=False)
        self.assertEqual(plan["region"], "eu-central-1")
        self.assertEqual(plan["finops_estimate"]["region"], "eu-central-1")
        self.assertEqual(plan["finops_estimate"]["primary_instance_type"], "g6.12xlarge")
        self.assertIn("--region eu-central-1", plan["gpu_userdata_preview"])
        self.assertIn(
            'export S3_CHECKPOINT_URI="s3://kaizen-u2-dev-ncv-duckdb/telemetry-lora/checkpoints"',
            plan["gpu_userdata_preview"],
        )
        self.assertIn('systemd-run --unit="kaizen-idle-stop-telemetry-lora"', plan["gpu_userdata_preview"])
        self.assertEqual(plan["s3_sync_target"], "s3://kaizen-u2-dev-ncv-duckdb/telemetry-lora/checkpoints/")

    def test_m4_r2_guardrails_malformed_json_shell_block_and_adk_validation(self) -> None:
        # Destructive shell commands must fail closed offline
        for cmd in (
            "rm -rf /",
            "rm -fr /*",
            "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda bs=1M",
            ":(){ :|:& };:",
        ):
            res = guardrails.evaluate_input_rail(cmd, bert_url="http://127.0.0.1:19999")
            self.assertFalse(res.allowed, f"Expected blocked for: {cmd}")
            self.assertEqual(res.category, "host_stability")

        self.assertTrue(guardrails.evaluate_input_rail("rm -rf /tmp/build", bert_url="http://127.0.0.1:19999").allowed)
        self.assertFalse(guardrails.evaluate_output_rail("leaked .env credentials").allowed)

        # Malformed JSON from /classify and /v1/systemone must degrade cleanly to offline_heuristic
        class _MalformedHandler(BaseHTTPRequestHandler):
            payloads: List[Any] = []
            calls: int = 0

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                _MalformedHandler.calls += 1
                body = _MalformedHandler.payloads.pop(0) if _MalformedHandler.payloads else ["unexpected_list"]
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        srv = HTTPServer(("127.0.0.1", 0), _MalformedHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        bad_url = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            _MalformedHandler.payloads = [
                {"score": None},
                {"answers": {"relevance": {"score": None}}},
            ]
            res_none = guardrails.filter_mapped_candidate(
                "Audit parity",
                "Verified endpoint parity across 14 fields.",
                bert_url=bad_url,
                fallback_jev_url=bad_url,
                probe_http=True,
            )
            self.assertTrue(res_none["kept"])
            self.assertEqual(res_none["backend"], "offline_heuristic")

            _MalformedHandler.payloads = [
                ["unexpected_list"],
                ["unexpected_list"],
            ]
            res_list = guardrails.filter_mapped_candidate(
                "Audit parity",
                "Verified endpoint parity across 14 fields.",
                bert_url=bad_url,
                fallback_jev_url=bad_url,
                probe_http=True,
            )
            self.assertTrue(res_list["kept"])
            self.assertEqual(res_list["backend"], "offline_heuristic")

            # Verify LlmArmyMapReduce._filter_one does not re-probe matching endpoint on offline_heuristic
            mr_probe = adk_agents.LlmArmyMapReduce(
                router=MagicMock(spec=model_router.ModelRouter),
                filter_url=bad_url,
                fallback_jev_url=bad_url,
            )
            _MalformedHandler.calls = 0
            _MalformedHandler.payloads = [["unexpected_list"], ["unexpected_list"]]
            with patch.object(mr_probe, "_score_candidate", side_effect=AssertionError("redundant probe")):
                filtered = mr_probe._filter_one(
                    "Audit parity",
                    0,
                    "Verified endpoint parity across 14 fields.",
                    f"{bad_url}/v1/systemone",
                )
            self.assertTrue(filtered["kept"])
            self.assertEqual(filtered["backend"], "offline_heuristic")
            self.assertEqual(_MalformedHandler.calls, 2)
        finally:
            srv.shutdown()
            srv.server_close()

        with self.assertRaises(ValueError):
            adk_agents.LlmArmyMapReduce(
                router=MagicMock(spec=model_router.ModelRouter),
                spot_tier="large",
                spot_nodes=-1,
                spot_dry_run=True,
            ).execute(prompt="Verify M4 parity", items=["shard-1"], dry_run=True)

    def test_single_node_large_spot_check_enforces_64gb_vram_over_g5_xlarge(self) -> None:
        class _AllPricesEc2(_FakeEc2Client):
            def describe_spot_price_history(self, **kwargs: Any) -> Dict[str, Any]:
                now = datetime.now(timezone.utc)
                return {
                    "SpotPriceHistory": [
                        {"InstanceType": "g5.xlarge", "AvailabilityZone": "eu-west-1a", "SpotPrice": "0.5000", "Timestamp": now},
                        {"InstanceType": "g5.2xlarge", "AvailabilityZone": "eu-west-1a", "SpotPrice": "0.8600", "Timestamp": now},
                        {"InstanceType": "g4dn.12xlarge", "AvailabilityZone": "eu-west-1a", "SpotPrice": "1.9612", "Timestamp": now},
                        {"InstanceType": "g5.12xlarge", "AvailabilityZone": "eu-west-1a", "SpotPrice": "2.4500", "Timestamp": now},
                    ]
                }

        sq = _FakeServiceQuotasClient(64.0)
        single = gpu_burst_orchestrator.plan_gpu_burst(
            tier="large",
            nodes=1,
            region="eu-west-1",
            ec2_client=_AllPricesEc2("eu-west-1", sps_score=9),
            sq_client=sq,
        )
        self.assertEqual(single["spot_placement"]["min_vram_gb"], 64)
        self.assertEqual(single["spot_placement"]["ranked_azs"][0]["vram_filter_gb"], 64)
        self.assertEqual(single["spot_placement"]["ranked_azs"][0]["recommended_instance_type"], "g4dn.12xlarge")

        fleet = gpu_burst_orchestrator.plan_spot_fleet(
            nodes=2,
            tier="large",
            region="eu-west-1",
            ec2_client=_AllPricesEc2("eu-west-1", sps_score=9),
            sq_client=sq,
        )
        self.assertEqual(fleet["spot_placement"]["min_vram_gb"], 0)
        self.assertEqual(fleet["spot_placement"]["ranked_azs"][0]["recommended_instance_type"], "g4dn.2xlarge")

    def test_distributed_256gb_16_node_fleet_active_quota_and_lora_portfolio(self) -> None:
        sq = _FakeServiceQuotasClient(64.0)
        dist = gpu_burst_orchestrator.plan_gpu_burst(
            tier="distributed_256gb",
            region="eu-west-1",
            ec2_client=_FakeEc2Client("eu-west-1", sps_score=9),
            sq_client=sq,
        )
        self.assertEqual(dist["default_state"], "STOPPED")
        self.assertFalse(dist["aws_mutated"])
        self.assertEqual(dist["nodes"], 16)
        self.assertTrue(dist["quota_check"]["passed"])
        self.assertEqual(dist["quota_check"]["requested_vcpus"], 64)
        self.assertEqual(dist["sparsity_config"]["aggregate_vram_gb"], 256)
        self.assertTrue(dist["sparsity_config"]["t4_sm75_guard"])
        self.assertFalse(dist["sparsity_config"]["airllm_disk_streaming"])
        self.assertFalse(dist["sparsity_config"]["cpu_draft_speculative"])
        self.assertEqual(dist["sparsity_config"]["primary_runtime"], "llama_cpp_rpc")
        self.assertEqual(dist["sparsity_config"]["fallback_runtime"], "vllm_ray_pp")
        self.assertEqual(dist["distributed_fleet_spec"]["placement_group"]["GroupName"], "kaizen-dist256-cpg")
        self.assertEqual(dist["distributed_fleet_spec"]["create_fleet_spec"]["Type"], "instant")
        self.assertTrue(dist["distributed_fleet_spec"]["create_fleet_spec"]["EbsDeleteOnTermination"])
        self.assertIn("reap_fleet_peers", dist["userdata_preview"])

        # Active running G-Spot instance (1x g4dn.xlarge = 4 vCPUs) must block 16-node fleet (64 + 4 > 64)
        class _BusyEc2(_FakeEc2Client):
            def describe_instances(self, **kwargs: Any) -> Dict[str, Any]:
                return {"Reservations": [{"Instances": [{"InstanceType": "g4dn.xlarge"}]}]}

        blocked = gpu_burst_orchestrator.plan_gpu_burst(
            tier="distributed_256gb",
            region="eu-west-1",
            dry_run=False,
            execute_approved_u2=True,
            ec2_client=_BusyEc2("eu-west-1", sps_score=9),
            sq_client=sq,
        )
        self.assertFalse(blocked["quota_check"]["passed"])
        self.assertEqual(blocked["quota_check"]["active_g_spot_vcpus"], 4)
        self.assertEqual(blocked["quota_check"]["available_g_spot_vcpus"], 60.0)
        self.assertFalse(blocked["launch_eligible"])

        # Verify canonical 5-adapter Kaizen-wide LoRA portfolio & legacy redirects
        adapters = [row["adapter"] for row in gpu_burst_orchestrator.LORA_PORTFOLIO]
        self.assertEqual(
            adapters,
            [
                "lora-java-spring-craft",
                "lora-rust-vector-parity",
                "lora-vue-dashboard-ui",
                "lora-system-rca-triage",
                "lora-openapi-contract-guard",
            ],
        )
        self.assertEqual(gpu_burst_orchestrator.resolve_lora_adapter("lora-avro-c"), "lora-rust-vector-parity")
        self.assertEqual(gpu_burst_orchestrator.resolve_lora_adapter("lora-sql-semilattice"), "lora-rust-vector-parity")
        self.assertEqual(gpu_burst_orchestrator.resolve_lora_adapter("lora-parity-rca-judge"), "lora-system-rca-triage")

        job_spec = gpu_burst_orchestrator.build_qlora_factory_job_spec(adapters=["lora-avro-c"], mode="sft_dpo")
        self.assertEqual(job_spec["adapters"][0]["adapter"], "lora-rust-vector-parity")
        self.assertFalse(job_spec["aws_mutated"])
        self.assertEqual(gpu_burst_orchestrator.run_self_test(), 0)

    def test_phase2b_kv_optimization_prefix_cache_kvpress_and_gpu_only_eagle3(self) -> None:
        # Turing SM75 (g4dn): safe float16 vLLM KV + prefix caching + q8_0/cache-reuse 256 for RPC + GPU EAGLE-3 allowed
        sm75_kv = gpu_burst_orchestrator.build_kv_optimization_config(
            "g4dn.xlarge", enable_kvpress=True, kvpress_method="ExpectedAttentionPress", enable_eagle3=True
        )
        self.assertEqual(sm75_kv["vllm_kv_cache_dtype"], "float16")
        self.assertIn("--enable-prefix-caching", sm75_kv["vllm_kv_flags"])
        self.assertIn("--speculative-method", sm75_kv["vllm_kv_flags"])
        self.assertEqual(sm75_kv["llama_cpp_kv_flags"], ["-ctk", "q8_0", "-ctv", "q8_0", "--cache-reuse", "256"])
        self.assertTrue(sm75_kv["kvpress"]["enabled"])
        self.assertEqual(sm75_kv["kvpress"]["method"], "ExpectedAttentionPress")
        self.assertTrue(sm75_kv["eagle3"]["enabled"])
        self.assertFalse(sm75_kv["cpu_draft_speculative"])

        # Ada SM89 (g6/g6e): fp8 vLLM KV + KVzip support
        sm89_kv = gpu_burst_orchestrator.build_kv_optimization_config(
            "g6.12xlarge", enable_kvpress=True, kvpress_method="KVzip", enable_eagle3=True
        )
        self.assertEqual(sm89_kv["vllm_kv_cache_dtype"], "fp8")
        self.assertEqual(sm89_kv["kvpress"]["method"], "KVzip")
        self.assertTrue(sm89_kv["eagle3"]["enabled"])

        # CPU instances (m7g/r7g/r7i): EAGLE-3 explicitly blocked per DE-006; llama.cpp q8_0 + cache-reuse 256 active
        for cpu_type in ("m7g.8xlarge", "r7g.16xlarge", "r7i.24xlarge"):
            cpu_kv = gpu_burst_orchestrator.build_kv_optimization_config(cpu_type, enable_eagle3=True)
            self.assertTrue(cpu_kv["is_cpu_instance"])
            self.assertFalse(cpu_kv["eagle3"]["enabled"])
            self.assertIn("DE-006", cpu_kv["eagle3"]["blocked_reason"])
            self.assertFalse(cpu_kv["cpu_draft_speculative"])
            self.assertEqual(cpu_kv["llama_cpp_kv_flags"], ["-ctk", "q8_0", "-ctv", "q8_0", "--cache-reuse", "256"])

        dist_cfg = gpu_burst_orchestrator.build_2d_sparsity_config("distributed_256gb")
        self.assertIn("--cache-reuse", dist_cfg["llama_cpp_rpc"]["head_flags"])
        self.assertIn("q8_0", dist_cfg["llama_cpp_rpc"]["head_flags"])
        self.assertIn("--enable-prefix-caching", dist_cfg["vllm_serving_flags"])

        with self.assertRaises(ValueError):
            gpu_burst_orchestrator.build_kv_optimization_config("g4dn.12xlarge", kvpress_method="InvalidPress")


if __name__ == "__main__":
    unittest.main()
