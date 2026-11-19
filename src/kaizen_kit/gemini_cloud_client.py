#!/usr/bin/env python3
"""
Gemini Cloud API Stdlib Client & Fail-Closed DataResidencyGate (`gemini_cloud_client.py`).

Zero external runtime dependencies (Python stdlib `urllib.request` + `json` + `kaizen_kit.guardrails`).
Targets Google Gemini Developer REST API (`https://generativelanguage.googleapis.com/v1beta`)
with `x-goog-api-key` authentication read strictly from `GEMINI_API_KEY` or `GOOGLE_API_KEY`.

Fail-Closed Security Invariant:
  Every outbound request and dry-run plan enforces `DataResidencyGate`, blocking by default
  unless `data_classification == "public_sanitized"` AND free of secrets, sensitive paths,
  and internal corporate identifiers.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
import urllib.error
import urllib.request

from kaizen_kit import guardrails

_LOG = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GENERATE_MODEL = "gemini-4-hydrogen"
DEFAULT_EXPLICIT_CACHE_MODEL = "gemini-3.5-flash-lite"
DEFAULT_DECISION_MODEL = "gemini-decision-1"
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-2"

GEMINI_4_IMPLICIT_CACHE_MIN_TOKENS = 4096
GEMINI_4_MULTITURN_MIN_DELTA_TOKENS = 2050

MAX_DECISION_QUESTIONS = 1000
MAX_CHOICE_OPTIONS = 255
MIN_CHOICE_OPTIONS = 2
MAX_INLINE_BATCH_BYTES = 20 * 1024 * 1024  # 20 MB inline limit

ALLOWED_DATA_CLASSIFICATION = "public_sanitized"
VALID_DECISION_TYPES = ("noul", "choice", "score")
VALID_SERVICE_TIERS = ("flex", "standard", "priority", "unspecified")
VALID_THINKING_LEVELS = ("MINIMAL", "LOW", "MEDIUM", "HIGH", "XHIGH")

EFFORT_TO_THINKING_LEVEL: Dict[str, str] = {
    "minimal": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
}

_THINKING_LEVEL_TO_BUDGET: Dict[str, int] = {
    "MINIMAL": 0,
    "LOW": 256,
    "MEDIUM": 1024,
    "HIGH": 4096,
    "XHIGH": 8192,
}

CORPORATE_IDENTIFIERS: Tuple[str, ...] = (
    "kaizen-prod",
    "kaizen-staging",
    "111122223333",
    "444455556666",
    "kaizen.atlassian.net",
    "gitlab.internal.example",
)

_SECRET_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("gitlab_pat", re.compile(r"\bglpat-[0-9A-Za-z_-]{8,}\b")),
    ("google_aq_key", re.compile(r"AQ\.Ab8[0-9A-Za-z._-]{4,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{8,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
)

_DEPRECATED_PREFIXES: Tuple[str, ...] = (
    "gemini-2.5-",
    "gemini-2.0-",
    "gemini-1.5-",
)
_DEPRECATED_EXACT = frozenset({
    "gemini-2.5",
    "gemini-2.0",
    "gemini-1.5",
    "gemini-flash-latest",
})


@dataclass(frozen=True)
class GeminiModelSpec:
    """Verified Gemini / Gemma model capabilities and API contract metadata."""

    model_id: str
    family: str
    endpoint_kind: str  # "generateContent" | "decisions" | "embedContent"
    context_tokens: int
    max_output_tokens: int
    supports_thinking: bool = False
    thinking_mode: str = "none"  # "level" | "budget" | "none"
    supports_caching: bool = False
    supports_explicit_cache: bool = False
    supports_flex_tier: bool = False
    supports_batch: bool = False
    supports_tools: bool = False
    default_dimensions: Optional[int] = None
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize model specification to a JSON-compatible dictionary."""
        return asdict(self)


GEMINI_MODELS: Dict[str, GeminiModelSpec] = {
    "gemini-4-neon": GeminiModelSpec(
        model_id="gemini-4-neon",
        family="gemini-4",
        endpoint_kind="generateContent",
        context_tokens=1_048_576,
        max_output_tokens=1_048_576,
        supports_thinking=True,
        thinking_mode="level",
        supports_caching=True,
        supports_flex_tier=True,
        supports_batch=True,
        supports_tools=True,
        notes="Verified v1beta adaptive-effort agentic & reasoning model (public_sanitized only).",
    ),
    "gemini-4-carbon": GeminiModelSpec(
        model_id="gemini-4-carbon",
        family="gemini-4",
        endpoint_kind="generateContent",
        context_tokens=1_048_576,
        max_output_tokens=1_048_576,
        supports_thinking=True,
        thinking_mode="level",
        supports_caching=True,
        supports_flex_tier=True,
        supports_batch=True,
        supports_tools=True,
        notes="Frontier long-horizon reasoning & deep architecture critique (unified 1M window).",
    ),
    "gemini-4-argon": GeminiModelSpec(
        model_id="gemini-4-argon",
        family="gemini-4",
        endpoint_kind="generateContent",
        context_tokens=1_048_576,
        max_output_tokens=1_048_576,
        supports_thinking=True,
        thinking_mode="level",
        supports_caching=True,
        supports_flex_tier=True,
        supports_batch=True,
        supports_tools=True,
        notes="Balanced frontier reasoning with lower latency/token overhead.",
    ),
    "gemini-4-hydrogen": GeminiModelSpec(
        model_id="gemini-4-hydrogen",
        family="gemini-4",
        endpoint_kind="generateContent",
        context_tokens=1_048_576,
        max_output_tokens=1_048_576,
        supports_thinking=True,
        thinking_mode="level",
        supports_caching=True,
        supports_flex_tier=True,
        supports_batch=True,
        supports_tools=True,
        notes="High-efficiency default coding, agentic & fast execution model.",
    ),
    "gemini-3.5-flash-lite": GeminiModelSpec(
        model_id="gemini-3.5-flash-lite",
        family="gemini-3.5",
        endpoint_kind="generateContent",
        context_tokens=1_048_576,
        max_output_tokens=65_536,
        supports_thinking=True,
        thinking_mode="level",
        supports_caching=True,
        supports_explicit_cache=True,
        supports_flex_tier=True,
        supports_batch=True,
        supports_tools=True,
        notes="Low-latency lightweight high-throughput Flash-Lite.",
    ),
    "gemini-3.1-flash-lite": GeminiModelSpec(
        model_id="gemini-3.1-flash-lite",
        family="gemini-3.1",
        endpoint_kind="generateContent",
        context_tokens=1_048_576,
        max_output_tokens=65_536,
        supports_thinking=True,
        thinking_mode="level",
        supports_caching=True,
        supports_explicit_cache=True,
        supports_flex_tier=True,
        supports_batch=True,
        supports_tools=True,
        notes="Ultra-economical high-volume Flash-Lite.",
    ),
    "gemma-4-31b-it": GeminiModelSpec(
        model_id="gemma-4-31b-it",
        family="gemma-4",
        endpoint_kind="generateContent",
        context_tokens=256_144,
        max_output_tokens=8_192,
        supports_thinking=True,
        thinking_mode="budget",
        supports_caching=False,
        supports_flex_tier=False,
        supports_batch=True,
        supports_tools=True,
        notes="Gemma 4 31B dense multimodal open-weights model via Gemini API.",
    ),
    "gemma-4-26b-a4b-it": GeminiModelSpec(
        model_id="gemma-4-26b-a4b-it",
        family="gemma-4",
        endpoint_kind="generateContent",
        context_tokens=256_144,
        max_output_tokens=128_000,
        supports_thinking=True,
        thinking_mode="budget",
        supports_caching=False,
        supports_flex_tier=False,
        supports_batch=True,
        supports_tools=True,
        notes="Gemma 4 26B-A4B sparse MoE multimodal model via Gemini API.",
    ),
    "gemini-decision-1": GeminiModelSpec(
        model_id="gemini-decision-1",
        family="gemini-decision",
        endpoint_kind="decisions",
        context_tokens=262_144,
        max_output_tokens=0,
        supports_thinking=False,
        thinking_mode="none",
        supports_caching=False,
        supports_flex_tier=False,
        supports_batch=False,
        supports_tools=False,
        notes="Zero-output-token calibrated classifier/scorer for up to 1000 parallel questions.",
    ),
    "gemini-embedding-2": GeminiModelSpec(
        model_id="gemini-embedding-2",
        family="gemini-embedding",
        endpoint_kind="embedContent",
        context_tokens=8_192,
        max_output_tokens=0,
        default_dimensions=3072,
        notes="Multimodal embedding model with Matryoshka outputDimensionality (3072/1536/768).",
    ),
    "gemini-embedding-001": GeminiModelSpec(
        model_id="gemini-embedding-001",
        family="gemini-embedding",
        endpoint_kind="embedContent",
        context_tokens=2_048,
        max_output_tokens=0,
        default_dimensions=3072,
        notes="Text-only embedding model (2048 token limit).",
    ),
}


def normalize_gemini_model(model: str, allow_raw_alias: bool = False) -> Tuple[str, Optional[str]]:
    """Normalize a model identifier and map deprecated Gemini aliases to `gemini-4-hydrogen`."""
    cleaned = (model or "").strip()
    if not cleaned:
        return DEFAULT_GENERATE_MODEL, None
    bare = cleaned[7:] if cleaned.lower().startswith("models/") else cleaned
    low = bare.lower()
    if not allow_raw_alias and (low in _DEPRECATED_EXACT or low.startswith(_DEPRECATED_PREFIXES)):
        return DEFAULT_GENERATE_MODEL, bare
    return bare, None


def resolve_adaptive_thinking(
    model: str,
    effort_or_level: Optional[str] = None,
    thinking_budget: Optional[int] = None,
) -> Tuple[Optional[str], Optional[int]]:
    """Resolve adaptive effort or thinking level/budget per model's `thinking_mode` (`level` | `budget` | `none`)."""
    if effort_or_level is not None and thinking_budget is not None:
        raise ValueError("thinking_budget and thinking_level/effort are mutually exclusive in thinkingConfig.")

    resolved_model, _ = normalize_gemini_model(model)
    spec = GEMINI_MODELS.get(resolved_model)
    mode = spec.thinking_mode if spec is not None else ("budget" if resolved_model.startswith("gemma-") else "level")

    if mode == "none":
        return None, None

    if thinking_budget is not None:
        return None, int(thinking_budget)

    if effort_or_level is None or not str(effort_or_level).strip():
        return None, None

    raw = str(effort_or_level).strip()
    lvl = EFFORT_TO_THINKING_LEVEL.get(raw.lower()) or raw.upper()
    if lvl not in VALID_THINKING_LEVELS:
        raise ValueError(f"Invalid thinking_level/effort={effort_or_level!r}; expected one of {VALID_THINKING_LEVELS}.")

    if mode == "budget":
        return None, _THINKING_LEVEL_TO_BUDGET[lvl]
    return lvl, None


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def build_capc_ordered_prompt(
    static_prefix: str,
    dynamic_context: str,
    dynamic_hooks: str = "",
) -> Dict[str, Any]:
    """Order prompt sections per CAPC: immutable static prefix FIRST, volatile context/hooks LAST."""
    prefix = static_prefix or ""
    context = dynamic_context or ""
    hooks = dynamic_hooks or ""

    parts: List[Dict[str, str]] = []
    if prefix:
        parts.append({"text": prefix})
    if context:
        parts.append({"text": context})
    if hooks:
        parts.append({"text": hooks})
    if not parts:
        parts.append({"text": ""})

    prefix_tokens = _estimate_text_tokens(prefix)
    dynamic_tokens = _estimate_text_tokens(context)
    hook_tokens = _estimate_text_tokens(hooks)
    delta_tokens = dynamic_tokens + hook_tokens
    ordered_text = "\n\n".join(seg for seg in (prefix, context, hooks) if seg)

    return {
        "prompt": ordered_text,
        "contents": [{"role": "user", "parts": parts}],
        "section_order": ["static_prefix", "dynamic_context", "dynamic_hooks"],
        "static_prefix": prefix,
        "dynamic_context": context,
        "dynamic_hooks": hooks,
        "static_prefix_tokens_est": prefix_tokens,
        "dynamic_delta_tokens_est": delta_tokens,
        "total_tokens_est": prefix_tokens + delta_tokens,
        "implicit_cache_eligible": prefix_tokens >= GEMINI_4_IMPLICIT_CACHE_MIN_TOKENS,
        "multiturn_delta_sufficient": delta_tokens >= GEMINI_4_MULTITURN_MIN_DELTA_TOKENS,
        "capc_cache_safe": True,
    }


def extract_cache_usage_telemetry(response: Dict[str, Any]) -> Dict[str, Any]:
    """Extract prompt/cached token counts and hit ratio from Gemini usageMetadata."""
    usage = (response or {}).get("usageMetadata") or (response or {}).get("usage") or {}
    if not isinstance(usage, Mapping):
        usage = {}
    prompt_tokens = int(usage.get("promptTokenCount") or usage.get("prompt_tokens") or 0)
    cached_tokens = int(usage.get("cachedContentTokenCount") or usage.get("cached_tokens") or 0)
    output_tokens = int(usage.get("candidatesTokenCount") or usage.get("completion_tokens") or 0)
    thoughts_tokens = int(usage.get("thoughtsTokenCount") or 0)
    ratio = round(cached_tokens / prompt_tokens, 4) if prompt_tokens > 0 else 0.0
    return {
        "prompt_token_count": prompt_tokens,
        "total_cached_tokens": cached_tokens,
        "candidates_token_count": output_tokens,
        "thoughts_token_count": thoughts_tokens,
        "cache_hit_ratio": ratio,
        "cache_hit": cached_tokens > 0,
    }


def _enforce_residency_static(payload_text: str, data_classification: str = "corporate_proprietary") -> str:
    classification = (data_classification or "").strip()
    if classification != ALLOWED_DATA_CLASSIFICATION:
        raise PermissionError(
            f"DATA_RESIDENCY_BLOCKED: data_classification={data_classification!r} is not permitted for "
            f"public Gemini Cloud API; only {ALLOWED_DATA_CLASSIFICATION!r} is allowed."
        )

    text = payload_text or ""
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise PermissionError(
                f"DATA_RESIDENCY_BLOCKED: Sensitive credential pattern ({label}) detected in payload."
            )

    low = text.lower()
    for ident in CORPORATE_IDENTIFIERS:
        if ident in low:
            raise PermissionError(
                f"DATA_RESIDENCY_BLOCKED: Internal corporate identifier ({ident}) detected in payload."
            )
    return text


def enforce_data_residency(payload_text: str, data_classification: str = "corporate_proprietary") -> None:
    """Fail-closed residency gate blocking non-public data, secrets, and corporate identifiers."""
    text = _enforce_residency_static(payload_text, data_classification=data_classification)
    window_size = 16384
    overlap = 512
    if len(text) <= window_size:
        chunks = [text]
    else:
        chunks = [text[pos : pos + window_size + overlap] for pos in range(0, len(text), window_size)]
    for chunk in chunks:
        rail = guardrails.evaluate_input_rail(chunk)
        if not rail.allowed:
            raise PermissionError(
                f"DATA_RESIDENCY_BLOCKED: Guardrail blocked outbound payload ({rail.category}: {rail.reason})."
            )


class DataResidencyGate:
    """Fail-closed outbound payload gate for public Gemini Cloud requests."""

    ALLOWED_CLASSIFICATION = ALLOWED_DATA_CLASSIFICATION

    @staticmethod
    def enforce(payload_text: str, data_classification: str = "corporate_proprietary") -> None:
        """Enforce classification, secret, corporate identifier, and input guardrail checks."""
        enforce_data_residency(payload_text, data_classification=data_classification)

    @staticmethod
    def enforce_data_residency(payload_text: str, data_classification: str = "corporate_proprietary") -> None:
        """Alias for `enforce_data_residency`."""
        enforce_data_residency(payload_text, data_classification=data_classification)


def _serialize_for_residency(*parts: Any) -> str:
    serialized: List[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str):
            serialized.append(part)
        else:
            serialized.append(json.dumps(part, ensure_ascii=False, sort_keys=True))
    return "\n".join(serialized)


def _normalize_single_content(item: Any) -> Dict[str, Any]:
    if isinstance(item, str):
        return {"role": "user", "parts": [{"text": item}]}
    if isinstance(item, dict):
        if "parts" in item:
            out = dict(item)
            out.setdefault("role", "user")
            return out
        return {"role": "user", "parts": [item]}
    return {"role": "user", "parts": [{"text": str(item)}]}


def _normalize_contents(contents: Any) -> List[Dict[str, Any]]:
    if contents is None:
        raise ValueError("contents must not be None")
    if isinstance(contents, str):
        if not contents.strip():
            raise ValueError("contents must not be empty")
        return [_normalize_single_content(contents)]
    if isinstance(contents, dict):
        return [_normalize_single_content(contents)]
    if isinstance(contents, (list, tuple)):
        if not contents:
            raise ValueError("contents sequence must not be empty")
        return [_normalize_single_content(item) for item in contents]
    return [_normalize_single_content(contents)]


def _normalize_system_instruction(system_instruction: Any) -> Dict[str, Any]:
    if isinstance(system_instruction, dict) and "parts" in system_instruction:
        return dict(system_instruction)
    return {"parts": [{"text": str(system_instruction)}]}


def _normalize_resource_name(name: str, prefix: str) -> str:
    cleaned = (name or "").strip().lstrip("/")
    if not cleaned:
        raise ValueError(f"Resource name for {prefix} must not be empty")
    if cleaned.startswith(f"{prefix}/"):
        return cleaned
    if f"/{prefix}/" in cleaned:
        return f"{prefix}/{cleaned.split(f'/{prefix}/', 1)[1]}"
    return f"{prefix}/{cleaned}"


def _build_thinking_config(
    thinking_budget: Optional[int],
    thinking_level: Optional[str],
    include_thoughts: bool,
) -> Optional[Dict[str, Any]]:
    if thinking_budget is not None and thinking_level is not None:
        raise ValueError("thinking_budget and thinking_level are mutually exclusive in thinkingConfig.")
    cfg: Dict[str, Any] = {}
    if thinking_budget is not None:
        cfg["thinkingBudget"] = int(thinking_budget)
    if thinking_level is not None:
        lvl = str(thinking_level).strip().upper()
        if lvl not in VALID_THINKING_LEVELS:
            raise ValueError(f"Invalid thinking_level={thinking_level!r}; expected one of {VALID_THINKING_LEVELS}.")
        cfg["thinkingLevel"] = lvl
    if include_thoughts:
        cfg["includeThoughts"] = True
    return cfg or None


def _build_tools_list(
    enable_google_search: bool = False,
    enable_code_execution: bool = False,
    enable_url_context: bool = False,
    function_declarations: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = []
    if enable_google_search:
        tools.append({"googleSearch": {}})
    if enable_code_execution:
        tools.append({"codeExecution": {}})
    if enable_url_context:
        tools.append({"urlContext": {}})
    if function_declarations:
        tools.append({"functionDeclarations": [dict(fn) for fn in function_declarations]})
    return tools


def _build_generate_request(
    contents: Any,
    *,
    system_instruction: Optional[Any] = None,
    service_tier: Optional[str] = None,
    thinking_budget: Optional[int] = None,
    thinking_level: Optional[str] = None,
    include_thoughts: bool = False,
    cached_content: Optional[str] = None,
    response_json_schema: Optional[Dict[str, Any]] = None,
    enable_google_search: bool = False,
    enable_code_execution: bool = False,
    enable_url_context: bool = False,
    function_declarations: Optional[Sequence[Dict[str, Any]]] = None,
    temperature: Optional[float] = None,
    max_output_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a Gemini REST `GenerateContentRequest` with verified wire field placement."""
    body: Dict[str, Any] = {"contents": _normalize_contents(contents)}

    if system_instruction is not None:
        body["systemInstruction"] = _normalize_system_instruction(system_instruction)

    if cached_content is not None and str(cached_content).strip():
        body["cachedContent"] = _normalize_resource_name(str(cached_content), "cachedContents")

    if service_tier is not None:
        tier = str(service_tier).strip().lower()
        if tier not in VALID_SERVICE_TIERS:
            raise ValueError(f"Invalid service_tier={service_tier!r}; expected one of {VALID_SERVICE_TIERS}.")
        body["serviceTier"] = tier

    tools = _build_tools_list(
        enable_google_search=enable_google_search,
        enable_code_execution=enable_code_execution,
        enable_url_context=enable_url_context,
        function_declarations=function_declarations,
    )
    if tools:
        body["tools"] = tools

    gen_cfg: Dict[str, Any] = {}
    if temperature is not None:
        gen_cfg["temperature"] = float(temperature)
    if max_output_tokens is not None:
        gen_cfg["maxOutputTokens"] = int(max_output_tokens)
    if response_json_schema is not None:
        gen_cfg["responseMimeType"] = "application/json"
        gen_cfg["responseJsonSchema"] = dict(response_json_schema)

    thinking_cfg = _build_thinking_config(thinking_budget, thinking_level, include_thoughts)
    if thinking_cfg is not None:
        gen_cfg["thinkingConfig"] = thinking_cfg

    if gen_cfg:
        body["generationConfig"] = gen_cfg

    return body


def _validate_decision_question(q_id: str, q_spec: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(q_id, str) or not q_id.strip():
        raise ValueError("Each decision question key must be a non-empty string.")
    if not isinstance(q_spec, Mapping):
        raise ValueError(f"Question {q_id!r} specification must be a dict.")

    q_type = str(q_spec.get("type", "")).strip().lower()
    if q_type not in VALID_DECISION_TYPES:
        raise ValueError(f"Question {q_id!r} has invalid type={q_type!r}; expected one of {VALID_DECISION_TYPES}.")

    instructions = q_spec.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError(f"Question {q_id!r} requires non-empty string 'instructions'.")

    normalized: Dict[str, Any] = {"type": q_type, "instructions": instructions}
    criteria = q_spec.get("criteria")

    if q_type == "choice":
        if not isinstance(criteria, Mapping) or not (MIN_CHOICE_OPTIONS <= len(criteria) <= MAX_CHOICE_OPTIONS):
            raise ValueError(
                f"Choice question {q_id!r} requires a criteria dict with {MIN_CHOICE_OPTIONS}..{MAX_CHOICE_OPTIONS} options."
            )
        normalized["criteria"] = {str(k): str(v) for k, v in criteria.items()}
    elif q_type == "score":
        if not isinstance(criteria, (list, tuple)) or not (MIN_CHOICE_OPTIONS <= len(criteria) <= MAX_CHOICE_OPTIONS):
            raise ValueError(
                f"Score question {q_id!r} requires a criteria list with {MIN_CHOICE_OPTIONS}..{MAX_CHOICE_OPTIONS} levels."
            )
        normalized["criteria"] = [str(item) for item in criteria]
    elif criteria is not None:
        if isinstance(criteria, Mapping):
            normalized["criteria"] = {str(k): str(v) for k, v in criteria.items()}
        else:
            normalized["criteria"] = criteria

    return normalized


class GeminiCloudClient:
    """Stdlib-only client for Gemini Developer API (`/v1beta`) with fail-closed data residency."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 120.0,
        allow_raw_alias: bool = False,
    ) -> None:
        env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        self._api_key = (api_key if api_key is not None else env_key).strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.timeout_s = float(timeout_s)
        self.allow_raw_alias = bool(allow_raw_alias)

    def has_api_key(self) -> bool:
        """Return whether an API key is configured without exposing its value."""
        return bool(self._api_key)

    def _require_api_key(self) -> str:
        if not self._api_key:
            raise RuntimeError(
                "GEMINI_API_KEY or GOOGLE_API_KEY environment variable is required for live Gemini Cloud calls."
            )
        return self._api_key

    def _http_request(
        self,
        method: str,
        path_or_url: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        raw_body: Optional[bytes] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        url = (
            path_or_url
            if path_or_url.startswith(("http://", "https://"))
            else f"{self.base_url}/{path_or_url.lstrip('/')}"
        )
        headers: Dict[str, str] = {"x-goog-api-key": self._require_api_key()}
        data: Optional[bytes] = raw_body

        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            headers.setdefault("Content-Type", "application/octet-stream")

        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                body_text = resp.read().decode("utf-8", errors="replace").strip()
                parsed = json.loads(body_text) if body_text else {}
                return parsed if isinstance(parsed, dict) else {"data": parsed}, resp_headers
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Gemini API HTTP {exc.code} on {method.upper()} {url}: {err_body}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            raise RuntimeError(f"Gemini API transport failure on {method.upper()} {url}: {exc}") from exc

    def _dry_run_envelope(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        **meta: Any,
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "dry_run": True,
            "method": method.upper(),
            "url": f"{self.base_url}/{path.lstrip('/')}",
        }
        if payload is not None:
            out["payload"] = payload
        out.update(meta)
        return out

    # ------------------------------------------------------------------
    # Decisions API (`gemini-decision-1`, 0 output tokens)
    # ------------------------------------------------------------------
    def decide(
        self,
        input_content: Any,
        questions: Mapping[str, Mapping[str, Any]],
        model: str = DEFAULT_DECISION_MODEL,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Call `/v1beta/decisions` to evaluate 1..1000 typed questions with 0 output tokens."""
        if input_content is None or (isinstance(input_content, str) and not input_content.strip()):
            raise ValueError("input_content must not be empty.")
        if not isinstance(questions, Mapping) or not (1 <= len(questions) <= MAX_DECISION_QUESTIONS):
            raise ValueError(f"questions must contain 1..{MAX_DECISION_QUESTIONS} entries.")

        normalized_questions = {
            q_id: _validate_decision_question(q_id, q_spec) for q_id, q_spec in questions.items()
        }
        enforce_data_residency(
            _serialize_for_residency(input_content, normalized_questions),
            data_classification=data_classification,
        )

        resolved_model, substituted_from = normalize_gemini_model(model, allow_raw_alias=self.allow_raw_alias)
        payload: Dict[str, Any] = {
            "model": resolved_model,
            "input": input_content,
            "questions": normalized_questions,
        }
        if dry_run:
            return self._dry_run_envelope(
                "POST",
                "decisions",
                payload=payload,
                model=resolved_model,
                substituted_from=substituted_from,
                question_count=len(normalized_questions),
                expected_output_tokens=0,
            )

        data, _ = self._http_request("POST", "decisions", payload=payload)
        return data

    def decide_noul(
        self,
        input_content: Any,
        instructions: str,
        *,
        question_id: str = "decision",
        true_description: Optional[str] = None,
        false_description: Optional[str] = None,
        model: str = DEFAULT_DECISION_MODEL,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Evaluate a single yes/no (`noul`) decision question."""
        spec: Dict[str, Any] = {"type": "noul", "instructions": instructions}
        criteria: Dict[str, str] = {}
        if true_description:
            criteria["true_description"] = true_description
        if false_description:
            criteria["false_description"] = false_description
        if criteria:
            spec["criteria"] = criteria
        return self.decide(
            input_content,
            {question_id: spec},
            model=model,
            data_classification=data_classification,
            dry_run=dry_run,
        )

    def decide_choice(
        self,
        input_content: Any,
        instructions: str,
        criteria: Mapping[str, str],
        *,
        question_id: str = "choice",
        model: str = DEFAULT_DECISION_MODEL,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Evaluate a single-select (`choice`) question across 2..255 named options."""
        return self.decide(
            input_content,
            {question_id: {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}},
            model=model,
            data_classification=data_classification,
            dry_run=dry_run,
        )

    def decide_score(
        self,
        input_content: Any,
        instructions: str,
        criteria: Sequence[str],
        *,
        question_id: str = "score",
        model: str = DEFAULT_DECISION_MODEL,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Evaluate an ordered rubric (`score`) question across 2..255 levels."""
        return self.decide(
            input_content,
            {question_id: {"type": "score", "instructions": instructions, "criteria": list(criteria)}},
            model=model,
            data_classification=data_classification,
            dry_run=dry_run,
        )

    def decide_code_review_checklist(
        self,
        diff_or_code: str,
        rules: Union[Sequence[Union[str, Mapping[str, Any]]], Mapping[str, str]],
        *,
        model: str = DEFAULT_DECISION_MODEL,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Evaluate 1..1000 code review rules in parallel over a diff/code snippet via Decision-1."""
        if not isinstance(diff_or_code, str) or not diff_or_code.strip():
            raise ValueError("diff_or_code must be a non-empty string.")

        questions: Dict[str, Dict[str, Any]] = {}
        if isinstance(rules, Mapping):
            for key, instr in rules.items():
                questions[str(key)] = {"type": "noul", "instructions": str(instr)}
        elif isinstance(rules, (list, tuple)):
            for idx, item in enumerate(rules, start=1):
                if isinstance(item, Mapping):
                    q_id = str(item.get("id") or item.get("key") or f"rule_{idx:03d}")
                    q_spec = {k: v for k, v in item.items() if k not in ("id", "key")}
                    q_spec.setdefault("type", "noul")
                    questions[q_id] = q_spec
                else:
                    questions[f"rule_{idx:03d}"] = {"type": "noul", "instructions": str(item)}
        else:
            raise ValueError("rules must be a sequence or mapping of checklist items.")

        return self.decide(
            diff_or_code,
            questions,
            model=model,
            data_classification=data_classification,
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------
    # Generate Content (`/v1beta/models/{model}:generateContent`)
    # ------------------------------------------------------------------
    def generate(
        self,
        contents: Any,
        model: str = DEFAULT_GENERATE_MODEL,
        system_instruction: Optional[Any] = None,
        service_tier: Optional[str] = "flex",
        thinking_budget: Optional[int] = None,
        thinking_level: Optional[str] = None,
        include_thoughts: bool = False,
        cached_content: Optional[str] = None,
        response_json_schema: Optional[Dict[str, Any]] = None,
        enable_google_search: bool = False,
        enable_code_execution: bool = False,
        enable_url_context: bool = False,
        function_declarations: Optional[Sequence[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
        effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate content via `POST /v1beta/models/{model}:generateContent`."""
        enforce_data_residency(
            _serialize_for_residency(contents, system_instruction, function_declarations, response_json_schema),
            data_classification=data_classification,
        )

        resolved_model, substituted_from = normalize_gemini_model(model, allow_raw_alias=self.allow_raw_alias)
        effective_effort = thinking_level if thinking_level is not None else effort
        resolved_lvl, resolved_budget = resolve_adaptive_thinking(
            resolved_model,
            effort_or_level=effective_effort,
            thinking_budget=thinking_budget,
        )
        payload = _build_generate_request(
            contents,
            system_instruction=system_instruction,
            service_tier=service_tier,
            thinking_budget=resolved_budget,
            thinking_level=resolved_lvl,
            include_thoughts=include_thoughts,
            cached_content=cached_content,
            response_json_schema=response_json_schema,
            enable_google_search=enable_google_search,
            enable_code_execution=enable_code_execution,
            enable_url_context=enable_url_context,
            function_declarations=function_declarations,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        path = f"models/{resolved_model}:generateContent"
        if dry_run:
            return self._dry_run_envelope(
                "POST",
                path,
                payload=payload,
                model=resolved_model,
                substituted_from=substituted_from,
            )

        data, _ = self._http_request("POST", path, payload=payload)
        cache_telemetry = extract_cache_usage_telemetry(data)
        data["cacheTelemetry"] = cache_telemetry
        _LOG.info(
            json.dumps(
                {"event": "gemini_cache_telemetry", "model": resolved_model, **cache_telemetry},
                sort_keys=True,
            )
        )
        return data

    def count_tokens(
        self,
        contents: Any,
        model: str = DEFAULT_GENERATE_MODEL,
        *,
        system_instruction: Optional[Any] = None,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Count input tokens via `POST /v1beta/models/{model}:countTokens`."""
        enforce_data_residency(
            _serialize_for_residency(contents, system_instruction),
            data_classification=data_classification,
        )
        resolved_model, substituted_from = normalize_gemini_model(model, allow_raw_alias=self.allow_raw_alias)
        payload: Dict[str, Any] = {"contents": _normalize_contents(contents)}
        if system_instruction is not None:
            payload["systemInstruction"] = _normalize_system_instruction(system_instruction)
        path = f"models/{resolved_model}:countTokens"
        if dry_run:
            return self._dry_run_envelope("POST", path, payload=payload, model=resolved_model, substituted_from=substituted_from)
        data, _ = self._http_request("POST", path, payload=payload)
        return data

    # ------------------------------------------------------------------
    # Context Caching (`/v1beta/cachedContents`)
    # ------------------------------------------------------------------
    def create_cache(
        self,
        model: str = DEFAULT_EXPLICIT_CACHE_MODEL,
        contents: Any = None,
        *,
        system_instruction: Optional[Any] = None,
        ttl: Optional[str] = "3600s",
        expire_time: Optional[str] = None,
        display_name: Optional[str] = None,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Create an explicit context cache via `POST /v1beta/cachedContents` (Gemini 3/2.5 only)."""
        if contents is None and system_instruction is None:
            raise ValueError("create_cache requires at least contents or system_instruction.")
        enforce_data_residency(
            _serialize_for_residency(contents, system_instruction, display_name, tools),
            data_classification=data_classification,
        )

        resolved_model, substituted_from = normalize_gemini_model(model, allow_raw_alias=self.allow_raw_alias)
        spec = GEMINI_MODELS.get(resolved_model)
        if (spec is not None and spec.family == "gemini-4") or resolved_model.startswith("gemini-4-"):
            raise ValueError(
                f"EXPLICIT_CACHE_UNSUPPORTED: {resolved_model!r} (family='gemini-4') is Implicit Caching ONLY "
                f"(>= {GEMINI_4_IMPLICIT_CACHE_MIN_TOKENS} prefix tokens); Explicit Cache requires Gemini 3/2.5 "
                f"(e.g. {DEFAULT_EXPLICIT_CACHE_MODEL!r})."
            )
        payload: Dict[str, Any] = {"model": f"models/{resolved_model}"}
        if contents is not None:
            payload["contents"] = _normalize_contents(contents)
        if system_instruction is not None:
            payload["systemInstruction"] = _normalize_system_instruction(system_instruction)
        if expire_time is not None:
            payload["expireTime"] = expire_time
        elif ttl is not None:
            payload["ttl"] = ttl
        if display_name is not None:
            payload["displayName"] = display_name
        if tools:
            payload["tools"] = [dict(t) for t in tools]

        if dry_run:
            return self._dry_run_envelope(
                "POST",
                "cachedContents",
                payload=payload,
                model=resolved_model,
                substituted_from=substituted_from,
            )

        data, _ = self._http_request("POST", "cachedContents", payload=payload)
        return data

    def get_cache(self, cache_name: str, *, dry_run: bool = False) -> Dict[str, Any]:
        """Fetch context cache metadata via `GET /v1beta/cachedContents/{id}`."""
        path = _normalize_resource_name(cache_name, "cachedContents")
        if dry_run:
            return self._dry_run_envelope("GET", path)
        data, _ = self._http_request("GET", path)
        return data

    def delete_cache(self, cache_name: str, *, dry_run: bool = False) -> Dict[str, Any]:
        """Delete a context cache via `DELETE /v1beta/cachedContents/{id}`."""
        path = _normalize_resource_name(cache_name, "cachedContents")
        if dry_run:
            return self._dry_run_envelope("DELETE", path)
        data, _ = self._http_request("DELETE", path)
        return data

    # ------------------------------------------------------------------
    # Batch API & Files Upload
    # ------------------------------------------------------------------
    def build_batch_jsonl(
        self,
        items: Sequence[Union[str, Mapping[str, Any]]],
        cached_content: Optional[str] = None,
        thinking_budget: Optional[int] = None,
        response_json_schema: Optional[Dict[str, Any]] = None,
        *,
        thinking_level: Optional[str] = None,
        include_thoughts: bool = False,
        system_instruction: Optional[Any] = None,
        service_tier: Optional[str] = None,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        data_classification: Optional[str] = None,
    ) -> str:
        """Build JSONL lines with `{key, request}` placing `cachedContent` at request root."""
        if not items:
            raise ValueError("items must contain at least one batch entry.")

        lines: List[str] = []
        for idx, raw_item in enumerate(items):
            if isinstance(raw_item, str):
                key = f"item-{idx}"
                req_obj = _build_generate_request(
                    raw_item,
                    system_instruction=system_instruction,
                    service_tier=service_tier,
                    thinking_budget=thinking_budget,
                    thinking_level=thinking_level,
                    include_thoughts=include_thoughts,
                    cached_content=cached_content,
                    response_json_schema=response_json_schema,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                )
            elif isinstance(raw_item, Mapping):
                key = str(raw_item.get("key") or raw_item.get("id") or f"item-{idx}")
                if "request" in raw_item and isinstance(raw_item["request"], Mapping):
                    req_obj = dict(raw_item["request"])
                    if cached_content and "cachedContent" not in req_obj:
                        req_obj["cachedContent"] = _normalize_resource_name(cached_content, "cachedContents")
                else:
                    item_contents = raw_item.get("contents")
                    if item_contents is None:
                        item_contents = raw_item.get("prompt")
                    req_obj = _build_generate_request(
                        item_contents,
                        system_instruction=raw_item.get("system_instruction", system_instruction),
                        service_tier=raw_item.get("service_tier", service_tier),
                        thinking_budget=raw_item.get("thinking_budget", thinking_budget),
                        thinking_level=raw_item.get("thinking_level", thinking_level),
                        include_thoughts=bool(raw_item.get("include_thoughts", include_thoughts)),
                        cached_content=raw_item.get("cached_content", cached_content),
                        response_json_schema=raw_item.get("response_json_schema", response_json_schema),
                        temperature=raw_item.get("temperature", temperature),
                        max_output_tokens=raw_item.get("max_output_tokens", max_output_tokens),
                    )
            else:
                raise ValueError(f"Unsupported batch item at index {idx}: {type(raw_item).__name__}")

            lines.append(json.dumps({"key": key, "request": req_obj}, ensure_ascii=False))

        jsonl_text = "\n".join(lines)
        if data_classification is not None:
            enforce_data_residency(jsonl_text, data_classification=data_classification)
        return jsonl_text

    def submit_batch_inline(
        self,
        model: str,
        requests: Sequence[Mapping[str, Any]],
        display_name: str,
        *,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Submit inline batch requests (<20 MB) via `POST /v1beta/models/{model}:batchGenerateContent`."""
        if not requests:
            raise ValueError("requests must contain at least one item.")
        residency_text = _serialize_for_residency(display_name, list(requests))
        _enforce_residency_static(residency_text, data_classification=data_classification)

        inlined: List[Dict[str, Any]] = []
        for idx, item in enumerate(requests):
            if not isinstance(item, Mapping):
                raise ValueError(f"Inline request at index {idx} must be a mapping.")
            entry: Dict[str, Any] = {}
            if "request" in item and isinstance(item["request"], Mapping):
                entry["request"] = dict(item["request"])
            else:
                item_contents = item.get("contents")
                if item_contents is None:
                    item_contents = item.get("prompt")
                entry["request"] = _build_generate_request(
                    item_contents,
                    system_instruction=item.get("system_instruction"),
                    service_tier=item.get("service_tier"),
                    thinking_budget=item.get("thinking_budget"),
                    thinking_level=item.get("thinking_level"),
                    include_thoughts=bool(item.get("include_thoughts", False)),
                    cached_content=item.get("cached_content"),
                    response_json_schema=item.get("response_json_schema"),
                    temperature=item.get("temperature"),
                    max_output_tokens=item.get("max_output_tokens"),
                )
            if "metadata" in item and isinstance(item["metadata"], Mapping):
                entry["metadata"] = dict(item["metadata"])
            elif "key" in item:
                entry["metadata"] = {"key": str(item["key"])}
            inlined.append(entry)

        resolved_model, substituted_from = normalize_gemini_model(model, allow_raw_alias=self.allow_raw_alias)
        payload: Dict[str, Any] = {
            "batch": {
                "displayName": display_name,
                "inputConfig": {
                    "requests": {
                        "requests": inlined,
                    }
                },
            }
        }
        encoded_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded_bytes) >= MAX_INLINE_BATCH_BYTES:
            raise ValueError(
                f"Inline batch payload is {len(encoded_bytes)} bytes (>= {MAX_INLINE_BATCH_BYTES} byte 20 MB guard); "
                "use upload_jsonl_file + submit_batch_file instead."
            )

        enforce_data_residency(residency_text, data_classification=data_classification)

        path = f"models/{resolved_model}:batchGenerateContent"
        if dry_run:
            return self._dry_run_envelope(
                "POST",
                path,
                payload=payload,
                model=resolved_model,
                substituted_from=substituted_from,
                payload_bytes=len(encoded_bytes),
            )

        data, _ = self._http_request("POST", path, payload=payload)
        return data

    def upload_jsonl_file(
        self,
        jsonl_text: str,
        display_name: str,
        *,
        mime_type: str = "application/jsonl",
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Upload batch JSONL content via `POST /v1beta/files` (supports 2-step resumable or direct JSON)."""
        if not isinstance(jsonl_text, str) or not jsonl_text.strip():
            raise ValueError("jsonl_text must be a non-empty JSONL string.")
        non_empty_lines = [line for line in jsonl_text.splitlines() if line.strip()]
        for idx, line in enumerate(non_empty_lines, start=1):
            try:
                json.loads(line)
            except ValueError as exc:
                raise ValueError(f"Invalid JSON on JSONL line {idx}: {exc}") from exc

        enforce_data_residency(
            _serialize_for_residency(display_name, jsonl_text),
            data_classification=data_classification,
        )

        raw_bytes = jsonl_text.encode("utf-8")
        start_payload: Dict[str, Any] = {
            "file": {
                "displayName": display_name,
                "mimeType": mime_type,
            }
        }
        upload_headers = {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(raw_bytes)),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "X-Goog-Upload-File-Name": f"{display_name}.jsonl",
        }
        if dry_run:
            return self._dry_run_envelope(
                "POST",
                "files",
                payload=start_payload,
                headers=upload_headers,
                size_bytes=len(raw_bytes),
                line_count=len(non_empty_lines),
            )

        data, resp_headers = self._http_request(
            "POST",
            "files",
            payload=start_payload,
            extra_headers=upload_headers,
        )
        upload_url = resp_headers.get("x-goog-upload-url")
        if upload_url:
            final_data, _ = self._http_request(
                "POST",
                upload_url,
                raw_body=raw_bytes,
                extra_headers={
                    "Content-Type": mime_type,
                    "Content-Length": str(len(raw_bytes)),
                    "X-Goog-Upload-Command": "upload, finalize",
                    "X-Goog-Upload-Offset": "0",
                },
            )
            return final_data
        return data

    def submit_batch_file(
        self,
        model: str,
        file_uri: str,
        display_name: str,
        *,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Create a file-backed batch job via `POST /v1beta/models/{model}:batchGenerateContent`."""
        enforce_data_residency(
            _serialize_for_residency(display_name, file_uri),
            data_classification=data_classification,
        )

        normalized_file = _normalize_resource_name(file_uri, "files")
        resolved_model, substituted_from = normalize_gemini_model(model, allow_raw_alias=self.allow_raw_alias)
        payload: Dict[str, Any] = {
            "batch": {
                "displayName": display_name,
                "inputConfig": {
                    "fileName": normalized_file,
                },
            }
        }
        path = f"models/{resolved_model}:batchGenerateContent"
        if dry_run:
            return self._dry_run_envelope(
                "POST",
                path,
                payload=payload,
                model=resolved_model,
                substituted_from=substituted_from,
            )

        data, _ = self._http_request("POST", path, payload=payload)
        return data

    def get_batch(self, batch_name: str, *, dry_run: bool = False) -> Dict[str, Any]:
        """Poll batch job status via `GET /v1beta/batches/{id}`."""
        path = _normalize_resource_name(batch_name, "batches")
        if dry_run:
            return self._dry_run_envelope("GET", path)
        data, _ = self._http_request("GET", path)
        return data

    def cancel_batch(self, batch_name: str, *, dry_run: bool = False) -> Dict[str, Any]:
        """Cancel a batch job via `POST /v1beta/batches/{id}:cancel`."""
        path = f"{_normalize_resource_name(batch_name, 'batches')}:cancel"
        if dry_run:
            return self._dry_run_envelope("POST", path, payload={})
        data, _ = self._http_request("POST", path, payload={})
        return data

    # ------------------------------------------------------------------
    # Embeddings (`/v1beta/models/{model}:embedContent`)
    # ------------------------------------------------------------------
    def embed(
        self,
        content: Any,
        model: str = DEFAULT_EMBEDDING_MODEL,
        *,
        task_type: Optional[str] = None,
        title: Optional[str] = None,
        output_dimensionality: Optional[int] = None,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Generate vector embeddings via `POST /v1beta/models/{model}:embedContent`."""
        if content is None or (isinstance(content, str) and not content.strip()):
            raise ValueError("content must not be empty.")
        enforce_data_residency(
            _serialize_for_residency(content, title),
            data_classification=data_classification,
        )

        resolved_model, substituted_from = normalize_gemini_model(model, allow_raw_alias=self.allow_raw_alias)
        content_obj = _normalize_single_content(content)
        content_obj.pop("role", None)
        payload: Dict[str, Any] = {"content": content_obj}
        if task_type is not None:
            payload["taskType"] = task_type
        if title is not None:
            payload["title"] = title
        if output_dimensionality is not None:
            payload["outputDimensionality"] = int(output_dimensionality)

        path = f"models/{resolved_model}:embedContent"
        if dry_run:
            return self._dry_run_envelope(
                "POST",
                path,
                payload=payload,
                model=resolved_model,
                substituted_from=substituted_from,
            )

        data, _ = self._http_request("POST", path, payload=payload)
        return data


def check_token_budget_and_route(
    contents: Any,
    *,
    model: str = "gemini-4-neon",
    max_sync_tokens: int = 32768,
    data_classification: str = "public_sanitized",
    use_api_count: bool = False,
    client: Optional[GeminiCloudClient] = None,
) -> Dict[str, Any]:
    """Pre-budget token estimator routing oversized inputs to `gemini_batch` (public) or `ec2_local` (corporate)."""
    serialized = _serialize_for_residency(contents)
    resolved_model, _ = normalize_gemini_model(model)
    estimated_tokens = _estimate_text_tokens(serialized)
    token_source = "local_heuristic"

    is_public = (data_classification or "").strip() == ALLOWED_DATA_CLASSIFICATION
    if is_public:
        enforce_data_residency(serialized, data_classification=data_classification)
        if use_api_count:
            active_client = client or GeminiCloudClient()
            if active_client.has_api_key():
                try:
                    cnt_resp = active_client.count_tokens(
                        contents,
                        model=resolved_model,
                        data_classification=data_classification,
                    )
                    if "totalTokens" in cnt_resp:
                        estimated_tokens = int(cnt_resp["totalTokens"])
                        token_source = "gemini_count_tokens_api"
                except (RuntimeError, OSError, ValueError):
                    token_source = "local_heuristic_fallback"

    over_budget = estimated_tokens > int(max_sync_tokens)
    if not is_public:
        recommended_route = "ec2_local"
    elif over_budget:
        recommended_route = "gemini_batch"
    else:
        recommended_route = "gemini_sync_flex"

    return {
        "model": resolved_model,
        "estimated_tokens": estimated_tokens,
        "max_sync_tokens": int(max_sync_tokens),
        "over_budget": over_budget,
        "recommended_route": recommended_route,
        "data_classification": data_classification,
        "token_source": token_source,
        "implicit_cache_eligible": estimated_tokens >= GEMINI_4_IMPLICIT_CACHE_MIN_TOKENS,
    }


def _verify_self_test_checks() -> List[Tuple[str, bool]]:
    checks: List[Tuple[str, bool]] = []
    expected_models = {
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
    checks.append(("verified_models", expected_models.issubset(GEMINI_MODELS.keys())))
    checks.append(("adaptive_thinking_neon", resolve_adaptive_thinking("gemini-4-neon", "high") == ("HIGH", None)))
    checks.append(("adaptive_thinking_gemma_budget", resolve_adaptive_thinking("gemma-4-26b-a4b-it", "medium") == (None, 1024)))
    capc = build_capc_ordered_prompt("S" * 16500, "dynamic diff", "hook suffix")
    checks.append(("capc_implicit_cache", capc["implicit_cache_eligible"] and capc["prompt"].endswith("hook suffix")))
    g4_cache_blocked = False
    try:
        GeminiCloudClient(api_key="").create_cache(
            model="gemini-4-neon",
            contents="Sanitized reference",
            data_classification="public_sanitized",
            dry_run=True,
        )
    except ValueError as exc:
        g4_cache_blocked = "EXPLICIT_CACHE_UNSUPPORTED" in str(exc)
    checks.append(("gemini4_explicit_cache_blocked", g4_cache_blocked))
    checks.append((
        "token_budget_routing",
        check_token_budget_and_route("x" * 200000, max_sync_tokens=32768, data_classification="public_sanitized")["recommended_route"]
        == "gemini_batch"
        and check_token_budget_and_route("x" * 200000, max_sync_tokens=32768, data_classification="corporate_proprietary")["recommended_route"]
        == "ec2_local",
    ))
    tel = extract_cache_usage_telemetry({"usageMetadata": {"promptTokenCount": 5000, "cachedContentTokenCount": 4000}})
    checks.append(("cache_telemetry_ratio", tel["cache_hit_ratio"] == 0.8 and tel["total_cached_tokens"] == 4000))
    checks.append(("deprecated_25", normalize_gemini_model("gemini-2.5-flash") == ("gemini-4-hydrogen", "gemini-2.5-flash")))
    checks.append(("deprecated_latest", normalize_gemini_model("gemini-flash-latest") == ("gemini-4-hydrogen", "gemini-flash-latest")))
    checks.append(("raw_alias_bypass", normalize_gemini_model("gemini-2.0-flash", allow_raw_alias=True) == ("gemini-2.0-flash", None)))

    prop_blocked = False
    try:
        enforce_data_residency("sanitized summary")
    except PermissionError as exc:
        prop_blocked = "DATA_RESIDENCY_BLOCKED" in str(exc)
    checks.append(("fail_closed_default", prop_blocked))

    corp_blocked = False
    try:
        enforce_data_residency("host kaizen-prod failed", data_classification="public_sanitized")
    except PermissionError as exc:
        corp_blocked = "DATA_RESIDENCY_BLOCKED" in str(exc)
    checks.append(("corp_ident_blocked", corp_blocked))

    secret_blocked = False
    try:
        enforce_data_residency("AIza" + "SampleKey" + "123456", data_classification="public_sanitized")
    except PermissionError as exc:
        secret_blocked = "DATA_RESIDENCY_BLOCKED" in str(exc)
    checks.append(("secret_blocked", secret_blocked))

    client = GeminiCloudClient(api_key="")
    gen_plan = client.generate(
        "Review sanitized algorithm",
        service_tier="flex",
        thinking_budget=512,
        include_thoughts=True,
        cached_content="cachedContents/demo1",
        response_json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        enable_google_search=True,
        data_classification="public_sanitized",
        dry_run=True,
    )
    gp = gen_plan["payload"]
    checks.append((
        "generate_wire_layout",
        gp.get("cachedContent") == "cachedContents/demo1"
        and "cachedContent" not in gp.get("generationConfig", {})
        and gp.get("serviceTier") == "flex"
        and gp.get("generationConfig", {}).get("thinkingConfig", {}).get("thinkingBudget") == 512
        and gp.get("generationConfig", {}).get("responseJsonSchema", {}).get("type") == "object",
    ))

    dec_plan = client.decide_noul(
        "def add(a, b): return a + b",
        "Does this function mutate global state?",
        data_classification="public_sanitized",
        dry_run=True,
    )
    checks.append(("decide_wire_layout", dec_plan["expected_output_tokens"] == 0 and dec_plan["url"].endswith("/decisions")))

    jsonl = client.build_batch_jsonl(
        [{"key": "k1", "contents": "Check pure function"}],
        cached_content="cachedContents/demo1",
        thinking_budget=256,
    )
    row = json.loads(jsonl.splitlines()[0])
    checks.append((
        "batch_jsonl_layout",
        row["key"] == "k1"
        and row["request"].get("cachedContent") == "cachedContents/demo1"
        and row["request"].get("generationConfig", {}).get("thinkingConfig", {}).get("thinkingBudget") == 256,
    ))
    return checks


def run_self_test() -> int:
    """Run deterministic offline checks for model normalization, DataResidencyGate, and wire formats."""
    checks = _verify_self_test_checks()
    failed = [name for name, ok in checks if not ok]
    if not failed:
        print(f"[PASS] gemini_cloud_client -- {len(checks)}/{len(checks)} offline residency & wire checks verified")
        return 0
    print(f"[FAIL] gemini_cloud_client -- failed checks: {failed}")
    return 1


def _parse_criteria_arg(raw_criteria: str, q_type: str) -> Any:
    if not raw_criteria:
        return None
    stripped = raw_criteria.strip()
    if stripped.startswith(("{", "[")):
        return json.loads(stripped)
    parts = [p.strip() for p in stripped.split(",") if p.strip()]
    if q_type == "choice":
        return {p: f"Option: {p}" for p in parts}
    return parts


def _build_parser() -> argparse.ArgumentParser:
    common_sub = argparse.ArgumentParser(add_help=False)
    common_sub.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    common_sub.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    common_sub.add_argument("--tier", default=argparse.SUPPRESS)
    common_sub.add_argument("--data-classification", default=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(
        prog="kaizen-kit gemini",
        description="Fail-closed Gemini Cloud REST client & Decision-1 CLI",
    )
    ap.add_argument("--json", action="store_true", dest="as_json", default=False)
    ap.add_argument("--dry-run", action="store_true", default=False)
    ap.add_argument("--tier", default="flex", choices=list(VALID_SERVICE_TIERS))
    ap.add_argument("--data-classification", default="corporate_proprietary")

    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", parents=[common_sub], help="Inspect verified Gemini models and API key presence")

    p_gen = sub.add_parser("generate", parents=[common_sub], help="Generate content (or dry-run plan)")
    p_gen.add_argument("--prompt", required=True)
    p_gen.add_argument("--model", default=DEFAULT_GENERATE_MODEL)
    p_gen.add_argument("--system", default=None)
    p_gen.add_argument("--thinking-budget", type=int, default=None)
    p_gen.add_argument("--thinking-level", default=None)
    p_gen.add_argument("--include-thoughts", action="store_true")
    p_gen.add_argument("--cached-content", default=None)
    p_gen.add_argument("--temperature", type=float, default=None)
    p_gen.add_argument("--max-output-tokens", type=int, default=None)

    p_dec = sub.add_parser("decide", parents=[common_sub], help="Run Decision-1 evaluation (or dry-run plan)")
    p_dec.add_argument("--input", required=True, dest="input_text")
    p_dec.add_argument("--question-type", default="noul", choices=list(VALID_DECISION_TYPES))
    p_dec.add_argument("--instructions", required=True)
    p_dec.add_argument("--criteria", default="")
    p_dec.add_argument("--model", default=DEFAULT_DECISION_MODEL)

    p_cache = sub.add_parser("cache-plan", parents=[common_sub], help="Build dry-run context cache creation plan")
    p_cache.add_argument("--model", default=DEFAULT_EXPLICIT_CACHE_MODEL)
    p_cache.add_argument("--content", default="Sanitized reference context")
    p_cache.add_argument("--system", default=None)
    p_cache.add_argument("--ttl", default="3600s")
    p_cache.add_argument("--display-name", default="kaizen-cache-plan")

    p_batch = sub.add_parser("batch-plan", parents=[common_sub], help="Build dry-run JSONL & inline batch plan")
    p_batch.add_argument("--model", default=DEFAULT_GENERATE_MODEL)
    p_batch.add_argument("--prompt", action="append", dest="prompts", default=[])
    p_batch.add_argument("--display-name", default="kaizen-batch-plan")
    p_batch.add_argument("--cached-content", default=None)
    p_batch.add_argument("--thinking-budget", type=int, default=None)
    p_batch.add_argument("--thinking-level", default=None)

    sub.add_parser("self-test", parents=[common_sub], help="Run offline residency and wire format self-tests")
    return ap


def _emit_output(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            print(f"{key}: {value}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint supporting status, generate, decide, cache-plan, batch-plan, and self-test."""
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    client = GeminiCloudClient()

    if args.cmd == "self-test":
        if args.as_json:
            checks = _verify_self_test_checks()
            passed = all(ok for _, ok in checks)
            _emit_output({"passed": passed, "checks": {k: v for k, v in checks}}, as_json=True)
            return 0 if passed else 1
        return run_self_test()

    if args.cmd == "status":
        status_payload = {
            "base_url": client.base_url,
            "api_key_configured": client.has_api_key(),
            "default_data_classification": "corporate_proprietary",
            "allowed_data_classification": ALLOWED_DATA_CLASSIFICATION,
            "model_count": len(GEMINI_MODELS),
            "models": {k: v.to_dict() for k, v in GEMINI_MODELS.items()},
        }
        _emit_output(status_payload, as_json=args.as_json)
        return 0

    try:
        if args.cmd == "generate":
            res = client.generate(
                args.prompt,
                model=args.model,
                system_instruction=args.system,
                service_tier=args.tier,
                thinking_budget=args.thinking_budget,
                thinking_level=args.thinking_level,
                include_thoughts=args.include_thoughts,
                cached_content=args.cached_content,
                temperature=args.temperature,
                max_output_tokens=args.max_output_tokens,
                data_classification=args.data_classification,
                dry_run=args.dry_run,
            )
            _emit_output(res, as_json=args.as_json)
            return 0

        if args.cmd == "decide":
            q_spec: Dict[str, Any] = {"type": args.question_type, "instructions": args.instructions}
            parsed_criteria = _parse_criteria_arg(args.criteria, args.question_type)
            if parsed_criteria is not None:
                q_spec["criteria"] = parsed_criteria
            res = client.decide(
                args.input_text,
                {"question_1": q_spec},
                model=args.model,
                data_classification=args.data_classification,
                dry_run=args.dry_run,
            )
            _emit_output(res, as_json=args.as_json)
            return 0

        if args.cmd == "cache-plan":
            res = client.create_cache(
                model=args.model,
                contents=args.content,
                system_instruction=args.system,
                ttl=args.ttl,
                display_name=args.display_name,
                data_classification=args.data_classification,
                dry_run=True,
            )
            _emit_output(res, as_json=args.as_json)
            return 0

        if args.cmd == "batch-plan":
            prompts = args.prompts or ["Sanitized batch item 1"]
            lvl, bgt = resolve_adaptive_thinking(
                args.model,
                effort_or_level=args.thinking_level,
                thinking_budget=args.thinking_budget,
            )
            items = [{"key": f"item-{i}", "contents": p} for i, p in enumerate(prompts)]
            jsonl_text = client.build_batch_jsonl(
                items,
                cached_content=args.cached_content,
                thinking_budget=bgt,
                thinking_level=lvl,
                service_tier=args.tier,
                data_classification=args.data_classification,
            )
            inline_plan = client.submit_batch_inline(
                model=args.model,
                requests=[
                    {
                        "key": f"item-{i}",
                        "contents": p,
                        "cached_content": args.cached_content,
                        "thinking_budget": bgt,
                        "thinking_level": lvl,
                        "service_tier": args.tier,
                    }
                    for i, p in enumerate(prompts)
                ],
                display_name=args.display_name,
                data_classification=args.data_classification,
                dry_run=True,
            )
            _emit_output(
                {
                    "dry_run": True,
                    "model": inline_plan["model"],
                    "jsonl_lines": jsonl_text.splitlines(),
                    "inline_plan": inline_plan,
                },
                as_json=args.as_json,
            )
            return 0
    except PermissionError as exc:
        err = {"blocked": True, "error": str(exc)}
        if args.as_json:
            print(json.dumps(err, indent=2))
        else:
            sys.stderr.write(f"{exc}\n")
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
