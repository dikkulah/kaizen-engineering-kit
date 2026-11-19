#!/usr/bin/env python3
"""
Neon Night-Shift Orchestrator (`neon_night_shift.py`).

Builds and executes overnight unattended pipelines triggered by "gece yatiyorum" /
"night-shift" workflows with strict fail-closed data residency:
  - Lane 0 (`ec2_local`): Proprietary Kaizen code, telemetry & MIRIX AutoDream on
    EC2 `m7g.8xlarge` (`:8770` FastMCP / `:4000` LiteLLM), zero Spot GPU ($0 overnight).
  - Lane 1 (`gemini_decision_1`): 0-output-token Decision-1 screening & checklist gate
    for `public_sanitized` candidates before any generative spend.
  - Lane 2 (`gemini_batch`): Async 50%-discounted JSONL/inline batch jobs (stackable
    with Context Caching via `cachedContents`) — fire-and-forget, zero polling loops.
  - Lane 3 (`gemini_flex`): Sync 50%-discounted `serviceTier="flex"` (1-15 min SLA)
    for sequential `public_sanitized` research & multi-step synthesis.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
import urllib.error
import urllib.request

try:
    from kaizen_kit import gemini_cloud_client, paths
except ImportError:
    import gemini_cloud_client  # type: ignore[no-redef]
    import paths  # type: ignore[no-redef]

_LOG = logging.getLogger("kaizen_kit.neon_night_shift")

ALLOWED_CLASSIFICATION = gemini_cloud_client.ALLOWED_DATA_CLASSIFICATION
VALID_MODES = (
    "ec2_local",
    "decision",
    "batch",
    "flex",
    "openai_batch",
    "anthropic_batch",
    "gemini_batch",
    "gemini_flex",
    "best_of_n",
    "lora_synth",
)
DEFAULT_EC2_MODEL = "ling-mini-2.0"
DEFAULT_DECISION_MODEL = gemini_cloud_client.DEFAULT_DECISION_MODEL
DEFAULT_GEMINI_MODEL = gemini_cloud_client.DEFAULT_GENERATE_MODEL
DEFAULT_NEON_GEMINI_MODEL = "gemini-4-neon"
DEFAULT_AUTODREAM_URL = os.environ.get("KAIZEN_FASTMCP_AUTODREAM_URL", "http://127.0.0.1:8770/mirix/autodream")
DEFAULT_CACHE_TTL = "86400s"
STATE_FILENAME = "night_shift_state.json"
REPORT_FILENAME = "night_shift_morning_report.md"
LORA_SFT_DPO_FILENAME = "night_shift_lora_sft_dpo.jsonl"
_EFFORT_MAP = {
    "minimal": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "xhigh": "XHIGH",
}


def _normalize_thinking_level(raw_level: Optional[str]) -> Optional[str]:
    if raw_level is None:
        return None
    cleaned = raw_level.strip().lower()
    if not cleaned:
        return None
    if cleaned not in _EFFORT_MAP:
        raise ValueError(f"Unsupported thinking_level/effort '{raw_level}'. Valid: {tuple(_EFFORT_MAP.keys())}")
    return _EFFORT_MAP[cleaned]


@dataclass(frozen=True)
class NightShiftTask:
    """Single unit of overnight work with explicit fail-closed classification."""

    task_id: str
    title: str
    prompt: str
    mode: str = "ec2_local"
    provider: str = ""
    data_classification: str = "corporate_proprietary"
    model: str = ""
    system_instruction: str = ""
    sequential: bool = False
    questions: Optional[Dict[str, Dict[str, Any]]] = None
    checklist_rules: Optional[List[str]] = None
    decision_gate_instructions: str = ""
    thinking_budget: Optional[int] = None
    thinking_level: Optional[str] = None
    max_output_tokens: Optional[int] = None
    response_json_schema: Optional[Dict[str, Any]] = None
    best_of_n: int = 1
    verify_cmd: str = ""
    lora_synthesis: bool = False
    lora_adapter: str = ""
    allow_distributed_256gb: bool = False

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id must not be empty.")
        if not self.prompt.strip():
            raise ValueError(f"Task '{self.task_id}' prompt must not be empty.")
        norm_mode = (self.mode or "ec2_local").strip().lower()
        if norm_mode not in VALID_MODES:
            raise ValueError(f"Unsupported task mode '{self.mode}'. Valid: {VALID_MODES}")
        if int(self.best_of_n) < 1:
            raise ValueError(f"Task '{self.task_id}' best_of_n must be >= 1.")
        object.__setattr__(self, "mode", norm_mode)
        object.__setattr__(self, "provider", (self.provider or "").strip().lower())
        object.__setattr__(self, "best_of_n", int(self.best_of_n))
        object.__setattr__(self, "thinking_level", _normalize_thinking_level(self.thinking_level))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], index: int = 1) -> "NightShiftTask":
        """Construct a NightShiftTask from a JSON-compatible dictionary."""
        raw_q = raw.get("questions")
        questions = {str(k): dict(v) for k, v in raw_q.items()} if isinstance(raw_q, Mapping) else None
        raw_rules = raw.get("checklist_rules")
        rules = [str(x) for x in raw_rules] if isinstance(raw_rules, (list, tuple)) else None
        raw_schema = raw.get("response_json_schema")
        schema = dict(raw_schema) if isinstance(raw_schema, Mapping) else None
        tb = raw.get("thinking_budget")
        mot = raw.get("max_output_tokens")
        raw_tl = raw.get("thinking_level") if raw.get("thinking_level") is not None else raw.get("effort")
        return cls(
            task_id=str(raw.get("task_id") or f"task_{index:03d}"),
            title=str(raw.get("title") or raw.get("task_id") or f"Night Task {index}"),
            prompt=str(raw.get("prompt") or ""),
            mode=str(raw.get("mode") or "ec2_local"),
            provider=str(raw.get("provider") or ""),
            data_classification=str(raw.get("data_classification") or "corporate_proprietary"),
            model=str(raw.get("model") or ""),
            system_instruction=str(raw.get("system_instruction") or ""),
            sequential=bool(raw.get("sequential", False)),
            questions=questions,
            checklist_rules=rules,
            decision_gate_instructions=str(raw.get("decision_gate_instructions") or ""),
            thinking_budget=int(tb) if tb is not None else None,
            thinking_level=str(raw_tl) if raw_tl is not None else None,
            max_output_tokens=int(mot) if mot is not None else None,
            response_json_schema=schema,
            best_of_n=int(raw.get("best_of_n") or 1),
            verify_cmd=str(raw.get("verify_cmd") or ""),
            lora_synthesis=bool(raw.get("lora_synthesis", False)),
            lora_adapter=str(raw.get("lora_adapter") or ""),
            allow_distributed_256gb=bool(raw.get("allow_distributed_256gb", False)),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize task into a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class NightShiftLanePlan:
    """Execution specification for one Night-Shift lane."""

    lane: str
    backend: str
    discount_profile: str
    sla: str
    tasks: Tuple[NightShiftTask, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize lane plan into a JSON-compatible dictionary."""
        return {
            "lane": self.lane,
            "backend": self.backend,
            "discount_profile": self.discount_profile,
            "sla": self.sla,
            "task_count": len(self.tasks),
            "tasks": [t.to_dict() for t in self.tasks],
        }


def build_openai_batch_jsonl(tasks: Sequence[NightShiftTask], default_model: str = "gpt-5.4") -> str:
    """Build standard OpenAI Batch API JSONL lines (/v1/batches with /v1/chat/completions)."""
    lines: List[str] = []
    for task in tasks:
        messages: List[Dict[str, str]] = []
        if task.system_instruction:
            messages.append({"role": "system", "content": task.system_instruction})
        messages.append({"role": "user", "content": task.prompt})
        model = task.model.strip() or default_model
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if task.max_output_tokens is not None:
            body["max_tokens"] = task.max_output_tokens
        if task.response_json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "batch_output", "schema": task.response_json_schema, "strict": True},
            }
        line = {
            "custom_id": task.task_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": body,
        }
        lines.append(json.dumps(line, ensure_ascii=False))
    return "\n".join(lines)


def build_anthropic_batch_jsonl(tasks: Sequence[NightShiftTask], default_model: str = "claude-sonnet-5-5") -> str:
    """Build standard Anthropic Message Batches JSONL lines (/v1/messages/batches)."""
    lines: List[str] = []
    for task in tasks:
        messages = [{"role": "user", "content": task.prompt}]
        model = task.model.strip() or default_model
        params: Dict[str, Any] = {
            "model": model,
            "max_tokens": task.max_output_tokens or 4096,
            "messages": messages,
        }
        if task.system_instruction:
            params["system"] = task.system_instruction
        line = {
            "custom_id": task.task_id,
            "params": params,
        }
        lines.append(json.dumps(line, ensure_ascii=False))
    return "\n".join(lines)


@dataclass(frozen=True)
class NightShiftPlan:
    """Complete Night-Shift plan with quarantine, Lever D/QLoRA lanes & FinOps telemetry."""

    generated_at: str
    ec2_local: NightShiftLanePlan
    gemini_decision_1: NightShiftLanePlan
    gemini_batch: NightShiftLanePlan
    gemini_flex: NightShiftLanePlan
    openai_batch: Optional[NightShiftLanePlan] = None
    anthropic_batch: Optional[NightShiftLanePlan] = None
    best_of_n: Optional[NightShiftLanePlan] = None
    lora_synth: Optional[NightShiftLanePlan] = None
    quarantined_tasks: Tuple[Dict[str, str], ...] = ()
    context_cache_enabled: bool = False
    shared_rubric_chars: int = 0
    cache_blocked_reason: str = ""
    batch_jsonl_preview: Tuple[str, ...] = ()
    openai_batch_jsonl_preview: Tuple[str, ...] = ()
    anthropic_batch_jsonl_preview: Tuple[str, ...] = ()
    finops_guard: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize full plan into a JSON-compatible dictionary."""
        lanes: Dict[str, Any] = {
            "ec2_local": self.ec2_local.to_dict(),
            "gemini_decision_1": self.gemini_decision_1.to_dict(),
            "gemini_batch": self.gemini_batch.to_dict(),
            "gemini_flex": self.gemini_flex.to_dict(),
        }
        if self.openai_batch is not None:
            lanes["openai_batch"] = self.openai_batch.to_dict()
        if self.anthropic_batch is not None:
            lanes["anthropic_batch"] = self.anthropic_batch.to_dict()
        if self.best_of_n is not None:
            lanes["best_of_n"] = self.best_of_n.to_dict()
        if self.lora_synth is not None:
            lanes["lora_synth"] = self.lora_synth.to_dict()
        return {
            "orchestrator": "neon-night-shift",
            "generated_at": self.generated_at,
            "lanes": lanes,
            "quarantined_count": len(self.quarantined_tasks),
            "quarantined_tasks": list(self.quarantined_tasks),
            "context_cache_enabled": self.context_cache_enabled,
            "shared_rubric_chars": self.shared_rubric_chars,
            "cache_blocked_reason": self.cache_blocked_reason,
            "batch_jsonl_preview": list(self.batch_jsonl_preview),
            "openai_batch_jsonl_preview": list(self.openai_batch_jsonl_preview),
            "anthropic_batch_jsonl_preview": list(self.anthropic_batch_jsonl_preview),
            "finops_guard": dict(self.finops_guard),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_residency_text(task: NightShiftTask) -> str:
    return json.dumps(
        {
            "title": task.title,
            "prompt": task.prompt,
            "system_instruction": task.system_instruction,
            "questions": task.questions,
            "checklist_rules": task.checklist_rules,
            "decision_gate_instructions": task.decision_gate_instructions,
            "response_json_schema": task.response_json_schema,
            "verify_cmd": task.verify_cmd,
        },
        ensure_ascii=False,
    )


def _check_gemini_residency(task: NightShiftTask) -> Optional[str]:
    """Return None if sanitized for Gemini API, otherwise return a quarantine reason."""
    try:
        gemini_cloud_client.enforce_data_residency(
            _task_residency_text(task),
            data_classification=task.data_classification,
        )
        return None
    except PermissionError as exc:
        return str(exc)


def load_tasks_from_file(path: Union[str, Path]) -> List[NightShiftTask]:
    """Load NightShiftTask definitions from a JSON file (list or `{tasks: [...]}`)."""
    file_path = Path(path).expanduser().resolve()
    raw = json.loads(file_path.read_text(encoding="utf-8"))
    items = raw.get("tasks") if isinstance(raw, Mapping) else raw
    if not isinstance(items, list) or not items:
        raise ValueError(f"Tasks file '{file_path}' must contain a non-empty JSON task list.")
    return [NightShiftTask.from_mapping(item, idx) for idx, item in enumerate(items, start=1)]


def build_default_overnight_tasks() -> List[NightShiftTask]:
    """Return the default 4-lane Neon Night-Shift starter pack."""
    return [
        NightShiftTask(
            task_id="ec2_autodream_consolidation",
            title="MIRIX AutoDream Overnight Memory Consolidation",
            prompt="Consolidate unprocessed episodic engineering records into semantic and procedural nodes.",
            mode="ec2_local",
            data_classification="corporate_proprietary",
            model=DEFAULT_EC2_MODEL,
        ),
        NightShiftTask(
            task_id="public_decision_triage",
            title="Gemini Decision-1 Zero-Output Checklist Gate",
            prompt=(
                "Evaluate generic Rust and Python concurrency helper design: "
                "uses bounded channels, explicit Result error types, deterministic unit tests, "
                "and avoids blocking waits inside async executors."
            ),
            mode="decision",
            data_classification="public_sanitized",
            model=DEFAULT_DECISION_MODEL,
            checklist_rules=[
                "Avoids blocking sleep or unbounded polling loops in async code",
                "Enforces fail-closed error handling with explicit typed errors",
            ],
        ),
        NightShiftTask(
            task_id="public_batch_digest",
            title="Gemini Batch Overnight Public Research Digest",
            prompt=(
                "Summarize tradeoffs between KV-cache Q8_0 quantization, GQA memory bandwidth scaling, "
                "and sparse-MoE active parameter limits for ARM64 CPU inference in 5 concise bullet points."
            ),
            mode="batch",
            data_classification="public_sanitized",
            model=DEFAULT_GEMINI_MODEL,
            system_instruction="Respond concisely in technical engineering bullets with reproducible metrics.",
        ),
        NightShiftTask(
            task_id="public_flex_synthesis",
            title="Gemini Flex Sequential Architecture Synthesis",
            prompt=(
                "Synthesize a minimal Clean Architecture pattern combining a read-only planning phase, "
                "offload execution, and set-theoretic parity verification in Python."
            ),
            mode="flex",
            data_classification="public_sanitized",
            model=DEFAULT_GEMINI_MODEL,
            sequential=True,
            system_instruction="Return concise structured engineering guidance without filler.",
        ),
    ]


def _resolve_task_model(task: NightShiftTask, fallback: str) -> str:
    return task.model.strip() or fallback


def _resolve_adaptive_thinking(
    model: str,
    thinking_level: Optional[str],
    thinking_budget: Optional[int],
) -> Tuple[Optional[str], Optional[int]]:
    resolver = getattr(gemini_cloud_client, "resolve_adaptive_thinking", None)
    if callable(resolver):
        return resolver(model, effort_or_level=thinking_level, thinking_budget=thinking_budget)
    return thinking_level, thinking_budget


def _build_optional_distributed_256gb_plan(enabled: bool) -> Optional[Dict[str, Any]]:
    if not enabled:
        return None
    try:
        from kaizen_kit import gpu_burst_orchestrator
    except ImportError:
        import gpu_burst_orchestrator  # type: ignore[no-redef]
    tiers = getattr(gpu_burst_orchestrator, "GPU_BURST_TIERS", {})
    if "distributed_256gb" in tiers:
        return gpu_burst_orchestrator.plan_gpu_burst(
            tier="distributed_256gb",
            nodes=16,
            dry_run=True,
            execute_approved_u2=False,
            check_aws=False,
        )
    return {
        "tier": "distributed_256gb",
        "nodes": 16,
        "instance_type": "g4dn.xlarge",
        "total_vcpus": 64,
        "vram_gb": 256,
        "backend": "llama_cpp_rpc",
        "default_state": "STOPPED",
        "dry_run": True,
        "execute_approved_u2": False,
    }


def _partition_lanes(tasks: Sequence[NightShiftTask]) -> Tuple[Dict[str, List[NightShiftTask]], List[Dict[str, str]]]:
    buckets: Dict[str, List[NightShiftTask]] = {
        "ec2_local": [],
        "decision": [],
        "batch": [],
        "flex": [],
        "openai_batch": [],
        "anthropic_batch": [],
        "best_of_n": [],
        "lora_synth": [],
    }
    quarantined: List[Dict[str, str]] = []
    for task in tasks:
        if task.mode == "ec2_local":
            buckets["ec2_local"].append(task)
            continue
        if task.mode in ("best_of_n", "lora_synth"):
            if task.model.strip().lower().startswith("gemini-"):
                block_reason = _check_gemini_residency(task)
                if block_reason is not None:
                    quarantined.append({"task_id": task.task_id, "requested_mode": task.mode, "reason": block_reason})
                    buckets[task.mode].append(
                        NightShiftTask(
                            task_id=task.task_id,
                            title=task.title,
                            prompt=task.prompt,
                            mode=task.mode,
                            provider=task.provider,
                            data_classification="corporate_proprietary",
                            model=DEFAULT_EC2_MODEL,
                            system_instruction=task.system_instruction,
                            sequential=task.sequential,
                            best_of_n=task.best_of_n,
                            verify_cmd=task.verify_cmd,
                            lora_synthesis=task.lora_synthesis,
                            lora_adapter=task.lora_adapter,
                            allow_distributed_256gb=task.allow_distributed_256gb,
                        )
                    )
                    continue
            buckets[task.mode].append(task)
            continue

        # Check explicit or model-inferred OpenAI / Anthropic batch mode
        is_openai = (
            task.mode == "openai_batch"
            or (task.mode == "batch" and (
                getattr(task, "provider", "").lower() == "openai"
                or task.model.strip().lower().startswith(("gpt-", "o1", "o3", "o4", "codex"))
            ))
        )
        if is_openai:
            buckets["openai_batch"].append(task)
            continue

        is_anthropic = (
            task.mode == "anthropic_batch"
            or (task.mode == "batch" and (
                getattr(task, "provider", "").lower() == "anthropic"
                or task.model.strip().lower().startswith(("claude-",))
            ))
        )
        if is_anthropic:
            buckets["anthropic_batch"].append(task)
            continue

        block_reason = _check_gemini_residency(task)
        if block_reason is None:
            buckets[task.mode].append(task)
            continue
        quarantined.append({"task_id": task.task_id, "requested_mode": task.mode, "reason": block_reason})
        buckets["ec2_local"].append(
            NightShiftTask(
                task_id=task.task_id,
                title=task.title,
                prompt=task.prompt,
                mode="ec2_local",
                provider=task.provider,
                data_classification="corporate_proprietary",
                model=task.model if task.model and not task.model.startswith("gemini-") else DEFAULT_EC2_MODEL,
                system_instruction=task.system_instruction,
                sequential=task.sequential,
            )
        )
    return buckets, quarantined


def _check_shared_rubric_cache(use_context_cache: bool, shared_rubric: str) -> Tuple[bool, str]:
    rubric = shared_rubric.strip()
    if not use_context_cache or not rubric:
        return False, ""
    try:
        gemini_cloud_client.enforce_data_residency(rubric, data_classification=ALLOWED_CLASSIFICATION)
        return True, ""
    except PermissionError as exc:
        return False, str(exc)


def _build_batch_items_for_client(
    batch_tasks: Sequence[NightShiftTask],
    *,
    cached_content: Optional[str] = None,
    default_model: str = DEFAULT_GEMINI_MODEL,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for task in batch_tasks:
        entry: Dict[str, Any] = {"key": task.task_id, "contents": task.prompt}
        if cached_content:
            entry["cached_content"] = cached_content
        elif task.system_instruction:
            entry["system_instruction"] = task.system_instruction
        model_name = _resolve_task_model(task, default_model)
        tl, tb = _resolve_adaptive_thinking(model_name, task.thinking_level, task.thinking_budget)
        if tl is not None:
            entry["thinking_level"] = tl
        if tb is not None:
            entry["thinking_budget"] = tb
        if task.max_output_tokens is not None:
            entry["max_output_tokens"] = task.max_output_tokens
        if task.response_json_schema is not None:
            entry["response_json_schema"] = dict(task.response_json_schema)
        items.append(entry)
    return items


def _supports_explicit_cache(model: str) -> bool:
    norm_fn = getattr(gemini_cloud_client, "normalize_gemini_model", None)
    resolved = norm_fn(model)[0] if callable(norm_fn) else model
    spec = getattr(gemini_cloud_client, "GEMINI_MODELS", {}).get(resolved)
    if spec is None:
        return not str(resolved).startswith("gemini-4")
    return bool(getattr(spec, "supports_caching", False)) and getattr(spec, "family", "") != "gemini-4"


def plan_night_shift(
    tasks: Optional[Sequence[NightShiftTask]] = None,
    *,
    use_context_cache: bool = True,
    shared_rubric: str = "",
    allow_distributed_256gb: bool = False,
) -> Dict[str, Any]:
    """Build a deterministic Night-Shift plan honoring DataResidencyGate and FinOps guards."""
    active_tasks = list(tasks) if tasks is not None else build_default_overnight_tasks()
    buckets, quarantined = _partition_lanes(active_tasks)
    cache_ready, cache_blocked_reason = _check_shared_rubric_cache(use_context_cache, shared_rubric)
    dist_enabled = bool(allow_distributed_256gb or any(t.allow_distributed_256gb for t in active_tasks))
    dist_plan = _build_optional_distributed_256gb_plan(dist_enabled)

    batch_preview: Tuple[str, ...] = ()
    explicit_cache_used = False
    if buckets["batch"]:
        helper_client = gemini_cloud_client.GeminiCloudClient(api_key="")
        batch_model = _resolve_task_model(buckets["batch"][0], DEFAULT_GEMINI_MODEL)
        explicit_cache_used = cache_ready and _supports_explicit_cache(batch_model)
        cached_ref = "cachedContents/dry-run-plan" if explicit_cache_used else None
        batch_items = _build_batch_items_for_client(
            buckets["batch"],
            cached_content=cached_ref,
            default_model=batch_model,
        )
        jsonl_text = helper_client.build_batch_jsonl(
            batch_items,
            cached_content=cached_ref,
            system_instruction=None if cached_ref else (shared_rubric.strip() if cache_ready else None),
            data_classification=ALLOWED_CLASSIFICATION,
        )
        batch_preview = tuple(jsonl_text.splitlines())

    openai_preview: Tuple[str, ...] = ()
    if buckets["openai_batch"]:
        openai_jsonl = build_openai_batch_jsonl(buckets["openai_batch"])
        openai_preview = tuple(openai_jsonl.splitlines()[:10])

    anthropic_preview: Tuple[str, ...] = ()
    if buckets["anthropic_batch"]:
        anthropic_jsonl = build_anthropic_batch_jsonl(buckets["anthropic_batch"])
        anthropic_preview = tuple(anthropic_jsonl.splitlines()[:10])

    plan = NightShiftPlan(
        generated_at=_utc_now(),
        ec2_local=NightShiftLanePlan(
            lane="ec2_local",
            backend="ec2_m7g_8xlarge (:8770 AutoDream / :4000 4-Worker MoE)",
            discount_profile="Fixed EC2 Brain compute (marginal token $0, overnight GPU Spot = $0)",
            sla="Immediate / <5s local AutoDream",
            tasks=tuple(buckets["ec2_local"]),
        ),
        gemini_decision_1=NightShiftLanePlan(
            lane="gemini_decision_1",
            backend="POST /v1beta/decisions (gemini-decision-1)",
            discount_profile="0 output tokens (input-only structured gating before generation)",
            sla="1-3s synchronous screening",
            tasks=tuple(buckets["decision"]),
        ),
        gemini_batch=NightShiftLanePlan(
            lane="gemini_batch",
            backend="POST /v1beta/models/{model}:batchGenerateContent",
            discount_profile="50% vs Standard + Context Cache input discount stackable",
            sla="Async <= 24h turnaround (zero polling loops)",
            tasks=tuple(buckets["batch"]),
        ),
        gemini_flex=NightShiftLanePlan(
            lane="gemini_flex",
            backend='POST /v1beta/models/{model}:generateContent (serviceTier="flex")',
            discount_profile="50% vs Standard (sync sheddable execution)",
            sla="1-15 min synchronous chain SLA",
            tasks=tuple(buckets["flex"]),
        ),
        openai_batch=NightShiftLanePlan(
            lane="openai_batch",
            backend="POST https://api.openai.com/v1/batches",
            discount_profile="50% vs Standard token costs (GPT-5.4 / GPT-5 / o3)",
            sla="Async <= 24h turnaround SLA",
            tasks=tuple(buckets["openai_batch"]),
        ),
        anthropic_batch=NightShiftLanePlan(
            lane="anthropic_batch",
            backend="POST https://api.anthropic.com/v1/messages/batches",
            discount_profile="50% vs Standard token costs (Claude Sonnet 5.5 / Opus 5.5)",
            sla="Async <= 24h turnaround SLA",
            tasks=tuple(buckets["anthropic_batch"]),
        ),
        best_of_n=NightShiftLanePlan(
            lane="best_of_n",
            backend="CascadeRouter.execute_best_of_n (EC2 MoE :4000 + ExecutionGateSpec)",
            discount_profile="Execution-verified Best-of-N with SFT/DPO rejection sampling ($0 GPU idle)",
            sla="Local EC2 / deterministic verifier (<60s/task)",
            tasks=tuple(buckets["best_of_n"]),
        ),
        lora_synth=NightShiftLanePlan(
            lane="lora_synth",
            backend="QLoRA SFT/DPO Dataset Synthesis (eu-west-1 corporate boundary)",
            discount_profile="Dry-run GPU Burst / m7g offline JSONL synthesis ($0 idle Spot GPU)",
            sla="Overnight JSONL export + dry-run QLoRA manifest",
            tasks=tuple(buckets["lora_synth"]),
        ),
        quarantined_tasks=tuple(quarantined),
        context_cache_enabled=explicit_cache_used and bool(buckets["batch"]),
        shared_rubric_chars=len(shared_rubric.strip()),
        cache_blocked_reason=cache_blocked_reason,
        batch_jsonl_preview=batch_preview,
        openai_batch_jsonl_preview=openai_preview,
        anthropic_batch_jsonl_preview=anthropic_preview,
        finops_guard={
            "gpu_spot_expected_running": 0,
            "gpu_spot_idle_cost_usd": 0.0,
            "overnight_polling_allowed": False,
            "residency_policy": "fail_closed_public_sanitized_only_for_gemini",
            "distributed_256gb_enabled": dist_enabled,
            "distributed_256gb_plan": dist_plan,
            "rough_estimate_note": "AWS & Gemini Pricing rough estimate (tahmini kaba hesap)",
        },
    )
    return plan.to_dict()


def _single_answer_passed(ans: Any, threshold: float = 0.5) -> bool:
    if isinstance(ans, bool):
        return ans
    if not isinstance(ans, Mapping):
        return True
    q_type = str(ans.get("type") or "").lower()
    if q_type == "noul" or "noul" in ans:
        raw = ans.get("noul")
        return bool(raw) if isinstance(raw, bool) else float(raw or 0.0) >= threshold
    if q_type == "score" or "score" in ans:
        return float(ans.get("score") or 0.0) >= 1.0
    if q_type == "choice" or "choice" in ans:
        choice_val = str(ans.get("choice") or "").strip().lower()
        return choice_val not in {"reject", "skip", "block", "fail", "no", "false"}
    return True


def _is_decision_passed(resp: Mapping[str, Any], threshold: float = 0.5) -> Tuple[bool, Dict[str, Any]]:
    """Evaluate Decision-1 response answers conservatively across noul, score, and choice types."""
    answers = resp.get("answers")
    if not isinstance(answers, Mapping) or not answers:
        return True, {"passed": True, "reason": "no_answers_field"}
    failed = [q_id for q_id, item in answers.items() if not _single_answer_passed(item, threshold=threshold)]
    return len(failed) == 0, {"passed": len(failed) == 0, "failed_questions": failed, "answer_count": len(answers)}


def _exec_decision_task(
    client: gemini_cloud_client.GeminiCloudClient,
    task: NightShiftTask,
    *,
    dry_run: bool,
) -> Dict[str, Any]:
    model = _resolve_task_model(task, DEFAULT_DECISION_MODEL)
    if task.questions:
        resp = client.decide(
            task.prompt,
            task.questions,
            model=model,
            data_classification=ALLOWED_CLASSIFICATION,
            dry_run=dry_run,
        )
    elif task.checklist_rules:
        resp = client.decide_code_review_checklist(
            task.prompt,
            task.checklist_rules,
            model=model,
            data_classification=ALLOWED_CLASSIFICATION,
            dry_run=dry_run,
        )
    else:
        resp = client.decide_noul(
            task.prompt,
            task.decision_gate_instructions or f"Should task '{task.title}' proceed overnight?",
            model=model,
            data_classification=ALLOWED_CLASSIFICATION,
            dry_run=dry_run,
        )
    passed, verdict = (True, {"passed": True, "dry_run": True}) if dry_run else _is_decision_passed(resp)
    return {"task_id": task.task_id, "passed": passed, "verdict": verdict, "response": resp}


def _apply_decision_gates(
    client: gemini_cloud_client.GeminiCloudClient,
    tasks: Sequence[NightShiftTask],
    *,
    dry_run: bool,
) -> Tuple[List[NightShiftTask], List[Dict[str, Any]], List[Dict[str, Any]]]:
    kept: List[NightShiftTask] = []
    gate_checks: List[Dict[str, Any]] = []
    filtered_out: List[Dict[str, Any]] = []
    for task in tasks:
        if not task.decision_gate_instructions.strip():
            kept.append(task)
            continue
        resp = client.decide_noul(
            task.prompt,
            task.decision_gate_instructions,
            question_id=f"gate_{task.task_id}",
            model=DEFAULT_DECISION_MODEL,
            data_classification=ALLOWED_CLASSIFICATION,
            dry_run=dry_run,
        )
        passed, verdict = (True, {"passed": True, "dry_run": True}) if dry_run else _is_decision_passed(resp)
        record = {"task_id": task.task_id, "mode": task.mode, "passed": passed, "verdict": verdict, "response": resp}
        gate_checks.append(record)
        if passed:
            kept.append(task)
        else:
            filtered_out.append(record)
    return kept, gate_checks, filtered_out


def _create_batch_cache(
    client: gemini_cloud_client.GeminiCloudClient,
    model: str,
    shared_rubric: str,
    *,
    dry_run: bool,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    if not shared_rubric.strip():
        return None, None, ""
    try:
        cache_resp = client.create_cache(
            model=model,
            system_instruction=shared_rubric,
            ttl=DEFAULT_CACHE_TTL,
            display_name="neon-night-shift-rubric",
            data_classification=ALLOWED_CLASSIFICATION,
            dry_run=dry_run,
        )
        if dry_run:
            return "cachedContents/dry-run-plan", cache_resp, ""
        name = str(cache_resp.get("name") or "") or None
        return name, cache_resp, ""
    except (RuntimeError, ValueError) as exc:
        _LOG.warning("Night-Shift Context Cache fallback to implicit/non-cached batch rubric: %s", exc)
        return None, None, str(exc)


def _submit_batch_lane(
    client: gemini_cloud_client.GeminiCloudClient,
    batch_tasks: Sequence[NightShiftTask],
    *,
    use_context_cache: bool,
    shared_rubric: str,
    dry_run: bool,
) -> Dict[str, Any]:
    if not batch_tasks:
        return {"submitted": False, "batch_job_names": [], "jobs": []}

    cache_allowed, cache_blocked_reason = _check_shared_rubric_cache(use_context_cache, shared_rubric)
    model = _resolve_task_model(batch_tasks[0], DEFAULT_GEMINI_MODEL)
    cache_name: Optional[str] = None
    cache_resp: Optional[Dict[str, Any]] = None
    cache_fallback = cache_blocked_reason

    if cache_allowed:
        cache_name, cache_resp, fallback_err = _create_batch_cache(client, model, shared_rubric, dry_run=dry_run)
        if fallback_err:
            cache_fallback = fallback_err

    items = _build_batch_items_for_client(batch_tasks, cached_content=cache_name, default_model=model)
    display_name = f"neon-night-shift-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"
    jsonl_text = client.build_batch_jsonl(
        items,
        cached_content=cache_name,
        system_instruction=None if cache_name else (shared_rubric.strip() if cache_allowed else None),
        data_classification=ALLOWED_CLASSIFICATION,
    )
    inline_bytes = len(jsonl_text.encode("utf-8"))
    if inline_bytes < gemini_cloud_client.MAX_INLINE_BATCH_BYTES:
        resp = client.submit_batch_inline(
            model=model,
            requests=items,
            display_name=display_name,
            data_classification=ALLOWED_CLASSIFICATION,
            dry_run=dry_run,
        )
        transport = "inline"
    else:
        up = client.upload_jsonl_file(
            jsonl_text,
            display_name=display_name,
            data_classification=ALLOWED_CLASSIFICATION,
            dry_run=dry_run,
        )
        file_uri = str((up.get("file") or {}).get("name") or up.get("name") or "files/dry-run-batch")
        resp = client.submit_batch_file(
            model=model,
            file_uri=file_uri,
            display_name=display_name,
            data_classification=ALLOWED_CLASSIFICATION,
            dry_run=dry_run,
        )
        transport = "files_api"

    job_name = str(resp.get("name") or "")
    return {
        "submitted": True,
        "transport": transport,
        "model": model,
        "cache_name": cache_name,
        "cache_response": cache_resp,
        "cache_fallback_reason": cache_fallback,
        "batch_job_names": [job_name] if job_name else [],
        "jobs": [resp],
    }


def _extract_generate_text(resp: Mapping[str, Any]) -> str:
    candidates = resp.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
    return "\n".join(str(p.get("text") or "") for p in parts if isinstance(p, Mapping) and p.get("text"))


def _trigger_ec2_autodream(
    local_tasks: Sequence[NightShiftTask],
    *,
    dry_run: bool,
    autodream_url: str,
) -> Dict[str, Any]:
    task_ids = [t.task_id for t in local_tasks]
    if not local_tasks:
        return {"triggered": False, "reason": "no_ec2_local_tasks", "task_ids": []}
    if dry_run:
        return {
            "triggered": False,
            "dry_run": True,
            "autodream_url": autodream_url,
            "task_ids": task_ids,
        }
    req = urllib.request.Request(
        autodream_url,
        data=json.dumps({"max_records": 30}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return {"triggered": True, "autodream_url": autodream_url, "task_ids": task_ids, "response": body}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        _LOG.info("EC2 AutoDream trigger deferred (%s): %s", autodream_url, exc)
        return {
            "triggered": False,
            "autodream_url": autodream_url,
            "task_ids": task_ids,
            "error": str(exc),
        }


def _resolve_lora_adapter_id(raw_adapter: str) -> str:
    candidate = (raw_adapter or "").strip() or "lora-rust-vector-parity"
    try:
        from kaizen_kit import gpu_burst_orchestrator
    except ImportError:
        import gpu_burst_orchestrator  # type: ignore[no-redef]
    resolver = getattr(gpu_burst_orchestrator, "resolve_lora_adapter", None)
    if callable(resolver):
        return resolver(candidate)
    return candidate


def _exec_best_of_n_lane(
    tasks: Sequence[NightShiftTask],
    *,
    dry_run: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run Execution-Verified Best-of-N (Lever D) and collect SFT/DPO preference pairs."""
    if not tasks:
        return [], []
    try:
        from kaizen_kit import carbon_orchestrator
    except ImportError:
        import carbon_orchestrator  # type: ignore[no-redef]

    router = carbon_orchestrator.CascadeRouter()
    exec_fn = getattr(router, "execute_best_of_n", None)
    results: List[Dict[str, Any]] = []
    pairs: List[Dict[str, Any]] = []
    for task in tasks:
        n_val = max(2, int(task.best_of_n))
        adapter = _resolve_lora_adapter_id(task.lora_adapter)
        if callable(exec_fn):
            res = exec_fn(
                task.prompt,
                n=n_val,
                task_type="code_generation",
                data_classification=task.data_classification,
                verify_cmd=task.verify_cmd,
                json_schema=task.response_json_schema,
                dry_run=dry_run,
            )
        else:
            res = {
                "winner": "",
                "passed_count": 0,
                "total_candidates": n_val,
                "escalated": False,
                "escalation_model": "claude-sonnet-5-5",
                "sft_dpo_pairs": [],
                "data_classification": task.data_classification,
                "dry_run": dry_run,
            }
        task_pairs = list(res.get("sft_dpo_pairs") or [])
        for p in task_pairs:
            enriched = dict(p)
            enriched.setdefault("task_id", task.task_id)
            enriched.setdefault("adapter", adapter)
            pairs.append(enriched)
        results.append(
            {
                "task_id": task.task_id,
                "n": n_val,
                "verify_cmd": task.verify_cmd,
                "lora_adapter": adapter,
                "result": res,
            }
        )
    return results, pairs


def _exec_lora_synth_lane(
    tasks: Sequence[NightShiftTask],
    collected_pairs: Sequence[Mapping[str, Any]],
    *,
    target_dir: Path,
    dry_run: bool,
) -> Dict[str, Any]:
    """Synthesize corporate-boundary QLoRA SFT/DPO JSONL manifest ($0 idle GPU)."""
    try:
        from kaizen_kit import guardrails
    except ImportError:
        import guardrails  # type: ignore[no-redef]

    records: List[Dict[str, Any]] = []
    blocked: List[Dict[str, str]] = []
    for pair in collected_pairs:
        prompt_txt = str(pair.get("prompt") or "")
        chosen_txt = str(pair.get("chosen") or "")
        rail = guardrails.evaluate_input_rail(prompt_txt, bert_url="")
        out_rail = guardrails.evaluate_output_rail(chosen_txt)
        if not rail.allowed or not out_rail.allowed:
            blocked.append(
                {
                    "task_id": str(pair.get("task_id") or "best_of_n"),
                    "reason": rail.reason if not rail.allowed else out_rail.reason,
                }
            )
            continue
        records.append(dict(pair))

    for task in tasks:
        adapter = _resolve_lora_adapter_id(task.lora_adapter)
        rail = guardrails.evaluate_input_rail(task.prompt, bert_url="")
        if not rail.allowed:
            blocked.append({"task_id": task.task_id, "reason": rail.reason})
            continue
        records.append(
            {
                "task_id": task.task_id,
                "adapter": adapter,
                "prompt": task.prompt,
                "verify_cmd": task.verify_cmd,
                "chosen": "",
                "rejected": "",
                "status": "planned_dry_run" if dry_run else "queued_for_ec2_synth",
            }
        )

    jsonl_path = target_dir / LORA_SFT_DPO_FILENAME
    if records:
        target_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
    sft_count = sum(1 for r in records if r.get("chosen"))
    dpo_count = sum(1 for r in records if r.get("chosen") and r.get("rejected"))
    return {
        "enabled": bool(tasks or collected_pairs),
        "record_count": len(records),
        "sft_count": sft_count,
        "dpo_count": dpo_count,
        "blocked_by_rail": blocked,
        "jsonl_file": str(jsonl_path) if records else "",
        "dry_run": dry_run,
        "gpu_spot_expected_running": 0,
    }


def _render_morning_markdown(state: Mapping[str, Any]) -> str:
    plan = state.get("plan") or {}
    lanes = plan.get("lanes") or {}
    batch_names = state.get("batch_job_names") or []
    filtered = state.get("filtered_out_by_decision_1") or []
    flex_results = state.get("flex_results") or []
    best_of_n_results = state.get("best_of_n_results") or []
    lora_synth = state.get("lora_synth_result") or {}
    morning_batches = state.get("morning_batches") or []
    finops = plan.get("finops_guard") or {}
    lines = [
        "# Neon Night-Shift Report",
        "",
        f"- **Generated:** {state.get('started_at', '')}",
        f"- **Mode:** {state.get('mode', 'dry_run')}",
        f"- **EC2 Local Tasks:** {lanes.get('ec2_local', {}).get('task_count', 0)}",
        f"- **Gemini Decision-1 Tasks:** {lanes.get('gemini_decision_1', {}).get('task_count', 0)}",
        f"- **Gemini Batch Tasks:** {lanes.get('gemini_batch', {}).get('task_count', 0)}",
        f"- **Gemini Flex Tasks:** {lanes.get('gemini_flex', {}).get('task_count', 0)}",
        f"- **Best-of-N Tasks (Lever D):** {lanes.get('best_of_n', {}).get('task_count', 0)}",
        f"- **QLoRA Synth Tasks:** {lanes.get('lora_synth', {}).get('task_count', 0)}",
        f"- **Quarantined (Residency Protected -> EC2):** {plan.get('quarantined_count', 0)}",
        f"- **Filtered Out by Decision-1 Gate:** {len(filtered)}",
        f"- **Recorded Batch Jobs:** {', '.join(batch_names) if batch_names else 'none'}",
        f"- **Flex Completions:** {len(flex_results)}",
        f"- **Best-of-N Completed:** {len(best_of_n_results)}",
        f"- **QLoRA SFT/DPO Records:** {lora_synth.get('record_count', 0)} (DPO pairs={lora_synth.get('dpo_count', 0)})",
        f"- **Distributed 256GB Overnight Spec:** {'ENABLED (dry-run STOPPED)' if finops.get('distributed_256gb_enabled') else 'disabled'}",
        f"- **GPU Spot Overnight Expected Running:** {finops.get('gpu_spot_expected_running', 0)}",
    ]
    if morning_batches:
        lines.extend(["", "## Morning Batch Status", ""])
        for jb in morning_batches:
            lines.append(f"- `{jb.get('name', jb.get('url', 'batch'))}` -> `{jb.get('state', 'DRY_RUN')}`")
    if flex_results:
        lines.extend(["", "## Gemini Flex Synchronous Outputs", ""])
        for fx in flex_results:
            preview = str(fx.get("text") or "[dry-run envelope]")[:200].replace("\n", " ")
            lines.append(f"- `{fx.get('task_id')}` (`{fx.get('model')}`): {preview}")
    lines.append("")
    return "\n".join(lines)


def _persist_night_state(target_dir: Path, state: Dict[str, Any]) -> Tuple[Path, Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    state_path = target_dir / STATE_FILENAME
    report_path = target_dir / REPORT_FILENAME
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(_render_morning_markdown(state), encoding="utf-8")
    return state_path, report_path


def run_night_shift(
    tasks: Optional[Sequence[NightShiftTask]] = None,
    *,
    execute: bool = False,
    use_context_cache: bool = True,
    shared_rubric: str = "",
    allow_distributed_256gb: bool = False,
    state_dir: Optional[Union[str, Path]] = None,
    client: Optional[gemini_cloud_client.GeminiCloudClient] = None,
    autodream_url: str = DEFAULT_AUTODREAM_URL,
) -> Dict[str, Any]:
    """Plan and optionally execute the Neon Night-Shift pipeline with fail-closed residency."""
    target_dir = Path(state_dir).expanduser().resolve() if state_dir is not None else paths.resolve_state_dir()
    active_client = client or gemini_cloud_client.GeminiCloudClient()
    active_tasks = list(tasks) if tasks is not None else build_default_overnight_tasks()
    plan = plan_night_shift(
        active_tasks,
        use_context_cache=use_context_cache,
        shared_rubric=shared_rubric,
        allow_distributed_256gb=allow_distributed_256gb,
    )

    has_live_client = client is not None or active_client.has_api_key()
    dry_run = not (execute and has_live_client)
    dry_run_reason = ""
    if not execute:
        dry_run_reason = "execute=False (dry-run requested)"
    elif not has_live_client:
        dry_run_reason = "GEMINI_API_KEY / GOOGLE_API_KEY not configured (auto-downgraded to dry-run)"

    buckets, _ = _partition_lanes(active_tasks)

    # Stage 1: Gemini Decision-1 (0 output tokens) screening & per-task gate filtering
    decision_results = [_exec_decision_task(active_client, t, dry_run=dry_run) for t in buckets["decision"]]
    kept_batch, batch_gates, batch_filtered = _apply_decision_gates(active_client, buckets["batch"], dry_run=dry_run)
    kept_flex, flex_gates, flex_filtered = _apply_decision_gates(active_client, buckets["flex"], dry_run=dry_run)
    filtered_out = batch_filtered + flex_filtered

    # Stage 2: Gemini Batch + optional Context Cache (fire-and-forget job IDs; zero overnight polling)
    batch_submission = _submit_batch_lane(
        active_client,
        kept_batch,
        use_context_cache=use_context_cache,
        shared_rubric=shared_rubric,
        dry_run=dry_run,
    )

    # Stage 3: Gemini Flex (sync 50% discount for sequential chains + gemini-4-neon adaptive effort)
    flex_results: List[Dict[str, Any]] = []
    for task in kept_flex:
        model = _resolve_task_model(task, DEFAULT_GEMINI_MODEL)
        tl, tb = _resolve_adaptive_thinking(model, task.thinking_level, task.thinking_budget)
        resp = active_client.generate(
            task.prompt,
            model=model,
            system_instruction=task.system_instruction or None,
            service_tier="flex",
            thinking_budget=tb,
            thinking_level=tl,
            response_json_schema=task.response_json_schema,
            max_output_tokens=task.max_output_tokens,
            data_classification=ALLOWED_CLASSIFICATION,
            dry_run=dry_run,
        )
        flex_results.append(
            {
                "task_id": task.task_id,
                "model": model,
                "service_tier": "flex",
                "thinking_level": tl,
                "text": "" if dry_run else _extract_generate_text(resp),
                "response": resp,
            }
        )

    # Stage 4: EC2 Local AutoDream (proprietary memory consolidation)
    ec2_local_result = _trigger_ec2_autodream(buckets["ec2_local"], dry_run=dry_run, autodream_url=autodream_url)

    # Stage 5: Execution-Verified Best-of-N (Lever D)
    best_of_n_results, collected_pairs = _exec_best_of_n_lane(buckets["best_of_n"], dry_run=not execute)

    # Stage 6: QLoRA Synthetic SFT/DPO Batch Generation Lane (corporate boundary, $0 idle GPU)
    lora_synth_result = _exec_lora_synth_lane(
        buckets["lora_synth"],
        collected_pairs,
        target_dir=target_dir,
        dry_run=not execute,
    )

    state: Dict[str, Any] = {
        "orchestrator": "neon-night-shift",
        "started_at": _utc_now(),
        "mode": "dry_run" if dry_run else "live",
        "executed_live": not dry_run,
        "dry_run_reason": dry_run_reason,
        "plan": plan,
        "decision_results": decision_results,
        "decision_gate_checks": batch_gates + flex_gates,
        "filtered_out_by_decision_1": filtered_out,
        "batch_submission": batch_submission,
        "batch_job_names": list(batch_submission.get("batch_job_names") or []),
        "flex_results": flex_results,
        "ec2_local_result": ec2_local_result,
        "best_of_n_results": best_of_n_results,
        "lora_synth_result": lora_synth_result,
    }
    state_path, report_path = _persist_night_state(target_dir, state)
    state["state_file"] = str(state_path)
    state["report_file"] = str(report_path)
    return state


def collect_morning_report(
    *,
    execute: bool = True,
    state_dir: Optional[Union[str, Path]] = None,
    client: Optional[gemini_cloud_client.GeminiCloudClient] = None,
) -> Dict[str, Any]:
    """Read night_shift_state.json, check recorded Gemini Batch jobs once (no loops), and write morning report."""
    target_dir = Path(state_dir).expanduser().resolve() if state_dir is not None else paths.resolve_state_dir()
    state_path = target_dir / STATE_FILENAME
    if not state_path.is_file():
        return {
            "ok": False,
            "error": f"No {STATE_FILENAME} found in {target_dir}. Run `kaizen-kit neon night-shift` first.",
            "state_file": str(state_path),
        }

    state = json.loads(state_path.read_text(encoding="utf-8"))
    active_client = client or gemini_cloud_client.GeminiCloudClient()
    has_live_client = client is not None or active_client.has_api_key()
    dry_run = not (execute and has_live_client)

    job_names: List[str] = list(state.get("batch_job_names") or [])
    morning_batches = [active_client.get_batch(name, dry_run=dry_run) for name in job_names]
    state["morning_collected_at"] = _utc_now()
    state["morning_batches"] = morning_batches
    state["morning_dry_run"] = dry_run
    state_path, report_path = _persist_night_state(target_dir, state)

    return {
        "ok": True,
        "orchestrator": "neon-night-shift",
        "morning_collected_at": state["morning_collected_at"],
        "dry_run": dry_run,
        "batch_count": len(morning_batches),
        "morning_batches": morning_batches,
        "filtered_out_count": len(state.get("filtered_out_by_decision_1") or []),
        "flex_count": len(state.get("flex_results") or []),
        "ec2_local_result": state.get("ec2_local_result"),
        "state_file": str(state_path),
        "report_file": str(report_path),
    }


def run_self_test() -> int:
    """Offline self-test for residency quarantine, Decision-1 gating, Batch+Cache, Flex, and morning report."""
    import tempfile

    mixed_tasks = build_default_overnight_tasks() + [
        NightShiftTask(
            task_id="leak_test_quarantine",
            title="Accidental Proprietary Prompt Marked Batch",
            prompt="Inspect kaizen-prod deployment logs for cloud_gateway_api",
            mode="batch",
            data_classification="public_sanitized",
        )
    ]
    with tempfile.TemporaryDirectory() as tmp:
        res = run_night_shift(mixed_tasks, execute=False, shared_rubric="Return concise outputs.", state_dir=tmp)
        quarantined_ids = {q["task_id"] for q in res["plan"]["quarantined_tasks"]}
        if "leak_test_quarantine" not in quarantined_ids:
            print(f"[FAIL] neon_night_shift failed to quarantine corporate identifier: {quarantined_ids}")
            return 1
        if not res["batch_submission"]["submitted"] or res["plan"]["finops_guard"]["gpu_spot_expected_running"] != 0:
            print(f"[FAIL] neon_night_shift unexpected dry-run batch/FinOps state: {res}")
            return 1
        morning = collect_morning_report(execute=False, state_dir=tmp)
        if not morning.get("ok") or not Path(morning["report_file"]).is_file():
            print(f"[FAIL] neon_night_shift morning collection failed: {morning}")
            return 1

    print("[PASS] neon_night_shift -- residency quarantine, 4-lane routing & morning report verified")
    return 0
