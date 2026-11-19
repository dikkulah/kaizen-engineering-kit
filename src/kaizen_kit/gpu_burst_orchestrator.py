#!/usr/bin/env python3
"""Unified AWS `kaizen-u2` GPU Burst & Tier-2 Spot Fleet Orchestrator (WS-4 & WS-5).

Consolidates ephemeral Spot batch analysis (`radar/ec2_spot_batch.py`) and hybrid
CPU/GPU training cluster planning (`qlora_pipeline/ec2_training_cluster.py`) into one
fail-safe orchestrator with:
  - Safe defaults: `DEFAULT_STATE = "STOPPED"` (`$0`), `dry_run=True`,
    `execute_approved_u2=False`, `idle_stop_minutes=15`, `min_sps=7`, `g_spot_vcpu_quota=64`.
  - 4-Tier Burst Matrix (`daytime_bedrock`, `large`, `frontier_1t`, `ultra_frontier_2_8t`)
    with 2D Sparsity (`EAGLE-3`/`MTP`/`ngram-lookup` x `REAP`/`KTransformers`/`ik_llama.cpp`).
  - Region-aware offering mapping (`describe_instance_type_offerings`) mapping `eu-west-1`
    Large-tier Spot to `g4dn.12xlarge` (4x T4 64GB, SPS=8..9, SM75 float16 guard, DE-006)
    while retaining `g6.12xlarge`/`g6e.12xlarge` (Ada SM89) for `eu-central-1` / `us-east-1`.
  - Tier-2 Spot Fleet (`2 <= nodes <= 10`) with `L-3819A6DF` (`64` vCPU) quota enforcement,
    `get_spot_placement_scores` (`SPS >= 7`) + `describe_spot_price_history` AZ ranking,
    and IMDSv2 2-minute interruption S3 checkpointing + 15-minute dead-man idle-stop.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import uuid

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - optional runtime dependency for offline dry-run
    boto3 = None  # type: ignore[assignment]
    BotoCoreError = Exception  # type: ignore[assignment,misc]
    ClientError = Exception  # type: ignore[assignment,misc]


DEFAULT_PROFILE = "kaizen-u2"
DEFAULT_REGION = "eu-west-1"
DEFAULT_BUCKET = "kaizen-u2-dev-ncv-duckdb"
DEFAULT_CHECKPOINT_PREFIX = "kaizen-cloud-brain/spot-checkpoints"
DEFAULT_MODEL_STASH_PREFIX = "kaizen-cloud-brain/models"
DEFAULT_AMI = "ami-0ab2e9e4df98786f8"
DEFAULT_SG = "sg-0123456789abcdef0"
DEFAULT_IAM_PROFILE = (
    "arn:aws:iam::444455556666:instance-profile/kaizen-u2-ondemand-worker-InstanceProfile-00EXAMPLE0000"
)
DEFAULT_PRIVATE_SUBNETS: Tuple[str, ...] = (
    "subnet-03333333333333333",  # eu-west-1a
    "subnet-02222222222222222",  # eu-west-1b
    "subnet-01111111111111111",  # eu-west-1c
)
DEFAULT_INSTANCE_TYPES: Tuple[str, ...] = (
    "g4dn.12xlarge",
    "g4dn.2xlarge",
    "g4dn.xlarge",
    "g5.2xlarge",
    "g5.xlarge",
)

DEFAULT_STATE = "STOPPED"
DEFAULT_DRY_RUN = True
DEFAULT_EXECUTE_APPROVED_U2 = False
DEFAULT_IDLE_STOP_MINUTES = 15
DEFAULT_MIN_SPS = 7
DEFAULT_G_SPOT_VCPU_QUOTA = 64.0
G_SPOT_QUOTA_CODE = "L-3819A6DF"
TIER_FLEET_MIN_NODES = 2
TIER_FLEET_MAX_NODES = 16
DISTRIBUTED_256GB_CPG_NAME = "kaizen-dist256-cpg"
DISTRIBUTED_256GB_DEFAULT_NODES = 16

BEDROCK_DAYTIME_MODELS: Tuple[str, ...] = (
    "claude-haiku-5-5",
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "claude-fable-5",
)

LORA_PORTFOLIO: Tuple[Dict[str, str], ...] = (
    {
        "adapter": "lora-java-spring-craft",
        "base_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "fallback_base_model": "Qwen/Qwen3-32B-AWQ",
        "quantization": "awq",
        "dtype": "float16",
        "kv_cache_dtype": "float16",
        "max_lora_rank": "16",
        "tp": "2",
        "gpus": "0,1",
    },
    {
        "adapter": "lora-rust-vector-parity",
        "base_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "fallback_base_model": "Qwen/Qwen3-32B-AWQ",
        "quantization": "awq",
        "dtype": "float16",
        "kv_cache_dtype": "float16",
        "max_lora_rank": "16",
        "tp": "2",
        "gpus": "0,1",
    },
    {
        "adapter": "lora-vue-dashboard-ui",
        "base_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "fallback_base_model": "Qwen/Qwen3-32B-AWQ",
        "quantization": "awq",
        "dtype": "float16",
        "kv_cache_dtype": "float16",
        "max_lora_rank": "16",
        "tp": "2",
        "gpus": "0,1",
    },
    {
        "adapter": "lora-system-rca-triage",
        "base_model": "Qwen/Qwen3.5-35B-A3B",
        "fallback_base_model": "Qwen/Qwen3-32B-AWQ",
        "quantization": "awq",
        "dtype": "float16",
        "kv_cache_dtype": "float16",
        "max_lora_rank": "16",
        "tp": "2",
        "gpus": "2,3",
    },
    {
        "adapter": "lora-openapi-contract-guard",
        "base_model": "Qwen/Qwen3.5-35B-A3B",
        "cpu_gate_model": "Qwen/Qwen3.5-4B",
        "fallback_base_model": "Qwen/Qwen3-32B-AWQ",
        "quantization": "awq",
        "dtype": "float16",
        "kv_cache_dtype": "float16",
        "max_lora_rank": "16",
        "tp": "2",
        "gpus": "2,3",
    },
)

LEGACY_LORA_ADAPTER_REDIRECTS: Dict[str, str] = {
    "lora-avro-c": "lora-rust-vector-parity",
    "lora-bson-legacy_batch_worker": "lora-rust-vector-parity",
    "lora-sql-semilattice": "lora-rust-vector-parity",
    "lora-scoring": "lora-rust-vector-parity",
    "lora-parity-rca-judge": "lora-system-rca-triage",
}


def resolve_lora_adapter(name: str) -> str:
    """Resolve legacy Telemetry-LSP LoRA adapter names to canonical Kaizen-wide adapters."""
    cleaned = (name or "").strip()
    if not cleaned:
        return "lora-system-rca-triage"
    return LEGACY_LORA_ADAPTER_REDIRECTS.get(cleaned, cleaned)


@dataclass(frozen=True)
class InstanceHardwareSpec:
    """Hardware and Spot FinOps metadata for an EC2 instance type."""

    instance_type: str
    vcpus: int
    ram_gib: int
    vram_gb: int
    architecture: str
    hourly_spot_usd_est: float
    is_g_family: bool


INSTANCE_SPECS: Dict[str, InstanceHardwareSpec] = {
    "g4dn.xlarge": InstanceHardwareSpec("g4dn.xlarge", 4, 16, 16, "x86_64", 0.22, True),
    "g4dn.2xlarge": InstanceHardwareSpec("g4dn.2xlarge", 8, 32, 16, "x86_64", 0.32, True),
    "g4dn.12xlarge": InstanceHardwareSpec("g4dn.12xlarge", 48, 192, 64, "x86_64", 1.9612, True),
    "g5.xlarge": InstanceHardwareSpec("g5.xlarge", 4, 16, 24, "x86_64", 0.67, True),
    "g5.2xlarge": InstanceHardwareSpec("g5.2xlarge", 8, 32, 24, "x86_64", 0.86, True),
    "g5.12xlarge": InstanceHardwareSpec("g5.12xlarge", 48, 192, 96, "x86_64", 2.45, True),
    "g5.24xlarge": InstanceHardwareSpec("g5.24xlarge", 96, 384, 96, "x86_64", 4.10, True),
    "g5.48xlarge": InstanceHardwareSpec("g5.48xlarge", 192, 768, 192, "x86_64", 8.20, True),
    "g6.xlarge": InstanceHardwareSpec("g6.xlarge", 4, 16, 24, "x86_64", 0.50, True),
    "g6.2xlarge": InstanceHardwareSpec("g6.2xlarge", 8, 32, 24, "x86_64", 0.68, True),
    "g6.12xlarge": InstanceHardwareSpec("g6.12xlarge", 48, 192, 96, "x86_64", 2.50, True),
    "g6e.12xlarge": InstanceHardwareSpec("g6e.12xlarge", 48, 384, 192, "x86_64", 13.12, True),
    "g6.24xlarge": InstanceHardwareSpec("g6.24xlarge", 96, 384, 96, "x86_64", 4.60, True),
    "g6.48xlarge": InstanceHardwareSpec("g6.48xlarge", 192, 768, 192, "x86_64", 9.20, True),
    "r7g.16xlarge": InstanceHardwareSpec("r7g.16xlarge", 64, 512, 0, "arm64", 1.43, False),
    "r7i.24xlarge": InstanceHardwareSpec("r7i.24xlarge", 96, 768, 0, "x86_64", 2.77, False),
    "c7g.xlarge": InstanceHardwareSpec("c7g.xlarge", 4, 8, 0, "arm64", 0.045, False),
    "c7g.large": InstanceHardwareSpec("c7g.large", 2, 4, 0, "arm64", 0.023, False),
}

INSTANCE_VCPU_MAP: Dict[str, int] = {k: v.vcpus for k, v in INSTANCE_SPECS.items()}

INSTANCE_GPU_ARCH: Dict[str, Dict[str, Any]] = {
    "g4dn": {
        "compute_cap": "7.5",
        "gpu_arch": "turing_sm75",
        "supports_bf16": False,
        "supports_fp8": False,
        "supports_mxfp4": False,
        "vllm_dtype": "float16",
        "kv_cache_dtype": "float16",
        "supports_ktransformers": False,
        "cpu_engine": "none",
    },
    "g5": {
        "compute_cap": "8.6",
        "gpu_arch": "ampere_sm86",
        "supports_bf16": True,
        "supports_fp8": False,
        "supports_mxfp4": False,
        "vllm_dtype": "bfloat16",
        "kv_cache_dtype": "auto",
        "supports_ktransformers": True,
        "cpu_engine": "ik_llama.cpp",
    },
    "g6": {
        "compute_cap": "8.9",
        "gpu_arch": "ada_sm89",
        "supports_bf16": True,
        "supports_fp8": True,
        "supports_mxfp4": True,
        "vllm_dtype": "bfloat16",
        "kv_cache_dtype": "fp8",
        "supports_ktransformers": True,
        "cpu_engine": "ik_llama.cpp",
    },
    "g6e": {
        "compute_cap": "8.9",
        "gpu_arch": "ada_sm89",
        "supports_bf16": True,
        "supports_fp8": True,
        "supports_mxfp4": True,
        "vllm_dtype": "bfloat16",
        "kv_cache_dtype": "fp8",
        "supports_ktransformers": True,
        "cpu_engine": "ik_llama.cpp",
    },
    "m7g": {
        "compute_cap": "cpu_arm64",
        "gpu_arch": "graviton3_arm64",
        "supports_bf16": False,
        "supports_fp8": False,
        "supports_mxfp4": False,
        "vllm_dtype": "none",
        "kv_cache_dtype": "q8_0",
        "supports_ktransformers": False,
        "cpu_engine": "ik_llama.cpp",
    },
    "c7g": {
        "compute_cap": "cpu_arm64",
        "gpu_arch": "graviton3_arm64",
        "supports_bf16": False,
        "supports_fp8": False,
        "supports_mxfp4": False,
        "vllm_dtype": "none",
        "kv_cache_dtype": "q8_0",
        "supports_ktransformers": False,
        "cpu_engine": "ik_llama.cpp",
    },
    "r7g": {
        "compute_cap": "cpu_arm64",
        "gpu_arch": "graviton3_arm64",
        "supports_bf16": False,
        "supports_fp8": False,
        "supports_mxfp4": False,
        "vllm_dtype": "none",
        "kv_cache_dtype": "q8_0",
        "supports_ktransformers": False,
        "cpu_engine": "ik_llama.cpp",
    },
    "r7i": {
        "compute_cap": "cpu_x86_amx",
        "gpu_arch": "sapphire_rapids_amx",
        "supports_bf16": False,
        "supports_fp8": False,
        "supports_mxfp4": False,
        "vllm_dtype": "none",
        "kv_cache_dtype": "q8_0",
        "supports_ktransformers": False,
        "cpu_engine": "ik_llama.cpp",
    },
}


def get_instance_gpu_arch(instance_type: Optional[str]) -> Dict[str, Any]:
    """Return architecture capabilities (`SM75`/`SM89`/CPU) for an EC2 instance family."""
    family = (instance_type or "").split(".", 1)[0].lower()
    if family in INSTANCE_GPU_ARCH:
        return dict(INSTANCE_GPU_ARCH[family])
    return dict(INSTANCE_GPU_ARCH["g4dn"])


STATIC_REGION_OFFERINGS: Dict[str, Tuple[str, ...]] = {
    "eu-west-1": (
        "g4dn.xlarge",
        "g4dn.2xlarge",
        "g4dn.12xlarge",
        "g5.xlarge",
        "g5.2xlarge",
        "g5.12xlarge",
        "r7g.16xlarge",
        "r7i.24xlarge",
        "c7g.xlarge",
        "c7g.large",
    ),
    "eu-central-1": (
        "g6.xlarge",
        "g6.2xlarge",
        "g6.12xlarge",
        "g6e.12xlarge",
        "g4dn.xlarge",
        "g4dn.2xlarge",
        "g4dn.12xlarge",
        "g5.xlarge",
        "g5.2xlarge",
        "g5.12xlarge",
        "r7g.16xlarge",
        "r7i.24xlarge",
        "c7g.xlarge",
        "c7g.large",
    ),
    "us-east-1": (
        "g6.xlarge",
        "g6.2xlarge",
        "g6.12xlarge",
        "g6e.12xlarge",
        "g4dn.xlarge",
        "g4dn.2xlarge",
        "g4dn.12xlarge",
        "g5.xlarge",
        "g5.2xlarge",
        "g5.12xlarge",
        "r7g.16xlarge",
        "r7i.24xlarge",
        "c7g.xlarge",
        "c7g.large",
    ),
}

AZ_ID_FALLBACK_MAP: Dict[str, str] = {
    "euw1-az1": "eu-west-1a",
    "euw1-az2": "eu-west-1b",
    "euw1-az3": "eu-west-1c",
    "euc1-az1": "eu-central-1a",
    "euc1-az2": "eu-central-1b",
    "euc1-az3": "eu-central-1c",
}


@dataclass(frozen=True)
class BurstTierProfile:
    """Configuration profile for one of the 4 Kaizen GPU/Cloud burst tiers."""

    name: str
    parameter_range: str
    ec2_required: bool
    models: Tuple[str, ...]
    depth_sparsity: Tuple[str, ...]
    width_sparsity: Tuple[str, ...]
    runtime_engines: Tuple[str, ...]
    default_quantization: str
    candidate_instance_types: Tuple[str, ...]
    preferred_by_region: Dict[str, Tuple[str, ...]]
    supports_aws_batch_night: bool = False
    description: str = ""


GPU_BURST_TIERS: Dict[str, BurstTierProfile] = {
    "daytime_bedrock": BurstTierProfile(
        name="daytime_bedrock",
        parameter_range="Managed Claude 5.5",
        ec2_required=False,
        models=BEDROCK_DAYTIME_MODELS,
        depth_sparsity=("prompt_cache_1h",),
        width_sparsity=("windows_isolated_bedrock",),
        runtime_engines=("aws_bedrock_claude_5_5",),
        default_quantization="managed",
        candidate_instance_types=(),
        preferred_by_region={},
        supports_aws_batch_night=False,
        description="Daytime Windows-isolated Bedrock Claude 5.5 + 1h Prompt Cache ($0 EC2, 0 GPU wait).",
    ),
    "large": BurstTierProfile(
        name="large",
        parameter_range="30B-235B MoE / 32B AWQ",
        ec2_required=True,
        models=(
            "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "Qwen/Qwen3.5-35B-A3B",
            "Qwen/Qwen3-32B-AWQ",
            "Qwen/Qwen3-Coder-Next",
            "Qwen/Qwen3.5-122B-A10B",
            "zai-org/GLM-4.5-Air",
            "Qwen/Qwen3-235B-A22B-Instruct-2507",
        ),
        depth_sparsity=("EAGLE-3", "MTP", "ngram-lookup"),
        width_sparsity=("REAP", "MoE-Expert-Pruning", "Multi-LoRA-AWQ"),
        runtime_engines=("vllm-runai-streamer", "sglang-radix-attention", "qlora-trainer-5-adapters"),
        default_quantization="T4 SM75: AWQ-Int4 + float16 KV (<=48GB); L4/L40S SM89: AWQ/FP8 + fp8_e4m3 KV",
        candidate_instance_types=(
            "g4dn.12xlarge",
            "g4dn.2xlarge",
            "g4dn.xlarge",
            "g6.12xlarge",
            "g6e.12xlarge",
            "g5.12xlarge",
            "g5.2xlarge",
            "g5.xlarge",
        ),
        preferred_by_region={
            "eu-west-1": ("g4dn.12xlarge", "g4dn.2xlarge", "g4dn.xlarge", "g5.12xlarge", "g5.2xlarge", "g5.xlarge"),
            "eu-central-1": ("g6.12xlarge", "g6e.12xlarge", "g4dn.12xlarge", "g5.12xlarge"),
            "us-east-1": ("g6.12xlarge", "g6e.12xlarge", "g4dn.12xlarge", "g5.12xlarge"),
        },
        supports_aws_batch_night=True,
        description="Large 30B-235B models via Run:ai S3->VRAM streaming + EAGLE-3/MTP/ngram + REAP + AWQ Multi-LoRA.",
    ),
    "distributed_256gb": BurstTierProfile(
        name="distributed_256gb",
        parameter_range="122B-235B MoE (256GB Distributed VRAM)",
        ec2_required=True,
        models=(
            "Qwen/Qwen3-235B-A22B-Instruct-2507",
            "Qwen/Qwen3-Coder-Next",
            "Qwen/Qwen3.5-122B-A10B",
            "unsloth/DeepSeek-V3.2-GGUF",
        ),
        depth_sparsity=("MTP", "ngram-lookup"),
        width_sparsity=("RPC-Layer-Sharding-16x", "Ray-Pipeline-Parallel-16x", "MoE-Expert-Pruning"),
        runtime_engines=("llama_cpp_rpc", "vllm_ray_pp"),
        default_quantization="16x T4 SM75 (256GB VRAM): Q4_K_M / AWQ-Int4 + float16 KV (zero AirLLM disk streaming, DE-005)",
        candidate_instance_types=("g4dn.xlarge", "g6.xlarge"),
        preferred_by_region={
            "eu-west-1": ("g4dn.xlarge",),
            "eu-central-1": ("g4dn.xlarge", "g6.xlarge"),
            "us-east-1": ("g4dn.xlarge", "g6.xlarge"),
        },
        supports_aws_batch_night=False,
        description="Distributed 256GB VRAM (16x g4dn.xlarge = 64vCPU exact L-3819A6DF) via CPG + llama.cpp RPC / Ray PP=16.",
    ),
    "frontier_1t": BurstTierProfile(
        name="frontier_1t",
        parameter_range="397B-1T / 17B-41B active",
        ec2_required=True,
        models=(
            "Qwen/Qwen3.5-397B-A17B",
            "unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF",
            "unsloth/DeepSeek-V3.2-GGUF",
            "unsloth/GLM-5-GGUF",
            "unsloth/Kimi-K2.5-GGUF",
        ),
        depth_sparsity=("MTP", "ngram-lookup"),
        width_sparsity=("REAP-50%", "KTransformers", "ik_llama.cpp"),
        runtime_engines=("ik_llama.cpp", "ktransformers"),
        default_quantization="Unsloth UD-Q2_K_XL (139-375 GB) / REAP-50% (avoid TQ1_0 for precision coding)",
        candidate_instance_types=("r7g.16xlarge", "r7i.24xlarge", "g6e.12xlarge"),
        preferred_by_region={
            "eu-west-1": ("r7g.16xlarge", "r7i.24xlarge"),
            "eu-central-1": ("g6e.12xlarge", "r7g.16xlarge", "r7i.24xlarge"),
            "us-east-1": ("g6e.12xlarge", "r7g.16xlarge", "r7i.24xlarge"),
        },
        supports_aws_batch_night=True,
        description="Frontier 397B-1T MoE via REAP-50% + UD-Q2_K_XL + ik_llama.cpp (ARM64/x86) or KTransformers (x86+CUDA).",
    ),
    "ultra_frontier_2_8t": BurstTierProfile(
        name="ultra_frontier_2_8t",
        parameter_range="675B-2.8T",
        ec2_required=True,
        models=(
            "moonshotai/Kimi-K3.1-Pro",
            "prometheusAIR/Kimi-K3-REAP55-GGUF",
            "hellohazime/Kimi-K3-REAP-512GB-GGUF",
            "mistralai/Mistral-Large-3-675B-Instruct-2512",
        ),
        depth_sparsity=("MTP", "ngram-lookup"),
        width_sparsity=("REAP55 (319 GiB)", "REAP640-IQ1_S (441 GB)", "ik_llama.cpp", "KTransformers"),
        runtime_engines=("ik_llama.cpp", "ktransformers", "aws-batch-spot-capacity-optimized"),
        default_quantization="REAP55 (319 GiB, 400/896 experts) / REAP640-IQ1_S (441 GB) / IQ4_XS (362 GB)",
        candidate_instance_types=("r7g.16xlarge", "r7i.24xlarge"),
        preferred_by_region={
            "eu-west-1": ("r7g.16xlarge", "r7i.24xlarge"),
            "eu-central-1": ("r7g.16xlarge", "r7i.24xlarge"),
            "us-east-1": ("r7g.16xlarge", "r7i.24xlarge"),
        },
        supports_aws_batch_night=True,
        description="Ultra-Frontier 675B-2.8T via REAP55/REAP640-IQ1_S + ik_llama.cpp + night AWS Batch (within 64vCPU G-quota).",
    ),
}


_AWS_REGION_RE = re.compile(
    r"^(?:us|eu|ap|ca|sa|me|af|il|cn)-(?:gov-)?(?:north|south|east|west|central|northeast|northwest|southeast|southwest)-\d+$"
)

_EC2_SIZE_VCPUS: Dict[str, int] = {
    "large": 2,
    "xlarge": 4,
    "2xlarge": 8,
    "4xlarge": 16,
    "8xlarge": 32,
    "12xlarge": 48,
    "16xlarge": 64,
    "24xlarge": 96,
    "48xlarge": 192,
    "metal": 96,
}


def validate_aws_region(region: str) -> str:
    """Validate AWS region format so invalid region names fail closed instead of silent fallback."""
    cleaned = (region or "").strip()
    if not _AWS_REGION_RE.match(cleaned):
        raise ValueError(
            f"Invalid AWS region '{region}'. Expected a valid region such as '{DEFAULT_REGION}' or 'eu-central-1'."
        )
    return cleaned


def validate_u2_profile(profile: str) -> None:
    """Enforce SEC-AWS-TENANCY-ISOLATION: only `kaizen-u2` may be planned or mutated."""
    if profile != DEFAULT_PROFILE:
        raise ValueError(
            f"SEC-AWS-TENANCY-ISOLATION: only '{DEFAULT_PROFILE}' is operational, got '{profile}'"
        )


def _is_g_family(instance_type: str) -> bool:
    spec = INSTANCE_SPECS.get(instance_type)
    if spec is not None:
        return spec.is_g_family
    return instance_type.startswith(("g4", "g5", "g6", "vt1"))


def _vcpus_for(instance_type: str) -> int:
    mapped = INSTANCE_VCPU_MAP.get(instance_type)
    if mapped is not None:
        return mapped
    if "." in instance_type:
        size_suffix = instance_type.rsplit(".", 1)[-1].lower()
        if size_suffix in _EC2_SIZE_VCPUS:
            return _EC2_SIZE_VCPUS[size_suffix]
    return 48


def _spot_hourly_usd(instance_type: str) -> float:
    spec = INSTANCE_SPECS.get(instance_type)
    return spec.hourly_spot_usd_est if spec is not None else 2.45


def build_runai_streamer_config(
    bucket: str = DEFAULT_BUCKET,
    s3_prefix: str = DEFAULT_MODEL_STASH_PREFIX,
    model_id: str = "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    concurrency: int = 16,
) -> Dict[str, Any]:
    """Build vLLM `runai_streamer` S3 streaming configuration (zero /tmp downloads)."""
    clean_prefix = s3_prefix.strip("/")
    model_tail = model_id.strip("/")
    s3_uri = (
        f"s3://{bucket}/{clean_prefix}/{model_tail}"
        if clean_prefix.endswith("models")
        else f"s3://{bucket}/{clean_prefix}/models/{model_tail}"
    )
    return {
        "load_format": "runai_streamer",
        "model_loader_extra_config": {"concurrency": int(concurrency)},
        "s3_model_uri": s3_uri,
        "vllm_cli_flags": [
            "--load-format",
            "runai_streamer",
            "--model-loader-extra-config",
            json.dumps({"concurrency": int(concurrency)}),
        ],
    }


VALID_KVPRESS_METHODS: Tuple[str, ...] = (
    "ExpectedAttentionPress",
    "KVzip",
    "SnapKVPress",
    "TOVAPress",
)


def build_kv_optimization_config(
    instance_type: str,
    *,
    enable_kvpress: bool = False,
    kvpress_method: str = "ExpectedAttentionPress",
    enable_eagle3: bool = False,
) -> Dict[str, Any]:
    """Build arch-aware KV-cache quantization, prefix caching, `kvpress`, and GPU-only `EAGLE-3` config."""
    if kvpress_method not in VALID_KVPRESS_METHODS:
        raise ValueError(
            f"Unsupported kvpress_method '{kvpress_method}'. Expected one of {VALID_KVPRESS_METHODS}"
        )
    arch = get_instance_gpu_arch(instance_type)
    is_cpu = str(arch.get("compute_cap", "")).startswith("cpu_")
    is_sm75 = arch.get("compute_cap") == "7.5"
    supports_fp8 = bool(arch.get("supports_fp8"))

    llama_cpp_flags = ["-ctk", "q8_0", "-ctv", "q8_0", "--cache-reuse", "256"]
    eagle3_allowed = bool(enable_eagle3 and not is_cpu)
    eagle3_blocked_reason = (
        "DE-006: EAGLE-3 / draft speculative decoding is GPU-only and forbidden on CPU instances."
        if (enable_eagle3 and is_cpu)
        else ""
    )

    if is_cpu:
        vllm_kv_dtype = "none"
        vllm_flags: List[str] = []
    else:
        vllm_kv_dtype = "fp8" if supports_fp8 else ("float16" if is_sm75 else "auto")
        vllm_flags = ["--enable-prefix-caching", "--kv-cache-dtype", vllm_kv_dtype]
        if eagle3_allowed:
            vllm_flags.extend(["--speculative-method", "eagle3", "--num-speculative-tokens", "5"])

    return {
        "instance_type": instance_type,
        "gpu_arch": arch["gpu_arch"],
        "is_cpu_instance": is_cpu,
        "t4_sm75_guard": is_sm75,
        "prefix_caching_enabled": True,
        "cpu_draft_speculative": False,
        "vllm_kv_cache_dtype": vllm_kv_dtype,
        "vllm_kv_flags": vllm_flags,
        "llama_cpp_kv_flags": llama_cpp_flags,
        "kvpress": {
            "enabled": bool(enable_kvpress),
            "method": kvpress_method,
            "supported_methods": list(VALID_KVPRESS_METHODS),
            "compression_ratio": 0.5 if enable_kvpress else 0.0,
        },
        "eagle3": {
            "requested": bool(enable_eagle3),
            "enabled": eagle3_allowed,
            "gpu_only": True,
            "blocked_reason": eagle3_blocked_reason,
        },
    }


def build_2d_sparsity_config(
    tier: str,
    model_id: Optional[str] = None,
    depth_method: str = "ngram-lookup",
    width_method: str = "reap",
    bucket: str = DEFAULT_BUCKET,
    model_prefix: str = DEFAULT_MODEL_STASH_PREFIX,
    instance_type: Optional[str] = None,
    nodes: int = 1,
) -> Dict[str, Any]:
    """Build 2D Sparsity configuration with SM75/SM89 and ARM64 KTransformers hardware guards."""
    if tier not in GPU_BURST_TIERS:
        raise ValueError(f"Unknown GPU burst tier '{tier}'. Valid tiers: {sorted(GPU_BURST_TIERS)}")
    spec = GPU_BURST_TIERS[tier]
    chosen_model = model_id or spec.models[0]

    if tier == "daytime_bedrock":
        return {
            "tier": tier,
            "model_id": chosen_model,
            "depth_axis": {"method": "prompt_cache_1h", "supported": list(spec.depth_sparsity)},
            "width_axis": {"method": "bedrock_managed", "supported": list(spec.width_sparsity)},
            "runtime_engines": list(spec.runtime_engines),
            "runai_streamer": None,
        }

    resolved_itype = instance_type or spec.candidate_instance_types[0]
    arch = get_instance_gpu_arch(resolved_itype)
    kv_opt = build_kv_optimization_config(
        resolved_itype,
        enable_eagle3=(depth_method.lower() == "eagle-3"),
    )

    if tier == "distributed_256gb":
        effective_nodes = max(2, int(nodes)) if int(nodes) > 1 else DISTRIBUTED_256GB_DEFAULT_NODES
        hw = INSTANCE_SPECS.get(resolved_itype)
        per_node_vram = hw.vram_gb if hw is not None else 16
        is_sm75 = arch["compute_cap"] == "7.5"
        streamer = build_runai_streamer_config(bucket=bucket, s3_prefix=model_prefix, model_id=chosen_model)
        rpc_workers = [f"worker-{idx}:50052" for idx in range(1, effective_nodes)]
        ray_pp_flags = [
            "--distributed-executor-backend",
            "ray",
            "--pipeline-parallel-size",
            str(effective_nodes),
            "--tensor-parallel-size",
            "1",
            "--dtype",
            arch["vllm_dtype"],
            "--kv-cache-dtype",
            arch["kv_cache_dtype"],
            "--enable-prefix-caching",
            "--load-format",
            "runai_streamer",
        ]
        if is_sm75:
            ray_pp_flags.append("--enforce-eager")
        return {
            "tier": tier,
            "model_id": chosen_model,
            "instance_type": resolved_itype,
            "nodes": effective_nodes,
            "aggregate_vram_gb": effective_nodes * per_node_vram,
            "gpu_arch": arch["gpu_arch"],
            "t4_sm75_guard": is_sm75,
            "airllm_disk_streaming": False,
            "cpu_draft_speculative": False,
            "placement_group": DISTRIBUTED_256GB_CPG_NAME,
            "quantization": spec.default_quantization,
            "depth_axis": {
                "selected": depth_method.upper() if depth_method.lower() == "mtp" else depth_method,
                "supported": list(spec.depth_sparsity),
                "speculative_flags": ["--speculative-model", "[ngram]", "--num-speculative-tokens", "5"],
            },
            "width_axis": {
                "selected": "RPC-LAYER-SHARDING-16X",
                "supported": list(spec.width_sparsity),
                "lora_portfolio_count": len(LORA_PORTFOLIO),
            },
            "primary_runtime": "llama_cpp_rpc",
            "fallback_runtime": "vllm_ray_pp",
            "llama_cpp_rpc": {
                "worker_cmd": "rpc-server -H 0.0.0.0 -p 50052",
                "rpc_endpoints": rpc_workers,
                "head_flags": ["-ngl", "999", "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--cache-reuse", "256"],
            },
            "vllm_serving_flags": ray_pp_flags,
            "kv_optimization": kv_opt,
            "runtime_engines": list(spec.runtime_engines),
            "runai_streamer": streamer,
        }

    if tier == "large":
        streamer = build_runai_streamer_config(bucket=bucket, s3_prefix=model_prefix, model_id=chosen_model)
        is_sm75 = arch["compute_cap"] == "7.5"
        serving_flags = [
            "--dtype",
            arch["vllm_dtype"],
            "--kv-cache-dtype",
            arch["kv_cache_dtype"],
            "--enable-prefix-caching",
            "--enable-lora",
            "--max-lora-rank",
            "16" if is_sm75 else "32",
        ]
        if is_sm75:
            serving_flags.append("--enforce-eager")
        spec_flags = (
            ["--speculative-method", "eagle3", "--num-speculative-tokens", "5"]
            if kv_opt["eagle3"]["enabled"]
            else ["--speculative-model", "[ngram]", "--num-speculative-tokens", "5"]
        )
        return {
            "tier": tier,
            "model_id": chosen_model,
            "instance_type": resolved_itype,
            "gpu_arch": arch["gpu_arch"],
            "t4_sm75_guard": is_sm75,
            "quantization": spec.default_quantization,
            "depth_axis": {
                "selected": depth_method.upper() if depth_method.lower() in ("mtp", "eagle-3") else depth_method,
                "supported": list(spec.depth_sparsity),
                "speculative_flags": spec_flags,
            },
            "width_axis": {
                "selected": width_method.upper(),
                "supported": list(spec.width_sparsity),
                "lora_portfolio_count": len(LORA_PORTFOLIO),
            },
            "vllm_serving_flags": serving_flags,
            "kv_optimization": kv_opt,
            "runtime_engines": list(spec.runtime_engines),
            "runai_streamer": streamer,
        }

    hw_spec = INSTANCE_SPECS.get(resolved_itype)
    total_ram = hw_spec.ram_gib if hw_spec is not None else (512 if tier == "frontier_1t" else 768)
    safe_ram_budget = int(total_ram * 0.86)
    selected_engine = "ktransformers" if arch["supports_ktransformers"] else "ik_llama.cpp"
    engine_flags = (
        ["-ctk", "q8_0", "-ctv", "q8_0", "--cache-reuse", "256", "-fmoe", "--flash-attn", "on"]
        if selected_engine == "ik_llama.cpp"
        else ["--cpu-infer", "32", "--max-new-tokens", "4096"]
    )
    heterogeneous = {
        "instance_type": resolved_itype,
        "engine": selected_engine,
        "ktransformers_constraint": "x86_64+CUDA only (incompatible with ARM64 r7g.*)",
        "flags": engine_flags,
        "ram_budget_gib": safe_ram_budget,
        "total_ram_gib": total_ram,
        "reap_mode": "REAP-50% (~135-160 GB)" if tier == "frontier_1t" else "REAP55 (319 GiB) / REAP640-IQ1_S (441 GB)",
    }
    return {
        "tier": tier,
        "model_id": chosen_model,
        "quantization": spec.default_quantization,
        "depth_axis": {"selected": depth_method, "supported": list(spec.depth_sparsity)},
        "width_axis": {"selected": width_method, "supported": list(spec.width_sparsity)},
        "runtime_engines": list(spec.runtime_engines),
        "heterogeneous_offload": heterogeneous,
        "kv_optimization": kv_opt,
        "runai_streamer": None,
    }


def resolve_region_instance_types(
    tier_name: str,
    region: str = DEFAULT_REGION,
    ec2_client: Any = None,
    candidate_types: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Resolve region-offered instance types; maps `eu-west-1` Large Spot to `g4dn.12xlarge` (SPS=8..9)."""
    region = validate_aws_region(region)
    if tier_name not in GPU_BURST_TIERS:
        raise ValueError(f"Unknown tier '{tier_name}'. Expected one of {sorted(GPU_BURST_TIERS)}")
    tier = GPU_BURST_TIERS[tier_name]
    if not tier.ec2_required:
        return {
            "tier": tier_name,
            "region": region,
            "ec2_required": False,
            "requested_candidates": [],
            "selected_instance_types": [],
            "excluded_types": [],
            "source": "bedrock_windows_isolated",
        }

    desired = tuple(candidate_types) if candidate_types else tier.candidate_instance_types
    offered_azs: Dict[str, List[str]] = {}
    source = "static_region_offerings_fallback"

    if ec2_client is not None:
        try:
            resp = ec2_client.describe_instance_type_offerings(
                LocationType="availability-zone",
                Filters=[{"Name": "instance-type", "Values": list(desired)}],
            )
            for row in resp.get("InstanceTypeOfferings", []):
                itype = str(row.get("InstanceType", ""))
                loc = str(row.get("Location", ""))
                if itype:
                    offered_azs.setdefault(itype, []).append(loc)
            source = "ec2.describe_instance_type_offerings"
        except (ClientError, BotoCoreError, OSError, TypeError, ValueError):
            offered_azs = {}

    if not offered_azs and source == "static_region_offerings_fallback":
        static_set = set(STATIC_REGION_OFFERINGS.get(region, STATIC_REGION_OFFERINGS[DEFAULT_REGION]))
        offered_azs = {t: [f"{region}a", f"{region}b", f"{region}c"] for t in desired if t in static_set}

    region_order = list(tier.preferred_by_region.get(region, desired))
    ordered_desired = region_order + [t for t in desired if t not in region_order]
    selected: List[str] = []
    excluded: List[Dict[str, str]] = []

    for itype in ordered_desired:
        if itype in offered_azs:
            selected.append(itype)
            continue
        reason = f"Not offered in {region} via {source}"
        if region == "eu-west-1" and itype in ("g6.12xlarge", "g6e.12xlarge"):
            reason = (
                "g6.12xlarge/g6e.12xlarge not offered in eu-west-1; "
                "mapped Large-tier Spot to g4dn.12xlarge (4x T4 64GB, SPS=8..9) with g5.12xlarge fallback (DE-006)"
            )
        excluded.append({"instance_type": itype, "reason": reason})

    return {
        "tier": tier_name,
        "region": region,
        "ec2_required": True,
        "requested_candidates": list(ordered_desired),
        "selected_instance_types": selected,
        "offered_azs": {k: sorted(set(v)) for k, v in offered_azs.items()},
        "excluded_types": excluded,
        "source": source,
    }


def _query_active_g_spot_vcpus(ec2_client: Any) -> int:
    """Count vCPUs of currently pending/running G-family Spot instances when `describe_instances` exists."""
    desc_fn = getattr(ec2_client, "describe_instances", None) if ec2_client is not None else None
    if not callable(desc_fn):
        return 0
    active_vcpus = 0
    try:
        next_token: Optional[str] = None
        seen_tokens: set[str] = set()
        while True:
            kwargs: Dict[str, Any] = {
                "Filters": [
                    {"Name": "instance-lifecycle", "Values": ["spot"]},
                    {"Name": "instance-state-name", "Values": ["pending", "running"]},
                ]
            }
            if next_token:
                kwargs["NextToken"] = next_token
            resp = desc_fn(**kwargs)
            for res in resp.get("Reservations", []) or []:
                for inst in res.get("Instances", []) or []:
                    itype = str(inst.get("InstanceType", ""))
                    if itype and _is_g_family(itype):
                        active_vcpus += _vcpus_for(itype)
            next_token = resp.get("NextToken")
            if not next_token or next_token in seen_tokens:
                break
            seen_tokens.add(next_token)
    except (AttributeError, ClientError, BotoCoreError, OSError, TypeError, ValueError):
        return 0
    return active_vcpus


def check_g_spot_quota(
    instance_types: Sequence[str],
    nodes: int = 1,
    sq_client: Any = None,
    region: str = DEFAULT_REGION,
    fallback_quota: float = DEFAULT_G_SPOT_VCPU_QUOTA,
    ec2_client: Any = None,
) -> Dict[str, Any]:
    """Validate `active_g_spot_vcpus + nodes * vcpus <= quota` against EC2 G-Spot quota (`L-3819A6DF`, 64 vCPUs)."""
    quota_vcpus = float(fallback_quota)
    quota_source = "fallback_default_64_vcpus"

    if sq_client is not None:
        try:
            q_resp = sq_client.get_service_quota(ServiceCode="ec2", QuotaCode=G_SPOT_QUOTA_CODE)
            val = (q_resp.get("Quota") or {}).get("Value")
            if val is not None:
                quota_vcpus = float(val)
                quota_source = f"service-quotas:{G_SPOT_QUOTA_CODE}"
        except (ClientError, BotoCoreError, OSError, TypeError, ValueError):
            pass

    active_vcpus = _query_active_g_spot_vcpus(ec2_client)
    available_vcpus = max(0.0, quota_vcpus - float(active_vcpus))

    if not instance_types:
        return {
            "passed": True,
            "quota_code": G_SPOT_QUOTA_CODE,
            "quota_vcpus": quota_vcpus,
            "active_g_spot_vcpus": active_vcpus,
            "available_g_spot_vcpus": available_vcpus,
            "quota_source": quota_source,
            "nodes": nodes,
            "requested_vcpus": 0,
            "valid_instance_types": [],
            "rejected_instance_types": [],
            "non_g_family": [],
            "diagnostic": "No EC2 instances required for this tier.",
        }

    valid_types: List[str] = []
    rejected_types: List[Dict[str, Any]] = []
    non_g_types: List[str] = []

    for itype in instance_types:
        vcpus_each = _vcpus_for(itype)
        total_req = int(nodes) * vcpus_each
        if not _is_g_family(itype):
            non_g_types.append(itype)
            valid_types.append(itype)
            continue
        if total_req <= available_vcpus:
            valid_types.append(itype)
            continue
        rejected_types.append(
            {
                "instance_type": itype,
                "vcpus_per_node": vcpus_each,
                "requested_vcpus": total_req,
                "active_g_spot_vcpus": active_vcpus,
                "available_g_spot_vcpus": available_vcpus,
                "quota_vcpus": quota_vcpus,
                "reason": (
                    f"{nodes}x {itype} requires {total_req} vCPUs + {active_vcpus} active > "
                    f"{quota_vcpus} G-Spot quota ({G_SPOT_QUOTA_CODE})"
                ),
            }
        )

    primary = instance_types[0]
    primary_req = int(nodes) * _vcpus_for(primary)
    passed = len(valid_types) > 0
    diagnostic = (
        f"Quota OK in {region}: valid={valid_types} within {available_vcpus}/{quota_vcpus} G-Spot vCPUs"
        if passed
        else (
            f"G-Spot quota exceeded in {region}: {nodes}x {primary} needs {primary_req} vCPUs "
            f"(active={active_vcpus}, available={available_vcpus}) > {quota_vcpus} ({G_SPOT_QUOTA_CODE}). "
            f"Use smaller G-types (e.g. g4dn.xlarge/g4dn.2xlarge), CPU-RAM Spot (r7g.16xlarge/r7i.24xlarge), "
            f"or terminate active Spot nodes first."
        )
    )
    return {
        "passed": passed,
        "primary_instance_type": primary,
        "primary_fits_quota": primary in valid_types,
        "quota_code": G_SPOT_QUOTA_CODE,
        "quota_vcpus": quota_vcpus,
        "active_g_spot_vcpus": active_vcpus,
        "available_g_spot_vcpus": available_vcpus,
        "quota_source": quota_source,
        "nodes": int(nodes),
        "requested_vcpus": primary_req,
        "valid_instance_types": valid_types,
        "rejected_instance_types": rejected_types,
        "non_g_family": non_g_types,
        "diagnostic": diagnostic,
    }


def _build_az_id_to_name_map(ec2_client: Any, region: str) -> Dict[str, str]:
    az_map = dict(AZ_ID_FALLBACK_MAP)
    if ec2_client is None:
        return az_map
    try:
        resp = ec2_client.describe_availability_zones(
            Filters=[{"Name": "region-name", "Values": [region]}]
        )
        for row in resp.get("AvailabilityZones", []):
            zid = str(row.get("ZoneId", ""))
            zname = str(row.get("ZoneName", ""))
            if zid and zname:
                az_map[zid] = zname
    except (ClientError, BotoCoreError, OSError, TypeError, ValueError):
        pass
    return az_map


def _latest_spot_prices_by_az(
    ec2_client: Any,
    instance_types: Sequence[str],
) -> Dict[Tuple[str, str], float]:
    if ec2_client is None or not instance_types:
        return {}
    prices: Dict[Tuple[str, str], Tuple[datetime, float]] = {}
    try:
        start_time = datetime.now(timezone.utc) - timedelta(hours=2)
        next_token: Optional[str] = None
        seen_tokens: set[str] = set()
        while True:
            req_kwargs: Dict[str, Any] = {
                "InstanceTypes": list(instance_types),
                "ProductDescriptions": ["Linux/UNIX"],
                "StartTime": start_time,
                "MaxResults": 100,
            }
            if next_token:
                req_kwargs["NextToken"] = next_token
            resp = ec2_client.describe_spot_price_history(**req_kwargs)
            for item in resp.get("SpotPriceHistory", []):
                az = str(item.get("AvailabilityZone", ""))
                itype = str(item.get("InstanceType", ""))
                raw_price = item.get("SpotPrice")
                ts = item.get("Timestamp")
                if not az or not itype or raw_price is None:
                    continue
                ts_dt = ts if isinstance(ts, datetime) else datetime.now(timezone.utc)
                val = float(raw_price)
                key = (itype, az)
                if key not in prices or ts_dt > prices[key][0]:
                    prices[key] = (ts_dt, val)
            next_token = resp.get("NextToken")
            if not next_token or next_token in seen_tokens:
                break
            seen_tokens.add(next_token)
    except (ClientError, BotoCoreError, OSError, TypeError, ValueError):
        return {}
    return {k: v[1] for k, v in prices.items()}


def evaluate_spot_placement_and_prices(
    ec2_client: Any,
    instance_types: Sequence[str],
    target_capacity: int = 1,
    region: str = DEFAULT_REGION,
    min_sps: int = DEFAULT_MIN_SPS,
    min_vram_gb: int = 0,
) -> Dict[str, Any]:
    """Query Spot placement scores (`SPS >= 7`) + Spot prices and rank AZs by `(Score DESC, SpotPrice ASC)`."""
    if not instance_types:
        return {
            "region": region,
            "target_capacity": int(target_capacity),
            "min_sps_required": int(min_sps),
            "min_vram_gb": int(min_vram_gb),
            "max_sps": 10,
            "sps_gate_passed": True,
            "sps_diagnostic": "No EC2 instances requested (managed Bedrock tier).",
            "placement_scores": [],
            "ranked_azs": [],
            "spot_prices_usd": {},
            "cheapest_az": None,
        }

    scores: List[Dict[str, Any]] = []
    score_error = ""
    if ec2_client is not None:
        try:
            resp = ec2_client.get_spot_placement_scores(
                InstanceTypes=list(instance_types),
                TargetCapacity=int(target_capacity),
                SingleAvailabilityZone=True,
                RegionNames=[region],
            )
            scores = list(resp.get("SpotPlacementScores", []))
        except (ClientError, BotoCoreError, OSError, TypeError, ValueError) as exc:
            score_error = str(exc)

    az_id_map = _build_az_id_to_name_map(ec2_client, region)
    price_map = _latest_spot_prices_by_az(ec2_client, instance_types)
    ranked: List[Dict[str, Any]] = []

    for row in scores:
        az_id = str(row.get("AvailabilityZoneId", ""))
        az_name = az_id_map.get(az_id, az_id)
        score = int(row.get("Score", 0))
        vram_candidates = [
            itype
            for itype in instance_types
            if (INSTANCE_SPECS.get(itype).vram_gb if INSTANCE_SPECS.get(itype) else 0) >= int(min_vram_gb)
        ]
        effective_types = vram_candidates if vram_candidates else list(instance_types)
        matching_prices = [
            (itype, price_map[(itype, az_name)])
            for itype in effective_types
            if (itype, az_name) in price_map
        ]
        matching_prices.sort(key=lambda pair: pair[1])
        best_itype = matching_prices[0][0] if matching_prices else effective_types[0]
        best_price = matching_prices[0][1] if matching_prices else _spot_hourly_usd(best_itype)
        ranked.append(
            {
                "region": str(row.get("Region", region)),
                "availability_zone_id": az_id,
                "availability_zone": az_name,
                "score": score,
                "recommended_instance_type": best_itype,
                "spot_price_usd_hr": round(float(best_price), 4),
                "vram_filter_gb": int(min_vram_gb),
            }
        )

    ranked.sort(key=lambda r: (-int(r["score"]), float(r["spot_price_usd_hr"]), r["availability_zone"]))
    max_sps = max((int(r["score"]) for r in ranked), default=0)
    sps_gate_passed = bool(ranked) and max_sps >= int(min_sps)

    if not ranked:
        diag = (
            f"Spot placement query unavailable ({score_error})"
            if score_error
            else f"No Spot placement scores returned for {list(instance_types)} in {region} (check_aws=False or offline)."
        )
    elif sps_gate_passed:
        top = ranked[0]
        diag = (
            f"SPS gate passed (max_sps={max_sps} >= {min_sps}); best AZ={top['availability_zone']} "
            f"({top['recommended_instance_type']} @ ${top['spot_price_usd_hr']:.4f}/hr)"
        )
    else:
        top = ranked[0]
        diag = (
            f"SPS gate failed: max_sps={max_sps} < required {min_sps} in {region} for "
            f"{list(instance_types)} (target_capacity={target_capacity}); best candidate AZ={top['availability_zone']} "
            f"@ ${top['spot_price_usd_hr']:.4f}/hr. Launch blocked unless allow_low_sps=True with execute_approved_u2=True."
        )

    cheapest_az = min(ranked, key=lambda r: float(r["spot_price_usd_hr"]))["availability_zone"] if ranked else None
    formatted_prices = {f"{itype}@{az}": round(val, 4) for (itype, az), val in sorted(price_map.items())}
    return {
        "region": region,
        "target_capacity": int(target_capacity),
        "min_sps_required": int(min_sps),
        "min_vram_gb": int(min_vram_gb),
        "max_sps": max_sps,
        "sps_gate_passed": sps_gate_passed,
        "sps_diagnostic": diag,
        "placement_scores": scores,
        "ranked_azs": ranked,
        "spot_prices_usd": formatted_prices,
        "cheapest_az": cheapest_az,
    }


def build_spot_userdata(
    run_id: str,
    bucket: str = DEFAULT_BUCKET,
    checkpoint_prefix: str = DEFAULT_CHECKPOINT_PREFIX,
    region: str = DEFAULT_REGION,
    idle_stop_minutes: int = DEFAULT_IDLE_STOP_MINUTES,
    nvme_dir: str = "/opt/dlami/nvme/kaizen-spot",
    encode_base64: bool = False,
    checkpoint_subpath: Optional[str] = None,
    fleet_nodes: int = 1,
) -> str:
    """Build Spot userdata with IMDSv2 2-minute interruption S3 checkpointing, 15-min idle-stop & fleet reaper."""
    clean_prefix = checkpoint_prefix.strip("/")
    tail = checkpoint_subpath.strip("/") if checkpoint_subpath is not None else run_id
    s3_ckpt = f"s3://{bucket}/{clean_prefix}/{tail}" if tail else f"s3://{bucket}/{clean_prefix}"
    idle_mins = max(1, int(idle_stop_minutes))
    reap_cmd = ""
    if int(fleet_nodes) > 1:
        reap_cmd = f"""
reap_fleet_peers() {{
  PEER_IDS="$(aws ec2 describe-instances --region "{region}" \\
    --filters "Name=tag:FleetRunId,Values={run_id}" "Name=instance-state-name,Values=pending,running,stopping,stopped" \\
    --query "Reservations[].Instances[].InstanceId" --output text 2>/dev/null || true)"
  if [ -n "$PEER_IDS" ]; then
    aws ec2 terminate-instances --region "{region}" --instance-ids $PEER_IDS >/dev/null 2>&1 || true
  fi
}}
"""
    peer_reap_hook = "\n      reap_fleet_peers || true" if int(fleet_nodes) > 1 else ""
    idle_reap_hook = "\nreap_fleet_peers || true" if int(fleet_nodes) > 1 else ""
    script = f"""#!/usr/bin/env bash
set -euo pipefail

export RUN_ID="{run_id}"
export FLEET_NODES="{int(max(1, fleet_nodes))}"
export NVME_WORK="{nvme_dir}"
export S3_CHECKPOINT_URI="{s3_ckpt}"
mkdir -p "${{NVME_WORK}}/checkpoints" "${{NVME_WORK}}/datasets"

sync_spot_checkpoints() {{
  aws s3 sync "${{NVME_WORK}}/checkpoints" "${{S3_CHECKPOINT_URI}}/" --region "{region}" --only-show-errors || true
}}
{reap_cmd}
trap sync_spot_checkpoints EXIT INT TERM

cat <<'EOF' > /usr/local/bin/kaizen-spot-imds-watcher.sh
#!/usr/bin/env bash
set -u
while true; do
  IMDS_TOKEN="$(curl -fsS -m 2 -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" || true)"
  if [ -n "$IMDS_TOKEN" ]; then
    ACTION="$(curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" "http://169.254.169.254/latest/meta-data/spot/instance-action" || true)"
    if [ -n "$ACTION" ]; then
      echo "[kaizen-spot] Interruption notice received: $ACTION — syncing checkpoints to {s3_ckpt}"
      aws s3 sync "{nvme_dir}/checkpoints" "{s3_ckpt}/" --region "{region}" --only-show-errors || true{peer_reap_hook}
      exit 0
    fi
  fi
  sleep 5
done
EOF
chmod +x /usr/local/bin/kaizen-spot-imds-watcher.sh
nohup /usr/local/bin/kaizen-spot-imds-watcher.sh >/var/log/kaizen-spot-imds.log 2>&1 &

cat <<'EOF' > /usr/local/bin/kaizen-deadman-idle-stop.sh
#!/usr/bin/env bash
aws s3 sync "{nvme_dir}/checkpoints" "{s3_ckpt}/" --region "{region}" --only-show-errors || true{idle_reap_hook}
/sbin/shutdown -h now
EOF
chmod +x /usr/local/bin/kaizen-deadman-idle-stop.sh

systemd-run --unit="kaizen-idle-stop-{run_id}" --on-active={idle_mins}m /usr/local/bin/kaizen-deadman-idle-stop.sh || true
aws s3 sync "${{S3_CHECKPOINT_URI}}/" "${{NVME_WORK}}/checkpoints/" --region "{region}" --only-show-errors || true
"""
    if encode_base64:
        return base64.b64encode(script.encode("utf-8")).decode("ascii")
    return script


def build_fleet_reaper_plan(
    run_id: str,
    region: str = DEFAULT_REGION,
    nodes: int = DISTRIBUTED_256GB_DEFAULT_NODES,
    instance_type: str = "g4dn.xlarge",
) -> Dict[str, Any]:
    """Build atomic Cluster Placement Group + Instant EC2 Fleet + zero-leftovers termination plan."""
    hw = INSTANCE_SPECS.get(instance_type)
    per_node_vram = hw.vram_gb if hw is not None else 16
    return {
        "fleet_run_id": run_id,
        "region": region,
        "nodes": int(nodes),
        "instance_type": instance_type,
        "aggregate_vcpus": int(nodes) * _vcpus_for(instance_type),
        "aggregate_vram_gb": int(nodes) * per_node_vram,
        "placement_group": {
            "GroupName": DISTRIBUTED_256GB_CPG_NAME,
            "Strategy": "cluster",
        },
        "create_fleet_spec": {
            "Type": "instant",
            "TargetCapacitySpecification": {
                "TotalTargetCapacity": int(nodes),
                "DefaultTargetCapacityType": "spot",
            },
            "SpotOptions": {
                "AllocationStrategy": "price-capacity-optimized",
                "SingleAvailabilityZone": True,
                "SingleInstanceType": True,
                "InstanceInterruptionBehavior": "terminate",
            },
            "InstanceInitiatedShutdownBehavior": "terminate",
            "EbsDeleteOnTermination": True,
            "Tags": {
                "FleetRunId": run_id,
                "KaizenAutoReap": "true",
                "ManagedBy": "kaizen-kit-gpu-burst",
            },
        },
        "zero_leftovers_policy": {
            "abort_on_partial_fulfillment": True,
            "cascade_terminate_on_single_node_interruption": True,
            "verify_zero_non_terminated_filter": f"tag:FleetRunId={run_id}",
            "verify_zero_orphan_ebs_volumes": True,
        },
    }


def build_qlora_factory_job_spec(
    adapters: Optional[Sequence[str]] = None,
    mode: str = "sft_dpo",
    tier: str = "large",
    region: str = DEFAULT_REGION,
    profile: str = DEFAULT_PROFILE,
    bucket: str = DEFAULT_BUCKET,
) -> Dict[str, Any]:
    """Build non-mutating Spot QLoRA SFT+DPO factory job specification backed by `LORA_PORTFOLIO`."""
    validate_u2_profile(profile)
    region = validate_aws_region(region)
    if mode not in ("sft", "dpo", "sft_dpo"):
        raise ValueError(f"Unsupported QLoRA factory mode '{mode}'. Expected 'sft', 'dpo', or 'sft_dpo'.")
    canonical_map = {row["adapter"]: dict(row) for row in LORA_PORTFOLIO}
    requested = [resolve_lora_adapter(a) for a in adapters] if adapters else list(canonical_map)
    resolved_specs = [canonical_map[a] for a in dict.fromkeys(requested) if a in canonical_map]
    burst = plan_gpu_burst(
        tier=tier,
        region=region,
        profile=profile,
        dry_run=True,
        execute_approved_u2=False,
        check_aws=False,
        bucket=bucket,
    )
    return {
        "mode": mode,
        "default_state": DEFAULT_STATE,
        "aws_mutated": False,
        "profile": profile,
        "region": region,
        "tier": tier,
        "adapters": resolved_specs,
        "sm75_fp16_enforced": True,
        "max_lora_rank": 16,
        "s3_datasets_uri": f"s3://{bucket}/kaizen-cloud-brain/lora-datasets/",
        "s3_checkpoints_uri": burst["checkpoint_s3_uri"],
        "spot_burst_plan": burst,
    }


def build_aws_batch_plan(
    tier: str = "ultra_frontier_2_8t",
    region: str = DEFAULT_REGION,
    bucket: str = DEFAULT_BUCKET,
    idle_stop_minutes: int = DEFAULT_IDLE_STOP_MINUTES,
) -> Dict[str, Any]:
    """Generate a non-mutating night AWS Batch (`SPOT_CAPACITY_OPTIMIZED`) specification."""
    tier_spec = GPU_BURST_TIERS.get(tier, GPU_BURST_TIERS["ultra_frontier_2_8t"])
    resolved = resolve_region_instance_types(tier, region=region, ec2_client=None)
    quota_res = check_g_spot_quota(resolved["selected_instance_types"], nodes=1, sq_client=None, region=region)
    valid_types = list(quota_res["valid_instance_types"])
    all_non_g_r7 = bool(valid_types) and all(not _is_g_family(t) and "r7" in t for t in valid_types)
    return {
        "compute_environment_name": f"kaizen-{tier.replace('_', '-')}-spot-ce",
        "allocation_strategy": "SPOT_CAPACITY_OPTIMIZED",
        "type": "MANAGED",
        "state": "ENABLED",
        "minvCpus": 0,
        "desiredvCpus": 0,
        "maxvCpus": 192 if all_non_g_r7 else int(DEFAULT_G_SPOT_VCPU_QUOTA),
        "instance_types": valid_types,
        "subnets": list(DEFAULT_PRIVATE_SUBNETS) if region == DEFAULT_REGION else [],
        "security_group_ids": [DEFAULT_SG] if region == DEFAULT_REGION else [],
        "idle_stop_minutes": int(idle_stop_minutes),
        "checkpoint_s3_uri": f"s3://{bucket}/{DEFAULT_CHECKPOINT_PREFIX}/",
        "models": list(tier_spec.models),
    }


def estimate_burst_finops(
    instance_types: Sequence[str],
    nodes: int = 1,
    gpu_hours: float = 1.0,
    cpu_hours: float = 0.0,
    s3_gb: float = 25.0,
    ebs_gb: float = 100.0,
    region: str = DEFAULT_REGION,
    spot_price_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute realistic AWS rough FinOps estimate (never $0 for active EC2 Spot)."""
    if not instance_types:
        return {
            "note": "AWS Pricing rough estimate (tahmini kaba hesap)",
            "profile": DEFAULT_PROFILE,
            "region": region,
            "ec2_spot_usd": 0.0,
            "s3_storage_usd": 0.0,
            "ebs_usd": 0.0,
            "estimated_total_usd": 0.0,
            "stopped_idle_cost_usd": 0.0,
        }
    primary = instance_types[0]
    hourly = spot_price_override if spot_price_override is not None else _spot_hourly_usd(primary)
    active_hours = max(0.0, float(gpu_hours))
    ec2_cost = round(hourly * max(1, int(nodes)) * active_hours, 2)
    cpu_cost = round(max(0.0, float(cpu_hours)) * _spot_hourly_usd("c7g.xlarge"), 2)
    s3_cost = round(max(0.0, float(s3_gb)) * 0.023, 2)
    ebs_cost = round(max(0.0, float(ebs_gb)) * 0.08 * (max(active_hours, 1.0) / 730.0), 2)
    total = round(ec2_cost + cpu_cost + s3_cost + ebs_cost, 2)
    return {
        "note": "AWS Pricing rough estimate (tahmini kaba hesap)",
        "profile": DEFAULT_PROFILE,
        "region": region,
        "primary_instance_type": primary,
        "nodes": int(nodes),
        "hourly_spot_usd_per_node": round(hourly, 4),
        "ec2_spot_usd": ec2_cost,
        "cpu_aux_spot_usd": cpu_cost,
        "s3_storage_usd": s3_cost,
        "ebs_usd": ebs_cost,
        "estimated_total_usd": total,
        "stopped_idle_cost_usd": 0.0,
        "assumptions": {
            "gpu_hours": gpu_hours,
            "cpu_hours": cpu_hours,
            "s3_gb": s3_gb,
            "ebs_gb": ebs_gb,
        },
    }


def _open_aws_clients(
    profile: str,
    region: str,
    ec2_client: Any = None,
    sq_client: Any = None,
) -> Tuple[Any, Any]:
    if boto3 is None:
        return ec2_client, sq_client
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        ec2 = ec2_client if ec2_client is not None else session.client("ec2")
        sq = sq_client if sq_client is not None else session.client("service-quotas")
        return ec2, sq
    except (ClientError, BotoCoreError, OSError, ValueError):
        return ec2_client, sq_client


def _resolve_live_or_offline_clients(
    profile: str,
    region: str,
    check_aws: bool,
    ec2_client: Any,
    sq_client: Any,
) -> Tuple[Any, Any]:
    if ec2_client is not None or sq_client is not None:
        return ec2_client, sq_client
    if not check_aws:
        return None, None
    return _open_aws_clients(profile, region)


def plan_gpu_burst(
    tier: str = "large",
    region: str = DEFAULT_REGION,
    profile: str = DEFAULT_PROFILE,
    nodes: int = 1,
    model_id: Optional[str] = None,
    depth_method: str = "ngram-lookup",
    width_method: str = "reap",
    bucket: str = DEFAULT_BUCKET,
    checkpoint_prefix: str = DEFAULT_CHECKPOINT_PREFIX,
    model_prefix: str = DEFAULT_MODEL_STASH_PREFIX,
    idle_stop_minutes: int = DEFAULT_IDLE_STOP_MINUTES,
    min_sps: int = DEFAULT_MIN_SPS,
    dry_run: bool = DEFAULT_DRY_RUN,
    execute_approved_u2: bool = DEFAULT_EXECUTE_APPROVED_U2,
    allow_low_sps: bool = False,
    check_aws: bool = True,
    ec2_client: Any = None,
    sq_client: Any = None,
    candidate_types: Optional[Sequence[str]] = None,
    gpu_hours: float = 1.0,
    cpu_hours: float = 0.0,
    min_vram_gb: int = 0,
) -> Dict[str, Any]:
    """Plan (and refuse mutation unless explicitly approved + safe) a Kaizen GPU/Cloud burst."""
    validate_u2_profile(profile)
    region = validate_aws_region(region)
    if tier not in GPU_BURST_TIERS:
        raise ValueError(f"Unknown tier '{tier}'. Valid tiers: {sorted(GPU_BURST_TIERS)}")
    if nodes < 0 or (GPU_BURST_TIERS[tier].ec2_required and nodes < 1):
        raise ValueError(f"nodes must be >= 1 for EC2 tier '{tier}', got {nodes}")

    effective_nodes = (
        DISTRIBUTED_256GB_DEFAULT_NODES
        if tier == "distributed_256gb" and int(nodes) == 1
        else int(nodes)
    )
    effective_min_vram_gb = (
        64
        if tier == "large" and effective_nodes == 1 and int(min_vram_gb) == 0 and candidate_types is None
        else (16 if tier == "distributed_256gb" and int(min_vram_gb) == 0 else int(min_vram_gb))
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    ec2, sq = _resolve_live_or_offline_clients(profile, region, check_aws, ec2_client, sq_client)
    region_mapping = resolve_region_instance_types(
        tier, region=region, ec2_client=ec2, candidate_types=candidate_types
    )
    selected_types = region_mapping["selected_instance_types"]
    quota_check = check_g_spot_quota(
        selected_types, nodes=max(1, effective_nodes), sq_client=sq, region=region, ec2_client=ec2
    )
    valid_types = list(quota_check["valid_instance_types"])
    primary_instance = valid_types[0] if valid_types else (selected_types[0] if selected_types else None)
    sparsity_config = build_2d_sparsity_config(
        tier=tier,
        model_id=model_id,
        depth_method=depth_method,
        width_method=width_method,
        bucket=bucket,
        model_prefix=model_prefix,
        instance_type=primary_instance,
        nodes=max(1, effective_nodes),
    )
    if not GPU_BURST_TIERS[tier].ec2_required:
        spot_placement = evaluate_spot_placement_and_prices(
            ec2_client=ec2,
            instance_types=[],
            target_capacity=max(1, effective_nodes),
            region=region,
            min_sps=min_sps,
            min_vram_gb=effective_min_vram_gb,
        )
        finops = estimate_burst_finops(
            instance_types=[],
            nodes=max(1, effective_nodes),
            gpu_hours=gpu_hours,
            cpu_hours=cpu_hours,
            region=region,
        )
    elif quota_check["passed"] and valid_types:
        spot_placement = evaluate_spot_placement_and_prices(
            ec2_client=ec2,
            instance_types=valid_types,
            target_capacity=max(1, effective_nodes),
            region=region,
            min_sps=min_sps,
            min_vram_gb=effective_min_vram_gb,
        )
        ranked_azs = spot_placement["ranked_azs"]
        if ranked_azs:
            top_az = ranked_azs[0]
            priced_type = str(top_az["recommended_instance_type"])
            priced_usd = float(top_az["spot_price_usd_hr"])
            finops_types = [priced_type] + [t for t in valid_types if t != priced_type]
            finops_override: Optional[float] = priced_usd
        else:
            finops_types = valid_types
            finops_override = None
        finops = estimate_burst_finops(
            instance_types=finops_types,
            nodes=max(1, effective_nodes),
            gpu_hours=gpu_hours,
            cpu_hours=cpu_hours,
            region=region,
            spot_price_override=finops_override,
        )
    else:
        spot_placement = {
            "region": region,
            "target_capacity": int(max(1, effective_nodes)),
            "min_sps_required": int(min_sps),
            "min_vram_gb": effective_min_vram_gb,
            "max_sps": 0,
            "sps_gate_passed": False,
            "sps_diagnostic": f"Quota gate blocked Spot placement: {quota_check['diagnostic']}",
            "placement_scores": [],
            "ranked_azs": [],
            "spot_prices_usd": {},
            "cheapest_az": None,
        }
        finops = estimate_burst_finops(
            instance_types=selected_types,
            nodes=max(1, effective_nodes),
            gpu_hours=gpu_hours,
            cpu_hours=cpu_hours,
            region=region,
            spot_price_override=None,
        )
    userdata = (
        build_spot_userdata(
            run_id=run_id,
            bucket=bucket,
            checkpoint_prefix=checkpoint_prefix,
            region=region,
            idle_stop_minutes=idle_stop_minutes,
            fleet_nodes=max(1, effective_nodes),
        )
        if GPU_BURST_TIERS[tier].ec2_required
        else ""
    )

    can_launch = (
        GPU_BURST_TIERS[tier].ec2_required
        and dry_run is False
        and execute_approved_u2 is True
        and bool(quota_check["passed"])
        and (bool(spot_placement["sps_gate_passed"]) or allow_low_sps)
    )
    block_reasons: List[str] = []
    if dry_run:
        block_reasons.append("dry_run=True (safe STOPPED default)")
    if not execute_approved_u2:
        block_reasons.append("execute_approved_u2=False")
    if not quota_check["passed"]:
        block_reasons.append(quota_check["diagnostic"])
    if GPU_BURST_TIERS[tier].ec2_required and not spot_placement["sps_gate_passed"] and not allow_low_sps:
        block_reasons.append(spot_placement["sps_diagnostic"])

    plan: Dict[str, Any] = {
        "run_id": run_id,
        "tier": tier,
        "parameter_range": GPU_BURST_TIERS[tier].parameter_range,
        "profile": profile,
        "region": region,
        "default_state": DEFAULT_STATE,
        "dry_run": bool(dry_run),
        "execute_approved_u2": bool(execute_approved_u2),
        "aws_mutated": False,
        "launch_eligible": can_launch,
        "block_reasons": block_reasons,
        "idle_stop_minutes": int(idle_stop_minutes),
        "nodes": int(effective_nodes),
        "region_mapping": region_mapping,
        "sparsity_config": sparsity_config,
        "quota_check": quota_check,
        "spot_placement": spot_placement,
        "finops_estimate": finops,
        "checkpoint_s3_uri": f"s3://{bucket}/{checkpoint_prefix.strip('/')}/{run_id}/",
        "userdata_preview": userdata,
    }
    if GPU_BURST_TIERS[tier].ec2_required and (tier == "distributed_256gb" or effective_nodes > 1):
        plan["distributed_fleet_spec"] = build_fleet_reaper_plan(
            run_id=run_id,
            region=region,
            nodes=max(1, effective_nodes),
            instance_type=primary_instance or "g4dn.xlarge",
        )
    if GPU_BURST_TIERS[tier].supports_aws_batch_night:
        plan["aws_batch_plan"] = build_aws_batch_plan(
            tier=tier, region=region, bucket=bucket, idle_stop_minutes=idle_stop_minutes
        )
    if can_launch:
        plan["mode"] = "APPROVED_LAUNCH_READY"
    elif dry_run or not execute_approved_u2:
        plan["mode"] = "DRY_RUN_PLAN_ONLY"
    else:
        plan["mode"] = "BLOCKED_BY_GUARD"
    return plan


def plan_spot_fleet(
    nodes: int = TIER_FLEET_MIN_NODES,
    tier: str = "large",
    region: str = DEFAULT_REGION,
    profile: str = DEFAULT_PROFILE,
    model_id: Optional[str] = None,
    idle_stop_minutes: int = DEFAULT_IDLE_STOP_MINUTES,
    min_sps: int = DEFAULT_MIN_SPS,
    dry_run: bool = DEFAULT_DRY_RUN,
    execute_approved_u2: bool = DEFAULT_EXECUTE_APPROVED_U2,
    allow_low_sps: bool = False,
    check_aws: bool = True,
    ec2_client: Any = None,
    sq_client: Any = None,
    candidate_types: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Plan a Tier-2 Spot Fleet (`2 <= nodes <= 16`) with G-Spot quota & SPS guards."""
    if tier not in GPU_BURST_TIERS:
        raise ValueError(f"Unknown tier '{tier}'. Valid tiers: {sorted(GPU_BURST_TIERS)}")
    if not GPU_BURST_TIERS[tier].ec2_required:
        raise ValueError(f"Tier '{tier}' does not support EC2 Spot Fleet (ec2_required=False)")
    if nodes < TIER_FLEET_MIN_NODES or nodes > TIER_FLEET_MAX_NODES:
        raise ValueError(
            f"Tier-2 Spot Fleet requires {TIER_FLEET_MIN_NODES} <= nodes <= {TIER_FLEET_MAX_NODES}, got {nodes}"
        )
    plan = plan_gpu_burst(
        tier=tier,
        region=region,
        profile=profile,
        nodes=nodes,
        model_id=model_id,
        idle_stop_minutes=idle_stop_minutes,
        min_sps=min_sps,
        dry_run=dry_run,
        execute_approved_u2=execute_approved_u2,
        allow_low_sps=allow_low_sps,
        check_aws=check_aws,
        ec2_client=ec2_client,
        sq_client=sq_client,
        candidate_types=candidate_types,
    )
    plan["workflow"] = "tier2_spot_fleet"
    plan["fleet_bounds"] = {"min_nodes": TIER_FLEET_MIN_NODES, "max_nodes": TIER_FLEET_MAX_NODES}
    return plan


def _tiers_payload() -> Dict[str, Any]:
    return {
        "default_state": DEFAULT_STATE,
        "dry_run_default": DEFAULT_DRY_RUN,
        "idle_stop_minutes": DEFAULT_IDLE_STOP_MINUTES,
        "min_sps": DEFAULT_MIN_SPS,
        "g_spot_vcpu_quota": DEFAULT_G_SPOT_VCPU_QUOTA,
        "fleet_bounds": {"min_nodes": TIER_FLEET_MIN_NODES, "max_nodes": TIER_FLEET_MAX_NODES},
        "lora_portfolio": list(LORA_PORTFOLIO),
        "tiers": {k: asdict(v) for k, v in GPU_BURST_TIERS.items()},
    }


def run_self_test() -> int:
    """Deterministic offline self-test for GPU burst tiers, eu-west-1 mapping, quota & fleet bounds."""
    assert DEFAULT_STATE == "STOPPED"
    assert set(GPU_BURST_TIERS) == {
        "daytime_bedrock",
        "large",
        "distributed_256gb",
        "frontier_1t",
        "ultra_frontier_2_8t",
    }
    assert resolve_lora_adapter("lora-avro-c") == "lora-rust-vector-parity"
    assert resolve_lora_adapter("lora-sql-semilattice") == "lora-rust-vector-parity"
    assert resolve_lora_adapter("lora-parity-rca-judge") == "lora-system-rca-triage"

    try:
        validate_u2_profile("production")
        print("[FAIL] gpu_burst_orchestrator allowed non-u2 profile")
        return 1
    except ValueError:
        pass

    eu_west_map = resolve_region_instance_types("large", region="eu-west-1", ec2_client=None)
    eu_central_map = resolve_region_instance_types("large", region="eu-central-1", ec2_client=None)
    if eu_west_map["selected_instance_types"][0] != "g4dn.12xlarge":
        print(f"[FAIL] eu-west-1 large mapping expected g4dn.12xlarge, got {eu_west_map}")
        return 1
    if "g6.12xlarge" not in eu_central_map["selected_instance_types"]:
        print(f"[FAIL] eu-central-1 missing g6.12xlarge: {eu_central_map}")
        return 1

    t4_cfg = build_2d_sparsity_config("large", instance_type="g4dn.12xlarge")
    if not t4_cfg["t4_sm75_guard"] or "fp8_e4m3" in t4_cfg["vllm_serving_flags"]:
        print(f"[FAIL] g4dn.12xlarge SM75 guard failed: {t4_cfg}")
        return 1

    dist_plan = plan_gpu_burst(tier="distributed_256gb", check_aws=False)
    if dist_plan["nodes"] != 16 or dist_plan["sparsity_config"]["aggregate_vram_gb"] != 256:
        print(f"[FAIL] distributed_256gb expected 16x16GB=256GB, got {dist_plan}")
        return 1

    f1t_arm = build_2d_sparsity_config("frontier_1t", instance_type="r7g.16xlarge")
    if f1t_arm["heterogeneous_offload"]["engine"] != "ik_llama.cpp":
        print(f"[FAIL] r7g.16xlarge ARM64 should select ik_llama.cpp, got {f1t_arm}")
        return 1

    quota_fail = check_g_spot_quota(["g4dn.12xlarge"], nodes=2, sq_client=None)
    quota_ok = check_g_spot_quota(["g4dn.xlarge"], nodes=16, sq_client=None)
    if quota_fail["passed"] or not quota_ok["passed"]:
        print(f"[FAIL] quota check mismatch: fail={quota_fail} ok={quota_ok}")
        return 1

    try:
        plan_spot_fleet(nodes=1, check_aws=False)
        return 1
    except ValueError:
        pass
    try:
        plan_spot_fleet(nodes=17, check_aws=False)
        return 1
    except ValueError:
        pass

    fleet = plan_spot_fleet(nodes=16, tier="distributed_256gb", check_aws=False)
    ud = fleet["userdata_preview"]
    checks = [
        fleet["default_state"] == "STOPPED",
        fleet["aws_mutated"] is False,
        "169.254.169.254/latest/api/token" in ud,
        "spot/instance-action" in ud,
        "reap_fleet_peers" in ud,
        "kaizen-cloud-brain/spot-checkpoints" in ud,
        "--on-active=15m" in ud,
    ]
    if not all(checks):
        print(f"[FAIL] gpu_burst_orchestrator self-test failed: {checks}")
        return 1
    print("[PASS] gpu_burst_orchestrator -- 5 tiers (+distributed_256gb), eu-west-1 g4dn SM75, 64vCPU quota, 2..16 fleet & reaper verified")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kaizen-kit gpu-burst", description="Unified GPU Burst & Tier-2 Spot Fleet CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_tiers = sub.add_parser("tiers", help="List the 4 GPU/Cloud burst tiers and safe defaults")
    p_tiers.add_argument("--json", action="store_true", dest="as_json")

    p_plan = sub.add_parser("plan", help="Plan a GPU/CPU burst tier (dry-run STOPPED by default)")
    p_plan.add_argument("--tier", choices=sorted(GPU_BURST_TIERS), default="large")
    p_plan.add_argument("--region", default=DEFAULT_REGION)
    p_plan.add_argument("--profile", default=DEFAULT_PROFILE)
    p_plan.add_argument("--model", default="")
    p_plan.add_argument("--nodes", type=int, default=1)
    p_plan.add_argument("--idle-minutes", type=int, default=DEFAULT_IDLE_STOP_MINUTES)
    p_plan.add_argument("--dry-run", action="store_true", default=True)
    p_plan.add_argument("--no-aws", action="store_true", help="Skip live read-only AWS Spot/quota checks")
    p_plan.add_argument("--json", action="store_true", dest="as_json")

    p_fleet = sub.add_parser("fleet", help="Plan a 2..10-node Tier-2 Spot Fleet")
    p_fleet.add_argument("--nodes", type=int, default=2)
    p_fleet.add_argument("--tier", choices=sorted(GPU_BURST_TIERS), default="large")
    p_fleet.add_argument("--region", default=DEFAULT_REGION)
    p_fleet.add_argument("--profile", default=DEFAULT_PROFILE)
    p_fleet.add_argument("--candidate", action="append", default=[])
    p_fleet.add_argument("--dry-run", action="store_true", default=True)
    p_fleet.add_argument("--no-aws", action="store_true")
    p_fleet.add_argument("--json", action="store_true", dest="as_json")

    p_spot = sub.add_parser("spot-check", help="Run read-only kaizen-u2 offerings, quota, SPS & Spot price check")
    p_spot.add_argument("--tier", choices=sorted(GPU_BURST_TIERS), default="large")
    p_spot.add_argument("--region", default=DEFAULT_REGION)
    p_spot.add_argument("--profile", default=DEFAULT_PROFILE)
    p_spot.add_argument("--nodes", type=int, default=1)
    p_spot.add_argument("--json", action="store_true", dest="as_json")

    sub.add_parser("self-test", help="Run deterministic offline self-test")
    return ap


def _emit(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.cmd == "self-test":
        return run_self_test()
    if args.cmd == "tiers":
        _emit(_tiers_payload(), args.as_json)
        return 0
    if args.cmd == "plan":
        res = plan_gpu_burst(
            tier=args.tier,
            region=args.region,
            profile=args.profile,
            nodes=args.nodes,
            model_id=args.model or None,
            idle_stop_minutes=args.idle_minutes,
            dry_run=True,
            execute_approved_u2=False,
            check_aws=not args.no_aws,
        )
        _emit(res, args.as_json)
        return 0
    if args.cmd == "fleet":
        res = plan_spot_fleet(
            nodes=args.nodes,
            tier=args.tier,
            region=args.region,
            profile=args.profile,
            dry_run=True,
            execute_approved_u2=False,
            check_aws=not args.no_aws,
            candidate_types=args.candidate or None,
        )
        _emit(res, args.as_json)
        return 0
    if args.cmd == "spot-check":
        res = plan_gpu_burst(
            tier=args.tier,
            region=args.region,
            profile=args.profile,
            nodes=args.nodes,
            dry_run=True,
            execute_approved_u2=False,
            check_aws=True,
        )
        _emit(
            {
                "profile": res["profile"],
                "region": res["region"],
                "tier": res["tier"],
                "default_state": res["default_state"],
                "region_mapping": res["region_mapping"],
                "quota_check": res["quota_check"],
                "spot_placement": res["spot_placement"],
            },
            args.as_json,
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
