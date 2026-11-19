#!/usr/bin/env python3
"""Carbon 6-Layer Open-Source LLM Army & Hybrid Cascade Orchestrator.

Coordinates thin-client orchestration across:
  - Layer 0/1/2 always-on EC2 Cloud-Brain (`m7g.8xlarge` Graviton3 ARM64, 126 GB DDR5)
  - Layer 3 managed daytime AWS Bedrock Claude 5.5 (1h Prompt Cache, Windows-isolated)
  - Layer 4 ephemeral Spot GPU Burst (`g4dn.12xlarge` SM75 in `eu-west-1`, `g6`/`g6e` SM89 in `eu-central-1`)
  - Layer 5 overnight high-RAM batch (`r7g.16xlarge` / `r7i.24xlarge`)

Enforces `DE-005` (no AirLLM disk-per-token streaming), `DE-006` (no slow CPU draft spec-decoding),
`L-3819A6DF` (64 vCPU G-Spot quota), SM75 vs SM89 tensor/KV guards, commercial-safe licenses,
ETH Cascade Routing, and PoLL multi-family code review juries.
"""

from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
import re
import shlex
import socket
import subprocess
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from . import gemini_cloud_client, gpu_burst_orchestrator, guardrails, localmodels, model_router, neon_night_shift

M7G_TOTAL_RAM_GIB = 126.0
M7G_SAFE_WEIGHT_CEILING_GIB = 72.0

UNCERTAINTY_MARKERS: Tuple[str, ...] = (
    "i cannot verify",
    "i am not sure",
    "i'm not sure",
    "insufficient context",
    "cannot determine",
    "need more context",
    "uncertain",
)


@dataclass(frozen=True)
class ArmyModelSpec:
    """Verified Hugging Face / Bedrock model metadata and hardware compatibility contract."""

    alias: str
    hf_repo_id: str
    layer: int
    role: str
    license_id: str
    commercial_safe: bool
    total_params_b: float
    active_params_b: float
    context_tokens: int
    quant_format: str
    footprint_gb: float
    target_hardware: str
    runtime_engine: str
    requires_sm89: bool = False
    requires_mxfp4: bool = False
    notes: str = ""


ARMY_MODELS: Dict[str, ArmyModelSpec] = {
    # Layer 0: Guard, Router, NLI & Code RAG (m7g.8xlarge CPU DDR5)
    "prompt-guard-deberta": ArmyModelSpec(
        alias="prompt-guard-deberta",
        hf_repo_id="protectai/deberta-v3-base-prompt-injection-v2",
        layer=0,
        role="guardrail_input",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=0.184,
        active_params_b=0.184,
        context_tokens=512,
        quant_format="FP16/ONNX-INT8",
        footprint_gb=0.37,
        target_hardware="m7g.8xlarge",
        runtime_engine="onnxruntime-arm64",
        notes="Sub-20ms prompt-injection front-door classifier.",
    ),
    "nli-deberta": ArmyModelSpec(
        alias="nli-deberta",
        hf_repo_id="MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
        layer=0,
        role="nli_verifier",
        license_id="MIT",
        commercial_safe=True,
        total_params_b=0.184,
        active_params_b=0.184,
        context_tokens=512,
        quant_format="FP16",
        footprint_gb=0.37,
        target_hardware="m7g.8xlarge",
        runtime_engine="onnxruntime-arm64",
        notes="Zero-shot intent routing and claim/citation entailment check.",
    ),
    "qwen3-guard-0.6b": ArmyModelSpec(
        alias="qwen3-guard-0.6b",
        hf_repo_id="Qwen/Qwen3Guard-Gen-0.6B",
        layer=0,
        role="guardrail_output",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=0.6,
        active_params_b=0.6,
        context_tokens=32768,
        quant_format="Q4_K_M",
        footprint_gb=0.50,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="Tri-class safe/controversial/unsafe guardrail.",
    ),
    "qwen3-embed-0.6b": ArmyModelSpec(
        alias="qwen3-embed-0.6b",
        hf_repo_id="Qwen/Qwen3-Embedding-0.6B",
        layer=0,
        role="rag_embedder",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=0.6,
        active_params_b=0.6,
        context_tokens=32768,
        quant_format="FP16",
        footprint_gb=1.20,
        target_hardware="m7g.8xlarge",
        runtime_engine="tei:8766",
        notes="Matryoshka 32-1024 dim code/text embeddings; pass query instruction prefix.",
    ),
    "bge-reranker-v2-m3": ArmyModelSpec(
        alias="bge-reranker-v2-m3",
        hf_repo_id="BAAI/bge-reranker-v2-m3",
        layer=0,
        role="rag_reranker",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=0.568,
        active_params_b=0.568,
        context_tokens=8192,
        quant_format="FP16",
        footprint_gb=1.14,
        target_hardware="m7g.8xlarge",
        runtime_engine="tei:8766",
        notes="Native TEI SequenceClassification reranker (use llama-server --reranking for Qwen3-Reranker).",
    ),
    "granite-4-h-tiny": ArmyModelSpec(
        alias="granite-4-h-tiny",
        hf_repo_id="ibm-granite/granite-4.0-h-tiny",
        layer=0,
        role="router_tool_caller",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=7.0,
        active_params_b=1.0,
        context_tokens=131072,
        quant_format="Q4_K_M",
        footprint_gb=4.23,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="1B active Hybrid Mamba-2 MoE for JSON routing and tool selection.",
    ),
    "bge-m3": ArmyModelSpec(
        alias="bge-m3",
        hf_repo_id="BAAI/bge-m3",
        layer=0,
        role="hybrid_rag_embedder",
        license_id="MIT",
        commercial_safe=True,
        total_params_b=0.568,
        active_params_b=0.568,
        context_tokens=8192,
        quant_format="FP16",
        footprint_gb=1.14,
        target_hardware="m7g.8xlarge",
        runtime_engine="tei:8766",
        notes="Lever B dense+sparse+multi-vector hybrid retrieval embedder.",
    ),
    "qwen3-reranker-0.6b": ArmyModelSpec(
        alias="qwen3-reranker-0.6b",
        hf_repo_id="Qwen/Qwen3-Reranker-0.6B",
        layer=0,
        role="hybrid_rag_reranker",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=0.6,
        active_params_b=0.6,
        context_tokens=32768,
        quant_format="Q8_0",
        footprint_gb=0.65,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="Lever B cross-encoder code/doc reranker via llama-server --reranking.",
    ),
    "granite-docling-258m": ArmyModelSpec(
        alias="granite-docling-258m",
        hf_repo_id="ibm-granite/granite-docling-258M",
        layer=0,
        role="doc_ocr_parser",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=0.258,
        active_params_b=0.258,
        context_tokens=8192,
        quant_format="FP16",
        footprint_gb=0.52,
        target_hardware="m7g.8xlarge",
        runtime_engine="onnxruntime-arm64",
        notes="Lever B ultra-light layout-preserving PDF/diagram-to-Markdown OCR parser.",
    ),
    "qwen3-guard-stream-0.6b": ArmyModelSpec(
        alias="qwen3-guard-stream-0.6b",
        hf_repo_id="Qwen/Qwen3Guard-Stream-0.6B",
        layer=0,
        role="guardrail_stream",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=0.6,
        active_params_b=0.6,
        context_tokens=32768,
        quant_format="Q8_0",
        footprint_gb=0.64,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="Lever E token-level real-time streaming guardrail.",
    ),
    "whisper-large-v3-turbo": ArmyModelSpec(
        alias="whisper-large-v3-turbo",
        hf_repo_id="openai/whisper-large-v3-turbo",
        layer=0,
        role="voice_asr",
        license_id="MIT",
        commercial_safe=True,
        total_params_b=0.809,
        active_params_b=0.809,
        context_tokens=448,
        quant_format="INT8",
        footprint_gb=0.85,
        target_hardware="m7g.8xlarge",
        runtime_engine="ctranslate2-arm64",
        notes="Lever E low-latency multilingual speech-to-text for Voice-to-Action.",
    ),
    "kokoro-82m": ArmyModelSpec(
        alias="kokoro-82m",
        hf_repo_id="hexgrad/Kokoro-82M",
        layer=0,
        role="voice_tts",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=0.082,
        active_params_b=0.082,
        context_tokens=2048,
        quant_format="ONNX-FP16",
        footprint_gb=0.17,
        target_hardware="m7g.8xlarge",
        runtime_engine="onnxruntime-arm64",
        notes="Lever E sub-realtime CPU text-to-speech synthesis.",
    ),
    # Layer 1: Micro & 1.58-bit Edge Workers (m7g.8xlarge CPU DDR5, standalone per DE-006)
    "bitnet-2b": ArmyModelSpec(
        alias="bitnet-2b",
        hf_repo_id="microsoft/bitnet-b1.58-2B-4T",
        layer=1,
        role="micro_classifier",
        license_id="MIT",
        commercial_safe=True,
        total_params_b=2.4,
        active_params_b=2.4,
        context_tokens=4096,
        quant_format="i2_s",
        footprint_gb=1.19,
        target_hardware="m7g.8xlarge",
        runtime_engine="bitnet.cpp-arm64",
        notes="Ultra-low-RAM 1.58-bit ARM64 NEON worker for <4K log/commit classification.",
    ),
    "qwen3.5-0.8b": ArmyModelSpec(
        alias="qwen3.5-0.8b",
        hf_repo_id="Qwen/Qwen3.5-0.8B",
        layer=1,
        role="micro_router",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=0.8,
        active_params_b=0.8,
        context_tokens=262144,
        quant_format="Q4_K_M",
        footprint_gb=0.54,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="Sub-1GB 262K-ctx Gated DeltaNet classifier and boilerplate filter.",
    ),
    "qwen3.5-2b": ArmyModelSpec(
        alias="qwen3.5-2b",
        hf_repo_id="Qwen/Qwen3.5-2B",
        layer=1,
        role="micro_triage",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=2.0,
        active_params_b=2.0,
        context_tokens=262144,
        quant_format="Q4_K_M",
        footprint_gb=1.27,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="262K-ctx JSON/tool router and diff pre-filter.",
    ),
    "qwen3.5-4b": ArmyModelSpec(
        alias="qwen3.5-4b",
        hf_repo_id="Qwen/Qwen3.5-4B",
        layer=1,
        role="micro_coder",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=4.0,
        active_params_b=4.0,
        context_tokens=262144,
        quant_format="Q4_K_M",
        footprint_gb=2.80,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="Top <=4B long-context AST/diff triage and fast CPU micro-coder.",
    ),
    "gemma-4-e2b": ArmyModelSpec(
        alias="gemma-4-e2b",
        hf_repo_id="google/gemma-4-E2B-it",
        layer=1,
        role="micro_reviewer",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=5.1,
        active_params_b=2.3,
        context_tokens=131072,
        quant_format="Q4_K_M",
        footprint_gb=3.11,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="Apache 2.0 PLE edge model with mmap-able embeddings.",
    ),
    "nemotron-nano-9b-v2": ArmyModelSpec(
        alias="nemotron-nano-9b-v2",
        hf_repo_id="nvidia/Nemotron-Nano-9B-v2",
        layer=1,
        role="micro_reasoner",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=9.0,
        active_params_b=9.0,
        context_tokens=131072,
        quant_format="Q4_K_M",
        footprint_gb=5.40,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="NVIDIA 9B dense code and reasoning engine with 128k context.",
    ),
    # Layer 2: Always-On CPU Sparse-MoE Pool (m7g.8xlarge DDR5, <=3.8B active)
    "qwen3-coder-30b-a3b": ArmyModelSpec(
        alias="qwen3-coder-30b-a3b",
        hf_repo_id="Qwen/Qwen3-Coder-30B-A3B-Instruct",
        layer=2,
        role="cpu_code_writer",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=30.5,
        active_params_b=3.3,
        context_tokens=262144,
        quant_format="Q4_K_M",
        footprint_gb=18.60,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="Primary CPU MoE code writer (~56.5 tok/s decode on m7g.8xlarge -t 24).",
    ),
    "qwen3.5-35b-a3b": ArmyModelSpec(
        alias="qwen3.5-35b-a3b",
        hf_repo_id="Qwen/Qwen3.5-35B-A3B",
        layer=2,
        role="cpu_reviewer_tool_caller",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=35.0,
        active_params_b=3.0,
        context_tokens=262144,
        quant_format="Q4_K_M",
        footprint_gb=19.70,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="Primary Layer 2 reviewer, tool-caller, and AutoDream synthesizer.",
    ),
    "glm-4.7-flash": ArmyModelSpec(
        alias="glm-4.7-flash",
        hf_repo_id="zai-org/GLM-4.7-Flash",
        layer=2,
        role="cpu_tool_coder",
        license_id="MIT",
        commercial_safe=True,
        total_params_b=30.0,
        active_params_b=3.0,
        context_tokens=202752,
        quant_format="Q4_K_M",
        footprint_gb=18.00,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="MIT 3B-active MoE secondary coder and structured JSON/tool caller.",
    ),
    "ling-mini-2.0": ArmyModelSpec(
        alias="ling-mini-2.0",
        hf_repo_id="inclusionAI/Ling-mini-2.0",
        layer=2,
        role="cpu_autodream_fast",
        license_id="MIT",
        commercial_safe=True,
        total_params_b=16.0,
        active_params_b=1.4,
        context_tokens=131072,
        quant_format="Q4_K_M",
        footprint_gb=9.94,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="1.4B-active MoE for fastest CPU AutoDream memory consolidation and triage.",
    ),
    "gemma-4-26b-a4b": ArmyModelSpec(
        alias="gemma-4-26b-a4b",
        hf_repo_id="google/gemma-4-26B-A4B-it",
        layer=2,
        role="cpu_multimodal_reviewer",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=25.2,
        active_params_b=3.8,
        context_tokens=262144,
        quant_format="UD-Q4_K_M",
        footprint_gb=16.90,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="Apache 2.0 diverse-family PoLL jury reviewer and multimodal UI inspector.",
    ),
    "gemma-4-12b-unified": ArmyModelSpec(
        alias="gemma-4-12b-unified",
        hf_repo_id="google/gemma-4-12B-it",
        layer=2,
        role="cpu_multimodal_ocr",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=12.0,
        active_params_b=12.0,
        context_tokens=131072,
        quant_format="Q4_K_M",
        footprint_gb=7.60,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="Lever B multimodal OCR and architecture diagram inspector (mmap warm-swap).",
    ),
    "nemotron-3-nano-30b-a3b": ArmyModelSpec(
        alias="nemotron-3-nano-30b-a3b",
        hf_repo_id="nvidia/Nemotron-3-Nano-30B-A3B",
        layer=2,
        role="cpu_code_reviewer",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=30.0,
        active_params_b=3.2,
        context_tokens=131072,
        quant_format="Q4_K_M",
        footprint_gb=18.20,
        target_hardware="m7g.8xlarge",
        runtime_engine="ik_llama.cpp",
        notes="NVIDIA 30B MoE (3.2B active) optimized for fast CPU/ARM64 inference and structured reasoning.",
    ),
    # Layer 3: Daytime Managed Corporate Bedrock (1h Prompt Cache, zero GPU idle wait)
    "claude-haiku-5-5": ArmyModelSpec(
        alias="claude-haiku-5-5",
        hf_repo_id="bedrock://eu-west-1/claude-haiku-5-5",
        layer=3,
        role="bedrock_fast_worker",
        license_id="Corporate-Bedrock",
        commercial_safe=True,
        total_params_b=0.0,
        active_params_b=0.0,
        context_tokens=200000,
        quant_format="managed",
        footprint_gb=0.0,
        target_hardware="aws-bedrock-windows-isolated",
        runtime_engine="bedrock_bridge",
    ),
    "claude-sonnet-5-5": ArmyModelSpec(
        alias="claude-sonnet-5-5",
        hf_repo_id="bedrock://eu-west-1/claude-sonnet-5-5",
        layer=3,
        role="bedrock_architect",
        license_id="Corporate-Bedrock",
        commercial_safe=True,
        total_params_b=0.0,
        active_params_b=0.0,
        context_tokens=200000,
        quant_format="managed",
        footprint_gb=0.0,
        target_hardware="aws-bedrock-windows-isolated",
        runtime_engine="bedrock_bridge",
    ),
    "claude-opus-5-5": ArmyModelSpec(
        alias="claude-opus-5-5",
        hf_repo_id="bedrock://eu-west-1/claude-opus-5-5",
        layer=3,
        role="bedrock_supreme_judge",
        license_id="Corporate-Bedrock",
        commercial_safe=True,
        total_params_b=0.0,
        active_params_b=0.0,
        context_tokens=200000,
        quant_format="managed",
        footprint_gb=0.0,
        target_hardware="aws-bedrock-windows-isolated",
        runtime_engine="bedrock_bridge",
    ),
    # Layer 3B: Public-Sanitized Gemini Cloud API (Fail-closed DataResidencyGate)
    "gemini-decision-1": ArmyModelSpec(
        alias="gemini-decision-1",
        hf_repo_id="gemini://v1beta/gemini-decision-1",
        layer=3,
        role="gemini_decision_gate",
        license_id="Google-Gemini-API",
        commercial_safe=True,
        total_params_b=0.0,
        active_params_b=0.0,
        context_tokens=262144,
        quant_format="managed",
        footprint_gb=0.0,
        target_hardware="google-gemini-api-public-sanitized",
        runtime_engine="gemini_cloud_client",
        notes="Layer 3B Decision-1 calibrated router/rubric gate (0 output tokens; public_sanitized only).",
    ),
    "gemini-4-neon": ArmyModelSpec(
        alias="gemini-4-neon",
        hf_repo_id="gemini://v1beta/gemini-4-neon",
        layer=3,
        role="gemini_neon_agentic",
        license_id="Google-Gemini-API",
        commercial_safe=True,
        total_params_b=0.0,
        active_params_b=0.0,
        context_tokens=1048576,
        quant_format="managed",
        footprint_gb=0.0,
        target_hardware="google-gemini-api-public-sanitized",
        runtime_engine="gemini_cloud_client",
        notes="Layer 3B verified v1beta adaptive-effort Neon model with Flex/Batch & caching (public_sanitized only).",
    ),
    "gemini-4-argon": ArmyModelSpec(
        alias="gemini-4-argon",
        hf_repo_id="gemini://v1beta/gemini-4-argon",
        layer=3,
        role="gemini_balanced_reasoner",
        license_id="Google-Gemini-API",
        commercial_safe=True,
        total_params_b=0.0,
        active_params_b=0.0,
        context_tokens=1048576,
        quant_format="managed",
        footprint_gb=0.0,
        target_hardware="google-gemini-api-public-sanitized",
        runtime_engine="gemini_cloud_client",
        notes="Layer 3B balanced frontier reasoning model with Flex/Batch & caching (public_sanitized only).",
    ),
    "gemini-4-hydrogen": ArmyModelSpec(
        alias="gemini-4-hydrogen",
        hf_repo_id="gemini://v1beta/gemini-4-hydrogen",
        layer=3,
        role="gemini_flex_worker",
        license_id="Google-Gemini-API",
        commercial_safe=True,
        total_params_b=0.0,
        active_params_b=0.0,
        context_tokens=1048576,
        quant_format="managed",
        footprint_gb=0.0,
        target_hardware="google-gemini-api-public-sanitized",
        runtime_engine="gemini_cloud_client",
        notes="Layer 3B default Gemini 4 coding/agentic worker with Flex tier & caching (public_sanitized only).",
    ),
    "gemini-4-carbon": ArmyModelSpec(
        alias="gemini-4-carbon",
        hf_repo_id="gemini://v1beta/gemini-4-carbon",
        layer=3,
        role="gemini_frontier_judge",
        license_id="Google-Gemini-API",
        commercial_safe=True,
        total_params_b=0.0,
        active_params_b=0.0,
        context_tokens=1048576,
        quant_format="managed",
        footprint_gb=0.0,
        target_hardware="google-gemini-api-public-sanitized",
        runtime_engine="gemini_cloud_client",
        notes="Layer 3B frontier 1M reasoning judge with Flex/Batch & thinking (public_sanitized only).",
    ),
    "gemini-3.5-flash-lite": ArmyModelSpec(
        alias="gemini-3.5-flash-lite",
        hf_repo_id="gemini://v1beta/gemini-3.5-flash-lite",
        layer=3,
        role="gemini_lite_flex",
        license_id="Google-Gemini-API",
        commercial_safe=True,
        total_params_b=0.0,
        active_params_b=0.0,
        context_tokens=1048576,
        quant_format="managed",
        footprint_gb=0.0,
        target_hardware="google-gemini-api-public-sanitized",
        runtime_engine="gemini_cloud_client",
        notes="Layer 3B low-latency Flash-Lite worker with Flex tier & batch (public_sanitized only).",
    ),
    "gemini-3.1-flash-lite": ArmyModelSpec(
        alias="gemini-3.1-flash-lite",
        hf_repo_id="gemini://v1beta/gemini-3.1-flash-lite",
        layer=3,
        role="gemini_ultra_lite",
        license_id="Google-Gemini-API",
        commercial_safe=True,
        total_params_b=0.0,
        active_params_b=0.0,
        context_tokens=1048576,
        quant_format="managed",
        footprint_gb=0.0,
        target_hardware="google-gemini-api-public-sanitized",
        runtime_engine="gemini_cloud_client",
        notes="Layer 3B ultra-economical high-volume Flash-Lite (public_sanitized only).",
    ),
    "gemma-4-26b-a4b-it": ArmyModelSpec(
        alias="gemma-4-26b-a4b-it",
        hf_repo_id="google/gemma-4-26B-A4B-it",
        layer=3,
        role="gemini_open_weights_moe",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=25.2,
        active_params_b=3.8,
        context_tokens=256144,
        quant_format="managed",
        footprint_gb=0.0,
        target_hardware="google-gemini-api-public-sanitized",
        runtime_engine="gemini_cloud_client",
        notes="Layer 3B Gemma 4 26B-A4B open-weights MoE served via Gemini API (public_sanitized only).",
    ),
    # Layer 4: Ephemeral Spot GPU Burst (g4dn.12xlarge / 16x g4dn.xlarge RPC eu-west-1 / g6 & g6e eu-central-1)
    "qwen3-32b-awq": ArmyModelSpec(
        alias="qwen3-32b-awq",
        hf_repo_id="Qwen/Qwen3-32B-AWQ",
        layer=4,
        role="gpu_review_lora_base",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=32.0,
        active_params_b=32.0,
        context_tokens=131072,
        quant_format="AWQ-Int4",
        footprint_gb=19.0,
        target_hardware="g4dn.12xlarge",
        runtime_engine="vllm-runai-streamer",
        requires_sm89=False,
        notes="100% T4 SM75 safe (--dtype float16 --kv-cache-dtype float16 --max-lora-rank 16).",
    ),
    "deepseek-r1-32b-awq": ArmyModelSpec(
        alias="deepseek-r1-32b-awq",
        hf_repo_id="deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
        layer=4,
        role="gpu_rca_reasoner",
        license_id="MIT",
        commercial_safe=True,
        total_params_b=32.0,
        active_params_b=32.0,
        context_tokens=131072,
        quant_format="AWQ-Int4",
        footprint_gb=19.0,
        target_hardware="g4dn.12xlarge",
        runtime_engine="vllm-runai-streamer",
        requires_sm89=False,
        notes="100% T4 SM75 safe paired with qwen3-32b-awq (~38 GB / 64 GB VRAM).",
    ),
    "nemotron-super-49b": ArmyModelSpec(
        alias="nemotron-super-49b",
        hf_repo_id="nvidia/Llama-3.3-Nemotron-Super-49B-v1.5",
        layer=4,
        role="gpu_frontier_analyst",
        license_id="NVIDIA-Open-Model",
        commercial_safe=True,
        total_params_b=49.0,
        active_params_b=49.0,
        context_tokens=131072,
        quant_format="AWQ-Int4",
        footprint_gb=28.0,
        target_hardware="g4dn.12xlarge",
        runtime_engine="vllm-runai-streamer",
        requires_sm89=False,
        notes="NVIDIA 49B pruned & distilled frontier reasoner competitive with 70B models.",
    ),
    "qwen3-coder-next": ArmyModelSpec(
        alias="qwen3-coder-next",
        hf_repo_id="Qwen/Qwen3-Coder-Next",
        layer=4,
        role="gpu_agentic_coder",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=80.0,
        active_params_b=3.0,
        context_tokens=262144,
        quant_format="AWQ-Int4",
        footprint_gb=44.0,
        target_hardware="g6.12xlarge",
        runtime_engine="vllm-runai-streamer",
        requires_sm89=True,
        notes="Requires SM89 L4/L40S for stable DeltaNet BF16/FP8; validate SM75 before T4 prod.",
    ),
    "qwen3.5-122b-a10b": ArmyModelSpec(
        alias="qwen3.5-122b-a10b",
        hf_repo_id="Qwen/Qwen3.5-122B-A10B",
        layer=4,
        role="gpu_arch_critic",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=122.0,
        active_params_b=10.0,
        context_tokens=262144,
        quant_format="AWQ-Int4",
        footprint_gb=62.0,
        target_hardware="g6.12xlarge",
        runtime_engine="sglang-radix-attention",
        requires_sm89=True,
        notes="Fits 4x L4 96 GB (eu-central-1) with ~34 GB KV cache headroom.",
    ),
    "glm-4.5-air": ArmyModelSpec(
        alias="glm-4.5-air",
        hf_repo_id="zai-org/GLM-4.5-Air",
        layer=4,
        role="gpu_mit_rca",
        license_id="MIT",
        commercial_safe=True,
        total_params_b=106.0,
        active_params_b=12.0,
        context_tokens=131072,
        quant_format="AWQ-Int4",
        footprint_gb=58.0,
        target_hardware="g6.12xlarge",
        runtime_engine="vllm-runai-streamer",
        requires_sm89=True,
    ),
    "gpt-oss-120b": ArmyModelSpec(
        alias="gpt-oss-120b",
        hf_repo_id="openai/gpt-oss-120b",
        layer=4,
        role="gpu_cot_critic",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=117.0,
        active_params_b=5.1,
        context_tokens=131072,
        quant_format="MXFP4",
        footprint_gb=62.0,
        target_hardware="g6.12xlarge",
        runtime_engine="vllm-runai-streamer",
        requires_sm89=True,
        requires_mxfp4=True,
        notes="Incompatible with Turing T4 SM75 (MXFP4 requires Ada SM89+).",
    ),
    "qwen3-235b-a22b-awq": ArmyModelSpec(
        alias="qwen3-235b-a22b-awq",
        hf_repo_id="Qwen/Qwen3-235B-A22B-Instruct-2507",
        layer=4,
        role="gpu_flagship_reviewer",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=235.0,
        active_params_b=22.0,
        context_tokens=262144,
        quant_format="AWQ-Int4",
        footprint_gb=117.0,
        target_hardware="g6e.12xlarge",
        runtime_engine="sglang-radix-attention",
        requires_sm89=True,
        notes="Fits 4x L40S 192 GB only at 4-bit (~117 GB); FP8 (~235 GB) exceeds 192 GB.",
    ),
    "olmocr-2-7b": ArmyModelSpec(
        alias="olmocr-2-7b",
        hf_repo_id="allenai/olmOCR-2-7B-1025",
        layer=4,
        role="gpu_doc_ocr",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=7.0,
        active_params_b=7.0,
        context_tokens=32768,
        quant_format="AWQ-Int4",
        footprint_gb=4.8,
        target_hardware="g4dn.xlarge",
        runtime_engine="vllm-runai-streamer",
        requires_sm89=False,
        notes="Lever B batch PDF/table/diagram OCR on SM75 T4.",
    ),
    "qwen3-235b-a22b-rpc": ArmyModelSpec(
        alias="qwen3-235b-a22b-rpc",
        hf_repo_id="Qwen/Qwen3-235B-A22B-Instruct-2507-GGUF",
        layer=4,
        role="distributed_256gb_rpc",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=235.0,
        active_params_b=22.0,
        context_tokens=262144,
        quant_format="Q4_K_M",
        footprint_gb=134.0,
        target_hardware="16x g4dn.xlarge",
        runtime_engine="llama_cpp_rpc",
        requires_sm89=False,
        notes="Lever C distributed_256gb (16x g4dn.xlarge = 64 vCPU L-3819A6DF, 256 GB SM75 float16 VRAM, kaizen-dist256-cpg).",
    ),
    # Layer 5: Night Batch Frontier (r7g.16xlarge / r7i.24xlarge / 16x g4dn.xlarge RPC / g6e.12xlarge)
    "qwen3.5-397b-a17b": ArmyModelSpec(
        alias="qwen3.5-397b-a17b",
        hf_repo_id="Qwen/Qwen3.5-397B-A17B",
        layer=5,
        role="night_frontier_sweeper",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=397.0,
        active_params_b=17.0,
        context_tokens=262144,
        quant_format="UD-Q2_K_XL",
        footprint_gb=139.0,
        target_hardware="16x g4dn.xlarge",
        runtime_engine="ik_llama.cpp",
        requires_sm89=False,
        notes="UD-Q2_K_XL fits 256 GB 16x g4dn.xlarge RPC, 192 GB L40S, or 512 GB r7g.16xlarge.",
    ),
    "qwen3-coder-480b": ArmyModelSpec(
        alias="qwen3-coder-480b",
        hf_repo_id="unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF",
        layer=5,
        role="night_coder_sweep",
        license_id="Apache-2.0",
        commercial_safe=True,
        total_params_b=480.0,
        active_params_b=35.0,
        context_tokens=262144,
        quant_format="UD-Q2_K_XL",
        footprint_gb=180.0,
        target_hardware="r7g.16xlarge",
        runtime_engine="ik_llama.cpp",
        notes="Use ik_llama.cpp on r7g.16xlarge (KTransformers incompatible with ARM64).",
    ),
    "deepseek-v3.2": ArmyModelSpec(
        alias="deepseek-v3.2",
        hf_repo_id="unsloth/DeepSeek-V3.2-GGUF",
        layer=5,
        role="night_deep_rca",
        license_id="MIT",
        commercial_safe=True,
        total_params_b=671.0,
        active_params_b=37.0,
        context_tokens=163840,
        quant_format="UD-Q2_K_XL",
        footprint_gb=247.0,
        target_hardware="r7g.16xlarge",
        runtime_engine="ik_llama.cpp",
    ),
    "glm-5": ArmyModelSpec(
        alias="glm-5",
        hf_repo_id="unsloth/GLM-5-GGUF",
        layer=5,
        role="night_reasoning_sweep",
        license_id="MIT",
        commercial_safe=True,
        total_params_b=744.0,
        active_params_b=40.0,
        context_tokens=200000,
        quant_format="UD-Q2_K_XL",
        footprint_gb=281.0,
        target_hardware="r7g.16xlarge",
        runtime_engine="ik_llama.cpp",
    ),
    "kimi-k2.5": ArmyModelSpec(
        alias="kimi-k2.5",
        hf_repo_id="unsloth/Kimi-K2.5-GGUF",
        layer=5,
        role="night_frontier_council",
        license_id="Modified-MIT",
        commercial_safe=True,
        total_params_b=1000.0,
        active_params_b=32.0,
        context_tokens=262144,
        quant_format="UD-Q2_K_XL",
        footprint_gb=375.0,
        target_hardware="r7g.16xlarge",
        runtime_engine="ik_llama.cpp",
        notes="Modified MIT: review >$20M/yr MaaS clause before external serving.",
    ),
}

BLOCKED_NON_COMMERCIAL_MODELS: Dict[str, str] = {
    "Salesforce/xLAM-2-1b-fc-r": "CC-BY-NC-4.0 (Non-Commercial only)",
    "Salesforce/xLAM-2-3b-fc-r": "CC-BY-NC-4.0 (Non-Commercial only)",
    "jinaai/jina-code-embeddings-0.5b": "CC-BY-NC-4.0 (Non-Commercial only)",
    "jinaai/jina-code-embeddings-1.5b": "CC-BY-NC-4.0 (Non-Commercial only)",
    "LiquidAI/LFM2.5-1.2B-Instruct": "LFM Open License v1.0 (<$10M revenue cap)",
    "LiquidAI/LFM2-2.6B": "LFM Open License v1.0 (<$10M revenue cap)",
    "MiniMaxAI/MiniMax-M2.7": "Non-Commercial license",
    "katanemo/Arch-Router-1.5B": "Restrictive katanemo-research license",
}


def validate_commercial_license(hf_repo_id: str) -> Dict[str, Any]:
    """Fail closed on non-commercial or revenue-restricted Hugging Face repos."""
    cleaned = (hf_repo_id or "").strip()
    low = cleaned.lower()
    for blocked_id, reason in BLOCKED_NON_COMMERCIAL_MODELS.items():
        if low == blocked_id.lower() or (blocked_id.endswith("-1b-fc-r") and "salesforce/xlam-2" in low):
            return {"allowed": False, "hf_repo_id": cleaned, "reason": reason}
    for prefix, reason in (
        ("salesforce/xlam-2", "CC-BY-NC-4.0 (Non-Commercial only)"),
        ("jinaai/jina-code-embeddings", "CC-BY-NC-4.0 (Non-Commercial only)"),
        ("liquidai/lfm2", "LFM Open License v1.0 (<$10M revenue cap)"),
    ):
        if low.startswith(prefix):
            return {"allowed": False, "hf_repo_id": cleaned, "reason": reason}
    for spec in ARMY_MODELS.values():
        if spec.hf_repo_id.lower() == low:
            return {
                "allowed": spec.commercial_safe,
                "hf_repo_id": cleaned,
                "license_id": spec.license_id,
                "reason": spec.notes or "Verified commercial-safe license",
            }
    return {
        "allowed": False,
        "hf_repo_id": cleaned,
        "reason": "Unverified model ID; add verified HF card & license to ARMY_MODELS first",
    }


def validate_deployment_fit(
    model_alias: str,
    instance_type: str,
    use_ktransformers: bool = False,
    use_cpu_draft_speculative: bool = False,
    nodes: int = 1,
    distributed_rpc: bool = False,
) -> Dict[str, Any]:
    """Verify hardware VRAM/RAM fit, SM75 vs SM89 tensor support, 64vCPU quota, and DE-005/DE-006."""
    normalized_alias = "granite-4-h-tiny" if model_alias == "granite-4.0-h-tiny" else model_alias
    if normalized_alias not in ARMY_MODELS:
        return {"ok": False, "reasons": [f"Unknown army model alias '{model_alias}'"]}
    if int(nodes) < 1:
        return {"ok": False, "model": normalized_alias, "instance_type": instance_type, "reasons": ["nodes must be >= 1"]}
    node_count = int(nodes)
    spec = ARMY_MODELS[normalized_alias]
    reasons: List[str] = []

    if node_count > 1 and not distributed_rpc:
        reasons.append(
            "DE-005 violation: multi-node deployment requires distributed_rpc=True "
            "(llama_cpp_rpc / vllm_ray_pp); AirLLM disk streaming is forbidden"
        )

    if use_cpu_draft_speculative and instance_type.startswith("m7g"):
        reasons.append("DE-006 violation: small-draft speculative decoding is slower than 3B-active MoE CPU decode")

    if instance_type == "m7g.8xlarge":
        if node_count > 1:
            reasons.append("m7g.8xlarge Cloud-Brain is a single-node resident host (nodes must be 1)")
        if spec.footprint_gb > M7G_SAFE_WEIGHT_CEILING_GIB:
            reasons.append(
                f"DE-005 violation: {spec.footprint_gb} GB exceeds {M7G_SAFE_WEIGHT_CEILING_GIB} GiB safe weight ceiling on m7g.8xlarge"
            )
        if use_ktransformers:
            reasons.append("KTransformers requires x86_64+CUDA; incompatible with ARM64 m7g.8xlarge")
        if spec.requires_mxfp4:
            reasons.append("MXFP4 requires Ada SM89+ GPU, not m7g.8xlarge CPU")
        return {
            "ok": not reasons,
            "model": normalized_alias,
            "instance_type": instance_type,
            "nodes": node_count,
            "distributed_rpc": distributed_rpc,
            "reasons": reasons,
        }

    if instance_type.startswith("aws-bedrock"):
        return {
            "ok": spec.layer == 3 and not reasons,
            "model": normalized_alias,
            "instance_type": instance_type,
            "nodes": node_count,
            "distributed_rpc": distributed_rpc,
            "reasons": reasons,
        }

    hw = gpu_burst_orchestrator.INSTANCE_SPECS.get(instance_type)
    if hw is None:
        return {"ok": False, "model": normalized_alias, "instance_type": instance_type, "reasons": [f"Unknown instance '{instance_type}'"]}

    quota = gpu_burst_orchestrator.check_g_spot_quota([instance_type], nodes=node_count, sq_client=None)
    if not quota["passed"]:
        reasons.append(quota["diagnostic"])

    arch = gpu_burst_orchestrator.get_instance_gpu_arch(instance_type)
    if use_ktransformers and not arch["supports_ktransformers"]:
        reasons.append(f"KTransformers requires x86_64+CUDA; incompatible with {instance_type} ({hw.architecture})")

    if hw.vram_gb > 0:
        if arch["compute_cap"] == "7.5" and (spec.requires_sm89 or spec.requires_mxfp4):
            reasons.append(f"Turing T4 SM75 ({instance_type}) lacks BF16/FP8/MXFP4 required by {normalized_alias}")
        effective_vram_gb = hw.vram_gb * (node_count if distributed_rpc else 1)
        max_vram_weights = effective_vram_gb * 0.85
        if distributed_rpc:
            if spec.footprint_gb > max_vram_weights:
                reasons.append(
                    f"DE-005 risk: {spec.footprint_gb} GB exceeds 85% of {effective_vram_gb} GB distributed RPC VRAM "
                    f"({node_count}x {instance_type})"
                )
        elif spec.footprint_gb > max_vram_weights and hw.ram_gib < spec.footprint_gb * 1.15:
            reasons.append(f"DE-005 risk: {spec.footprint_gb} GB exceeds {instance_type} ({hw.vram_gb} GB VRAM)")
    else:
        effective_ram_gib = hw.ram_gib * (node_count if distributed_rpc else 1)
        if spec.footprint_gb > effective_ram_gib * 0.86:
            reasons.append(f"DE-005 violation: {spec.footprint_gb} GB exceeds 86% of {effective_ram_gib} GiB RAM on {instance_type}")

    return {
        "ok": not reasons,
        "model": normalized_alias,
        "instance_type": instance_type,
        "nodes": node_count,
        "distributed_rpc": distributed_rpc,
        "reasons": reasons,
    }


@dataclass(frozen=True)
class CascadeDecision:
    """RouteLLM + ETH Cascade Routing execution plan."""

    task_type: str
    estimated_complexity: str
    initial_model: str
    escalation_chain: Tuple[str, ...]
    target_hardware: str
    rationale: str
    data_classification: str = "corporate_proprietary"
    decision_gate_model: Optional[str] = None
    service_tier: Optional[str] = None
    thinking_level: Optional[str] = None


@dataclass(frozen=True)
class ExecutionGateSpec:
    """Deterministic execution, JSON-schema, AST, and NLI verification contract for CascadeRouter."""

    json_schema: Optional[Dict[str, Any]] = None
    ast_lang: Optional[str] = None
    verifier_fn: Optional[Callable[[str], Any]] = None
    verify_cmd: str = ""
    entailment_premise: Optional[str] = None
    strict: bool = True


TALE_TOKEN_BUDGET_POLICY: Dict[str, Dict[str, Any]] = {
    "triage_fast": {
        "tasks": ("classify", "triage", "file_selection", "poll_prescreen", "intent", "route"),
        "recommended_public_model": "gemini-decision-1",
        "recommended_corporate_model": "qwen3.5-4b",
        "thinking_level": "MINIMAL",
        "thinking_budget": 0,
        "max_output_tokens": 256,
        "decision_zero_output_tokens": True,
    },
    "low": {
        "recommended_public_model": "gemini-3.5-flash-lite",
        "recommended_corporate_model": "qwen3.5-4b",
        "thinking_level": "LOW",
        "thinking_budget": 256,
        "max_output_tokens": 512,
        "decision_zero_output_tokens": False,
    },
    "medium": {
        "recommended_public_model": "gemini-4-hydrogen",
        "recommended_corporate_model": "qwen3-coder-30b-a3b",
        "thinking_level": "MEDIUM",
        "thinking_budget": 1024,
        "max_output_tokens": 1536,
        "decision_zero_output_tokens": False,
    },
    "high": {
        "recommended_public_model": "gemini-4-neon",
        "recommended_corporate_model": "qwen3.5-35b-a3b",
        "thinking_level": "HIGH",
        "thinking_budget": 4096,
        "max_output_tokens": 4096,
        "decision_zero_output_tokens": False,
    },
}

_SIGNAL_LINE_RE = re.compile(
    r"(?:\b(?:error|fail|failed|exception|traceback|warning|critical|panic|assert)\b|^\s*(?:def |class |fn |pub |@@|\+|\-))",
    re.IGNORECASE,
)


def compute_tale_budget(
    task_type: str,
    complexity: str,
    prompt_tokens: int = 0,
) -> Dict[str, Any]:
    """Compute TALE (Token-Budget-Aware LLM Reasoning) output cap, thinking level, and model lock."""
    norm_task = (task_type or "code_write").strip().lower()
    norm_comp = (complexity or "medium").strip().lower()
    if norm_comp not in ("low", "medium", "high"):
        norm_comp = "medium"

    triage_tasks = TALE_TOKEN_BUDGET_POLICY["triage_fast"]["tasks"]
    if norm_task in triage_tasks:
        tier_key = "triage_fast"
        entry = TALE_TOKEN_BUDGET_POLICY["triage_fast"]
    else:
        tier_key = norm_comp
        entry = TALE_TOKEN_BUDGET_POLICY[norm_comp]

    p_tokens = max(0, int(prompt_tokens))
    max_out = int(entry["max_output_tokens"])
    lvl = str(entry["thinking_level"])
    return {
        "policy_tier": tier_key,
        "task_type": norm_task,
        "complexity": norm_comp,
        "prompt_tokens": p_tokens,
        "thinking_level": lvl,
        "thinking_budget": int(entry["thinking_budget"]),
        "max_output_tokens": max_out,
        "decision_zero_output_tokens": bool(entry["decision_zero_output_tokens"]),
        "recommended_public_model": str(entry["recommended_public_model"]),
        "recommended_corporate_model": str(entry["recommended_corporate_model"]),
        "implicit_cache_eligible": p_tokens >= gemini_cloud_client.GEMINI_4_IMPLICIT_CACHE_MIN_TOKENS,
        "tale_instruction": f"Be concise; stay within <= {max_out} output tokens (effort={lvl}).",
    }


def compress_dynamic_context_capc(
    static_prefix: str,
    dynamic_context: str,
    *,
    max_dynamic_chars: int = 12000,
) -> Dict[str, Any]:
    """CAPC dynamic-only context pruner: preserves `static_prefix` byte-for-byte; compresses ONLY `dynamic_context`."""
    prefix = static_prefix if static_prefix is not None else ""
    dyn = dynamic_context if dynamic_context is not None else ""
    budget = max(64, int(max_dynamic_chars))
    orig_len = len(dyn)

    if orig_len <= budget:
        ordered = gemini_cloud_client.build_capc_ordered_prompt(prefix, dyn)
        return {
            "static_prefix": prefix,
            "dynamic_context": dyn,
            "combined_prompt": ordered["prompt"],
            "prefix_preserved": True,
            "was_compressed": False,
            "original_dynamic_chars": orig_len,
            "compressed_dynamic_chars": orig_len,
            "chars_saved": 0,
            "tokens_saved_estimate": 0,
            "implicit_cache_eligible": ordered["implicit_cache_eligible"],
        }

    lines = dyn.splitlines()
    head_budget = int(budget * 0.40)
    tail_budget = int(budget * 0.40)
    mid_budget = max(0, budget - head_budget - tail_budget - 48)

    head_chunk = dyn[:head_budget]
    tail_chunk = dyn[-tail_budget:] if tail_budget > 0 else ""

    signal_lines: List[str] = []
    used_mid = 0
    for line in lines:
        stripped = line.strip()
        if stripped and _SIGNAL_LINE_RE.search(stripped):
            if used_mid + len(stripped) + 1 > mid_budget:
                break
            signal_lines.append(stripped)
            used_mid += len(stripped) + 1

    mid_section = "\n".join(signal_lines)
    marker = f"\n...[CAPC pruned {orig_len - budget} volatile chars]...\n"
    if mid_section:
        candidate = f"{head_chunk}\n{mid_section}{marker}{tail_chunk}"
    else:
        candidate = f"{head_chunk}{marker}{tail_chunk}"

    pruned = candidate[:budget]
    saved_chars = max(0, orig_len - len(pruned))
    ordered = gemini_cloud_client.build_capc_ordered_prompt(prefix, pruned)
    return {
        "static_prefix": prefix,
        "dynamic_context": pruned,
        "combined_prompt": ordered["prompt"],
        "prefix_preserved": True,
        "was_compressed": True,
        "original_dynamic_chars": orig_len,
        "compressed_dynamic_chars": len(pruned),
        "chars_saved": saved_chars,
        "tokens_saved_estimate": saved_chars // 4,
        "implicit_cache_eligible": ordered["implicit_cache_eligible"],
    }


_TASK_CRITERIA: Dict[str, str] = {
    "code_write": "Generate, refactor, or modify source code",
    "code_review": "Review code, audit architecture, security, concurrency, or bugs",
    "classify": "Classify, label, or route an item",
    "autodream": "Summarize or consolidate notes or memory",
}


def _extract_first_answer(resp: Dict[str, Any], preferred_key: str) -> Dict[str, Any]:
    answers = resp.get("answers")
    if not isinstance(answers, Mapping) or not answers:
        return {}
    if preferred_key in answers and isinstance(answers[preferred_key], Mapping):
        return dict(answers[preferred_key])
    first = next(iter(answers.values()))
    return dict(first) if isinstance(first, Mapping) else {}


def _calibrate_with_decision_1(
    prompt: str,
    resolved_task: str,
    complexity: str,
    requested_task_type: str,
    data_classification: str,
    gemini_client: Optional[gemini_cloud_client.GeminiCloudClient] = None,
) -> Tuple[str, str, str, str]:
    client = gemini_client or gemini_cloud_client.GeminiCloudClient()
    if gemini_client is None and not client.has_api_key():
        return resolved_task, complexity, "local-heuristic", " (Decision-1 offline fallback: API key absent)"
    try:
        choice_resp = client.decide_choice(
            prompt,
            "Select the primary engineering task category.",
            _TASK_CRITERIA,
            question_id="task_type",
            data_classification=data_classification,
        )
        noul_resp = client.decide_noul(
            prompt,
            "Does this task require frontier-level deep reasoning or architectural critique?",
            true_description="High complexity or frontier reasoning required",
            false_description="Standard engineering or lightweight task",
            question_id="frontier_needed",
            data_classification=data_classification,
        )
        choice_entry = _extract_first_answer(choice_resp, "task_type")
        chosen = str(choice_entry.get("choice") or "").strip().lower()
        if requested_task_type in ("", "auto") and chosen in _TASK_CRITERIA:
            resolved_task = chosen
        noul_entry = _extract_first_answer(noul_resp, "frontier_needed")
        noul_score = noul_entry.get("noul")
        if noul_score is not None and float(noul_score) >= 0.5:
            complexity = "high"
        return resolved_task, complexity, "gemini-decision-1", " (calibrated by gemini-decision-1, 0 output tokens)"
    except PermissionError:
        raise
    except (RuntimeError, OSError, ValueError, KeyError, TypeError):
        return resolved_task, complexity, "local-heuristic", " (Decision-1 offline fallback)"


def _build_gemini_flex_decision(
    resolved_task: str,
    complexity: str,
    gate_model: Optional[str],
    gate_note: str,
) -> CascadeDecision:
    if resolved_task in ("classify", "autodream"):
        initial = "gemini-3.5-flash-lite"
        chain: Tuple[str, ...] = ("gemini-4-hydrogen", "gemini-4-neon", "gemini-4-carbon")
        thinking_level = "MINIMAL"
    elif resolved_task == "code_review" or complexity == "high":
        initial = "gemini-4-hydrogen"
        chain = ("gemini-4-carbon", "gemini-4-neon")
        thinking_level = "HIGH" if complexity == "high" else "MEDIUM"
    elif complexity == "low":
        initial = "gemini-3.5-flash-lite"
        chain = ("gemini-4-hydrogen", "gemini-4-neon", "gemini-4-carbon")
        thinking_level = "LOW"
    else:
        initial = "gemini-4-hydrogen"
        chain = ("gemini-4-neon", "gemini-4-carbon")
        thinking_level = "MEDIUM"
    return CascadeDecision(
        task_type=resolved_task,
        estimated_complexity=complexity,
        initial_model=initial,
        escalation_chain=chain,
        target_hardware="google-gemini-api-public-sanitized",
        rationale=f"Public-sanitized Gemini Flex cascade (`serviceTier=flex`, `thinkingLevel={thinking_level}`){gate_note}.",
        data_classification="public_sanitized",
        decision_gate_model=gate_model,
        service_tier="flex",
        thinking_level=thinking_level,
    )


def _build_corporate_decision(
    resolved_task: str,
    complexity: str,
    allow_gpu_burst: bool,
    prefer_oss: bool,
    data_classification: str = "corporate_proprietary",
    gate_model: Optional[str] = None,
    gate_note: str = "",
) -> CascadeDecision:
    if resolved_task == "classify":
        return CascadeDecision(
            task_type=resolved_task,
            estimated_complexity=complexity,
            initial_model="granite-4-h-tiny",
            escalation_chain=("qwen3.5-4b", "claude-haiku-5-5"),
            target_hardware="m7g.8xlarge",
            rationale=f"RouteLLM Layer-0 1B-active MoE classifier with Qwen3.5-4B / Haiku fallback{gate_note}.",
            data_classification=data_classification,
            decision_gate_model=gate_model,
        )

    if resolved_task == "autodream":
        return CascadeDecision(
            task_type=resolved_task,
            estimated_complexity=complexity,
            initial_model="ling-mini-2.0",
            escalation_chain=("gemma-4-26b-a4b", "claude-haiku-5-5"),
            target_hardware="m7g.8xlarge",
            rationale=f"Fastest 1.4B-active CPU MoE consolidation before Gemma-4 / Bedrock Haiku{gate_note}.",
            data_classification=data_classification,
            decision_gate_model=gate_model,
        )

    if resolved_task == "code_review":
        if allow_gpu_burst:
            return CascadeDecision(
                task_type=resolved_task,
                estimated_complexity=complexity,
                initial_model="qwen3-32b-awq",
                escalation_chain=("deepseek-r1-32b-awq", "claude-opus-5-5"),
                target_hardware="g4dn.12xlarge",
                rationale=f"Ephemeral T4 SM75 AWQ reviewer escalating to DeepSeek-R1-32B / Bedrock Opus 5.5{gate_note}.",
                data_classification=data_classification,
                decision_gate_model=gate_model,
            )
        initial = "qwen3.5-35b-a3b" if prefer_oss else "claude-sonnet-5-5"
        chain = ("gemma-4-26b-a4b", "claude-opus-5-5") if prefer_oss else ("claude-opus-5-5",)
        return CascadeDecision(
            task_type=resolved_task,
            estimated_complexity=complexity,
            initial_model=initial,
            escalation_chain=chain,
            target_hardware="m7g.8xlarge -> aws-bedrock",
            rationale=f"Cascade Routing: Layer-2 3B-active MoE reviewer with diverse Gemma-4 & Opus 5.5 judge escalation{gate_note}.",
            data_classification=data_classification,
            decision_gate_model=gate_model,
        )

    if complexity == "high" and not prefer_oss:
        return CascadeDecision(
            task_type=resolved_task,
            estimated_complexity=complexity,
            initial_model="claude-sonnet-5-5",
            escalation_chain=("claude-opus-5-5",),
            target_hardware="aws-bedrock",
            rationale=f"High-complexity daytime synthesis directly on Bedrock Claude Sonnet 5.5{gate_note}.",
            data_classification=data_classification,
            decision_gate_model=gate_model,
        )
    return CascadeDecision(
        task_type=resolved_task,
        estimated_complexity=complexity,
        initial_model="qwen3-coder-30b-a3b",
        escalation_chain=("qwen3.5-35b-a3b", "claude-sonnet-5-5"),
        target_hardware="m7g.8xlarge -> aws-bedrock",
        rationale=f"Primary 3.3B-active CPU coder escalating to Qwen3.5-35B-A3B and Bedrock Sonnet 5.5{gate_note}.",
        data_classification=data_classification,
        decision_gate_model=gate_model,
    )


def _classify_task_and_complexity(prompt: str, task_type: str, context_tokens: int) -> Tuple[str, str]:
    low = (prompt or "").lower()
    resolved_task = task_type.strip().lower() if task_type and task_type != "auto" else ""
    if not resolved_task:
        if any(k in low for k in ("review", "audit", "rca", "root cause", "architecture", "security", "parity")):
            resolved_task = "code_review"
        elif any(k in low for k in ("autodream", "consolidat", "memory", "summarize")):
            resolved_task = "autodream"
        elif any(k in low for k in ("classify", "route", "intent", "guardrail", "label")):
            resolved_task = "classify"
        else:
            resolved_task = "code_write"

    hard_signals = ("distributed", "concurrency", "race", "deadlock", "security", "cve", "migration", "architecture", "cross-repo", "ktransformers")
    is_high = context_tokens > 32000 or len(prompt) > 2500 or any(sig in low for sig in hard_signals)
    complexity = "high" if is_high else ("medium" if len(prompt) > 400 or resolved_task == "code_review" else "low")
    return resolved_task, complexity


_FENCED_BLOCK_RE = re.compile(r"```(?:[a-zA-Z0-9_+-]+)?\s*\n(.*?)```", re.DOTALL)
_ALLOWED_VERIFY_BINARIES = frozenset({"python", "python3", "pytest", "cargo", "mvn", "git"})
_FORBIDDEN_CMD_CHARS = frozenset({";", "&", "|", ">", "<", "`", "$", "\n", "\r"})


def _extract_code_or_raw(text: str) -> str:
    match = _FENCED_BLOCK_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _validate_json_schema_node(value: Any, schema: Mapping[str, Any], path: str = "$") -> List[str]:
    errors: List[str] = []
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object, got {type(value).__name__}"]
        for req_key in schema.get("required") or ():
            if req_key not in value:
                errors.append(f"{path}: missing required key {req_key!r}")
        props = schema.get("properties")
        if isinstance(props, Mapping):
            for prop_key, prop_schema in props.items():
                if prop_key in value and isinstance(prop_schema, Mapping):
                    errors.extend(_validate_json_schema_node(value[prop_key], prop_schema, f"{path}.{prop_key}"))
    elif expected_type == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array, got {type(value).__name__}"]
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for idx, item in enumerate(value):
                errors.extend(_validate_json_schema_node(item, item_schema, f"{path}[{idx}]"))
    elif expected_type == "string" and not isinstance(value, str):
        errors.append(f"{path}: expected string, got {type(value).__name__}")
    elif expected_type == "boolean" and not isinstance(value, bool):
        errors.append(f"{path}: expected boolean, got {type(value).__name__}")
    elif expected_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        errors.append(f"{path}: expected integer, got {type(value).__name__}")
    elif expected_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        errors.append(f"{path}: expected number, got {type(value).__name__}")

    enum_vals = schema.get("enum")
    if isinstance(enum_vals, (list, tuple)) and value not in enum_vals:
        errors.append(f"{path}: {value!r} not in enum {list(enum_vals)!r}")
    return errors


def _check_balanced_delimiters(snippet: str) -> bool:
    stack: List[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    openers = frozenset(pairs.values())
    in_str: Optional[str] = None
    escaped = False
    for ch in snippet:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_str is not None:
            if ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'"):
            in_str = ch
            continue
        if ch in openers:
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack[-1] != pairs[ch]:
                return False
            stack.pop()
    return not stack and in_str is None


def _run_allowlisted_verify_cmd(verify_cmd: str) -> Tuple[bool, str]:
    cleaned = verify_cmd.strip()
    if not cleaned:
        return True, ""
    if any(ch in cleaned for ch in _FORBIDDEN_CMD_CHARS):
        return False, "verify_cmd contains disallowed shell metacharacters"
    try:
        argv = shlex.split(cleaned, posix=False)
    except ValueError as exc:
        return False, f"invalid verify_cmd syntax: {exc}"
    if not argv:
        return True, ""
    exe_base = argv[0].strip('"\'').replace("\\", "/").rsplit("/", 1)[-1].lower()
    if exe_base.endswith(".exe"):
        exe_base = exe_base[:-4]
    if exe_base not in _ALLOWED_VERIFY_BINARIES:
        return False, f"disallowed verify_cmd binary {exe_base!r}"
    cleaned_argv = [tok.strip('"\'') for tok in argv]
    try:
        proc = subprocess.run(
            cleaned_argv,
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"verify_cmd execution error: {exc}"
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or proc.stdout or "").strip()[:200]
        return False, f"verify_cmd exited {proc.returncode}: {stderr_tail}"
    return True, ""


def _check_entailment(premise: str, hypothesis: str) -> Tuple[bool, str]:
    if _probe_loopback_port(8765, timeout=0.15):
        res = localmodels.JevClient().noul(
            f"Premise:\n{premise[:4000]}\n\nHypothesis:\n{hypothesis[:4000]}",
            "Is the hypothesis entailed by and consistent with the premise without contradiction?",
        )
        if res is not None:
            if res.noul < 0.5:
                return False, f"nli_entailment rejected (noul={res.noul:.2f})"
            return True, ""
    low_h = hypothesis.lower()
    if any(marker in low_h for marker in UNCERTAINTY_MARKERS):
        return False, "nli_entailment offline fallback detected abstention/uncertainty"
    premise_tokens = {tok for tok in re.findall(r"[a-zA-Z0-9_]{4,}", premise.lower())}
    hyp_tokens = {tok for tok in re.findall(r"[a-zA-Z0-9_]{4,}", low_h)}
    if premise_tokens and hyp_tokens and premise_tokens.isdisjoint(hyp_tokens):
        return False, "nli_entailment offline fallback found zero lexical overlap with premise"
    return True, ""


def _is_known_model_alias(name: str) -> bool:
    key = (name or "").strip().lower()
    if not key:
        return False
    return (
        key in ARMY_MODELS
        or key in model_router.OSS_ALIASES
        or key in getattr(model_router, "DEPRECATED_OSS_ALIASES", {})
        or key in model_router.BURST_ALIASES
        or model_router.is_bedrock_model(key)
        or model_router.is_gemini_model(key)
    )


class CascadeRouter:
    """Upfront RouteLLM dispatcher paired with ETH confidence-based escalation."""

    def __init__(self, router: Optional[model_router.ModelRouter] = None) -> None:
        self.router = router or model_router.get_default_router()

    def route_task(
        self,
        prompt: str,
        task_type: str = "auto",
        context_tokens: int = 0,
        allow_gpu_burst: bool = False,
        prefer_oss: bool = True,
        data_classification: str = "corporate_proprietary",
        prefer_gemini_flex: bool = False,
        use_decision_gate: bool = True,
        gemini_client: Optional[gemini_cloud_client.GeminiCloudClient] = None,
    ) -> CascadeDecision:
        resolved_task, complexity = _classify_task_and_complexity(prompt, task_type, context_tokens)
        if data_classification != "public_sanitized":
            return _build_corporate_decision(
                resolved_task,
                complexity,
                allow_gpu_burst,
                prefer_oss,
                data_classification=data_classification,
            )

        gemini_cloud_client.enforce_data_residency(prompt, data_classification=data_classification)
        gate_model: Optional[str] = None
        gate_note = ""
        if use_decision_gate:
            resolved_task, complexity, gate_model, gate_note = _calibrate_with_decision_1(
                prompt,
                resolved_task,
                complexity,
                task_type,
                data_classification,
                gemini_client=gemini_client,
            )

        low_task = (task_type or "").strip().lower()
        is_batch_or_bg = low_task in ("batch", "background") or "batch" in low_task or "background" in low_task
        if prefer_gemini_flex or is_batch_or_bg:
            return _build_gemini_flex_decision(resolved_task, complexity, gate_model, gate_note)

        return _build_corporate_decision(
            resolved_task,
            complexity,
            allow_gpu_burst,
            prefer_oss,
            data_classification=data_classification,
            gate_model=gate_model,
            gate_note=gate_note,
        )

    @staticmethod
    def evaluate_execution_gate(output: str, gate: Optional[ExecutionGateSpec] = None) -> Dict[str, Any]:
        """Evaluate output via deterministic JSON schema, AST, allowlisted command, callback, and NLI hooks."""
        cleaned = (output or "").strip()
        if not cleaned:
            return {"passed": False, "score": 0.0, "reasons": ["empty_output"], "checks": {"non_empty": False}}

        checks: Dict[str, bool] = {"non_empty": True}
        reasons: List[str] = []
        low = cleaned.lower()
        has_uncertainty = any(marker in low for marker in UNCERTAINTY_MARKERS)

        has_deterministic_oracle = bool(
            gate is not None
            and (
                gate.json_schema is not None
                or gate.ast_lang
                or gate.verifier_fn is not None
                or gate.verify_cmd.strip()
                or gate.entailment_premise
            )
        )

        if not has_deterministic_oracle:
            if len(cleaned) < 20:
                checks["min_length"] = False
                return {"passed": False, "score": 0.25, "reasons": ["output_too_short"], "checks": checks}
            checks["min_length"] = True
            if has_uncertainty:
                checks["no_uncertainty_markers"] = False
                return {"passed": False, "score": 0.35, "reasons": ["uncertainty_marker_detected"], "checks": checks}
            checks["no_uncertainty_markers"] = True
            return {"passed": True, "score": 0.85, "reasons": [], "checks": checks}

        assert gate is not None
        if gate.strict and has_uncertainty and gate.json_schema is None:
            checks["no_uncertainty_markers"] = False
            reasons.append("uncertainty_marker_detected")

        if gate.json_schema is not None:
            raw_json = _extract_code_or_raw(cleaned)
            try:
                parsed_json = json.loads(raw_json)
                schema_errs = _validate_json_schema_node(parsed_json, gate.json_schema)
                checks["json_schema"] = not schema_errs
                reasons.extend(schema_errs)
            except ValueError as exc:
                checks["json_schema"] = False
                reasons.append(f"invalid_json: {exc}")

        if gate.ast_lang:
            lang = gate.ast_lang.strip().lower()
            snippet = _extract_code_or_raw(cleaned)
            if lang in ("python", "py"):
                try:
                    ast.parse(snippet)
                    checks["ast_syntax"] = True
                except SyntaxError as exc:
                    checks["ast_syntax"] = False
                    reasons.append(f"python_syntax_error: {exc.msg}")
            elif lang == "json":
                try:
                    json.loads(snippet)
                    checks["ast_syntax"] = True
                except ValueError as exc:
                    checks["ast_syntax"] = False
                    reasons.append(f"json_syntax_error: {exc}")
            else:
                ok_delim = bool(snippet) and _check_balanced_delimiters(snippet)
                checks["ast_syntax"] = ok_delim
                if not ok_delim:
                    reasons.append(f"{lang}_unbalanced_delimiters")

        if gate.verify_cmd.strip():
            cmd_ok, cmd_reason = _run_allowlisted_verify_cmd(gate.verify_cmd)
            checks["verify_cmd"] = cmd_ok
            if not cmd_ok:
                reasons.append(cmd_reason)

        if gate.verifier_fn is not None:
            try:
                ver_res = gate.verifier_fn(cleaned)
                if isinstance(ver_res, tuple) and len(ver_res) >= 2:
                    fn_ok, fn_reason = bool(ver_res[0]), str(ver_res[1] or "")
                elif isinstance(ver_res, Mapping):
                    fn_ok = bool(ver_res.get("passed", False))
                    fn_reason = str(ver_res.get("reason") or "")
                else:
                    fn_ok, fn_reason = bool(ver_res), ""
            except Exception as exc:
                fn_ok, fn_reason = False, f"verifier_fn raised {exc}"
            checks["verifier_fn"] = fn_ok
            if not fn_ok:
                reasons.append(fn_reason or "verifier_fn_failed")

        if gate.entailment_premise:
            ent_ok, ent_reason = _check_entailment(gate.entailment_premise, cleaned)
            checks["nli_entailment"] = ent_ok
            if not ent_ok:
                reasons.append(ent_reason)

        passed = all(checks.values()) and not reasons
        score = 0.95 if passed else 0.15
        return {"passed": passed, "score": score, "reasons": reasons, "checks": checks}

    @staticmethod
    def _score_confidence(output: str, gate: Optional[ExecutionGateSpec] = None) -> float:
        return float(CascadeRouter.evaluate_execution_gate(output, gate)["score"])

    def execute_cascade(
        self,
        prompt: str,
        system: str = "",
        task_type: str = "auto",
        context_tokens: int = 0,
        allow_gpu_burst: bool = False,
        prefer_oss: bool = True,
        min_confidence: float = 0.65,
        max_escalations: int = 2,
        data_classification: str = "corporate_proprietary",
        prefer_gemini_flex: bool = False,
        use_decision_gate: bool = True,
        gemini_client: Optional[gemini_cloud_client.GeminiCloudClient] = None,
        gate: Optional[ExecutionGateSpec] = None,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        rail = guardrails.evaluate_input_rail(prompt)
        if not rail.allowed:
            return {
                "status": "BLOCKED_BY_INPUT_RAIL",
                "reason": rail.reason,
                "attempts": [],
                "final_model": "",
                "escalated": False,
                "output": "",
            }

        effective_schema = json_schema if json_schema is not None else (gate.json_schema if gate is not None else None)
        effective_gate = (
            ExecutionGateSpec(
                json_schema=effective_schema,
                ast_lang=gate.ast_lang if gate else None,
                verifier_fn=gate.verifier_fn if gate else None,
                verify_cmd=gate.verify_cmd if gate else "",
                entailment_premise=gate.entailment_premise if gate else None,
                strict=gate.strict if gate else True,
            )
            if (gate is not None or effective_schema is not None)
            else None
        )

        decision = self.route_task(
            prompt=prompt,
            task_type=task_type,
            context_tokens=context_tokens,
            allow_gpu_burst=allow_gpu_burst,
            prefer_oss=prefer_oss,
            data_classification=data_classification,
            prefer_gemini_flex=prefer_gemini_flex,
            use_decision_gate=use_decision_gate,
            gemini_client=gemini_client,
        )
        candidates = (decision.initial_model,) + decision.escalation_chain[: max(0, int(max_escalations))]
        attempts: List[Dict[str, Any]] = []
        final_out = ""
        final_model = candidates[0]

        for idx, candidate in enumerate(candidates):
            try:
                extra_kwargs: Dict[str, Any] = {}
                if effective_schema is not None:
                    extra_kwargs["json_schema"] = effective_schema
                if model_router.is_gemini_model(candidate):
                    if decision.thinking_level is not None and effective_schema is not None:
                        extra_kwargs["thinking_level"] = decision.thinking_level
                    out = self.router.chat_text(
                        prompt,
                        system=system,
                        model=candidate,
                        max_tokens=2048,
                        temperature=0.2,
                        data_classification=data_classification,
                        service_tier=decision.service_tier or "flex",
                        **extra_kwargs,
                    )
                else:
                    out = self.router.chat_text(
                        prompt,
                        system=system,
                        model=candidate,
                        max_tokens=2048,
                        temperature=0.2,
                        **extra_kwargs,
                    )
                gate_res = self.evaluate_execution_gate(out, effective_gate)
                conf = float(gate_res["score"])
                attempts.append(
                    {
                        "step": idx + 1,
                        "model": candidate,
                        "confidence": conf,
                        "gate_passed": gate_res["passed"],
                        "gate_reasons": gate_res["reasons"],
                        "error": "",
                    }
                )
                final_out = out
                final_model = candidate
                if gate_res["passed"] and conf >= float(min_confidence):
                    break
            except PermissionError:
                raise
            except Exception as exc:
                attempts.append(
                    {
                        "step": idx + 1,
                        "model": candidate,
                        "confidence": 0.0,
                        "gate_passed": False,
                        "gate_reasons": [str(exc)],
                        "error": str(exc),
                    }
                )

        return {
            "status": "OK" if final_out else "ERROR",
            "data_classification": data_classification,
            "decision": asdict(decision),
            "attempts": attempts,
            "final_model": final_model,
            "escalated": len(attempts) > 1,
            "output": final_out,
        }

    def execute_best_of_n(
        self,
        prompt: str,
        *,
        n: int = 3,
        task_type: str = "code_generation",
        data_classification: str = "corporate_proprietary",
        gate: Optional[ExecutionGateSpec] = None,
        verify_cmd: str = "",
        json_schema: Optional[Dict[str, Any]] = None,
        candidates: Optional[List[str]] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Lever D: Sample N candidates, filter by deterministic execution gate, emit SFT/DPO pairs, escalate only if 0 pass."""
        rail = guardrails.evaluate_input_rail(prompt)
        if not rail.allowed:
            return {
                "status": "BLOCKED_BY_INPUT_RAIL",
                "reason": rail.reason,
                "winner": None,
                "output": "",
                "passed_count": 0,
                "total_candidates": 0,
                "escalated": False,
                "escalation_model": "",
                "candidates": [],
                "sft_dpo_pairs": [],
                "data_classification": data_classification,
            }

        is_public = data_classification == "public_sanitized"
        if is_public:
            gemini_cloud_client.enforce_data_residency(prompt, data_classification=data_classification)

        sample_count = max(1, int(n))
        effective_schema = json_schema if json_schema is not None else (gate.json_schema if gate is not None else None)
        effective_cmd = verify_cmd.strip() or (gate.verify_cmd if gate is not None else "")
        effective_gate = ExecutionGateSpec(
            json_schema=effective_schema,
            ast_lang=gate.ast_lang if gate is not None else None,
            verifier_fn=gate.verifier_fn if gate is not None else None,
            verify_cmd=effective_cmd,
            entailment_premise=gate.entailment_premise if gate is not None else None,
            strict=gate.strict if gate is not None else True,
        )

        default_pool = (
            ["gemini-4-hydrogen", "gemini-4-neon", "gemini-3.5-flash-lite"]
            if is_public
            else ["qwen3-coder-30b-a3b", "qwen3.5-35b-a3b", "glm-4.7-flash", "gemma-4-26b-a4b"]
        )
        escalation_model = "gemini-4-carbon" if is_public else "claude-sonnet-5-5"

        pre_generated_outputs: Optional[List[str]] = None
        if candidates:
            if all(_is_known_model_alias(c) for c in candidates):
                model_schedule = [candidates[i % len(candidates)] for i in range(sample_count)]
                if not is_public and any(model_router.is_gemini_model(m) for m in model_schedule):
                    raise PermissionError(
                        f"DATA_RESIDENCY_BLOCKED: Gemini candidate models require data_classification='public_sanitized', got {data_classification!r}."
                    )
            else:
                pre_generated_outputs = list(candidates)
                sample_count = len(pre_generated_outputs)
                model_schedule = [default_pool[i % len(default_pool)] for i in range(sample_count)]
        else:
            model_schedule = [default_pool[i % len(default_pool)] for i in range(sample_count)]

        if dry_run and pre_generated_outputs is None:
            planned_candidates = [
                {"index": idx, "model": m, "output": "", "passed": False, "score": 0.0, "reasons": [], "planned": True}
                for idx, m in enumerate(model_schedule)
            ]
            return {
                "status": "DRY_RUN",
                "dry_run": True,
                "task_type": task_type,
                "winner": None,
                "output": "",
                "passed_count": 0,
                "total_candidates": len(planned_candidates),
                "escalated": False,
                "escalation_model": escalation_model,
                "candidates": planned_candidates,
                "sft_dpo_pairs": [],
                "data_classification": data_classification,
            }

        evaluated: List[Dict[str, Any]] = []
        for idx in range(sample_count):
            cand_model = model_schedule[idx]
            if pre_generated_outputs is not None:
                cand_text = pre_generated_outputs[idx]
                err_text = ""
            else:
                temp = round(0.2 + 0.15 * (idx % 4), 2)
                extra_kw: Dict[str, Any] = {}
                if effective_schema is not None:
                    extra_kw["json_schema"] = effective_schema
                try:
                    if model_router.is_gemini_model(cand_model):
                        cand_text = self.router.chat_text(
                            prompt,
                            model=cand_model,
                            max_tokens=2048,
                            temperature=temp,
                            data_classification=data_classification,
                            service_tier="flex",
                            **extra_kw,
                        )
                    else:
                        cand_text = self.router.chat_text(
                            prompt,
                            model=cand_model,
                            max_tokens=2048,
                            temperature=temp,
                            **extra_kw,
                        )
                    err_text = ""
                except PermissionError:
                    raise
                except Exception as exc:
                    cand_text = ""
                    err_text = str(exc)

            gate_res = self.evaluate_execution_gate(cand_text, effective_gate)
            out_rail = guardrails.evaluate_output_rail(cand_text) if cand_text else None
            out_rail_ok = bool(out_rail is not None and out_rail.allowed)
            passed = bool(gate_res["passed"] and out_rail_ok and not err_text)
            reasons = list(gate_res["reasons"])
            if err_text:
                reasons.append(err_text)
            if out_rail is not None and not out_rail.allowed:
                reasons.append(f"output_rail:{out_rail.reason}")

            evaluated.append(
                {
                    "index": idx,
                    "model": cand_model,
                    "output": cand_text,
                    "passed": passed,
                    "score": float(gate_res["score"]) if passed else 0.0,
                    "reasons": reasons,
                }
            )

        survivors = [c for c in evaluated if c["passed"]]
        failed_entries = [c for c in evaluated if not c["passed"] and c["output"].strip()]
        escalated = False

        if survivors:
            winner_entry: Optional[Dict[str, Any]] = max(survivors, key=lambda item: (item["score"], len(item["output"])))
        elif not dry_run and pre_generated_outputs is None:
            escalated = True
            try:
                extra_esc: Dict[str, Any] = {"json_schema": effective_schema} if effective_schema is not None else {}
                if model_router.is_gemini_model(escalation_model):
                    esc_out = self.router.chat_text(
                        prompt,
                        model=escalation_model,
                        max_tokens=2048,
                        temperature=0.1,
                        data_classification=data_classification,
                        service_tier="flex",
                        **extra_esc,
                    )
                else:
                    esc_out = self.router.chat_text(
                        prompt,
                        model=escalation_model,
                        max_tokens=2048,
                        temperature=0.1,
                        **extra_esc,
                    )
                esc_gate = self.evaluate_execution_gate(esc_out, effective_gate)
                esc_rail = guardrails.evaluate_output_rail(esc_out) if esc_out else None
                esc_passed = bool(esc_gate["passed"] and esc_rail is not None and esc_rail.allowed)
                winner_entry = {
                    "index": len(evaluated),
                    "model": escalation_model,
                    "output": esc_out,
                    "passed": esc_passed,
                    "score": float(esc_gate["score"]) if esc_passed else 0.0,
                    "reasons": list(esc_gate["reasons"]),
                }
            except PermissionError:
                raise
            except Exception as exc:
                winner_entry = {
                    "index": len(evaluated),
                    "model": escalation_model,
                    "output": "",
                    "passed": False,
                    "score": 0.0,
                    "reasons": [str(exc)],
                }
        else:
            winner_entry = None

        sft_dpo_pairs: List[Dict[str, Any]] = []
        if winner_entry is not None and winner_entry["passed"]:
            for rej in failed_entries:
                rej_rail = guardrails.evaluate_output_rail(rej["output"])
                if rej_rail.allowed:
                    sft_dpo_pairs.append(
                        {
                            "prompt": prompt,
                            "chosen": winner_entry["output"],
                            "rejected": rej["output"],
                            "chosen_model": winner_entry["model"],
                            "rejected_model": rej["model"],
                            "gate_reasons": rej["reasons"],
                            "data_classification": data_classification,
                        }
                    )

        winner_out = winner_entry["output"] if winner_entry is not None else ""
        return {
            "status": "OK" if (winner_entry is not None and winner_out) else "ERROR",
            "task_type": task_type,
            "winner": winner_entry,
            "output": winner_out,
            "passed_count": len(survivors),
            "total_candidates": len(evaluated),
            "escalated": escalated,
            "escalation_model": escalation_model,
            "candidates": evaluated,
            "sft_dpo_pairs": sft_dpo_pairs,
            "data_classification": data_classification,
        }


def _normalize_rubric_rules(
    rules: Union[Sequence[Union[str, Mapping[str, Any]]], Mapping[str, str]],
) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    if isinstance(rules, Mapping):
        for key, instr in rules.items():
            normalized[str(key)] = str(instr)
    elif isinstance(rules, (list, tuple)):
        for idx, item in enumerate(rules, start=1):
            if isinstance(item, Mapping):
                q_id = str(item.get("id") or item.get("key") or f"rule_{idx:03d}")
                normalized[q_id] = str(item.get("instructions") or item.get("rule") or q_id)
            else:
                normalized[f"rule_{idx:03d}"] = str(item)
    else:
        raise ValueError("rules must be a sequence or mapping of checklist items.")
    if not normalized:
        raise ValueError("rules must not be empty.")
    return normalized


def _summarize_rubric_answers(answers: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    passed: List[str] = []
    failed: List[str] = []
    for rule_id, entry in answers.items():
        if not isinstance(entry, Mapping):
            failed.append(str(rule_id))
            continue
        score = entry.get("noul")
        is_ok = float(score) >= 0.5 if score is not None else bool(entry.get("passed", True))
        (passed if is_ok else failed).append(str(rule_id))
    return passed, failed


def run_decision_rubric_gate(
    diff_or_code: str,
    rules: Union[Sequence[Union[str, Mapping[str, Any]]], Mapping[str, str]],
    *,
    data_classification: str = "corporate_proprietary",
    dry_run: bool = False,
    client: Any = None,
) -> Dict[str, Any]:
    """Pack N review rules into one 0-output-token Decision-1 call (public_sanitized) or JEV :8765 (corporate)."""
    if not isinstance(diff_or_code, str) or not diff_or_code.strip():
        raise ValueError("diff_or_code must be a non-empty string.")
    rule_map = _normalize_rubric_rules(rules)

    if data_classification == "public_sanitized":
        active_client = client or gemini_cloud_client.GeminiCloudClient()
        res = active_client.decide_code_review_checklist(
            diff_or_code,
            rules,
            data_classification=data_classification,
            dry_run=dry_run,
        )
        if dry_run:
            return {
                "status": "DRY_RUN",
                "backend": "gemini-decision-1",
                "model": "gemini-decision-1",
                "data_classification": data_classification,
                "rule_count": len(rule_map),
                "expected_output_tokens": 0,
                "envelope": res,
            }
        answers = res.get("answers") or {}
        passed_rules, failed_rules = _summarize_rubric_answers(answers)
        return {
            "status": "OK",
            "backend": "gemini-decision-1",
            "model": "gemini-decision-1",
            "data_classification": data_classification,
            "rule_count": len(answers) or len(rule_map),
            "expected_output_tokens": 0,
            "passed": not failed_rules,
            "passed_rules": passed_rules,
            "failed_rules": failed_rules,
            "answers": answers,
            "usage": res.get("usage") or {"total_output_tokens": 0},
        }

    if dry_run:
        return {
            "status": "DRY_RUN",
            "backend": "ec2_jev_8765",
            "model": "systemone-modernbert",
            "data_classification": data_classification,
            "rule_count": len(rule_map),
            "expected_output_tokens": 0,
        }

    jev = client if client is not None and hasattr(client, "batch") else localmodels.JevClient()
    jev_questions = {rid: {"type": "noul", "instructions": instr} for rid, instr in rule_map.items()}
    jev_answers = jev.batch(diff_or_code, jev_questions)
    if jev_answers is not None:
        passed_rules, failed_rules = _summarize_rubric_answers(jev_answers)
        return {
            "status": "OK",
            "backend": "ec2_jev_8765",
            "model": "systemone-modernbert",
            "data_classification": data_classification,
            "rule_count": len(jev_answers),
            "expected_output_tokens": 0,
            "passed": not failed_rules,
            "passed_rules": passed_rules,
            "failed_rules": failed_rules,
            "answers": jev_answers,
        }

    rail = guardrails.evaluate_input_rail(diff_or_code)
    fallback_answers = {
        rid: {"type": "noul", "noul": 1.0 if rail.allowed else 0.0, "confidence": 0.5, "fallback": True}
        for rid in rule_map
    }
    passed_rules, failed_rules = _summarize_rubric_answers(fallback_answers)
    return {
        "status": "OK",
        "backend": "offline_deterministic_fallback",
        "model": "local-guardrails-fallback",
        "data_classification": data_classification,
        "rule_count": len(rule_map),
        "expected_output_tokens": 0,
        "passed": not failed_rules,
        "passed_rules": passed_rules,
        "failed_rules": failed_rules,
        "answers": fallback_answers,
    }


def _parse_verdict(text: str) -> str:
    upper = (text or "").upper()
    m = re.search(r"\b(BLOCK|NEEDS_WORK|APPROVE)\b", upper)
    return m.group(1) if m else "NEEDS_WORK"


def run_poll_review_jury(
    diff_or_code: str,
    criteria: str = "Correctness, security, Clean Architecture, and edge cases",
    panel_models: Optional[Sequence[str]] = None,
    router: Optional[model_router.ModelRouter] = None,
    dry_run: bool = False,
    data_classification: str = "corporate_proprietary",
    use_decision_gate: bool = False,
    gemini_client: Optional[gemini_cloud_client.GeminiCloudClient] = None,
) -> Dict[str, Any]:
    """Run a diverse multi-family PoLL (Panel of LLM Evaluators) code review jury."""
    panel = tuple(panel_models) if panel_models else ("qwen3.5-35b-a3b", "gemma-4-26b-a4b", "claude-sonnet-5-5")
    rubric_result: Optional[Dict[str, Any]] = None
    if use_decision_gate:
        rules = [c.strip() for c in criteria.split(",") if c.strip()] or [criteria]
        rubric_result = run_decision_rubric_gate(
            diff_or_code,
            rules,
            data_classification=data_classification,
            dry_run=dry_run,
            client=gemini_client,
        )

    if dry_run:
        dry_out: Dict[str, Any] = {
            "status": "DRY_RUN",
            "panel_models": list(panel),
            "criteria": criteria,
            "tie_breaker": "claude-opus-5-5",
            "verdict": "DRY_RUN",
            "votes": {},
        }
        if rubric_result is not None:
            dry_out["decision_rubric"] = rubric_result
        return dry_out

    active_router = router or model_router.get_default_router()
    review_prompt = (
        f"Review the following code/diff strictly against criteria: {criteria}\n"
        "Start your reply with exactly one verdict token: APPROVE, NEEDS_WORK, or BLOCK, "
        "followed by concise bullet findings.\n\n"
        f"{diff_or_code[:12000]}"
    )

    def _review_one(model_name: str) -> Dict[str, Any]:
        if model_router.is_gemini_model(model_name):
            raw = active_router.chat_text(
                review_prompt,
                system="You are an independent PoLL code review juror. Report verified concrete defects only.",
                model=model_name,
                max_tokens=1024,
                temperature=0.1,
                data_classification=data_classification,
            )
        else:
            raw = active_router.chat_text(
                review_prompt,
                system="You are an independent PoLL code review juror. Report verified concrete defects only.",
                model=model_name,
                max_tokens=1024,
                temperature=0.1,
            )
        return {"model": model_name, "verdict": _parse_verdict(raw), "review": raw}

    with ThreadPoolExecutor(max_workers=max(1, len(panel))) as pool:
        reviews = list(pool.map(_review_one, panel))

    counts: Dict[str, int] = {"APPROVE": 0, "NEEDS_WORK": 0, "BLOCK": 0}
    for item in reviews:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1

    distinct_verdicts = len({r["verdict"] for r in reviews})
    if counts["BLOCK"] > len(reviews) / 2:
        consensus = "BLOCK"
    elif counts["APPROVE"] > len(reviews) / 2:
        consensus = "APPROVE"
    else:
        consensus = "NEEDS_WORK"

    result: Dict[str, Any] = {
        "status": "OK",
        "panel_models": list(panel),
        "consensus_verdict": consensus,
        "votes": counts,
        "disagreement": distinct_verdicts > 1,
        "tie_breaker_recommended": distinct_verdicts >= len(panel) or consensus == "NEEDS_WORK",
        "tie_breaker_model": "claude-opus-5-5",
        "reviews": reviews,
    }
    if rubric_result is not None:
        result["decision_rubric"] = rubric_result
    return result


def generate_m7g_litellm_config() -> Dict[str, Any]:
    """Generate m7g.8xlarge LiteLLM (:4000) resident stack YAML and DDR5 RAM budget report."""
    resident_aliases = (
        "qwen3-coder-30b-a3b",
        "qwen3.5-35b-a3b",
        "ling-mini-2.0",
        "qwen3.5-4b",
        "bitnet-2b",
        "granite-4-h-tiny",
        "qwen3-guard-0.6b",
        "qwen3-embed-0.6b",
    )
    mmap_warm_swap_aliases = (
        "gemma-4-26b-a4b",
        "gemma-4-12b-unified",
        "glm-4.7-flash",
        "qwen3-guard-stream-0.6b",
    )
    total_weights_gb = round(sum(ARMY_MODELS[a].footprint_gb for a in resident_aliases), 2)
    yaml_lines = [
        "model_list:",
        "  - model_name: qwen3-coder-30b-a3b",
        "    litellm_params:",
        "      model: openai/qwen3-coder-30b-a3b",
        "      api_base: http://127.0.0.1:18807/v1",
        "      api_key: local",
        "  - model_name: qwen3.5-35b-a3b",
        "    litellm_params:",
        "      model: openai/qwen3.5-35b-a3b",
        "      api_base: http://127.0.0.1:18801/v1",
        "      api_key: local",
        "  - model_name: ling-mini-2.0",
        "    litellm_params:",
        "      model: openai/ling-mini-2.0",
        "      api_base: http://127.0.0.1:18802/v1",
        "      api_key: local",
        "  - model_name: qwen3.5-4b",
        "    litellm_params:",
        "      model: openai/qwen3.5-4b",
        "      api_base: http://127.0.0.1:18803/v1",
        "      api_key: local",
        "  - model_name: granite-4-h-tiny",
        "    litellm_params:",
        "      model: openai/granite-4-h-tiny",
        "      api_base: http://127.0.0.1:18804/v1",
        "      api_key: local",
        "  - model_name: granite-4.0-h-tiny",
        "    litellm_params:",
        "      model: openai/granite-4-h-tiny",
        "      api_base: http://127.0.0.1:18804/v1",
        "      api_key: local",
        "  - model_name: bitnet-2b",
        "    litellm_params:",
        "      model: openai/bitnet-b1.58-2b-4t",
        "      api_base: http://127.0.0.1:18805/v1",
        "      api_key: local",
        "  - model_name: bitnet",
        "    litellm_params:",
        "      model: openai/bitnet-b1.58-2b-4t",
        "      api_base: http://127.0.0.1:18805/v1",
        "      api_key: local",
        "  - model_name: qwen3-guard-0.6b",
        "    litellm_params:",
        "      model: openai/qwen3-guard-0.6b",
        "      api_base: http://127.0.0.1:18806/v1",
        "      api_key: local",
        "  - model_name: qwen3-guard-stream-0.6b",
        "    litellm_params:",
        "      model: openai/qwen3-guard-stream-0.6b",
        "      api_base: http://127.0.0.1:18806/v1",
        "      api_key: local",
        "  - model_name: qwen3-embed-0.6b",
        "    litellm_params:",
        "      model: openai/qwen3-embedding-0.6b",
        "      api_base: http://127.0.0.1:8766/v1",
        "      api_key: local",
        "  - model_name: gemma-4-26b-a4b",
        "    litellm_params:",
        "      model: openai/gemma-4-26b-a4b",
        "      api_base: http://127.0.0.1:18809/v1",
        "      api_key: local",
        "  - model_name: gemma-4-12b-unified",
        "    litellm_params:",
        "      model: openai/gemma-4-12b-unified",
        "      api_base: http://127.0.0.1:18809/v1",
        "      api_key: local",
        "  - model_name: glm-4.7-flash",
        "    litellm_params:",
        "      model: openai/glm-4.7-flash",
        "      api_base: http://127.0.0.1:18809/v1",
        "      api_key: local",
        "  - model_name: mini-pool",
        "    litellm_params:",
        "      model: openai/qwen3-coder-30b-a3b",
        "      api_base: http://127.0.0.1:18807/v1",
        "      api_key: local",
        "litellm_settings:",
        "  drop_params: true",
        "  request_timeout: 120",
    ]
    return {
        "instance_type": "m7g.8xlarge",
        "total_ddr5_gib": M7G_TOTAL_RAM_GIB,
        "safe_weight_ceiling_gib": M7G_SAFE_WEIGHT_CEILING_GIB,
        "resident_models": list(resident_aliases),
        "mmap_warm_swap_models": list(mmap_warm_swap_aliases),
        "resident_weights_gb": total_weights_gb,
        "safe_weight_headroom_gb": round(M7G_SAFE_WEIGHT_CEILING_GIB - total_weights_gb, 2),
        "headroom_gb": round(M7G_TOTAL_RAM_GIB - total_weights_gb, 2),
        "retired_cpu_dense_models": [
            "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B",
            "microsoft/Phi-4-reasoning-plus",
            "microsoft/Phi-4-mini-instruct",
            "google/gemma-3-12b-it",
            "nvidia/Nemotron-Mini-4B-Instruct",
            "Qwen/Qwen2.5-Coder-3B-Instruct",
        ],
        "litellm_yaml": "\n".join(yaml_lines) + "\n",
    }


def build_provision_plan(
    region: str = gpu_burst_orchestrator.DEFAULT_REGION,
    profile: str = gpu_burst_orchestrator.DEFAULT_PROFILE,
    bucket: str = gpu_burst_orchestrator.DEFAULT_BUCKET,
) -> Dict[str, Any]:
    """Generate non-mutating Phase 2 (S3/m7g.8xlarge) and Phase 3 (g4dn.12xlarge smoke) blueprints."""
    gpu_burst_orchestrator.validate_u2_profile(profile)
    region = gpu_burst_orchestrator.validate_aws_region(region)
    burst_plan = gpu_burst_orchestrator.plan_gpu_burst(
        tier="large",
        region=region,
        profile=profile,
        nodes=1,
        model_id="Qwen/Qwen3-32B-AWQ",
        dry_run=True,
        execute_approved_u2=False,
        check_aws=False,
    )
    m7g_cfg = generate_m7g_litellm_config()
    s3_models = f"s3://{bucket}/{gpu_burst_orchestrator.DEFAULT_MODEL_STASH_PREFIX}"
    phase2_cmds = [
        f"hf download unsloth/Qwen3.5-35B-A3B-GGUF Qwen3.5-35B-A3B-Q4_K_M.gguf && aws s3 cp ... {s3_models}/ --profile {profile} --region {region}",
        f"hf download inclusionAI/Ling-mini-2.0-GGUF Ling-mini-2.0-Q4_K_M.gguf && aws s3 cp ... {s3_models}/ --profile {profile} --region {region}",
        f"hf download unsloth/Qwen3.5-4B-GGUF Qwen3.5-4B-Q4_K_M.gguf && aws s3 cp ... {s3_models}/ --profile {profile} --region {region}",
        f"hf download microsoft/bitnet-b1.58-2B-4T-gguf ggml-model-i2_s.gguf && aws s3 cp ... {s3_models}/ --profile {profile} --region {region}",
        "Deploy generated litellm_config.yaml to EC2 Cloud-Brain :4000 and restart worker services (:18801..:18807).",
    ]
    phase3_vllm_cmd = (
        "vllm serve s3://kaizen-u2-dev-ncv-duckdb/kaizen-cloud-brain/models/Qwen/Qwen3-32B-AWQ "
        "--load-format runai_streamer --tensor-parallel-size 4 --dtype float16 "
        "--kv-cache-dtype float16 --enable-prefix-caching --enforce-eager --enable-lora --max-lora-rank 16"
    )
    dist_fit = validate_deployment_fit(
        "qwen3-235b-a22b-rpc",
        "g4dn.xlarge",
        nodes=16,
        distributed_rpc=True,
    )
    return {
        "aws_mutated": False,
        "dry_run": True,
        "requires_explicit_user_approval": True,
        "profile": profile,
        "region": region,
        "phase2_m7g_rollout": {
            "resident_weights_gb": m7g_cfg["resident_weights_gb"],
            "safe_weight_headroom_gb": m7g_cfg["safe_weight_headroom_gb"],
            "headroom_gb": m7g_cfg["headroom_gb"],
            "commands": phase2_cmds,
        },
        "phase3_spot_smoke_test": {
            "target_instance": burst_plan["region_mapping"]["selected_instance_types"][0],
            "vllm_command": phase3_vllm_cmd,
            "burst_plan_preview": {
                "default_state": burst_plan["default_state"],
                "idle_stop_minutes": burst_plan["idle_stop_minutes"],
                "finops_estimate": burst_plan["finops_estimate"],
                "vllm_serving_flags": burst_plan["sparsity_config"].get("vllm_serving_flags", []),
            },
        },
        "phase4_distributed_256gb_rpc": {
            "tier": "distributed_256gb",
            "default_state": "STOPPED",
            "idle_cost_usd": 0.0,
            "instance_type": "g4dn.xlarge",
            "nodes": 16,
            "total_vcpus": 64,
            "quota_code": "L-3819A6DF",
            "total_vram_gb": 256,
            "placement_group": "kaizen-dist256-cpg",
            "primary_runtime": "llama_cpp_rpc",
            "fallback_runtime": "vllm_ray_pp",
            "fit_verified": dist_fit["ok"],
        },
    }


NEON_LOOPBACK_PORTS: Tuple[Tuple[str, int], ...] = (
    ("litellm", 4000),
    ("jev", 8765),
    ("fastmcp", 8770),
    ("zoekt", 6070),
    ("coderag", 18808),
    ("serena", 24280),
    ("claude_ram", 24302),
    ("gpu_burst", 8000),
)

NEON_TWO_PHASE_PLAYBOOK: Dict[str, Any] = {
    "orchestrator": "neon",
    "version": "1.0",
    "principle": (
        "Phase A thin-client planning (MCP/LSP/Decision-1 gate); "
        "Phase B offload-first execution; Phase C PoLL Jury and deterministic gate verification."
    ),
    "phase_a_planner": {
        "name": "Neon Planner",
        "mode": "read_only_thin_client",
        "mcp_tools": ["serena", "zoekt_search", "coderag_query", "neon_decide"],
        "decision_1_gate": {
            "corporate_proprietary": "ec2_jev_8765 (/v1/systemone, 0 output tokens)",
            "public_sanitized": "gemini-decision-1 (0 output tokens, fail-closed data residency)",
        },
        "commands": [
            "claude-ram map <repo>",
            "claude-ram slice <file>:<start>-<end>",
            'kaizen-kit neon neon-plan --prompt "<task>" --json',
        ],
    },
    "phase_b_executor": {
        "name": "Neon Executor",
        "mode": "offload_first",
        "backends": {
            "m7g_8xlarge_litellm_4000": [
                "qwen3.5-35b-a3b",
                "qwen3-coder-30b-a3b",
                "ling-mini-2.0",
                "gemma-4-26b-a4b",
            ],
            "claude_ram_corporate_bedrock": ["claude-sonnet-5-5", "claude-opus-5-5"],
            "public_sanitized_gemini_flex_batch": [
                "gemini-decision-1",
                "gemini-4-neon",
                "gemini-4-hydrogen",
                "gemini-4-carbon",
                "gemini-3.5-flash-lite",
            ],
            "ephemeral_gpu_burst": ["qwen3-32b-awq", "deepseek-r1-32b-awq", "qwen3-235b-a22b-rpc"],
        },
        "commands": [
            'claude-ram ask --model sonnet --repo <repo> --files "<glob>" "<question>"',
            'claude-ram patch --model sonnet --file <rel_path> "<instruction>"',
            'kaizen-kit gemini generate --prompt "<sanitized_prompt>" --model gemini-4-hydrogen --tier flex --data-classification public_sanitized --dry-run --json',
        ],
    },
    "phase_c_verifier": {
        "name": "Neon Verifier",
        "gates": ["poll_review_jury", "doctor --self-test", "senior_review_lint"],
        "commands": [
            'kaizen-kit carbon jury --code "<diff_or_snippet>" --dry-run --json',
            "kaizen-kit doctor --self-test",
            "kaizen-kit senior-lint <path>",
        ],
    },
}


def build_neon_phase_plan(
    prompt: str,
    task_type: str = "auto",
    data_classification: str = "corporate_proprietary",
    context_tokens: int = 0,
    allow_gpu_burst: bool = False,
    *,
    use_decision_gate: bool = True,
    gemini_client: Optional[gemini_cloud_client.GeminiCloudClient] = None,
) -> Dict[str, Any]:
    """Build a structured Neon Two-Phase (Planner, Offload Executor, Verifier) execution plan."""
    is_public = data_classification == "public_sanitized"
    decision = CascadeRouter().route_task(
        prompt,
        task_type=task_type,
        context_tokens=context_tokens,
        allow_gpu_burst=allow_gpu_burst,
        data_classification=data_classification,
        prefer_gemini_flex=is_public,
        use_decision_gate=use_decision_gate,
        gemini_client=gemini_client,
    )

    if is_public:
        offload_models = [
            "gemini-decision-1",
            "gemini-4-neon",
            "gemini-4-hydrogen",
            "gemini-4-carbon",
            "gemini-3.5-flash-lite",
        ]
        offload_backends: Dict[str, Any] = {
            "gemini_decision_1": ["gemini-decision-1"],
            "gemini_flex_batch": ["gemini-4-neon", "gemini-4-hydrogen", "gemini-4-carbon", "gemini-3.5-flash-lite"],
        }
        phase_a_commands = [
            'kaizen-kit gemini decide --input "<sanitized_input>" --instructions "<question>" --data-classification public_sanitized --dry-run --json',
            'kaizen-kit neon neon-plan --prompt "<task>" --data-classification public_sanitized --json',
        ]
        phase_b_commands = [
            'kaizen-kit gemini generate --prompt "<sanitized_prompt>" --model gemini-4-hydrogen --tier flex --data-classification public_sanitized --dry-run --json',
            'kaizen-kit gemini batch-plan --model gemini-4-hydrogen --tier flex --data-classification public_sanitized --json',
        ]
    else:
        offload_models = [
            "qwen3.5-35b-a3b",
            "qwen3-coder-30b-a3b",
            "ling-mini-2.0",
            "gemma-4-26b-a4b",
            "claude-sonnet-5-5",
            "claude-opus-5-5",
        ]
        if allow_gpu_burst:
            offload_models.extend(["qwen3-32b-awq", "deepseek-r1-32b-awq"])
        offload_backends = {
            "m7g_8xlarge_litellm_4000": [
                "qwen3.5-35b-a3b",
                "qwen3-coder-30b-a3b",
                "ling-mini-2.0",
                "gemma-4-26b-a4b",
            ],
            "claude_ram": ["claude-sonnet-5-5", "claude-opus-5-5"],
            "corporate_bedrock_claude": ["claude-sonnet-5-5", "claude-opus-5-5"],
        }
        phase_a_commands = [
            "claude-ram map <repo>",
            "claude-ram slice <file>:<start>-<end>",
            'kaizen-kit neon neon-plan --prompt "<task>" --data-classification corporate_proprietary --json',
        ]
        phase_b_commands = [
            'claude-ram ask --model sonnet --repo <repo> --files "<glob>" "<question>"',
            'claude-ram patch --model sonnet --file <rel_path> "<instruction>"',
            'kaizen-kit carbon route --prompt "<task>" --json',
        ]

    return {
        "orchestrator": "neon",
        "data_classification": data_classification,
        "gemini_exposure": is_public,
        "phase_a_planner": {
            "role": "Neon Planner (Read-Only Thin-Client Discovery & Gate)",
            "read_only": True,
            "mcp_tools": ["serena", "zoekt_search", "coderag_query", "neon_decide"],
            "recommended_decision_backend": "gemini-decision-1" if is_public else "ec2_jev_8765",
            "decision_gate_model": decision.decision_gate_model,
            "commands": phase_a_commands,
        },
        "phase_b_executor": {
            "role": "Neon Executor (Offload-First Multi-Backend Synthesis)",
            "offload_first": True,
            "primary_model": decision.initial_model,
            "escalation_chain": list(decision.escalation_chain),
            "target_hardware": decision.target_hardware,
            "service_tier": decision.service_tier,
            "recommended_offload_models": offload_models,
            "offload_backends": offload_backends,
            "commands": phase_b_commands,
        },
        "phase_c_verifier": {
            "role": "Neon Verifier (PoLL Jury + Deterministic Gates + Senior Review Lint)",
            "gates": ["poll_review_jury", "doctor --self-test", "senior_review_lint"],
            "poll_jury_models": (
                ["gemini-4-hydrogen", "gemini-4-carbon"]
                if is_public
                else ["qwen3.5-35b-a3b", "gemma-4-26b-a4b", "claude-sonnet-5-5"]
            ),
            "commands": [
                'kaizen-kit carbon jury --code "<diff_or_snippet>" --dry-run --json',
                "kaizen-kit doctor --self-test",
                "kaizen-kit senior-lint <path>",
            ],
        },
        "cascade_decision": asdict(decision),
    }


def _probe_loopback_port(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def get_neon_live_status(timeout_s: float = 0.25) -> Dict[str, Any]:
    """Probe Neon loopback service ports (0.25s timeout) and inspect safe boolean capability flags."""
    def _probe_entry(entry: Tuple[str, int]) -> Tuple[str, Dict[str, Any]]:
        name, port = entry
        is_up = _probe_loopback_port(port, timeout=timeout_s)
        return name, {"port": port, "up": is_up, "status": "UP" if is_up else "DOWN"}

    with ThreadPoolExecutor(max_workers=len(NEON_LOOPBACK_PORTS)) as pool:
        services = dict(pool.map(_probe_entry, NEON_LOOPBACK_PORTS))

    ports_up = {name: info["up"] for name, info in services.items()}
    return {
        "orchestrator": "neon",
        "host": "127.0.0.1",
        "probe_timeout_s": timeout_s,
        "services": services,
        "ports_up": ports_up,
        "up_count": sum(1 for up in ports_up.values() if up),
        "total_services": len(NEON_LOOPBACK_PORTS),
        "gemini_api_key_configured": bool(gemini_cloud_client.GeminiCloudClient().has_api_key()),
        "local_bedrock_enabled": not model_router.is_local_bedrock_disabled(),
    }


def run_self_test() -> int:
    """Deterministic offline self-test for 6-layer catalog, license gate, fit checks, Cascade & PoLL."""
    layers = {spec.layer for spec in ARMY_MODELS.values()}
    if layers != {0, 1, 2, 3, 4, 5}:
        print(f"[FAIL] carbon_orchestrator missing layers: {layers}")
        return 1
    if validate_commercial_license("Salesforce/xLAM-2-1b-fc-r")["allowed"]:
        print("[FAIL] non-commercial license gate leaked xLAM-2")
        return 1
    if validate_deployment_fit("gpt-oss-120b", "g4dn.12xlarge")["ok"]:
        print("[FAIL] T4 SM75 allowed MXFP4 gpt-oss-120b")
        return 1
    if validate_deployment_fit("qwen3-coder-480b", "r7g.16xlarge", use_ktransformers=True)["ok"]:
        print("[FAIL] ARM64 r7g.16xlarge allowed KTransformers")
        return 1
    if not validate_deployment_fit("qwen3-32b-awq", "g4dn.12xlarge")["ok"]:
        print("[FAIL] qwen3-32b-awq should fit g4dn.12xlarge")
        return 1
    if not validate_deployment_fit("qwen3-235b-a22b-rpc", "g4dn.xlarge", nodes=16, distributed_rpc=True)["ok"]:
        print("[FAIL] qwen3-235b-a22b-rpc should fit 16x g4dn.xlarge distributed_256gb RPC")
        return 1
    if validate_deployment_fit("qwen3-235b-a22b-rpc", "g4dn.xlarge", nodes=17, distributed_rpc=True)["ok"]:
        print("[FAIL] 17x g4dn.xlarge (68 vCPU) must fail L-3819A6DF 64 vCPU quota")
        return 1

    gate_check = CascadeRouter.evaluate_execution_gate(
        '{"ok": true}',
        ExecutionGateSpec(json_schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}),
    )
    if not gate_check["passed"]:
        print(f"[FAIL] ExecutionGateSpec valid JSON rejected: {gate_check}")
        return 1

    m7g_cfg = generate_m7g_litellm_config()
    if m7g_cfg["resident_weights_gb"] >= M7G_SAFE_WEIGHT_CEILING_GIB:
        print(f"[FAIL] m7g resident footprint too high: {m7g_cfg['resident_weights_gb']}")
        return 1

    decision = CascadeRouter().route_task("Review cross-repo VectorCube concurrency race", task_type="code_review")
    if decision.initial_model != "qwen3.5-35b-a3b" or "claude-opus-5-5" not in decision.escalation_chain:
        print(f"[FAIL] unexpected cascade decision: {decision}")
        return 1

    prov = build_provision_plan()
    if prov["aws_mutated"] is not False or not prov["requires_explicit_user_approval"]:
        print("[FAIL] provision plan must stay dry-run non-mutating")
        return 1

    if not {"phase_a_planner", "phase_b_executor", "phase_c_verifier"}.issubset(NEON_TWO_PHASE_PLAYBOOK.keys()):
        print("[FAIL] NEON_TWO_PHASE_PLAYBOOK missing required phase definitions")
        return 1

    corp_neon = build_neon_phase_plan(
        "Review VectorCube concurrency race",
        task_type="code_review",
        data_classification="corporate_proprietary",
    )
    if (
        corp_neon["orchestrator"] != "neon"
        or corp_neon["gemini_exposure"] is not False
        or corp_neon["cascade_decision"]["decision_gate_model"] is not None
        or corp_neon["cascade_decision"]["service_tier"] is not None
        or corp_neon["phase_b_executor"]["primary_model"] != "qwen3.5-35b-a3b"
    ):
        print(f"[FAIL] corporate neon phase plan unexpected: {corp_neon}")
        return 1

    class _OfflineDecisionStub:
        def has_api_key(self) -> bool:
            return True

        def decide_choice(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
            return {"answers": {"task_type": {"type": "choice", "choice": "code_review", "confidence": 0.95}}}

        def decide_noul(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
            return {"answers": {"frontier_needed": {"type": "noul", "noul": 0.85, "confidence": 0.92}}}

    pub_neon = build_neon_phase_plan(
        "Review generic lock-free queue algorithm",
        task_type="code_review",
        data_classification="public_sanitized",
        gemini_client=_OfflineDecisionStub(),  # type: ignore[arg-type]
    )
    if (
        pub_neon["gemini_exposure"] is not True
        or pub_neon["cascade_decision"]["decision_gate_model"] != "gemini-decision-1"
        or pub_neon["cascade_decision"]["service_tier"] != "flex"
        or pub_neon["cascade_decision"]["initial_model"] != "gemini-4-hydrogen"
        or "gemini-decision-1" not in pub_neon["phase_b_executor"]["recommended_offload_models"]
    ):
        print(f"[FAIL] public_sanitized neon phase plan unexpected: {pub_neon}")
        return 1

    print("[PASS] carbon_orchestrator -- 6-layer catalog, license gate, SM75/ARM64 guards, Cascade, Neon & PoLL verified")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kaizen-kit carbon", description="Carbon Open-Source LLM Army Orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_cat = sub.add_parser("catalog", help="List verified 6-layer Army models")
    p_cat.add_argument("--layer", type=int, default=-1)
    p_cat.add_argument("--json", action="store_true", dest="as_json")

    p_route = sub.add_parser("route", help="Preview CascadeRouter decision for a prompt")
    p_route.add_argument("--prompt", required=True)
    p_route.add_argument("--task-type", default="auto")
    p_route.add_argument("--context-tokens", type=int, default=0)
    p_route.add_argument("--allow-gpu-burst", action="store_true")
    p_route.add_argument("--data-classification", default="corporate_proprietary")
    p_route.add_argument("--prefer-gemini-flex", action="store_true")
    p_route.add_argument("--json", action="store_true", dest="as_json")

    p_neon_plan = sub.add_parser("neon-plan", help="Build Neon Two-Phase (Planner/Executor/Verifier) plan")
    p_neon_plan.add_argument("--prompt", required=True)
    p_neon_plan.add_argument("--task-type", default="auto")
    p_neon_plan.add_argument("--data-classification", default="corporate_proprietary")
    p_neon_plan.add_argument("--context-tokens", type=int, default=0)
    p_neon_plan.add_argument("--allow-gpu-burst", action="store_true")
    p_neon_plan.add_argument("--json", action="store_true", dest="as_json")

    p_night = sub.add_parser(
        "night-shift",
        help="Plan, submit, or collect overnight Neon Night-Shift (EC2 AutoDream + Gemini Decision-1/Batch/Flex)",
    )
    p_night.add_argument("--tasks-file", default="", help="Optional JSON file of NightShiftTask definitions")
    p_night.add_argument("--execute", action="store_true", help="Execute live calls when GEMINI_API_KEY is configured")
    p_night.add_argument("--morning", action="store_true", help="Collect morning report and check Batch jobs once")
    p_night.add_argument("--no-cache", action="store_true", help="Disable Gemini Context Caching on Batch lane")
    p_night.add_argument("--shared-rubric", default="", help="Optional shared public rubric text for Context Caching")
    p_night.add_argument("--json", action="store_true", dest="as_json")

    p_status = sub.add_parser("status", help="Probe Neon loopback ports and safe capability flags")
    p_status.add_argument("--json", action="store_true", dest="as_json")

    p_jury = sub.add_parser("jury", help="Preview or run PoLL multi-family code review jury")
    p_jury.add_argument("--code", required=True)
    p_jury.add_argument("--dry-run", action="store_true")
    p_jury.add_argument("--json", action="store_true", dest="as_json")

    p_fit = sub.add_parser("validate-fit", help="Validate hardware & DE-005/DE-006 fit")
    p_fit.add_argument("--model", required=True)
    p_fit.add_argument("--instance", required=True)
    p_fit.add_argument("--nodes", type=int, default=1)
    p_fit.add_argument("--distributed-rpc", action="store_true")
    p_fit.add_argument("--ktransformers", action="store_true")
    p_fit.add_argument("--json", action="store_true", dest="as_json")

    p_bon = sub.add_parser("best-of-n", help="Plan or run Execution-Verified Best-of-N cascade")
    p_bon.add_argument("--prompt", required=True)
    p_bon.add_argument("--n", type=int, default=3)
    p_bon.add_argument("--verify-cmd", default="")
    p_bon.add_argument("--data-classification", default="corporate_proprietary")
    p_bon.add_argument("--dry-run", action="store_true")
    p_bon.add_argument("--json", action="store_true", dest="as_json")

    p_lite = sub.add_parser("litellm-config", help="Generate m7g.8xlarge LiteLLM config & RAM budget")
    p_lite.add_argument("--json", action="store_true", dest="as_json")

    p_prov = sub.add_parser("provision-plan", help="Generate non-mutating Phase 2/3 rollout blueprint")
    p_prov.add_argument("--region", default=gpu_burst_orchestrator.DEFAULT_REGION)
    p_prov.add_argument("--profile", default=gpu_burst_orchestrator.DEFAULT_PROFILE)
    p_prov.add_argument("--json", action="store_true", dest="as_json")

    sub.add_parser("self-test", help="Run deterministic offline self-test")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    if args.cmd == "self-test":
        return run_self_test()
    if args.cmd == "catalog":
        items = [asdict(m) for m in ARMY_MODELS.values() if args.layer < 0 or m.layer == args.layer]
        print(json.dumps({"count": len(items), "models": items}, indent=2))
        return 0
    if args.cmd == "route":
        dec = CascadeRouter().route_task(
            args.prompt,
            task_type=args.task_type,
            context_tokens=args.context_tokens,
            allow_gpu_burst=args.allow_gpu_burst,
            data_classification=args.data_classification,
            prefer_gemini_flex=args.prefer_gemini_flex,
        )
        print(json.dumps(asdict(dec), indent=2))
        return 0
    if args.cmd == "neon-plan":
        try:
            plan = build_neon_phase_plan(
                args.prompt,
                task_type=args.task_type,
                data_classification=args.data_classification,
                context_tokens=args.context_tokens,
                allow_gpu_burst=args.allow_gpu_burst,
            )
        except PermissionError as exc:
            err_payload = {
                "ok": False,
                "blocked": True,
                "data_classification": args.data_classification,
                "error": str(exc),
            }
            print(json.dumps(err_payload, indent=2))
            return 1
        print(json.dumps(plan, indent=2))
        return 0
    if args.cmd == "night-shift":
        if args.morning:
            report = neon_night_shift.collect_morning_report(execute=True)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report.get("ok") else 1
        tasks = neon_night_shift.load_tasks_from_file(args.tasks_file) if args.tasks_file else None
        result = neon_night_shift.run_night_shift(
            tasks,
            execute=args.execute,
            use_context_cache=not args.no_cache,
            shared_rubric=args.shared_rubric,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "status":
        status = get_neon_live_status()
        if args.as_json:
            print(json.dumps(status, indent=2))
        else:
            lines = [
                f"Neon Status: {status['up_count']}/{status['total_services']} loopback services UP",
                f"  gemini_api_key_configured: {status['gemini_api_key_configured']}",
                f"  local_bedrock_enabled    : {status['local_bedrock_enabled']}",
            ]
            for name, info in status["services"].items():
                lines.append(f"  {name:12s} (:{info['port']}): {info['status']}")
            print("\n".join(lines))
        return 0
    if args.cmd == "jury":
        res = run_poll_review_jury(args.code, dry_run=args.dry_run)
        print(json.dumps(res, indent=2))
        return 0
    if args.cmd == "validate-fit":
        res = validate_deployment_fit(
            args.model,
            args.instance,
            use_ktransformers=args.ktransformers,
            nodes=args.nodes,
            distributed_rpc=args.distributed_rpc,
        )
        print(json.dumps(res, indent=2))
        return 0 if res["ok"] else 1
    if args.cmd == "best-of-n":
        bon_res = CascadeRouter().execute_best_of_n(
            args.prompt,
            n=args.n,
            verify_cmd=args.verify_cmd,
            data_classification=args.data_classification,
            dry_run=args.dry_run,
        )
        print(json.dumps(bon_res, indent=2, ensure_ascii=False))
        return 0 if bon_res["status"] in ("OK", "DRY_RUN") else 1
    if args.cmd == "litellm-config":
        cfg = generate_m7g_litellm_config()
        print(json.dumps(cfg, indent=2) if args.as_json else cfg["litellm_yaml"])
        return 0
    if args.cmd == "provision-plan":
        print(json.dumps(build_provision_plan(region=args.region, profile=args.profile), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
