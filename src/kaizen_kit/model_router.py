#!/usr/bin/env python3
"""Unified Multi-Provider ModelRouter for Open-Source, Cloud, and Local Harnesses.

Routes without vendor or Gemini lock-in across:
  - OpenAI / ChatGPT / Codex (`gpt-5`, `gpt-4.1`, `gpt-4.1-mini`, `o3`, `o4-mini`,
    `codex-mini-latest`, `openai/*`) -> `OPENAI_BASE_URL` (default `https://api.openai.com/v1`).
  - Anthropic Claude API (`claude-sonnet-4`, `claude-opus-4`, `claude-haiku-4`,
    `anthropic/*`) -> `https://api.anthropic.com/v1/messages`.
  - Local Ollama (`ollama/*`, `OLLAMA_HOST` default `http://127.0.0.1:11434/v1`).
  - Generic OpenAI-Compatible (`openai_compat/*`, `vllm/*`, `litellm/*`, `openrouter/*`
    via `KAIZEN_LLM_BASE_URL` / `LITELLM_BASE_URL` / `OPENROUTER_API_KEY`).
  - AWS Bedrock Claude 5.5 (`haiku`, `sonnet`, `opus`, `fable`) -> `bedrock_bridge`.
  - EC2 OSS Sparse-MoE & micro pool (`qwen3.5-4b`, `qwen3-coder-30b-a3b`, `mini-pool`,
    `auto`) -> EC2 LiteLLM (`:4000`) with loopback fallback to JEV Gateway (`:8765`).
  - Optional Google Gemini Public API (`gemini-*`, fail-closed `DataResidencyGate`).
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from . import bedrock_bridge, gemini_cloud_client

DataResidencyGate = gemini_cloud_client.DataResidencyGate

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434/v1"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENAI_COMPAT_URL = "http://127.0.0.1:8000/v1"

PROVIDER_PRIORITY: Tuple[str, ...] = (
    "openai",
    "anthropic",
    "ollama",
    "ec2_litellm",
    "gemini",
)

SUPPORTED_DEFAULT_PROVIDERS = frozenset({
    "openai",
    "anthropic",
    "ollama",
    "openai_compat",
    "ec2_litellm",
    "gemini",
    "bedrock",
})

PROVIDER_NAME_ALIASES: Dict[str, str] = {
    "chatgpt": "openai",
    "codex": "openai",
    "claude": "anthropic",
    "local": "ollama",
    "vllm": "openai_compat",
    "litellm": "openai_compat",
    "openrouter": "openai_compat",
    "openai-compat": "openai_compat",
    "gemini_public_api": "gemini",
}

DEFAULT_PROVIDER_MODELS: Dict[str, str] = {
    "openai": "gpt-5.4",
    "anthropic": "claude-sonnet-5-5",
    "ollama": "ollama/qwen3.5-4b",
    "openai_compat": "openai_compat/qwen3.5-4b",
    "ec2_litellm": "qwen3.5-4b",
    "gemini": "gemini-4-hydrogen",
    "bedrock": "sonnet",
}

OPENAI_ALIASES: Dict[str, str] = {
    "gpt-5.4": "gpt-5.4",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.2-codex": "gpt-5.2-codex",
    "gpt-5": "gpt-5",
    "gpt-5-mini": "gpt-5-mini",
    "gpt-4.1": "gpt-4.1",
    "gpt-4.1-mini": "gpt-4.1-mini",
    "o3": "o3",
    "o4-mini": "o4-mini",
    "codex-mini-latest": "codex-mini-latest",
    "chatgpt": "gpt-5.4",
}

ANTHROPIC_ALIASES: Dict[str, str] = {
    "claude-sonnet-5-5": "claude-sonnet-5-5",
    "claude-opus-5-5": "claude-opus-5-5",
    "claude-haiku-5-5": "claude-haiku-5-5",
    "claude-fable-5": "claude-fable-5",
    "claude-5.5-sonnet": "claude-sonnet-5-5",
    "claude-5.5-opus": "claude-opus-5-5",
    "claude-5.5-haiku": "claude-haiku-5-5",
    "claude-sonnet-4": "claude-sonnet-4",
    "claude-opus-4": "claude-opus-4",
    "claude-haiku-4": "claude-haiku-4",
    "claude-4-sonnet": "claude-sonnet-4",
    "claude-4-opus": "claude-opus-4",
    "claude-4-haiku": "claude-haiku-4",
}

OLLAMA_ALIASES: Dict[str, str] = {
    "llama3.3": "llama3.3",
    "llama3.2": "llama3.2",
    "codestral": "codestral",
    "devstral-small": "devstral-small",
    "deepseek-v3": "deepseek-v3",
    "deepseek-r1": "deepseek-r1",
    "qwen2.5-coder": "qwen2.5-coder",
    "deepseek-coder-v2": "deepseek-coder-v2",
}

OPENAI_COMPAT_PREFIXES: Tuple[str, ...] = (
    "openai_compat/",
    "openai_compat:",
    "openai-compat/",
    "openai-compat:",
    "vllm/",
    "vllm:",
    "litellm/",
    "litellm:",
    "openrouter/",
    "openrouter:",
)

BEDROCK_ALIASES: Dict[str, str] = {
    "haiku": "claude-haiku-5-5",
    "haiku-5-5": "claude-haiku-5-5",
    "claude-haiku": "claude-haiku-5-5",
    "claude-haiku-5-5": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "sonnet-5-5": "claude-sonnet-5-5",
    "claude-sonnet": "claude-sonnet-5-5",
    "claude-sonnet-5-5": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
    "opus-5-5": "claude-opus-5-5",
    "claude-opus": "claude-opus-5-5",
    "claude-opus-5-5": "claude-opus-5-5",
    "fable": "claude-fable-5",
    "fable-5": "claude-fable-5",
    "claude-fable": "claude-fable-5",
    "claude-fable-5": "claude-fable-5",
}

OSS_ALIASES: Dict[str, str] = {
    "mini-pool": "mini-pool",
    "auto": "auto",
    "bitnet": "bitnet-2b",
    "bitnet-2b": "bitnet-2b",
    "bitnet-b1.58-2b-4t": "bitnet-2b",
    "qwen3.5-0.8b": "qwen3.5-0.8b",
    "qwen3.5-2b": "qwen3.5-2b",
    "qwen3.5-4b": "qwen3.5-4b",
    "gemma-4-e2b": "gemma-4-e2b",
    "gemma-4-12b-unified": "gemma-4-12b-unified",
    "granite-4-h-tiny": "granite-4-h-tiny",
    "granite-4.0-h-tiny": "granite-4-h-tiny",
    "qwen3-guard": "qwen3-guard-0.6b",
    "qwen3-guard-0.6b": "qwen3-guard-0.6b",
    "qwen3-guard-stream-0.6b": "qwen3-guard-stream-0.6b",
    "gemma4": "gemma-4-26b-a4b",
    "gemma-4-26b-a4b": "gemma-4-26b-a4b",
    "qwen3-30b-a3b": "qwen3-coder-30b-a3b",
    "qwen3-coder-30b-a3b": "qwen3-coder-30b-a3b",
    "qwen3.5-35b-a3b": "qwen3.5-35b-a3b",
    "glm-4.7-flash": "glm-4.7-flash",
    "ling-mini-2.0": "ling-mini-2.0",
    "nemotron-nano-9b-v2": "nemotron-nano-9b-v2",
    "nemotron-nano-9b": "nemotron-nano-9b-v2",
    "nemotron-nano": "nemotron-nano-9b-v2",
    "nemotron-3-nano-30b-a3b": "nemotron-3-nano-30b-a3b",
    "nemotron-30b-a3b": "nemotron-3-nano-30b-a3b",
    "nemotron-super-49b": "nemotron-super-49b",
    "deepseek-v3.2": "deepseek-v3.2",
    "grok-3": "grok-3",
    "grok-code": "grok-code",
    "kimi-k2": "kimi-k2-chat",
    "kimi-k2.5": "kimi-k2.5",
}

DEPRECATED_OSS_ALIASES: Dict[str, str] = {
    "qwen3b": "qwen3.5-4b",
    "qwen2.5-coder-3b": "qwen3.5-4b",
    "qwen-coder": "qwen3-coder-30b-a3b",
    "phi4": "qwen3.5-4b",
    "phi-4-mini": "qwen3.5-4b",
    "phi-4-reasoning-plus": "qwen3.5-35b-a3b",
    "gemma3": "gemma-4-e2b",
    "gemma-3": "gemma-4-e2b",
    "nemotron": "granite-4-h-tiny",
    "nemotron-mini": "granite-4-h-tiny",
    "deepseek-r1-14b": "qwen3.5-35b-a3b",
    "deepseek-r1-distill-qwen-14b": "qwen3.5-35b-a3b",
    "ring-mini-2.0": "ling-mini-2.0",
}

BURST_ALIASES: Dict[str, str] = {
    "gpu-burst": "Qwen/Qwen3-32B-AWQ",
    "qwen3-32b-awq": "Qwen/Qwen3-32B-AWQ",
    "deepseek-r1-32b-awq": "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
    "qwen3-coder-next": "Qwen/Qwen3-Coder-Next",
    "qwen3.5-122b-a10b": "Qwen/Qwen3.5-122B-A10B",
    "glm-4.5-air": "zai-org/GLM-4.5-Air",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "qwen3-235b-a22b-awq": "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "distributed-256gb": "Qwen/Qwen3-235B-A22B-Instruct-2507-GGUF",
    "distributed_256gb": "Qwen/Qwen3-235B-A22B-Instruct-2507-GGUF",
    "qwen3-235b-a22b-rpc": "Qwen/Qwen3-235B-A22B-Instruct-2507-GGUF",
}

GEMINI_ALIASES: Dict[str, str] = {
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


def is_local_bedrock_disabled() -> bool:
    return os.environ.get("KAIZEN_DISABLE_LOCAL_BEDROCK", "").strip() == "1"


def _require_loopback(url: str) -> str:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"Router endpoint scheme must be http or https, got {scheme!r}")
    host = (parsed.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"Router endpoint must be loopback, got host {host!r}")
    return url.rstrip("/")


def _validate_http_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Provider endpoint must be a valid http(s) URL, got {url!r}")
    return url.rstrip("/")


def _normalize_openai_base_url(raw_url: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> str:
    env_map = os.environ if env is None else env
    candidate = (raw_url or env_map.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL).strip()
    return _validate_http_url(candidate)


def _normalize_anthropic_endpoint(raw_url: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> str:
    env_map = os.environ if env is None else env
    candidate = (
        raw_url
        or env_map.get("ANTHROPIC_MESSAGES_URL")
        or env_map.get("ANTHROPIC_BASE_URL")
        or DEFAULT_ANTHROPIC_URL
    ).strip()
    validated = _validate_http_url(candidate)
    if validated.endswith("/messages"):
        return validated
    if validated.endswith("/v1"):
        return f"{validated}/messages"
    return f"{validated}/v1/messages"


def _normalize_ollama_endpoint(raw_host: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> str:
    env_map = os.environ if env is None else env
    candidate = (raw_host or env_map.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).strip().rstrip("/")
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    validated = _validate_http_url(candidate)
    if not validated.endswith("/v1"):
        return f"{validated}/v1"
    return validated


def _resolve_openai_compat_config(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[str, str, str]:
    """Return `(normalized_v1_url, resolved_api_key, provider_hint)`."""
    env_map = os.environ if env is None else env
    raw = (
        base_url
        or env_map.get("KAIZEN_LLM_BASE_URL")
        or env_map.get("LITELLM_BASE_URL")
        or (DEFAULT_OPENROUTER_BASE_URL if env_map.get("OPENROUTER_API_KEY", "").strip() else DEFAULT_OPENAI_COMPAT_URL)
    ).strip()
    validated = _validate_http_url(raw)
    parsed = urlparse(validated)
    if parsed.path in ("", "/"):
        validated = f"{validated}/v1"
    resolved_key = (
        api_key
        or env_map.get("KAIZEN_LLM_API_KEY")
        or env_map.get("LITELLM_API_KEY")
        or env_map.get("OPENROUTER_API_KEY")
        or env_map.get("OPENAI_API_KEY")
        or "local"
    ).strip()
    hint = "openrouter-api" if "openrouter.ai" in (parsed.hostname or "").lower() else "openai-compatible"
    return validated, resolved_key, hint


def _port_open(url: str, timeout: float = 0.25) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


def _post_json_with_headers(
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    timeout: float,
) -> Dict[str, Any]:
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"Upstream HTTP {exc.code} from {url}: {body}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"Request to {url} failed: {exc}") from exc


def _post_json(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    return _post_json_with_headers(
        url,
        payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
        timeout=timeout,
    )


def detect_available_providers(
    env: Optional[Mapping[str, str]] = None,
    *,
    probe_local_ports: bool = True,
    litellm_url: str = "http://127.0.0.1:4000",
) -> List[str]:
    """Detect configured/reachable LLM providers without requiring Gemini."""
    env_map = os.environ if env is None else env
    found: List[str] = []

    if env_map.get("OPENAI_API_KEY", "").strip():
        found.append("openai")
    if env_map.get("ANTHROPIC_API_KEY", "").strip():
        found.append("anthropic")

    ollama_ep = _normalize_ollama_endpoint(env=env_map)
    if env_map.get("OLLAMA_HOST", "").strip() or (probe_local_ports and _port_open(ollama_ep, timeout=0.15)):
        found.append("ollama")

    has_compat_env = any(
        env_map.get(k, "").strip() for k in ("KAIZEN_LLM_BASE_URL", "LITELLM_BASE_URL", "OPENROUTER_API_KEY")
    )
    if has_compat_env:
        found.append("openai_compat")

    litellm_ep = (env_map.get("KAIZEN_LITELLM_URL") or litellm_url).strip()
    if env_map.get("KAIZEN_LITELLM_URL", "").strip() or (probe_local_ports and _port_open(litellm_ep, timeout=0.15)):
        found.append("ec2_litellm")

    if env_map.get("GEMINI_API_KEY", "").strip() or env_map.get("GOOGLE_API_KEY", "").strip():
        found.append("gemini")

    if env_map.get("AWS_PROFILE", "").strip() and env_map.get("KAIZEN_DISABLE_LOCAL_BEDROCK", "").strip() != "1":
        found.append("bedrock")

    return found


def resolve_default_provider(
    env: Optional[Mapping[str, str]] = None,
    *,
    probe_local_ports: bool = True,
    litellm_url: str = "http://127.0.0.1:4000",
) -> str:
    """Resolve default provider: honors `KAIZEN_DEFAULT_PROVIDER`, then `openai -> anthropic -> ollama -> ec2_litellm -> gemini`."""
    env_map = os.environ if env is None else env
    explicit = (env_map.get("KAIZEN_DEFAULT_PROVIDER") or "").strip().lower()
    if explicit:
        mapped = PROVIDER_NAME_ALIASES.get(explicit, explicit)
        if mapped not in SUPPORTED_DEFAULT_PROVIDERS:
            raise ValueError(
                f"Unsupported KAIZEN_DEFAULT_PROVIDER={explicit!r}; expected one of {sorted(SUPPORTED_DEFAULT_PROVIDERS)}"
            )
        return mapped

    available = set(
        detect_available_providers(env=env_map, probe_local_ports=probe_local_ports, litellm_url=litellm_url)
    )
    for candidate in (*PROVIDER_PRIORITY, "openai_compat", "bedrock"):
        if candidate in available:
            return candidate
    return "ollama"


@dataclass(frozen=True)
class RouteTarget:
    """Resolved routing target for a requested model alias."""

    requested_model: str
    canonical_model: str
    backend: str
    endpoint: str
    provider_hint: str
    requires_local_aws_profile: bool


def is_bedrock_model(model: str) -> bool:
    key = (model or "sonnet").strip().lower()
    if key in BEDROCK_ALIASES:
        return True
    return key.startswith("arn:aws:bedrock:") or key.startswith("anthropic.")


def is_openai_model(model: str) -> bool:
    key = (model or "").strip().lower()
    if key in BURST_ALIASES:
        return False
    if key in OPENAI_ALIASES:
        return True
    return key.startswith(("openai/", "openai:", "gpt-", "codex-"))


def is_anthropic_model(model: str) -> bool:
    key = (model or "").strip().lower()
    if is_bedrock_model(key):
        return False
    if key in ANTHROPIC_ALIASES:
        return True
    return key.startswith(("anthropic/", "anthropic:", "claude-3-", "claude-4-"))


def is_ollama_model(model: str) -> bool:
    key = (model or "").strip().lower()
    return key in OLLAMA_ALIASES or key.startswith(("ollama/", "ollama:"))


def is_openai_compat_model(model: str) -> bool:
    key = (model or "").strip().lower()
    return key.startswith(OPENAI_COMPAT_PREFIXES)


def is_gemini_model(model: str) -> bool:
    """Return True when the model name targets Google Gemini Developer Public API."""
    key = (model or "").strip().lower()
    bare = key[7:] if key.startswith("models/") else key
    return bare in GEMINI_ALIASES or bare in gemini_cloud_client.GEMINI_MODELS or bare.startswith("gemini-")


def _strip_provider_prefix(model: str, prefixes: Tuple[str, ...]) -> str:
    low = model.lower()
    for pfx in prefixes:
        if low.startswith(pfx):
            return model[len(pfx) :].strip()
    return model


def _resolve_non_legacy_provider_target(
    cleaned: str,
    key: str,
    *,
    openai_base_url: Optional[str],
    anthropic_url: Optional[str],
    ollama_host: Optional[str],
    openai_compat_url: Optional[str],
) -> Optional[RouteTarget]:
    if is_openai_compat_model(cleaned):
        bare = _strip_provider_prefix(cleaned, OPENAI_COMPAT_PREFIXES)
        compat_ep, _, compat_hint = _resolve_openai_compat_config(base_url=openai_compat_url)
        return RouteTarget(cleaned, bare, "openai_compat", compat_ep, compat_hint, False)

    if is_openai_model(cleaned):
        bare = _strip_provider_prefix(cleaned, ("openai/", "openai:"))
        canonical = OPENAI_ALIASES.get(bare.lower(), bare)
        return RouteTarget(
            cleaned, canonical, "openai", _normalize_openai_base_url(openai_base_url), "openai-api", False
        )

    if is_anthropic_model(cleaned):
        bare = _strip_provider_prefix(cleaned, ("anthropic/", "anthropic:"))
        canonical = ANTHROPIC_ALIASES.get(bare.lower(), bare)
        return RouteTarget(
            cleaned, canonical, "anthropic", _normalize_anthropic_endpoint(anthropic_url), "anthropic-messages-api", False
        )

    if is_ollama_model(cleaned):
        bare = _strip_provider_prefix(cleaned, ("ollama/", "ollama:"))
        canonical = OLLAMA_ALIASES.get(bare.lower(), bare)
        return RouteTarget(
            cleaned, canonical, "ollama", _normalize_ollama_endpoint(ollama_host), "ollama-local", False
        )

    return None


def resolve_model_target(
    model: str = "sonnet",
    litellm_url: str = "http://127.0.0.1:4000",
    gateway_url: str = "http://127.0.0.1:8765",
    bridge_url: str = "http://127.0.0.1:18889",
    burst_url: str = "http://127.0.0.1:8000",
    prefer_bridge_http: bool = False,
    *,
    openai_base_url: Optional[str] = None,
    anthropic_url: Optional[str] = None,
    ollama_host: Optional[str] = None,
    openai_compat_url: Optional[str] = None,
) -> RouteTarget:
    """Resolve a model alias to OpenAI, Anthropic, Ollama, OpenAI-Compat, Bedrock, EC2 OSS, GPU Burst, or Gemini."""
    cleaned = (model or "sonnet").strip()
    if cleaned.lower() in ("default", "auto-provider"):
        default_prov = resolve_default_provider(litellm_url=litellm_url)
        cleaned = DEFAULT_PROVIDER_MODELS[default_prov]
    key = cleaned.lower()

    if is_bedrock_model(cleaned):
        canonical = BEDROCK_ALIASES.get(key, cleaned)
        endpoint = f"{_require_loopback(bridge_url)}/v1" if prefer_bridge_http else f"bedrock://{bedrock_bridge.AWS_REGION}/{bedrock_bridge.AWS_PROFILE}"
        return RouteTarget(cleaned, canonical, "bedrock", endpoint, "aws-bedrock-claude-5.5", True)

    if key in BURST_ALIASES:
        safe_burst = _require_loopback(burst_url)
        return RouteTarget(cleaned, BURST_ALIASES[key], "ephemeral_gpu_burst", f"{safe_burst}/v1", "ec2-spot-gpu-burst", False)

    direct = _resolve_non_legacy_provider_target(
        cleaned,
        key,
        openai_base_url=openai_base_url,
        anthropic_url=anthropic_url,
        ollama_host=ollama_host,
        openai_compat_url=openai_compat_url,
    )
    if direct is not None:
        return direct

    if is_gemini_model(cleaned):
        bare = key[7:] if key.startswith("models/") else key
        mapped = GEMINI_ALIASES.get(bare, bare)
        canonical, _ = gemini_cloud_client.normalize_gemini_model(mapped)
        canonical = GEMINI_ALIASES.get(canonical, canonical)
        return RouteTarget(
            cleaned, canonical, "gemini_public_api", gemini_cloud_client.DEFAULT_BASE_URL, "google-gemini-developer-api", False
        )

    is_known_ec2_oss = key in OSS_ALIASES or key in DEPRECATED_OSS_ALIASES
    if not is_known_ec2_oss:
        default_env_prov = PROVIDER_NAME_ALIASES.get(
            os.environ.get("KAIZEN_DEFAULT_PROVIDER", "").strip().lower(),
            os.environ.get("KAIZEN_DEFAULT_PROVIDER", "").strip().lower(),
        )
        if default_env_prov == "ollama" or (":" in key and os.environ.get("OLLAMA_HOST", "").strip()):
            return RouteTarget(cleaned, cleaned, "ollama", _normalize_ollama_endpoint(ollama_host), "ollama-local", False)
        if default_env_prov == "openai_compat":
            compat_ep, _, compat_hint = _resolve_openai_compat_config(base_url=openai_compat_url)
            return RouteTarget(cleaned, cleaned, "openai_compat", compat_ep, compat_hint, False)

    canonical_oss = OSS_ALIASES.get(key) or DEPRECATED_OSS_ALIASES.get(key, cleaned)
    safe_litellm = _require_loopback(litellm_url)
    safe_gateway = _require_loopback(gateway_url)
    if _port_open(safe_litellm):
        return RouteTarget(cleaned, canonical_oss, "ec2_litellm", f"{safe_litellm}/v1", "ec2-litellm", False)
    return RouteTarget(cleaned, canonical_oss, "ec2_gateway", f"{safe_gateway}/v1", "ec2-jev-gateway", False)


def _messages_to_gemini_inputs(messages: List[Dict[str, Any]]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    system_parts: List[str] = []
    contents: List[Dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role") or "user").strip().lower()
        text = str(msg.get("content") or "")
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        contents.append({"role": gemini_role, "parts": [{"text": text}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]
    return ("\n\n".join(system_parts) if system_parts else None), contents


def _openai_tools_to_gemini_decls(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None
    decls: List[Dict[str, Any]] = []
    for item in tools:
        if item.get("type") == "function" and isinstance(item.get("function"), dict):
            decls.append(dict(item["function"]))
        else:
            decls.append(dict(item))
    return decls or None


def _extract_gemini_text(resp: Dict[str, Any]) -> str:
    candidates = resp.get("candidates") or []
    if candidates and isinstance(candidates[0], dict):
        parts = (candidates[0].get("content") or {}).get("parts") or []
        non_thought = [str(p["text"]) for p in parts if isinstance(p, dict) and p.get("text") and not p.get("thought")]
        if non_thought:
            return "".join(non_thought)
        all_text = [str(p["text"]) for p in parts if isinstance(p, dict) and p.get("text")]
        if all_text:
            return "".join(all_text)
    choices = resp.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return str((choices[0].get("message") or {}).get("content") or "")
    return str(resp.get("text") or "")


def _normalize_schema_params(
    response_format: Optional[Dict[str, Any]],
    json_schema: Optional[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Normalize Lever A structured output parameters into (openai_response_format, raw_json_schema)."""
    raw_schema: Optional[Dict[str, Any]] = dict(json_schema) if isinstance(json_schema, dict) else None
    openai_fmt: Optional[Dict[str, Any]] = dict(response_format) if isinstance(response_format, dict) else None

    if raw_schema is None and openai_fmt is not None:
        nested = openai_fmt.get("json_schema")
        if isinstance(nested, dict):
            raw_schema = dict(nested["schema"]) if isinstance(nested.get("schema"), dict) else dict(nested)
        elif isinstance(openai_fmt.get("schema"), dict):
            raw_schema = dict(openai_fmt["schema"])

    if openai_fmt is None and raw_schema is not None:
        openai_fmt = {
            "type": "json_schema",
            "json_schema": {
                "name": "structured_output",
                "strict": True,
                "schema": raw_schema,
            },
        }
    return openai_fmt, raw_schema


def _apply_capc_to_messages(
    messages: List[Dict[str, Any]],
    capc_static_prefix: Optional[str],
    auto_compress_context: bool,
    max_dynamic_chars: int = 12000,
) -> List[Dict[str, Any]]:
    """Apply CAPC prefix-first ordering & dynamic-only context pruning without mutating caller messages."""
    if capc_static_prefix is None and not auto_compress_context:
        return messages

    cloned = [dict(m) for m in messages]
    user_idx = -1
    for idx in range(len(cloned) - 1, -1, -1):
        if str(cloned[idx].get("role") or "user").strip().lower() != "system":
            user_idx = idx
            break
    if user_idx < 0:
        return cloned

    dyn_text = str(cloned[user_idx].get("content") or "")
    if auto_compress_context and len(dyn_text) > max_dynamic_chars:
        budget = max(64, int(max_dynamic_chars))
        head = dyn_text[: int(budget * 0.45)]
        tail = dyn_text[-int(budget * 0.45) :]
        marker = f"\n...[CAPC pruned {len(dyn_text) - budget} volatile chars]...\n"
        dyn_text = f"{head}{marker}{tail}"[:budget]

    if capc_static_prefix:
        ordered = gemini_cloud_client.build_capc_ordered_prompt(capc_static_prefix, dyn_text)
        cloned[user_idx]["content"] = ordered["prompt"]
    else:
        cloned[user_idx]["content"] = dyn_text
    return cloned


def _messages_to_anthropic_inputs(
    messages: List[Dict[str, Any]],
    raw_schema: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    system_parts: List[str] = []
    anth_messages: List[Dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role") or "user").strip().lower()
        text = str(msg.get("content") or "")
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        anth_role = "assistant" if role == "assistant" else "user"
        anth_messages.append({"role": anth_role, "content": text})
    if raw_schema is not None:
        system_parts.append(
            f"Respond strictly with valid JSON matching this JSON Schema: {json.dumps(raw_schema, sort_keys=True)}"
        )
    if not anth_messages:
        anth_messages = [{"role": "user", "content": ""}]
    return ("\n\n".join(system_parts) if system_parts else None), anth_messages


def _openai_tools_to_anthropic_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None
    converted: List[Dict[str, Any]] = []
    for item in tools:
        if item.get("type") == "function" and isinstance(item.get("function"), dict):
            fn = item["function"]
            converted.append({
                "name": str(fn.get("name") or "tool"),
                "description": str(fn.get("description") or ""),
                "input_schema": dict(fn.get("parameters") or {"type": "object"}),
            })
        else:
            converted.append(dict(item))
    return converted or None


def _extract_anthropic_text(resp: Dict[str, Any]) -> str:
    blocks = resp.get("content")
    if isinstance(blocks, list):
        texts = [
            str(b.get("text") or "")
            for b in blocks
            if isinstance(b, dict) and b.get("type", "text") == "text" and b.get("text")
        ]
        if texts:
            return "".join(texts)
    choices = resp.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return str((choices[0].get("message") or {}).get("content") or "")
    return str(resp.get("text") or "")


def _is_loopback_endpoint(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in LOOPBACK_HOSTS


class ModelRouter:
    """Routes LLM calls across OpenAI, Anthropic, Ollama, OpenAI-Compat, Bedrock, EC2 OSS, GPU Burst, and Gemini."""

    def __init__(
        self,
        litellm_url: Optional[str] = None,
        gateway_url: Optional[str] = None,
        bridge_url: Optional[str] = None,
        burst_url: Optional[str] = None,
        prefer_bridge_http: bool = False,
        timeout_s: float = 90.0,
        *,
        openai_base_url: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        anthropic_url: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        ollama_host: Optional[str] = None,
        openai_compat_url: Optional[str] = None,
        openai_compat_api_key: Optional[str] = None,
    ) -> None:
        self.litellm_url = _require_loopback(litellm_url or os.environ.get("KAIZEN_LITELLM_URL") or "http://127.0.0.1:4000")
        self.gateway_url = _require_loopback(gateway_url or os.environ.get("KAIZEN_MINI_POOL_URL") or "http://127.0.0.1:8765")
        self.bridge_url = _require_loopback(bridge_url or os.environ.get("KAIZEN_BEDROCK_URL") or f"http://127.0.0.1:{bedrock_bridge.DEFAULT_PORT}")
        self.burst_url = _require_loopback(burst_url or os.environ.get("KAIZEN_GPU_BURST_URL") or "http://127.0.0.1:8000")
        self.prefer_bridge_http = prefer_bridge_http
        self.timeout_s = float(timeout_s)
        self.openai_base_url = openai_base_url
        self.openai_api_key = openai_api_key
        self.anthropic_url = anthropic_url
        self.anthropic_api_key = anthropic_api_key
        self.ollama_host = ollama_host
        self.openai_compat_url = openai_compat_url
        self.openai_compat_api_key = openai_compat_api_key

    def resolve(self, model: str = "sonnet") -> RouteTarget:
        return resolve_model_target(
            model=model,
            litellm_url=self.litellm_url,
            gateway_url=self.gateway_url,
            bridge_url=self.bridge_url,
            burst_url=self.burst_url,
            prefer_bridge_http=self.prefer_bridge_http,
            openai_base_url=self.openai_base_url,
            anthropic_url=self.anthropic_url,
            ollama_host=self.ollama_host,
            openai_compat_url=self.openai_compat_url,
        )

    def resolve_default(self) -> RouteTarget:
        return self.resolve("default")

    def invoke(
        self,
        messages: List[Dict[str, Any]],
        model: str = "sonnet",
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        data_classification: str = "corporate_proprietary",
        service_tier: str = "flex",
        response_format: Optional[Dict[str, Any]] = None,
        json_schema: Optional[Dict[str, Any]] = None,
        thinking_level: Optional[str] = None,
        thinking_budget: Optional[int] = None,
        effort: Optional[str] = None,
        capc_static_prefix: Optional[str] = None,
        auto_compress_context: bool = False,
    ) -> Dict[str, Any]:
        target = self.resolve(model)
        prepared_messages = _apply_capc_to_messages(
            messages,
            capc_static_prefix=capc_static_prefix,
            auto_compress_context=auto_compress_context,
        )
        openai_fmt, raw_schema = _normalize_schema_params(response_format, json_schema)
        if target.backend == "gemini_public_api":
            return self._invoke_gemini(
                prepared_messages,
                target,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                data_classification=data_classification,
                service_tier=service_tier,
                response_json_schema=raw_schema,
                thinking_level=thinking_level,
                thinking_budget=thinking_budget,
                effort=effort,
            )
        if target.backend == "anthropic":
            return self._invoke_anthropic(
                prepared_messages,
                target,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                raw_schema=raw_schema,
            )

        payload: Dict[str, Any] = {
            "model": target.canonical_model,
            "messages": prepared_messages,
            "max_tokens": int(max_tokens),
        }
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if tools:
            payload["tools"] = tools
        if openai_fmt is not None:
            payload["response_format"] = openai_fmt

        if target.backend == "openai":
            return self._invoke_openai(payload, target)
        if target.backend == "ollama":
            return self._invoke_ollama(payload, target)
        if target.backend == "openai_compat":
            return self._invoke_openai_compat(payload, target)
        if target.backend == "bedrock":
            return self._invoke_bedrock(payload, target)
        if target.backend == "ephemeral_gpu_burst":
            return self._invoke_burst(payload, target)
        return self._invoke_ec2_oss(payload, target)

    def _invoke_gemini(
        self,
        messages: List[Dict[str, Any]],
        target: RouteTarget,
        *,
        max_tokens: int,
        temperature: Optional[float],
        tools: Optional[List[Dict[str, Any]]],
        data_classification: str,
        service_tier: str,
        response_json_schema: Optional[Dict[str, Any]] = None,
        thinking_level: Optional[str] = None,
        thinking_budget: Optional[int] = None,
        effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        system_instruction, contents = _messages_to_gemini_inputs(messages)
        fn_decls = _openai_tools_to_gemini_decls(tools)
        serialized = json.dumps(
            {
                "system": system_instruction,
                "contents": contents,
                "tools": fn_decls,
                "response_json_schema": response_json_schema,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        gemini_cloud_client.enforce_data_residency(serialized, data_classification=data_classification)
        effective_effort = thinking_level if thinking_level is not None else effort
        resolved_lvl, resolved_bgt = gemini_cloud_client.resolve_adaptive_thinking(
            target.canonical_model,
            effort_or_level=effective_effort,
            thinking_budget=thinking_budget,
        )
        gen_kwargs: Dict[str, Any] = {
            "model": target.canonical_model,
            "system_instruction": system_instruction,
            "service_tier": service_tier,
            "function_declarations": fn_decls,
            "temperature": temperature,
            "max_output_tokens": int(max_tokens),
            "data_classification": data_classification,
        }
        if response_json_schema is not None:
            gen_kwargs["response_json_schema"] = response_json_schema
        if resolved_lvl is not None:
            gen_kwargs["thinking_level"] = resolved_lvl
        if resolved_bgt is not None:
            gen_kwargs["thinking_budget"] = resolved_bgt

        client = gemini_cloud_client.GeminiCloudClient()
        resp = client.generate(contents, **gen_kwargs)
        return {
            "model": target.canonical_model,
            "backend": target.backend,
            "service_tier": service_tier,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": _extract_gemini_text(resp)}}],
            "usage": resp.get("usageMetadata") or {},
            "raw": resp,
        }

    def _invoke_openai(self, payload: Dict[str, Any], target: RouteTarget) -> Dict[str, Any]:
        api_key = (self.openai_api_key or os.environ.get("OPENAI_API_KEY") or "").strip()
        if not api_key and not _is_loopback_endpoint(target.endpoint):
            raise RuntimeError(
                f"OPENAI_API_KEY is required to invoke OpenAI model {target.canonical_model!r} at {target.endpoint}."
            )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key or 'local'}",
        }
        return _post_json_with_headers(f"{target.endpoint}/chat/completions", payload, headers, self.timeout_s)

    def _invoke_anthropic(
        self,
        messages: List[Dict[str, Any]],
        target: RouteTarget,
        *,
        max_tokens: int,
        temperature: Optional[float],
        tools: Optional[List[Dict[str, Any]]],
        raw_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        api_key = (self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key and not _is_loopback_endpoint(target.endpoint):
            raise RuntimeError(
                f"ANTHROPIC_API_KEY is required to invoke Anthropic model {target.canonical_model!r} at {target.endpoint}."
            )
        system_text, anth_messages = _messages_to_anthropic_inputs(messages, raw_schema=raw_schema)
        anth_payload: Dict[str, Any] = {
            "model": target.canonical_model,
            "messages": anth_messages,
            "max_tokens": int(max_tokens),
        }
        if system_text:
            anth_payload["system"] = system_text
        if temperature is not None:
            anth_payload["temperature"] = float(temperature)
        anth_tools = _openai_tools_to_anthropic_tools(tools)
        if anth_tools:
            anth_payload["tools"] = anth_tools

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key or "local",
            "anthropic-version": "2023-06-01",
        }
        resp = _post_json_with_headers(target.endpoint, anth_payload, headers, self.timeout_s)
        return {
            "model": target.canonical_model,
            "backend": target.backend,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": _extract_anthropic_text(resp)}}],
            "usage": resp.get("usage") or {},
            "raw": resp,
        }

    def _invoke_ollama(self, payload: Dict[str, Any], target: RouteTarget) -> Dict[str, Any]:
        api_key = (os.environ.get("OLLAMA_API_KEY") or "ollama").strip()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        return _post_json_with_headers(f"{target.endpoint}/chat/completions", payload, headers, self.timeout_s)

    def _invoke_openai_compat(self, payload: Dict[str, Any], target: RouteTarget) -> Dict[str, Any]:
        _, api_key, _ = _resolve_openai_compat_config(
            base_url=self.openai_compat_url,
            api_key=self.openai_compat_api_key,
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        return _post_json_with_headers(f"{target.endpoint}/chat/completions", payload, headers, self.timeout_s)

    def _invoke_bedrock(self, payload: Dict[str, Any], target: RouteTarget) -> Dict[str, Any]:
        if is_local_bedrock_disabled():
            raise RuntimeError(
                "Local AWS Bedrock invocation is disabled (KAIZEN_DISABLE_LOCAL_BEDROCK=1); "
                "AWS credentials must remain isolated on the local Windows workstation."
            )
        if self.prefer_bridge_http:
            return _post_json(f"{target.endpoint}/chat/completions", payload, self.timeout_s)
        return bedrock_bridge.invoke_bedrock(payload, model_alias=target.canonical_model)

    def _invoke_burst(self, payload: Dict[str, Any], target: RouteTarget) -> Dict[str, Any]:
        if not _port_open(self.burst_url):
            raise RuntimeError(
                f"Ephemeral Spot GPU Burst endpoint {self.burst_url} is STOPPED/closed for "
                f"{target.canonical_model}; use Bedrock Claude 5.5 or run `kaizen-kit gpu-burst plan`."
            )
        return _post_json(f"{target.endpoint}/chat/completions", payload, self.timeout_s)

    def _invoke_ec2_oss(self, payload: Dict[str, Any], target: RouteTarget) -> Dict[str, Any]:
        candidates: List[Tuple[str, str]] = [
            (f"{self.litellm_url}/v1/chat/completions", self.litellm_url),
            (f"{self.gateway_url}/v1/chat/completions", self.gateway_url),
        ]
        if target.backend == "ec2_gateway":
            candidates.reverse()

        errors: List[str] = []
        for chat_url, base_url in candidates:
            if not _port_open(base_url):
                errors.append(f"{base_url} closed")
                continue
            try:
                return _post_json(chat_url, payload, self.timeout_s)
            except RuntimeError as exc:
                errors.append(str(exc))

        raise RuntimeError(f"EC2 OSS endpoints unreachable for {target.canonical_model}: {', '.join(errors)}")

    def chat_text(
        self,
        prompt: str,
        system: str = "",
        model: str = "sonnet",
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        data_classification: str = "corporate_proprietary",
        service_tier: str = "flex",
        response_format: Optional[Dict[str, Any]] = None,
        json_schema: Optional[Dict[str, Any]] = None,
        thinking_level: Optional[str] = None,
        thinking_budget: Optional[int] = None,
        effort: Optional[str] = None,
        capc_static_prefix: Optional[str] = None,
        auto_compress_context: bool = False,
    ) -> str:
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = self.invoke(
            messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            data_classification=data_classification,
            service_tier=service_tier,
            response_format=response_format,
            json_schema=json_schema,
            thinking_level=thinking_level,
            thinking_budget=thinking_budget,
            effort=effort,
            capc_static_prefix=capc_static_prefix,
            auto_compress_context=auto_compress_context,
        )
        choices = resp.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return str(msg.get("content") or "")

    def list_models(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        openai_ep = _normalize_openai_base_url(self.openai_base_url)
        for canonical in sorted(set(OPENAI_ALIASES.values())):
            entries.append({"id": canonical, "backend": "openai", "endpoint": openai_ep})
        anth_ep = _normalize_anthropic_endpoint(self.anthropic_url)
        for canonical in sorted(set(ANTHROPIC_ALIASES.values())):
            entries.append({"id": canonical, "backend": "anthropic", "endpoint": anth_ep})
        ollama_ep = _normalize_ollama_endpoint(self.ollama_host)
        for canonical in sorted(set(OLLAMA_ALIASES.values())):
            entries.append({"id": canonical, "backend": "ollama", "endpoint": ollama_ep})
        for canonical in sorted(set(BEDROCK_ALIASES.values())):
            entries.append({"id": canonical, "backend": "bedrock", "region": bedrock_bridge.AWS_REGION})
        for canonical in sorted(set(OSS_ALIASES.values())):
            entries.append({"id": canonical, "backend": "ec2_oss", "endpoint": self.litellm_url})
        for alias, canonical in sorted(BURST_ALIASES.items()):
            entries.append({"id": alias, "hf_model": canonical, "backend": "ephemeral_gpu_burst", "endpoint": self.burst_url})
        for alias, canonical in sorted(GEMINI_ALIASES.items()):
            entries.append({
                "id": alias,
                "canonical_model": canonical,
                "backend": "gemini_public_api",
                "endpoint": gemini_cloud_client.DEFAULT_BASE_URL,
            })
        return entries


def get_default_router() -> ModelRouter:
    return ModelRouter()
