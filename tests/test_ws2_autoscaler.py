"""Tests for WS-2 Mini-Pool 7-gateway SSM tunneling, model aliases, and autoscaler CLI wiring."""

import pytest

from kaizen_kit import localmodels, mini_pool


def test_tunnel_ports_use_7_core_gateway_ports_plus_optional():
    for required_port in (4000, 8765, 8766, 6070, 8770, 18808, 24280):
        assert required_port in mini_pool.TUNNEL_PORTS
    assert len(mini_pool.TUNNEL_PORTS) == 7
    assert len(set(mini_pool.TUNNEL_PORTS)) == 7
    assert 18807 in mini_pool.LEGACY_WORKER_TUNNEL_PORTS
    combined = (*mini_pool.TUNNEL_PORTS, *mini_pool.OPTIONAL_TUNNEL_PORTS)
    assert 18081 in combined
    assert len(set(combined)) == 8


def test_available_pool_models_include_qwen3_30b_aliases():
    assert "qwen3-30b-a3b" in localmodels.AVAILABLE_POOL_MODELS
    assert "qwen3-coder-30b-a3b" in localmodels.AVAILABLE_POOL_MODELS


def test_scale_workers_dry_run_invokes_autoscaler_ram_guard():
    res = mini_pool.scale_workers(2, dry_run=True)
    assert res["status"] == "dry_run"
    assert res["workers"] == 2
    assert "/opt/kaizen-mini-pool/autoscaler.py --scale 2" in res["command"]


@pytest.mark.parametrize("bad_workers", [0, -1, 11])
def test_scale_workers_rejects_out_of_range(bad_workers: int):
    with pytest.raises(ValueError, match="between 1 and 10"):
        mini_pool.scale_workers(bad_workers, dry_run=True)


def test_cli_parser_accepts_scale_and_qwen3_30b_model_flag():
    parser = mini_pool._build_cli_parser()
    scale_args = parser.parse_args(["scale", "--workers", "3", "--dry-run"])
    assert scale_args.cmd == "scale"
    assert scale_args.workers == 3
    assert scale_args.dry_run is True

    ask_args = parser.parse_args(["ask", "hello", "--model", "qwen3-30b-a3b"])
    assert ask_args.cmd == "ask"
    assert ask_args.model == "qwen3-30b-a3b"


def test_eu_west_1_spot_types_enforce_de006_g4dn_only_and_matryoshka_dim_align():
    from kaizen_kit.radar import ec2_spot_batch, embeddings

    assert ec2_spot_batch.DEFAULT_EU_WEST_1_SPOT_TYPES == ("g4dn.12xlarge", "g4dn.2xlarge", "g4dn.xlarge")
    for itype in ec2_spot_batch.DEFAULT_EU_WEST_1_SPOT_TYPES:
        assert itype.startswith("g4dn.")

    vec_1024 = [1.0] * 1024
    norm_768 = embeddings._l2_normalize(vec_1024, dim=embeddings.EMBED_DIM)
    assert len(norm_768) == 768

