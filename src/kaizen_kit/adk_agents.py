#!/usr/bin/env python3
"""Google ADK Kaizen Domain Workflows, 7-MCP Toolset Registry & LLM Army MapReduce.

Integrates `google-adk[mcp]` (`LlmAgent`, `SequentialAgent`, `ParallelAgent`, `LoopAgent`,
`McpToolset`), `ModelRouter` (Corporate AWS Bedrock Claude 5.5 + EC2 OSS pool `:4000`/`:8765`),
ModernBERT SystemOne (`:18800`), and non-secret 7-MCP server discovery.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from . import gpu_burst_orchestrator, guardrails, model_router
from .gates import parity_gate, senior_review_lint, story_guard, verify_provenance
from .hooks import lossless_test_distiller

try:
    from google.adk.agents import BaseAgent, LlmAgent, LoopAgent, ParallelAgent, SequentialAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams, StreamableHTTPConnectionParams
    from google.genai import types as genai_types
    from mcp import StdioServerParameters
    from pydantic import PrivateAttr

    HAS_GOOGLE_ADK = True
except ImportError:
    HAS_GOOGLE_ADK = False
    BaseAgent = object  # type: ignore[assignment,misc]
    BaseLlm = object  # type: ignore[assignment,misc]
    LlmRequest = Any  # type: ignore[assignment,misc]
    LlmResponse = Any  # type: ignore[assignment,misc]
    Runner = None  # type: ignore[assignment,misc]
    InMemorySessionService = None  # type: ignore[assignment,misc]
    McpToolset = None  # type: ignore[assignment,misc]
    StdioConnectionParams = None  # type: ignore[assignment,misc]
    StreamableHTTPConnectionParams = None  # type: ignore[assignment,misc]
    genai_types = None  # type: ignore[assignment,misc]
    StdioServerParameters = None  # type: ignore[assignment,misc]
    PrivateAttr = None  # type: ignore[assignment,misc]


DEFAULT_FASTMCP_URL = "http://127.0.0.1:8770/mcp"
DEFAULT_MODERNBERT_URL = "http://127.0.0.1:18800"
DEFAULT_JEV_URL = "http://127.0.0.1:8765"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

REQUIRED_MCP_SERVERS: Tuple[str, ...] = (
    "serena",
    "fastmcp",
    "kaizen-gitlab",
    "internal-git-gitlab",
    "kaizen-devops",
    "kaizen-atlassian",
    "awsknowledge",
    "context7",
)

MCP_SERVER_ALIASES: Dict[str, str] = {
    "context7": "context7",
    "context7_context7": "context7",
}

WORKFLOW_ALIASES: Dict[str, str] = {
    "kaizenondemandparityloop": "KaizenOndemandParityLoop",
    "ondemand-parity": "KaizenOndemandParityLoop",
    "parity-loop": "KaizenOndemandParityLoop",
    "kaizenucrcapipeline": "KaizenUcRcaPipeline",
    "uc-rca": "KaizenUcRcaPipeline",
    "kaizenseniorreviewswarm": "KaizenSeniorReviewSwarm",
    "senior-review": "KaizenSeniorReviewSwarm",
    "senior-swarm": "KaizenSeniorReviewSwarm",
    "kaizencloud_composeopsparityagent": "KaizenCloudComposeOpsParityAgent",
    "cloud_compose-ops": "KaizenCloudComposeOpsParityAgent",
    "kaizenopenapicontractsyncagent": "KaizenOpenApiContractSyncAgent",
    "openapi-sync": "KaizenOpenApiContractSyncAgent",
    "llmarmymapreduce": "LlmArmyMapReduce",
    "map-reduce": "LlmArmyMapReduce",
    "llm-army": "LlmArmyMapReduce",
}

JENKINS_PREFLIGHT_GATES: Tuple[str, ...] = (
    "Gate 1: Fast-Forward Rebase (origin/master or gerrit/telemetry_dashboard-frontend_dev)",
    "Gate 2: Cross-Repo Dependency Publish Order (shared-utils/avro/security-api first)",
    "Gate 3: Zero-Tolerance Local Compile & Lint (mvn test-compile / vue-tsc / cargo check)",
    "Gate 4: CloudCompose Master Compose & Env Zip Parity (MASTER/* vs env-* vs build-*-zip.sh)",
)

SENIOR_REVIEW_SPECS: Tuple[Tuple[str, str, str], ...] = (
    (
        "CoreJavaGuardReviewer",
        "Core-Java-Guard",
        "Enforce zero inline package paths, single-line SLF4J {} logs, no Log-and-Throw, guard clauses <= 1, Spring DI, Apache Commons, Java 21 getFirst/getLast.",
    ),
    (
        "ConcurrencySecurityGuardReviewer",
        "Concurrency-Security-Guard",
        "Enforce principal-isolated singleflight keys, fail-closed auth, deep cache immutability, E2E deadline budgets, no POST/PUT retries, OpenAPI constant parity.",
    ),
    (
        "VueStateGuardReviewer",
        "Vue-State-Guard",
        "Enforce dynamic watch(() => props.readonly), zero table clearing before await, resetPagination(), filter preservation, zero computed/util collisions, read-only form guards.",
    ),
    (
        "IdentityBoundaryGuardReviewer",
        "Identity-Boundary-Guard",
        "Enforce Keycloak token source of truth and strict microservice boundary isolation between Lambda workers and AAS/CloudGatewayApi models.",
    ),
    (
        "FinOpsObservabilityGuardReviewer",
        "FinOps-Observability-Guard",
        "Enforce explicit HTTP 4xx/5xx upstream error logging, DEBUG downgrade for hot loops, and cache index lookup before payload decompression.",
    ),
    (
        "ResilienceDtoGuardReviewer",
        "Resilience-DTO-Guard",
        "Enforce generic fallback catch preventing runtime errors masquerading as 403 Forbidden and typed domain result objects over raw booleans.",
    ),
    (
        "DtoHygieneGuardReviewer",
        "DTO-Hygiene-Guard",
        "Enforce zero dead code, zero unused variables/orphan methods, and clean immutable DTO contracts.",
    ),
)

_SECRET_ARG_RE = re.compile(
    r"(?:glpat-[0-9A-Za-z_-]{8,}|(?:AKIA|ASIA)[0-9A-Z]{16}|acc-sk-[0-9a-fA-F]{12,}|Bearer\s+\S+)",
    re.IGNORECASE,
)
_SECRET_FLAG_RE = re.compile(
    r"^((?:-p|--?(?:token|password|secret|api[-_]?key|auth|access[-_]?token|private[-_]?token))=)(.+)$",
    re.IGNORECASE,
)
_SECRET_SPACE_FLAG_RE = re.compile(
    r"^(?:-p|--?(?:token|password|secret|api[-_]?key|auth|access[-_]?token|private[-_]?token))$",
    re.IGNORECASE,
)
_URL_IN_ARG_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SENSITIVE_QUERY_KEYS = frozenset(
    {"token", "key", "api_key", "apikey", "secret", "password", "auth", "access_token", "private_token"}
)
_JSON_SCHEMA_TYPES = frozenset({"OBJECT", "STRING", "INTEGER", "NUMBER", "BOOLEAN", "ARRAY", "NULL"})
_RAW_SECRET_TEXT_RE = re.compile(
    r"(?:glpat-[0-9A-Za-z_-]{16,}|(?:AKIA|ASIA)[0-9A-Z]{16}|acc-sk-[0-9a-fA-F]{16,}|-----BEGIN [A-Z ]+PRIVATE KEY-----)"
)


@dataclass(frozen=True)
class FallbackMcpToolset:
    """Typed non-network McpToolset placeholder when google-adk is absent."""

    server_name: str
    transport: str
    endpoint_or_command: str
    args: Tuple[str, ...] = ()
    tool_filter: Optional[List[str]] = None


@dataclass
class FallbackBaseAgent:
    """Typed fallback ADK workflow/agent container when google-adk is absent."""

    name: str
    description: str = ""
    model: Any = ""
    instruction: str = ""
    sub_agents: List[Any] = field(default_factory=list)
    tools: List[Any] = field(default_factory=list)
    max_iterations: Optional[int] = None


@dataclass(frozen=True)
class McpServerSpec:
    """Non-secret transport metadata for a discovered MCP server."""

    name: str
    canonical_name: str
    transport: str
    url: str = ""
    command: str = ""
    args: Tuple[str, ...] = ()
    env_keys: Tuple[str, ...] = ()
    disabled: bool = False
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "canonical_name": self.canonical_name,
            "transport": self.transport,
            "url": self.url,
            "command": self.command,
            "args": list(self.args),
            "env_keys": list(self.env_keys),
            "disabled": self.disabled,
            "source": self.source,
        }


@dataclass(frozen=True)
class WorkflowDescriptor:
    """Metadata descriptor for an Kaizen ADK domain workflow."""

    name: str
    agent_type: str
    description: str
    default_model: str
    mcp_servers: Tuple[str, ...]
    sub_agent_names: Tuple[str, ...]
    gates: Tuple[str, ...] = ()


def has_antigravity_sdk() -> bool:
    try:
        return importlib.util.find_spec("google.antigravity") is not None
    except (ModuleNotFoundError, ValueError):
        return False


def _google_adk_version() -> str:
    if not HAS_GOOGLE_ADK:
        return "not-installed"
    try:
        from importlib.metadata import version

        return version("google-adk")
    except Exception:
        return "installed"


def describe_runtime_capabilities() -> Dict[str, Any]:
    raw_fastmcp = (os.environ.get("KAIZEN_FASTMCP_URL") or DEFAULT_FASTMCP_URL).strip()
    raw_modernbert = (os.environ.get("KAIZEN_MODERNBERT_URL") or DEFAULT_MODERNBERT_URL).strip()
    return {
        "google_adk_installed": HAS_GOOGLE_ADK,
        "google_adk_version": _google_adk_version(),
        "google_antigravity_installed": has_antigravity_sdk(),
        "fastmcp_installed": importlib.util.find_spec("fastmcp") is not None,
        "fastmcp_url": _require_loopback(raw_fastmcp),
        "modernbert_url": _require_loopback(raw_modernbert),
    }


def _canonical_mcp_name(name: str) -> str:
    key = name.strip()
    return MCP_SERVER_ALIASES.get(key.lower(), key)


def _scrub_arg(value: str) -> str:
    m = _SECRET_FLAG_RE.match(value)
    if m:
        return f"{m.group(1)}<REDACTED>"
    cleaned = _URL_IN_ARG_RE.sub(lambda match: _sanitize_mcp_url(match.group(0)), value)
    if _SECRET_ARG_RE.search(cleaned):
        return _SECRET_ARG_RE.sub("<REDACTED>", cleaned)
    return cleaned


def _scrub_args(raw_args: Sequence[Any]) -> Tuple[str, ...]:
    scrubbed: List[str] = []
    redact_next = False
    for item in raw_args:
        text = str(item)
        if redact_next:
            scrubbed.append("<REDACTED>")
            redact_next = False
            continue
        if _SECRET_SPACE_FLAG_RE.match(text.strip()):
            scrubbed.append(text)
            redact_next = True
            continue
        scrubbed.append(_scrub_arg(text))
    return tuple(scrubbed)


def _sanitize_mcp_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    if not parsed.scheme and not parsed.netloc:
        return raw_url
    netloc = parsed.netloc
    if parsed.username is not None or parsed.password is not None:
        host_part = netloc.rsplit("@", 1)[-1]
        creds = "<REDACTED>:<REDACTED>" if parsed.password is not None else "<REDACTED>"
        netloc = f"{creds}@{host_part}"
    query = parsed.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        redacted = [
            (key, "<REDACTED>" if key.lower().replace("-", "_") in _SENSITIVE_QUERY_KEYS else val)
            for key, val in pairs
        ]
        query = urlencode(redacted, safe="<>")
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def _extract_mcp_spec(name: str, raw_cfg: Dict[str, Any], source_label: str) -> Optional[McpServerSpec]:
    canonical = _canonical_mcp_name(name)
    raw_url = str(raw_cfg.get("url") or raw_cfg.get("serverUrl") or "").strip()
    raw_cmd = str(raw_cfg.get("command") or "").strip()
    raw_env = raw_cfg.get("env") if isinstance(raw_cfg.get("env"), dict) else {}
    env_keys = tuple(sorted(str(k) for k in raw_env.keys()))
    disabled = bool(raw_cfg.get("disabled", False))

    if raw_url:
        return McpServerSpec(
            name=name,
            canonical_name=canonical,
            transport="http",
            url=_sanitize_mcp_url(raw_url),
            env_keys=env_keys,
            disabled=disabled,
            source=source_label,
        )
    if not raw_cmd:
        return None
    raw_args = raw_cfg.get("args") if isinstance(raw_cfg.get("args"), list) else []
    return McpServerSpec(
        name=name,
        canonical_name=canonical,
        transport="stdio",
        command=raw_cmd,
        args=_scrub_args(raw_args),
        env_keys=env_keys,
        disabled=disabled,
        source=source_label,
    )


def _ingest_mcp_file(path: Path, base_dir: Path, registry: Dict[str, McpServerSpec]) -> None:
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return

    servers = payload.get("mcpServers", payload)
    if not isinstance(servers, dict):
        return
    try:
        rel_source = path.relative_to(base_dir).as_posix()
    except ValueError:
        rel_source = path.name

    for srv_name, srv_cfg in servers.items():
        if not isinstance(srv_cfg, dict):
            continue
        spec = _extract_mcp_spec(str(srv_name), srv_cfg, rel_source)
        if spec is not None:
            registry[spec.canonical_name] = spec


def _resolve_gemini_config_dir(config_dir: Optional[Path] = None) -> Path:
    if config_dir is not None:
        return Path(config_dir).expanduser().resolve()
    env_dir = os.environ.get("GEMINI_CONFIG_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return Path.home() / ".gemini" / "config"


def discover_mcp_registry(
    config_dir: Optional[Path] = None,
    fastmcp_url: Optional[str] = None,
) -> Dict[str, McpServerSpec]:
    """Discover non-secret MCP server metadata from config files and FastMCP gateway."""
    base_dir = _resolve_gemini_config_dir(config_dir)
    registry: Dict[str, McpServerSpec] = {}

    _ingest_mcp_file(base_dir / "mcp_config.json", base_dir, registry)
    plugins_dir = base_dir / "plugins"
    if plugins_dir.is_dir():
        for plugin_cfg in sorted(plugins_dir.glob("*/mcp_config.json")):
            _ingest_mcp_file(plugin_cfg, base_dir, registry)

    resolved_fastmcp = _sanitize_mcp_url(
        _require_loopback(
            (fastmcp_url or os.environ.get("KAIZEN_FASTMCP_URL") or DEFAULT_FASTMCP_URL).strip()
        )
    )
    existing_fastmcp = registry.get("fastmcp")
    if existing_fastmcp is not None and existing_fastmcp.url:
        _require_loopback(existing_fastmcp.url)
    registry.setdefault(
        "fastmcp",
        McpServerSpec(
            name="fastmcp",
            canonical_name="fastmcp",
            transport="http",
            url=resolved_fastmcp,
            env_keys=(),
            disabled=False,
            source="builtin:fastmcp",
        ),
    )
    return registry


def resolve_mcp_spec(registry: Dict[str, McpServerSpec], name: str) -> Optional[McpServerSpec]:
    canonical = _canonical_mcp_name(name)
    if canonical in registry:
        return registry[canonical]
    return registry.get(name)


def build_mcp_toolset(
    spec: McpServerSpec,
    tool_filter: Optional[List[str]] = None,
    forward_env: bool = False,
) -> Any:
    """Create a lazy ADK McpToolset without triggering live network or subprocess calls."""
    if not HAS_GOOGLE_ADK:
        endpoint = spec.url if spec.transport == "http" else spec.command
        return FallbackMcpToolset(
            server_name=spec.canonical_name,
            transport=spec.transport,
            endpoint_or_command=endpoint,
            args=spec.args,
            tool_filter=tool_filter,
        )

    if spec.transport == "http":
        return McpToolset(
            connection_params=StreamableHTTPConnectionParams(url=spec.url),
            tool_filter=tool_filter,
        )
    env_map: Optional[Dict[str, str]] = None
    if forward_env:
        forwarded = {key: os.environ[key] for key in spec.env_keys if key in os.environ}
        env_map = forwarded or None
    params = StdioServerParameters(command=spec.command, args=list(spec.args), env=env_map)
    return McpToolset(
        connection_params=StdioConnectionParams(server_params=params),
        tool_filter=tool_filter,
    )


def build_mcp_toolsets(
    server_names: Sequence[str],
    registry: Optional[Dict[str, McpServerSpec]] = None,
    skip_disabled: bool = True,
    forward_env: bool = False,
) -> List[Any]:
    reg = registry if registry is not None else discover_mcp_registry()
    toolsets: List[Any] = []
    seen: set[str] = set()
    for raw_name in server_names:
        spec = resolve_mcp_spec(reg, raw_name)
        if spec is None or spec.canonical_name in seen:
            continue
        if skip_disabled and spec.disabled:
            continue
        seen.add(spec.canonical_name)
        toolsets.append(build_mcp_toolset(spec, forward_env=forward_env))
    return toolsets


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = getattr(content, "parts", None) or []
    chunks = [str(getattr(part, "text", "")) for part in parts if getattr(part, "text", None)]
    return "\n".join(chunks).strip()


def _normalize_json_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_normalize_json_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for key, val in schema.items():
        if key == "type" and isinstance(val, str) and val.upper() in _JSON_SCHEMA_TYPES:
            out[key] = val.lower()
        else:
            out[key] = _normalize_json_schema(val)
    return out


def _declaration_to_openai_tool(decl: Any) -> Optional[Dict[str, Any]]:
    name = str(getattr(decl, "name", "") or "").strip()
    if not name:
        return None
    description = str(getattr(decl, "description", "") or f"Tool {name}")
    raw_schema = getattr(decl, "parameters_json_schema", None)
    if raw_schema is None:
        raw_schema = getattr(decl, "parameters", None)
    if hasattr(raw_schema, "model_dump"):
        raw_schema = raw_schema.model_dump(exclude_none=True, mode="json")
    params = _normalize_json_schema(raw_schema) if isinstance(raw_schema, dict) else {}
    if not params:
        params = {"type": "object", "properties": {}}
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": params,
        },
    }


def _append_openai_tool(decl: Any, seen: set[str], tools: List[Dict[str, Any]]) -> None:
    if decl is None:
        return
    tool_schema = _declaration_to_openai_tool(decl)
    if tool_schema is None:
        return
    name = tool_schema["function"]["name"]
    if name in seen:
        return
    seen.add(name)
    tools.append(tool_schema)


def _extract_openai_tools(llm_request: Any) -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = []
    seen: set[str] = set()
    cfg = getattr(llm_request, "config", None)
    for entry in getattr(cfg, "tools", None) or []:
        for decl in getattr(entry, "function_declarations", None) or []:
            _append_openai_tool(decl, seen, tools)
    tools_dict = getattr(llm_request, "tools_dict", None) or {}
    for tool_obj in tools_dict.values():
        get_decl = getattr(tool_obj, "_get_declaration", None)
        if callable(get_decl):
            _append_openai_tool(get_decl(), seen, tools)
    return tools


def _next_call_id(raw_id: Any, name: str, pending_by_name: Dict[str, List[str]], counter: List[int]) -> str:
    counter[0] += 1
    call_id = str(raw_id or f"call_{counter[0]}")
    pending_by_name.setdefault(name, []).append(call_id)
    return call_id


def _resolve_response_call_id(
    raw_id: Any,
    name: str,
    pending_by_name: Dict[str, List[str]],
    counter: List[int],
) -> str:
    if raw_id:
        return str(raw_id)
    queue = pending_by_name.get(name)
    if queue:
        return queue.pop(0)
    counter[0] += 1
    return f"call_{counter[0]}"


def _part_to_tool_call(
    part: Any,
    pending_by_name: Dict[str, List[str]],
    counter: List[int],
) -> Optional[Dict[str, Any]]:
    fc = getattr(part, "function_call", None)
    if fc is None:
        return None
    fc_name = str(getattr(fc, "name", "") or "").strip()
    if not fc_name:
        return None
    fc_id = _next_call_id(getattr(fc, "id", None), fc_name, pending_by_name, counter)
    raw_args = getattr(fc, "args", None)
    args_dict = raw_args if isinstance(raw_args, dict) else ({} if raw_args is None else {"value": raw_args})
    return {
        "id": fc_id,
        "type": "function",
        "function": {"name": fc_name, "arguments": json.dumps(args_dict, default=str)},
    }


def _part_to_tool_message(
    part: Any,
    pending_by_name: Dict[str, List[str]],
    counter: List[int],
) -> Optional[Dict[str, Any]]:
    fr = getattr(part, "function_response", None)
    if fr is None:
        return None
    fr_name = str(getattr(fr, "name", "") or "tool")
    fr_id = _resolve_response_call_id(getattr(fr, "id", None), fr_name, pending_by_name, counter)
    resp_obj = getattr(fr, "response", None)
    content_str = json.dumps({} if resp_obj is None else resp_obj, default=str)
    return {
        "role": "tool",
        "tool_call_id": fr_id,
        "name": fr_name,
        "content": content_str,
    }


def _resolve_openai_role(item: Any) -> str:
    raw_role = str(getattr(item, "role", "user") or "user").lower()
    return "assistant" if raw_role == "model" else raw_role


def _collect_item_parts(
    parts: Sequence[Any],
    pending_by_name: Dict[str, List[str]],
    counter: List[int],
) -> Tuple[List[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
    text_chunks: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    tool_msgs: List[Dict[str, Any]] = []
    for part in parts:
        if getattr(part, "text", None):
            text_chunks.append(str(part.text))
        tc = _part_to_tool_call(part, pending_by_name, counter)
        if tc is not None:
            tool_calls.append(tc)
        tm = _part_to_tool_message(part, pending_by_name, counter)
        if tm is not None:
            tool_msgs.append(tm)
    return text_chunks, tool_calls, tool_msgs


def _content_item_to_messages(
    item: Any,
    pending_by_name: Dict[str, List[str]],
    counter: List[int],
) -> List[Dict[str, Any]]:
    if isinstance(item, str):
        return [{"role": "user", "content": item}] if item else []
    text_chunks, tool_calls, tool_msgs = _collect_item_parts(
        getattr(item, "parts", None) or [],
        pending_by_name,
        counter,
    )
    out: List[Dict[str, Any]] = []
    text = "\n".join(text_chunks).strip()
    if tool_calls:
        out.append({"role": "assistant", "content": text or None, "tool_calls": tool_calls})
    elif text:
        out.append({"role": _resolve_openai_role(item), "content": text})
    out.extend(tool_msgs)
    return out


def _extract_llm_messages(llm_request: Any) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    cfg = getattr(llm_request, "config", None)
    sys_inst = getattr(cfg, "system_instruction", None) if cfg is not None else None
    sys_text = _content_to_text(sys_inst) if sys_inst is not None else ""
    if sys_text:
        messages.append({"role": "system", "content": sys_text})

    pending_by_name: Dict[str, List[str]] = {}
    counter = [0]
    for item in getattr(llm_request, "contents", None) or []:
        messages.extend(_content_item_to_messages(item, pending_by_name, counter))

    if not messages:
        messages.append({"role": "user", "content": "Respond concisely with engineering findings."})
    return messages


def _parse_tool_call_args(raw_args: Any) -> Dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed = json.loads(raw_args)
        except ValueError:
            return {"raw": raw_args}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {}


def _tool_call_to_part(tc: Dict[str, Any], idx: int) -> Optional[Any]:
    fn = tc.get("function") or {}
    name = str(fn.get("name") or "").strip()
    if not name:
        return None
    args = _parse_tool_call_args(fn.get("arguments"))
    tc_id = str(tc.get("id") or f"call_{idx + 1}")
    return genai_types.Part(function_call=genai_types.FunctionCall(id=tc_id, name=name, args=args))


def _extract_response_parts(msg: Dict[str, Any]) -> List[Any]:
    text = str(msg.get("content") or "")
    parts: List[Any] = [genai_types.Part.from_text(text=text)] if text else []
    for idx, tc in enumerate(msg.get("tool_calls") or []):
        part = _tool_call_to_part(tc, idx)
        if part is not None:
            parts.append(part)
    return parts or [genai_types.Part.from_text(text="")]


def _build_llm_response(resp: Dict[str, Any], fallback_model: str) -> Any:
    choices = resp.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else {}
    return LlmResponse(
        model_version=str(resp.get("model") or fallback_model),
        content=genai_types.Content(role="model", parts=_extract_response_parts(msg)),
        turn_complete=True,
    )


def _extract_generation_params(llm_request: Any) -> Tuple[int, Optional[float]]:
    cfg = getattr(llm_request, "config", None)
    if cfg is None:
        return 2048, None
    max_tokens = getattr(cfg, "max_output_tokens", None) or 2048
    temperature = getattr(cfg, "temperature", None)
    return int(max_tokens), float(temperature) if temperature is not None else None


def _extract_response_format(llm_request: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Extract OpenAI-compatible `response_format` and raw JSON schema from ADK `LlmRequest`."""
    cfg = getattr(llm_request, "config", None)
    if cfg is None:
        return None, None
    raw_schema = getattr(cfg, "response_json_schema", None)
    if raw_schema is None:
        raw_schema = getattr(cfg, "response_schema", None)
    if hasattr(raw_schema, "model_dump"):
        raw_schema = raw_schema.model_dump(exclude_none=True, mode="json")
    if isinstance(raw_schema, dict) and raw_schema:
        norm = _normalize_json_schema(raw_schema)
        return {
            "type": "json_schema",
            "json_schema": {"name": "adk_output", "schema": norm, "strict": True},
        }, norm
    mime = str(getattr(cfg, "response_mime_type", "") or "").strip().lower()
    if mime == "application/json":
        return {"type": "json_object"}, None
    return None, None


def _invoke_router_safe(router_obj: Any, messages: List[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
    invoke_fn = router_obj.invoke
    sig = inspect.signature(invoke_fn)
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    filtered = {
        k: v
        for k, v in kwargs.items()
        if v is not None and (accepts_var_kw or k in sig.parameters)
    }
    return invoke_fn(messages, **filtered)


def _chat_text_router_safe(router_obj: Any, prompt: str, system: str, model: str, max_tokens: int, temperature: float, **kwargs: Any) -> str:
    chat_fn = router_obj.chat_text
    sig = inspect.signature(chat_fn)
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    filtered = {
        k: v
        for k, v in kwargs.items()
        if v is not None and (accepts_var_kw or k in sig.parameters)
    }
    return chat_fn(prompt, system, model, max_tokens, temperature, **filtered)


if HAS_GOOGLE_ADK:

    class ModelRouterLlm(BaseLlm):
        """Google ADK `BaseLlm` adapter backed by `kaizen_kit.model_router.ModelRouter`."""

        _router: Optional[model_router.ModelRouter] = PrivateAttr(default=None)
        _response_format: Optional[Dict[str, Any]] = PrivateAttr(default=None)
        _json_schema: Optional[Dict[str, Any]] = PrivateAttr(default=None)
        _effort: Optional[str] = PrivateAttr(default=None)
        _mask_stale_turns: int = PrivateAttr(default=4)

        def __init__(
            self,
            model: str = "claude-sonnet-5-5",
            router: Optional[model_router.ModelRouter] = None,
            response_format: Optional[Dict[str, Any]] = None,
            json_schema: Optional[Dict[str, Any]] = None,
            effort: Optional[str] = None,
            mask_stale_turns: int = 4,
        ) -> None:
            super().__init__(model=model)
            self._router = router
            self._response_format = response_format
            self._json_schema = json_schema
            self._effort = effort
            self._mask_stale_turns = max(0, int(mask_stale_turns))

        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            active_router = self._router or model_router.get_default_router()
            raw_messages = _extract_llm_messages(llm_request)
            messages = lossless_test_distiller.mask_stale_turn_observations(
                raw_messages, keep_recent_turns=self._mask_stale_turns
            )
            max_tokens, temperature = _extract_generation_params(llm_request)
            openai_tools = _extract_openai_tools(llm_request)
            req_fmt, req_schema = _extract_response_format(llm_request)
            resp_fmt = req_fmt or self._response_format
            json_schema = req_schema or self._json_schema
            resp = await asyncio.to_thread(
                _invoke_router_safe,
                active_router,
                messages,
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=openai_tools or None,
                response_format=resp_fmt,
                json_schema=json_schema,
                effort=self._effort,
            )
            yield _build_llm_response(resp, self.model)

else:

    @dataclass
    class ModelRouterLlm:  # type: ignore[no-redef]
        """Typed adapter fallback when `google-adk` is not installed."""

        model: str = "claude-sonnet-5-5"
        router: Optional[model_router.ModelRouter] = None
        response_format: Optional[Dict[str, Any]] = None
        json_schema: Optional[Dict[str, Any]] = None
        effort: Optional[str] = None
        mask_stale_turns: int = 4


def run_story_guard_tool(workspace: str) -> Dict[str, Any]:
    """Run deterministic Storybook Clean Architecture & config parity gates."""
    return story_guard.run_all_gates(Path(workspace).resolve())


def run_provenance_check_tool(workspace: str) -> Dict[str, Any]:
    """Scan a Rust/Java workspace for AST provenance & synthetic telemetry violations."""
    rc = verify_provenance.main(["--workspace", str(Path(workspace).resolve())])
    return {"workspace": str(Path(workspace).resolve()), "passed": rc == 0, "exit_code": rc}


def run_parity_gate_tool(legacy_json: str, target_json: str) -> Dict[str, Any]:
    """Evaluate strict POPULATED_MATCH vs EMPTY_BOTH_200 parity between legacy and target payloads."""
    try:
        legacy_obj = json.loads(legacy_json)
        target_obj = json.loads(target_json)
    except ValueError as exc:
        return {"passed": False, "classification": "INVALID_JSON", "error": str(exc)}
    report = parity_gate.compare_nodes(legacy_obj, target_obj)
    report = parity_gate.finalize_classification(legacy_obj, target_obj, report)
    report["passed"] = report.get("oracle_classification") in ("POPULATED_MATCH", "EMPTY_BOTH_200")
    return report


def run_senior_review_lint_tool(target_path: str, pre_flight: bool = False) -> Dict[str, Any]:
    """Run the Kaizen Senior Review & Jenkins Pre-Flight CI linter."""
    args = [str(Path(target_path).resolve())] + (["--pre-flight"] if pre_flight else [])
    rc = senior_review_lint.main(args)
    return {"target": str(Path(target_path).resolve()), "pre_flight": pre_flight, "passed": rc == 0}


def exit_parity_loop(tool_context: Any, reason: str = "Parity verified") -> Dict[str, Any]:
    """Signal ADK LoopAgent termination once parity and provenance gates pass."""
    actions = getattr(tool_context, "actions", None)
    if actions is not None:
        actions.escalate = True
    return {"escalated": True, "reason": reason}


def run_hybrid_rag_ocr_tool(
    query: str,
    document_path: str = "",
    limit: int = 10,
    workspace: str = ".",
) -> Dict[str, Any]:
    """Run Hybrid RAG (vector + FTS + RRF rerank) and optional OCR document extraction."""
    from . import localmodels
    from .gates import symbol_rag_indexer

    bounded = max(1, min(int(limit), 50))
    ocr_result: Optional[Dict[str, Any]] = None
    if document_path.strip():
        ocr_cls = getattr(localmodels, "DocumentOcrClient", None)
        if ocr_cls is not None:
            ocr_result = ocr_cls().parse_document(document_path)
        else:
            ocr_result = {"ok": False, "document_path": document_path, "reason": "ocr_client_unavailable"}

    hits = symbol_rag_indexer.search_concepts(query, limit=bounded, workspace_root=Path(workspace).resolve())
    hybrid_cls = getattr(localmodels, "HybridEmbedRerankClient", None)
    if hybrid_cls is not None and hits:
        client = hybrid_cls()
        if client.healthy():
            hits = client.rerank(query, hits, top_k=bounded)
            return {"ok": True, "backend": "hybrid_embed_rerank", "hits": hits, "ocr": ocr_result}
    return {"ok": True, "backend": "symbol_rag_hybrid", "hits": hits, "ocr": ocr_result}


def run_best_of_n_cascade_tool(
    prompt: str,
    n: int = 3,
    verify_cmd: str = "",
    data_classification: str = "corporate_proprietary",
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Execute Lever D Best-of-N cascade with deterministic execution gate verification."""
    from . import carbon_orchestrator

    router = carbon_orchestrator.CascadeRouter()
    exec_fn = getattr(router, "execute_best_of_n", None)
    if callable(exec_fn):
        return exec_fn(
            prompt,
            n=max(1, int(n)),
            data_classification=data_classification,
            verify_cmd=verify_cmd,
            dry_run=dry_run,
        )
    dec = router.route_task(prompt, data_classification=data_classification)
    return {
        "winner": "",
        "passed_count": 0,
        "total_candidates": max(1, int(n)),
        "escalated": False,
        "escalation_model": dec.escalation_chain[-1] if dec.escalation_chain else "claude-sonnet-5-5",
        "data_classification": data_classification,
        "dry_run": dry_run,
    }


def run_streaming_guardrail_tool(
    text: str,
    tool_name: str = "",
    tool_args_json: str = "",
) -> Dict[str, Any]:
    """Evaluate streaming output chunks and optional tool-call arguments via Guardrails."""
    session_cls = getattr(guardrails, "StreamingGuardrailSession", None)
    if tool_name.strip():
        tool_rail_fn = getattr(guardrails, "evaluate_tool_call_rail", None)
        parsed_args: Any = {}
        if tool_args_json.strip():
            try:
                parsed_args = json.loads(tool_args_json)
            except ValueError:
                parsed_args = {"raw": tool_args_json}
        if callable(tool_rail_fn):
            res = tool_rail_fn(tool_name, parsed_args)
            return asdict(res) if hasattr(res, "__dataclass_fields__") else dict(res)
    if session_cls is not None:
        sess = session_cls()
        ev = sess.inspect_chunk(text)
        return asdict(ev) if hasattr(ev, "__dataclass_fields__") else dict(ev)
    out_res = guardrails.evaluate_output_rail(text)
    return asdict(out_res)


def run_voice_to_action_tool(transcript: str, dry_run: bool = True) -> Dict[str, Any]:
    """Convert voice transcript (stripping voice tags) into constrained action items."""
    from .hooks import prompt_decomposer

    adapter_cls = getattr(guardrails, "VoiceToActionAdapter", None)
    if adapter_cls is not None:
        res = adapter_cls().extract_actions_from_transcript(transcript)
        return {"ok": res.get("status") in ("ok", "empty"), "dry_run": dry_run, **res}
    cleaned = prompt_decomposer.strip_voice_tags(transcript)
    decomp = prompt_decomposer.decompose_prompt(cleaned)
    return {"ok": True, "dry_run": dry_run, "cleaned_transcript": cleaned, "workstreams": decomp.get("workstreams", [])}


def _make_llm_agent(
    name: str,
    description: str,
    instruction: str,
    model_name: str,
    router: Optional[model_router.ModelRouter],
    tools: Optional[List[Any]] = None,
) -> Any:
    llm = ModelRouterLlm(model=model_name, router=router)
    if HAS_GOOGLE_ADK:
        return LlmAgent(
            name=name,
            description=description,
            model=llm,
            instruction=instruction,
            tools=list(tools or []),
        )
    return FallbackBaseAgent(
        name=name,
        description=description,
        model=llm,
        instruction=instruction,
        tools=list(tools or []),
    )


def _make_workflow_agent(kind: str, name: str, description: str, sub_agents: List[Any], max_iterations: int = 3) -> Any:
    if not HAS_GOOGLE_ADK:
        return FallbackBaseAgent(
            name=name,
            description=description,
            sub_agents=sub_agents,
            max_iterations=max_iterations if kind == "LoopAgent" else None,
        )
    if kind == "LoopAgent":
        return LoopAgent(name=name, description=description, sub_agents=sub_agents, max_iterations=max_iterations)
    if kind == "SequentialAgent":
        return SequentialAgent(name=name, description=description, sub_agents=sub_agents)
    if kind == "ParallelAgent":
        return ParallelAgent(name=name, description=description, sub_agents=sub_agents)
    raise ValueError(f"Unsupported ADK workflow kind: {kind}")


def build_ondemand_parity_loop(
    router: Optional[model_router.ModelRouter] = None,
    attach_mcps: bool = False,
    registry: Optional[Dict[str, McpServerSpec]] = None,
) -> Any:
    mcp_tools = build_mcp_toolsets(("serena", "fastmcp"), registry=registry) if attach_mcps else []
    story_agent = _make_llm_agent(
        name="StoryGuardVerifier",
        description="Verify Storybook architecture, config/YAML externalization, and VectorCube invariants.",
        instruction="Run story_guard checks and report exact gate failures before proceeding.",
        model_name="claude-haiku-5-5",
        router=router,
        tools=[run_story_guard_tool] + mcp_tools,
    )
    prov_agent = _make_llm_agent(
        name="AstProvenanceAuditor",
        description="Enforce INV-ISOLATION, INV-PROVENANCE, zero synthetic telemetry, and compile-time schemas.",
        instruction="Audit AST provenance via verify_provenance and inspect symbols with serena/zoekt/code-rag.",
        model_name="claude-sonnet-5-5",
        router=router,
        tools=[run_provenance_check_tool] + mcp_tools,
    )
    parity_agent = _make_llm_agent(
        name="ParityOracleGate",
        description="Verify POPULATED_MATCH vs EMPTY_BOTH_200 parity and Server-Timing latency targets.",
        instruction="Evaluate parity payloads; call exit_parity_loop only when POPULATED_MATCH and gates pass.",
        model_name="claude-sonnet-5-5",
        router=router,
        tools=[run_parity_gate_tool, exit_parity_loop],
    )
    return _make_workflow_agent(
        kind="LoopAgent",
        name="KaizenOndemandParityLoop",
        description="Iterative Ondemand parity loop combining story_guard, verify_provenance, parity_gate, and Serena/Zoekt/Code-RAG.",
        sub_agents=[story_agent, prov_agent, parity_agent],
        max_iterations=3,
    )


def build_uc_rca_pipeline(
    router: Optional[model_router.ModelRouter] = None,
    attach_mcps: bool = False,
    registry: Optional[Dict[str, McpServerSpec]] = None,
) -> Any:
    atlassian_tools = build_mcp_toolsets(("kaizen-atlassian",), registry=registry) if attach_mcps else []
    aws_tools = build_mcp_toolsets(("awsknowledge",), registry=registry) if attach_mcps else []
    code_tools = build_mcp_toolsets(("serena", "fastmcp", "kaizen-devops"), registry=registry) if attach_mcps else []

    jira_agent = _make_llm_agent(
        name="UcTicketContextFetcher",
        description="Fetch Jira UC-* ticket context, comments, attachments, and Confluence specs via kaizen-atlassian.",
        instruction="Extract exact reproduction steps, environment, HAR Root=1-<hex> IDs, and linked Confluence specs.",
        model_name="claude-haiku-5-5",
        router=router,
        tools=atlassian_tools,
    )
    correlator_agent = _make_llm_agent(
        name="HarCloudWatchCorrelator",
        description="Correlate HAR Root=1-<hex> traces with AWS CloudWatch kaizen-prod (uca/uc-prd) log groups.",
        instruction="Correlate X-Amzn-Trace-Id Root=1-<hex> across kaizen-prod read-only CloudWatch logs and HAR statuses.",
        model_name="claude-sonnet-5-5",
        router=router,
        tools=aws_tools,
    )
    trace_agent = _make_llm_agent(
        name="CodebaseTraceInvestigator",
        description="Trace symbol definitions, 4-year git forensics, and Code-RAG/Zoekt hits across C:/work-space.",
        instruction="Locate offending handlers/schemas using Serena LSP, Zoekt, Code-RAG, and kaizen-devops git log/blame.",
        model_name="claude-sonnet-5-5",
        router=router,
        tools=code_tools,
    )
    falsifier_agent = _make_llm_agent(
        name="PopperianTddFalsifier",
        description="Design minimal Red-Green TDD falsification test and verified root-cause remediation plan.",
        instruction="Formulate a Popperian falsification test proving the root cause before proposing the minimal fix.",
        model_name="claude-opus-5-5",
        router=router,
    )
    return _make_workflow_agent(
        kind="SequentialAgent",
        name="KaizenUcRcaPipeline",
        description="Sequential UC RCA pipeline: Jira/Confluence -> HAR & CloudWatch -> Serena/Zoekt/Code-RAG -> Opus 5.5 Popperian TDD falsifier.",
        sub_agents=[jira_agent, correlator_agent, trace_agent, falsifier_agent],
    )


def build_senior_review_swarm(
    router: Optional[model_router.ModelRouter] = None,
    attach_mcps: bool = False,
    registry: Optional[Dict[str, McpServerSpec]] = None,
) -> Any:
    review_tools = (
        build_mcp_toolsets(("kaizen-gitlab", "internal-git-gitlab", "kaizen-devops"), registry=registry)
        if attach_mcps
        else []
    )
    sub_agents: List[Any] = []
    for agent_name, guard_label, mandate in SENIOR_REVIEW_SPECS:
        sub_agents.append(
            _make_llm_agent(
                name=agent_name,
                description=f"Kaizen senior reviewer ({guard_label}).",
                instruction=f"[{guard_label}] {mandate}",
                model_name="claude-sonnet-5-5",
                router=router,
                tools=[run_senior_review_lint_tool] + review_tools,
            )
        )

    gates_summary = "; ".join(JENKINS_PREFLIGHT_GATES)
    sub_agents.append(
        _make_llm_agent(
            name="JenkinsPreFlightShield",
            description="Enforce the 4 Jenkins Pre-Flight CI gates across GitLab, Gerrit (corp & edge-gerrit), and Jenkins.",
            instruction=f"Verify all 4 Jenkins gates: {gates_summary}.",
            model_name="claude-haiku-5-5",
            router=router,
            tools=[run_senior_review_lint_tool] + review_tools,
        )
    )
    return _make_workflow_agent(
        kind="ParallelAgent",
        name="KaizenSeniorReviewSwarm",
        description="Parallel 7-standard Kaizen senior review swarm + 4 Jenkins pre-flight gates across GitLab/Gerrit/Jenkins.",
        sub_agents=sub_agents,
    )


def build_cloud_compose_ops_parity_agent(
    router: Optional[model_router.ModelRouter] = None,
    attach_mcps: bool = False,
    registry: Optional[Dict[str, McpServerSpec]] = None,
) -> Any:
    mcp_tools = (
        build_mcp_toolsets(("kaizen-gitlab", "kaizen-devops", "awsknowledge", "context7_context7"), registry=registry)
        if attach_mcps
        else []
    )
    return _make_llm_agent(
        name="KaizenCloudComposeOpsParityAgent",
        description="Audit kaizen-cloud-docker-compose MASTER vs env-* parity, Jenkins upstep params, and AWS CFN docs.",
        instruction=(
            "Verify MASTER/docker-compose.yml, MASTER/conf/, env-*/ directories, and build-*-zip.sh parity "
            "using kaizen-gitlab, kaizen-devops, awsknowledge, and context7."
        ),
        model_name="claude-sonnet-5-5",
        router=router,
        tools=mcp_tools,
    )


def build_openapi_contract_sync_agent(
    router: Optional[model_router.ModelRouter] = None,
    attach_mcps: bool = False,
    registry: Optional[Dict[str, McpServerSpec]] = None,
) -> Any:
    mcp_tools = (
        build_mcp_toolsets(("kaizen-gitlab", "kaizen-devops", "awsknowledge", "context7_context7"), registry=registry)
        if attach_mcps
        else []
    )
    return _make_llm_agent(
        name="KaizenOpenApiContractSyncAgent",
        description="Synchronize OpenAPI/Swagger 1:1 contracts across CloudGatewayApi/serverless endpoints and official framework docs.",
        instruction=(
            "Compare legacy/target endpoint schemas, SpringDoc/utoipa specs, and API Gateway definitions using "
            "kaizen-gitlab, kaizen-devops, awsknowledge, and context7."
        ),
        model_name="claude-sonnet-5-5",
        router=router,
        tools=mcp_tools,
    )


def _require_loopback(url: str) -> str:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"Endpoint scheme must be http or https, got {scheme!r}")
    host = (parsed.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"Endpoint must be loopback, got host {host!r}")
    return url.rstrip("/")


def _port_open(url: str, timeout: float = 0.25) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


def _post_json(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=raw, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"Filter request to {url} failed: {exc}") from exc


def _default_safety_rail(text: str) -> Tuple[bool, str]:
    if not text.strip():
        return False, "empty_output"
    if _RAW_SECRET_TEXT_RE.search(text):
        return False, "raw_secret_detected"
    return True, "ok"


@dataclass
class LlmArmyMapReduce:
    """Map (EC2 :4000/:8765 or Spot Burst) -> Filter (ModernBERT :18800 + guardrails) -> Reduce (Claude Opus/Sonnet 5.5)."""

    router: Optional[model_router.ModelRouter] = None
    map_model: str = "mini-pool"
    reduce_model: str = "claude-opus-5-5"
    filter_url: str = ""
    fallback_jev_url: str = ""
    min_score: float = 0.45
    max_concurrency: int = 4
    safety_rail_fn: Optional[Callable[[str], Tuple[bool, str]]] = None
    timeout_s: float = 30.0
    use_guardrails: bool = True
    spot_tier: str = ""
    spot_nodes: int = 0
    spot_dry_run: bool = True
    burst_url: str = ""
    response_format: Optional[Dict[str, Any]] = None
    json_schema: Optional[Dict[str, Any]] = None
    best_of_n: int = 1
    verify_cmd: str = ""
    effort: Optional[str] = None
    mask_stale_turns: int = 4
    compact_report_max_lines: int = 20
    code_agent_batch_mode: bool = False

    def __post_init__(self) -> None:
        if self.router is None:
            self.router = model_router.get_default_router()
        raw_filter = self.filter_url or os.environ.get("KAIZEN_MODERNBERT_URL") or DEFAULT_MODERNBERT_URL
        raw_jev = self.fallback_jev_url or os.environ.get("JEV_STYLE_URL") or DEFAULT_JEV_URL
        raw_burst = self.burst_url or os.environ.get("KAIZEN_GPU_BURST_URL", "")
        self.filter_url = _require_loopback(raw_filter)
        self.fallback_jev_url = _require_loopback(raw_jev)
        self.burst_url = _require_loopback(raw_burst) if raw_burst else ""

    def _active_filter_endpoint(self) -> Optional[str]:
        if _port_open(self.filter_url):
            return f"{self.filter_url}/v1/systemone"
        if _port_open(self.fallback_jev_url):
            return f"{self.fallback_jev_url}/v1/systemone"
        return None

    def _score_candidate(self, prompt: str, text: str, endpoint: Optional[str]) -> Tuple[float, str]:
        if endpoint is None:
            score = 0.85 if len(text.strip()) >= 12 else 0.25
            return score, "offline_heuristic"
        payload = {
            "state": text[:6000],
            "questions": {
                "relevance": {
                    "type": "score",
                    "instructions": f"Score technical relevance and factual grounding for: {prompt[:400]}",
                }
            },
        }
        try:
            data = _post_json(endpoint, payload, self.timeout_s)
        except RuntimeError:
            return 0.5, "modernbert_error_fallback"
        if not isinstance(data, dict):
            return 0.5, "modernbert_unparsed"
        answers = data.get("answers")
        ans = answers.get("relevance") if isinstance(answers, dict) else None
        if not isinstance(ans, dict) or "score" not in ans:
            return 0.5, "modernbert_unparsed"
        try:
            return float(ans["score"]), f"modernbert:{endpoint}"
        except (TypeError, ValueError):
            return 0.5, "modernbert_unparsed"

    def _build_spot_burst_plan(self) -> Optional[Dict[str, Any]]:
        if not self.spot_tier:
            return None
        if self.spot_nodes < 0:
            raise ValueError(f"spot_nodes must be >= 0, got {self.spot_nodes}")
        effective_nodes = self.spot_nodes
        if self.spot_tier == "distributed_256gb":
            effective_nodes = self.spot_nodes or 16
            return gpu_burst_orchestrator.plan_gpu_burst(
                tier=self.spot_tier,
                nodes=effective_nodes,
                dry_run=self.spot_dry_run,
                execute_approved_u2=False,
                check_aws=False,
            )
        if effective_nodes >= gpu_burst_orchestrator.TIER_FLEET_MIN_NODES:
            return gpu_burst_orchestrator.plan_spot_fleet(
                nodes=effective_nodes,
                tier=self.spot_tier,
                dry_run=self.spot_dry_run,
                execute_approved_u2=False,
                check_aws=False,
            )
        return gpu_burst_orchestrator.plan_gpu_burst(
            tier=self.spot_tier,
            nodes=max(1, effective_nodes),
            dry_run=self.spot_dry_run,
            execute_approved_u2=False,
            check_aws=False,
        )

    def _filter_one(self, prompt: str, idx: int, item_text: str, endpoint: Optional[str]) -> Dict[str, Any]:
        if self.safety_rail_fn is not None:
            safe, rail_reason = self.safety_rail_fn(item_text)
            if not safe:
                return {"index": idx, "kept": False, "score": 0.0, "rail": rail_reason, "backend": "safety_rail", "text": ""}

        if self.use_guardrails:
            g_res = guardrails.filter_mapped_candidate(
                prompt=prompt,
                candidate_text=item_text,
                bert_url=self.filter_url,
                min_score=self.min_score,
                fallback_jev_url=self.fallback_jev_url,
                timeout_s=min(self.timeout_s, 3.0),
                probe_http=endpoint is not None,
            )
            if g_res.get("rail") not in ("ok", "clean"):
                return {
                    "index": idx,
                    "kept": False,
                    "score": 0.0,
                    "rail": str(g_res["rail"]),
                    "backend": str(g_res["backend"]),
                    "text": "",
                }
            known_bases = {self.filter_url.rstrip("/"), self.fallback_jev_url.rstrip("/")}
            endpoint_norm = endpoint.rstrip("/") if endpoint is not None else ""
            endpoint_base = endpoint_norm.removesuffix("/v1/systemone")
            if g_res.get("backend") == "offline_heuristic" and endpoint is not None and endpoint_norm not in known_bases and endpoint_base not in known_bases:
                score, backend = self._score_candidate(prompt, item_text, endpoint)
                kept = score >= self.min_score
                return {
                    "index": idx,
                    "kept": kept,
                    "score": round(score, 4),
                    "rail": "ok",
                    "backend": backend,
                    "text": item_text if kept else "",
                }
            kept = bool(g_res["kept"])
            return {
                "index": idx,
                "kept": kept,
                "score": float(g_res["score"]),
                "rail": str(g_res["rail"]),
                "backend": str(g_res["backend"]),
                "text": item_text if kept else "",
            }

        safe, rail_reason = _default_safety_rail(item_text)
        if not safe:
            return {"index": idx, "kept": False, "score": 0.0, "rail": rail_reason, "backend": "safety_rail", "text": ""}
        score, backend = self._score_candidate(prompt, item_text, endpoint)
        kept = score >= self.min_score
        return {
            "index": idx,
            "kept": kept,
            "score": round(score, 4),
            "rail": rail_reason,
            "backend": backend,
            "text": item_text if kept else "",
        }

    async def execute_async(
        self,
        prompt: str,
        items: Sequence[str],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        assert self.router is not None
        map_target = self.router.resolve(self.map_model)
        reduce_target = self.router.resolve(self.reduce_model)
        input_rail_dict: Optional[Dict[str, Any]] = None
        if self.use_guardrails:
            input_rail_res = guardrails.evaluate_input_rail(prompt, bert_url=self.filter_url)
            input_rail_dict = asdict(input_rail_res)
            if not input_rail_res.allowed:
                return {
                    "status": "BLOCKED_BY_INPUT_RAIL",
                    "workflow": "LlmArmyMapReduce",
                    "input_rail": input_rail_dict,
                    "spot_burst_plan": None,
                    "mapped_count": 0,
                    "kept_count": 0,
                    "filtered_out_count": 0,
                    "noise_reduction_ratio": 0.0,
                    "output": "",
                }

        spot_burst_plan = self._build_spot_burst_plan()
        effective_resp_fmt = self.response_format
        if effective_resp_fmt is None and self.json_schema:
            effective_resp_fmt = {
                "type": "json_schema",
                "json_schema": {"name": "map_reduce_output", "schema": self.json_schema, "strict": True},
            }
        if dry_run:
            return {
                "status": "DRY_RUN",
                "workflow": "LlmArmyMapReduce",
                "item_count": len(items),
                "min_score": self.min_score,
                "guardrails_enabled": self.use_guardrails,
                "input_rail": input_rail_dict,
                "spot_burst_plan": spot_burst_plan,
                "response_format": effective_resp_fmt,
                "best_of_n": self.best_of_n,
                "verify_cmd": self.verify_cmd,
                "effort": self.effort,
                "mask_stale_turns": self.mask_stale_turns,
                "compact_report_max_lines": self.compact_report_max_lines,
                "code_agent_batch_mode": self.code_agent_batch_mode,
                "map_target": {"model": map_target.canonical_model, "backend": map_target.backend, "endpoint": map_target.endpoint},
                "filter_endpoint": f"{self.filter_url}/v1/systemone",
                "reduce_target": {"model": reduce_target.canonical_model, "backend": reduce_target.backend, "endpoint": reduce_target.endpoint},
                "stages": ["InputRail(Guardrails)", "Map(EC2 OSS / Spot Burst)", "Filter(ModernBERT+Guardrails)", "Reduce(Bedrock Claude 5.5)"],
            }

        sem = asyncio.Semaphore(max(1, int(self.max_concurrency)))
        map_router = self.router
        if self.spot_tier and self.burst_url and _port_open(self.burst_url):
            map_router = model_router.ModelRouter(
                litellm_url=self.burst_url,
                gateway_url=self.router.gateway_url,
                bridge_url=self.router.bridge_url,
                burst_url=self.burst_url,
            )

        async def _map_worker(idx: int, chunk: str) -> Tuple[int, str]:
            async with sem:
                msg = f"Task: {prompt}\n\nItem #{idx + 1}:\n{chunk}"
                out = await asyncio.to_thread(
                    map_router.chat_text,
                    msg,
                    "Extract concise technical evidence from this item.",
                    self.map_model,
                    1024,
                    0.2,
                )
                return idx, out

        if self.code_agent_batch_mode and len(items) > 1:
            batched_items = "\n".join(f"[Item #{i + 1}] {chunk}" for i, chunk in enumerate(items))
            batch_prompt = (
                f"Task: {prompt}\n\nBatch-process all {len(items)} items in ONE compact report "
                f"(<= {max(1, int(self.compact_report_max_lines))} lines):\n{batched_items}"
            )
            batch_out = await asyncio.to_thread(
                map_router.chat_text,
                batch_prompt,
                "Extract concise batched technical evidence across all items.",
                self.map_model,
                1024,
                0.2,
            )
            mapped = [(0, batch_out)]
        else:
            mapped = await asyncio.gather(*(_map_worker(i, item) for i, item in enumerate(items)))
        endpoint = self._active_filter_endpoint()
        filtered = await asyncio.gather(
            *(asyncio.to_thread(self._filter_one, prompt, idx, text, endpoint) for idx, text in mapped)
        )
        kept_entries = [entry for entry in filtered if entry["kept"]]
        filtered_out_count = len(mapped) - len(kept_entries)
        noise_reduction_ratio = round(filtered_out_count / max(1, len(mapped)), 4)

        if not kept_entries:
            return {
                "status": "NO_CANDIDATES_KEPT",
                "workflow": "LlmArmyMapReduce",
                "mapped_count": len(mapped),
                "kept_count": 0,
                "filtered_out_count": filtered_out_count,
                "noise_reduction_ratio": noise_reduction_ratio,
                "input_rail": input_rail_dict,
                "spot_burst_plan": spot_burst_plan,
                "filtered": filtered,
                "output": "",
            }

        joined = "\n\n".join(f"[Item {e['index'] + 1} score={e['score']}]\n{e['text']}" for e in kept_entries)
        reduce_prompt = f"Synthesize verified findings for task:\n{prompt}\n\nFiltered Evidence:\n{joined}"
        reduced = await asyncio.to_thread(
            _chat_text_router_safe,
            self.router,
            reduce_prompt,
            "You are the Kaizen Reduce synthesizer. Produce an exact, evidence-backed summary.",
            self.reduce_model,
            2048,
            0.1,
            response_format=effective_resp_fmt,
            json_schema=self.json_schema,
            effort=self.effort,
        )
        if self.compact_report_max_lines > 0:
            r_lines = (reduced or "").splitlines()
            if len(r_lines) > self.compact_report_max_lines:
                reduced = "\n".join(r_lines[: self.compact_report_max_lines]) + (
                    f"\n[...truncated to {self.compact_report_max_lines} lines...]"
                )
        best_of_n_verdict: Optional[Dict[str, Any]] = None
        if self.best_of_n > 1 or self.verify_cmd.strip():
            best_of_n_verdict = await asyncio.to_thread(
                run_best_of_n_cascade_tool,
                reduce_prompt,
                self.best_of_n,
                self.verify_cmd,
                "corporate_proprietary",
                False,
            )
        return {
            "status": "OK",
            "workflow": "LlmArmyMapReduce",
            "mapped_count": len(mapped),
            "kept_count": len(kept_entries),
            "filtered_out_count": filtered_out_count,
            "noise_reduction_ratio": noise_reduction_ratio,
            "input_rail": input_rail_dict,
            "spot_burst_plan": spot_burst_plan,
            "best_of_n": best_of_n_verdict,
            "map_model": map_target.canonical_model,
            "reduce_model": reduce_target.canonical_model,
            "filter_backend": kept_entries[0]["backend"],
            "filtered": filtered,
            "output": reduced,
        }

    def execute(self, prompt: str, items: Sequence[str], dry_run: bool = False) -> Dict[str, Any]:
        return asyncio.run(self.execute_async(prompt=prompt, items=items, dry_run=dry_run))


def build_llm_army_map_reduce_agent(
    router: Optional[model_router.ModelRouter] = None,
    attach_mcps: bool = False,
    registry: Optional[Dict[str, McpServerSpec]] = None,
) -> Any:
    fastmcp_tools = build_mcp_toolsets(("fastmcp",), registry=registry) if attach_mcps else []
    map_worker = _make_llm_agent(
        name="Ec2OssMapWorker",
        description="Parallel EC2 OSS mini-pool (:4000/:8765) map worker.",
        instruction="Process shard independently and return concise structured evidence.",
        model_name="mini-pool",
        router=router,
        tools=fastmcp_tools,
    )
    map_stage = _make_workflow_agent(
        kind="ParallelAgent",
        name="MapFanOutStage",
        description="Parallel fan-out across EC2 OSS mini-pool workers.",
        sub_agents=[map_worker],
    )
    filter_stage = _make_llm_agent(
        name="ModernBertRailFilterStage",
        description="Filter mapped items via ModernBERT SystemOne (:18800) and safety/NeMo rail gate.",
        instruction="Score relevance and enforce safety rails before passing evidence to Reduce.",
        model_name="granite-4-h-tiny",
        router=router,
    )
    reduce_stage = _make_llm_agent(
        name="BedrockOpusReduceStage",
        description="Reduce verified evidence using Corporate Bedrock Claude Opus 5.5.",
        instruction="Synthesize filtered evidence into a verified engineering conclusion.",
        model_name="claude-opus-5-5",
        router=router,
    )
    return _make_workflow_agent(
        kind="SequentialAgent",
        name="LlmArmyMapReduce",
        description="Map (EC2 :8765/:4000) -> Filter (ModernBERT :18800 + safety rail) -> Reduce (Claude Opus 5.5).",
        sub_agents=[map_stage, filter_stage, reduce_stage],
    )


WORKFLOW_BUILDERS: Dict[str, Callable[..., Any]] = {
    "KaizenOndemandParityLoop": build_ondemand_parity_loop,
    "KaizenUcRcaPipeline": build_uc_rca_pipeline,
    "KaizenSeniorReviewSwarm": build_senior_review_swarm,
    "KaizenCloudComposeOpsParityAgent": build_cloud_compose_ops_parity_agent,
    "KaizenOpenApiContractSyncAgent": build_openapi_contract_sync_agent,
    "LlmArmyMapReduce": build_llm_army_map_reduce_agent,
}

WORKFLOW_CATALOG: Dict[str, WorkflowDescriptor] = {
    "KaizenOndemandParityLoop": WorkflowDescriptor(
        name="KaizenOndemandParityLoop",
        agent_type="LoopAgent",
        description="Ondemand parity loop combining story_guard, verify_provenance, parity_gate (POPULATED_MATCH vs EMPTY_BOTH_200), and Serena/Zoekt/Code-RAG.",
        default_model="claude-sonnet-5-5",
        mcp_servers=("serena", "fastmcp"),
        sub_agent_names=("StoryGuardVerifier", "AstProvenanceAuditor", "ParityOracleGate"),
        gates=("story_guard", "verify_provenance", "parity_gate"),
    ),
    "KaizenUcRcaPipeline": WorkflowDescriptor(
        name="KaizenUcRcaPipeline",
        agent_type="SequentialAgent",
        description="Jira UC-* + Confluence -> HAR Root=1-<hex> & CloudWatch kaizen-prod -> Serena/Zoekt/Code-RAG -> Popperian TDD falsifier.",
        default_model="claude-opus-5-5",
        mcp_servers=("kaizen-atlassian", "awsknowledge", "serena", "fastmcp", "kaizen-devops"),
        sub_agent_names=("UcTicketContextFetcher", "HarCloudWatchCorrelator", "CodebaseTraceInvestigator", "PopperianTddFalsifier"),
    ),
    "KaizenSeniorReviewSwarm": WorkflowDescriptor(
        name="KaizenSeniorReviewSwarm",
        agent_type="ParallelAgent",
        description="7 Kaizen senior engineering reviewers + 4 Jenkins pre-flight gates across kaizen-gitlab, internal-git-gitlab, and kaizen-devops.",
        default_model="claude-sonnet-5-5",
        mcp_servers=("kaizen-gitlab", "internal-git-gitlab", "kaizen-devops"),
        sub_agent_names=tuple(spec[0] for spec in SENIOR_REVIEW_SPECS) + ("JenkinsPreFlightShield",),
        gates=JENKINS_PREFLIGHT_GATES,
    ),
    "KaizenCloudComposeOpsParityAgent": WorkflowDescriptor(
        name="KaizenCloudComposeOpsParityAgent",
        agent_type="LlmAgent",
        description="CloudCompose ops & MASTER/env-* compose parity agent wired to kaizen-gitlab, kaizen-devops, awsknowledge, and context7.",
        default_model="claude-sonnet-5-5",
        mcp_servers=("kaizen-gitlab", "kaizen-devops", "awsknowledge", "context7"),
        sub_agent_names=("KaizenCloudComposeOpsParityAgent",),
    ),
    "KaizenOpenApiContractSyncAgent": WorkflowDescriptor(
        name="KaizenOpenApiContractSyncAgent",
        agent_type="LlmAgent",
        description="OpenAPI/Swagger 1:1 contract sync agent wired to kaizen-gitlab, kaizen-devops, awsknowledge, and context7.",
        default_model="claude-sonnet-5-5",
        mcp_servers=("kaizen-gitlab", "kaizen-devops", "awsknowledge", "context7"),
        sub_agent_names=("KaizenOpenApiContractSyncAgent",),
    ),
    "LlmArmyMapReduce": WorkflowDescriptor(
        name="LlmArmyMapReduce",
        agent_type="SequentialAgent",
        description="Map (EC2 :8765/:4000) -> Filter (ModernBERT :18800 + safety rail) -> Reduce (Claude Opus 5.5).",
        default_model="claude-opus-5-5",
        mcp_servers=("fastmcp",),
        sub_agent_names=("MapFanOutStage", "ModernBertRailFilterStage", "BedrockOpusReduceStage"),
    ),
}


def resolve_workflow_name(name: str) -> str:
    cleaned = (name or "").strip()
    if cleaned in WORKFLOW_CATALOG:
        return cleaned
    alias = WORKFLOW_ALIASES.get(cleaned.lower())
    if alias:
        return alias
    raise ValueError(f"Unknown ADK workflow {name!r}. Available: {', '.join(WORKFLOW_CATALOG.keys())}")


def build_workflow(
    name: str,
    router: Optional[model_router.ModelRouter] = None,
    attach_mcps: bool = False,
    registry: Optional[Dict[str, McpServerSpec]] = None,
) -> Any:
    canonical = resolve_workflow_name(name)
    builder = WORKFLOW_BUILDERS[canonical]
    return builder(router=router, attach_mcps=attach_mcps, registry=registry)


def list_workflows() -> List[Dict[str, Any]]:
    return [
        {
            "name": desc.name,
            "agent_type": desc.agent_type,
            "default_model": desc.default_model,
            "mcp_servers": list(desc.mcp_servers),
            "sub_agents": list(desc.sub_agent_names),
            "description": desc.description,
        }
        for desc in WORKFLOW_CATALOG.values()
    ]


def _collect_agent_tree(agent: Any) -> Dict[str, Any]:
    raw_model = getattr(agent, "model", "")
    model_name = getattr(raw_model, "model", str(raw_model or ""))
    subs = [_collect_agent_tree(child) for child in getattr(agent, "sub_agents", []) or []]
    node: Dict[str, Any] = {
        "name": getattr(agent, "name", ""),
        "class": type(agent).__name__,
    }
    if model_name:
        node["model"] = model_name
    if subs:
        node["sub_agents"] = subs
    return node


def inspect_workflow(name: str, registry: Optional[Dict[str, McpServerSpec]] = None) -> Dict[str, Any]:
    canonical = resolve_workflow_name(name)
    desc = WORKFLOW_CATALOG[canonical]
    reg = registry if registry is not None else discover_mcp_registry()
    agent = build_workflow(canonical, attach_mcps=False, registry=reg)
    mcp_details = []
    for srv in desc.mcp_servers:
        spec = resolve_mcp_spec(reg, srv)
        mcp_details.append(spec.to_dict() if spec is not None else {"name": srv, "status": "MISSING"})

    return {
        "name": desc.name,
        "agent_type": desc.agent_type,
        "default_model": desc.default_model,
        "description": desc.description,
        "gates": list(desc.gates),
        "mcp_servers": mcp_details,
        "topology": _collect_agent_tree(agent),
        "capabilities": describe_runtime_capabilities(),
    }


def _extract_event_texts(event: Any) -> List[str]:
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    return [str(getattr(part, "text", "")) for part in parts if getattr(part, "text", None)]


async def _run_agent_async(agent: Any, prompt: str) -> List[str]:
    svc = InMemorySessionService()
    await svc.create_session(app_name="kaizen-adk", user_id="operator", session_id="run-1")
    runner = Runner(app_name="kaizen-adk", agent=agent, session_service=svc)
    msg = genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=prompt)])
    outputs: List[str] = []
    async for ev in runner.run_async(user_id="operator", session_id="run-1", new_message=msg):
        outputs.extend(_extract_event_texts(ev))
    return outputs


def _collect_runner_events(agent: Any, prompt: str) -> List[str]:
    if not HAS_GOOGLE_ADK or Runner is None or InMemorySessionService is None:
        return []
    return asyncio.run(_run_agent_async(agent, prompt))


def run_workflow(
    name: str,
    prompt: str,
    dry_run: bool = False,
    attach_mcps: bool = False,
    router: Optional[model_router.ModelRouter] = None,
    registry: Optional[Dict[str, McpServerSpec]] = None,
) -> Dict[str, Any]:
    canonical = resolve_workflow_name(name)
    if canonical == "LlmArmyMapReduce":
        pipeline = LlmArmyMapReduce(router=router)
        return pipeline.execute(prompt=prompt, items=[prompt], dry_run=dry_run)

    reg = registry if registry is not None else discover_mcp_registry()
    if dry_run:
        info = inspect_workflow(canonical, registry=reg)
        info["status"] = "DRY_RUN"
        info["prompt"] = prompt
        info["attach_mcps"] = attach_mcps
        return info

    agent = build_workflow(canonical, router=router, attach_mcps=attach_mcps, registry=reg)
    if not HAS_GOOGLE_ADK:
        return {
            "status": "ADK_NOT_INSTALLED",
            "workflow": canonical,
            "error": "Install google-adk[mcp] to execute ADK workflows.",
        }
    outputs = _collect_runner_events(agent, prompt)
    return {
        "status": "OK",
        "workflow": canonical,
        "events": len(outputs),
        "output": "\n\n".join(outputs),
    }


def _mcp_matrix_payload(config_dir: Optional[Path] = None) -> Dict[str, Any]:
    reg = discover_mcp_registry(config_dir=config_dir)
    entries = []
    for req_name in REQUIRED_MCP_SERVERS:
        spec = resolve_mcp_spec(reg, req_name)
        entries.append(spec.to_dict() if spec is not None else {"name": req_name, "status": "MISSING"})
    extra = [spec.to_dict() for key, spec in sorted(reg.items()) if key not in REQUIRED_MCP_SERVERS]
    return {"required": entries, "additional": extra, "capabilities": describe_runtime_capabilities()}


def _parse_json_schema_arg(raw_arg: str) -> Optional[Dict[str, Any]]:
    text = (raw_arg or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        text = Path(text[1:]).expanduser().read_text(encoding="utf-8")
    elif Path(text).is_file():
        text = Path(text).read_text(encoding="utf-8")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("--json-schema must decode to a JSON object.")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kaizen-kit adk", description="Google ADK Kaizen Domain Agents & 7-MCP CLI")
    sub = ap.add_subparsers(dest="adk_cmd", required=True)

    p_list = sub.add_parser("list", help="List registered Kaizen ADK workflows")
    p_list.add_argument("--json", action="store_true", dest="as_json")

    p_insp = sub.add_parser("inspect", help="Inspect workflow topology, sub-agents, and bound MCPs")
    p_insp.add_argument("workflow")
    p_insp.add_argument("--json", action="store_true", dest="as_json")

    p_mcp = sub.add_parser("mcp-matrix", help="Show discovered non-secret 7-MCP + FastMCP registry")
    p_mcp.add_argument("--config-dir", default="")
    p_mcp.add_argument("--json", action="store_true", dest="as_json")

    p_mr = sub.add_parser("map-reduce", help="Run or dry-run LlmArmyMapReduce pipeline")
    p_mr.add_argument("--prompt", required=True)
    p_mr.add_argument("--item", action="append", default=[])
    p_mr.add_argument("--map-model", default="mini-pool")
    p_mr.add_argument("--reduce-model", default="claude-opus-5-5")
    p_mr.add_argument("--min-score", type=float, default=0.45)
    p_mr.add_argument(
        "--spot-tier",
        default="",
        choices=["", "daytime_bedrock", "large", "frontier_1t", "ultra_frontier_2_8t", "distributed_256gb"],
    )
    p_mr.add_argument("--spot-nodes", type=int, default=0)
    p_mr.add_argument("--json-schema", default="", help="Inline JSON schema or @path/to/schema.json")
    p_mr.add_argument("--best-of-n", type=int, default=1)
    p_mr.add_argument("--verify-cmd", default="")
    p_mr.add_argument("--effort", default="", choices=["", "minimal", "low", "medium", "high"])
    p_mr.add_argument("--batch-mode", action="store_true", help="Consolidate map items into single CodeAgent batch call")
    p_mr.add_argument("--compact-lines", type=int, default=20, help="Compact report line cap (default: 20)")
    p_mr.add_argument("--no-guardrails", action="store_true")
    p_mr.add_argument("--dry-run", action="store_true")
    p_mr.add_argument("--json", action="store_true", dest="as_json")

    p_run = sub.add_parser("run", help="Run or dry-run a named Kaizen ADK workflow")
    p_run.add_argument("workflow")
    p_run.add_argument("--prompt", required=True)
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--attach-mcps", action="store_true")
    p_run.add_argument("--json", action="store_true", dest="as_json")

    p_rag = sub.add_parser("rag-ocr", help="Run Hybrid RAG + optional Document OCR probe")
    p_rag.add_argument("--query", required=True)
    p_rag.add_argument("--document-path", default="")
    p_rag.add_argument("--limit", type=int, default=10)
    p_rag.add_argument("--workspace", default=".")
    p_rag.add_argument("--json", action="store_true", dest="as_json")

    p_voice = sub.add_parser("voice-to-action", help="Convert voice transcript into structured action items")
    p_voice.add_argument("--transcript", required=True)
    p_voice.add_argument("--dry-run", action="store_true")
    p_voice.add_argument("--json", action="store_true", dest="as_json")
    return ap


def _print_result(payload: Any, as_json: bool) -> None:
    if as_json or isinstance(payload, (dict, list)):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(str(payload))


def main(argv: Optional[List[str]] = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)

    if args.adk_cmd == "list":
        _print_result({"workflows": list_workflows(), "capabilities": describe_runtime_capabilities()}, args.as_json)
        return 0
    if args.adk_cmd == "inspect":
        _print_result(inspect_workflow(args.workflow), args.as_json)
        return 0
    if args.adk_cmd == "mcp-matrix":
        cfg = Path(args.config_dir) if args.config_dir else None
        _print_result(_mcp_matrix_payload(config_dir=cfg), args.as_json)
        return 0
    if args.adk_cmd == "map-reduce":
        items = args.item if args.item else [args.prompt]
        schema = _parse_json_schema_arg(args.json_schema)
        pipeline = LlmArmyMapReduce(
            map_model=args.map_model,
            reduce_model=args.reduce_model,
            min_score=args.min_score,
            use_guardrails=not args.no_guardrails,
            spot_tier=args.spot_tier,
            spot_nodes=args.spot_nodes,
            spot_dry_run=True,
            json_schema=schema,
            best_of_n=args.best_of_n,
            verify_cmd=args.verify_cmd,
            effort=args.effort or None,
            compact_report_max_lines=args.compact_lines,
            code_agent_batch_mode=args.batch_mode,
        )
        res = pipeline.execute(prompt=args.prompt, items=items, dry_run=args.dry_run)
        _print_result(res, args.as_json)
        return 0 if res.get("status") in {"OK", "DRY_RUN", "NO_CANDIDATES_KEPT", "BLOCKED_BY_INPUT_RAIL"} else 1
    if args.adk_cmd == "run":
        res = run_workflow(
            args.workflow,
            prompt=args.prompt,
            dry_run=args.dry_run,
            attach_mcps=args.attach_mcps,
        )
        _print_result(res, args.as_json)
        return 0 if res.get("status") in {"OK", "DRY_RUN"} else 1
    if args.adk_cmd == "rag-ocr":
        res = run_hybrid_rag_ocr_tool(
            query=args.query,
            document_path=args.document_path,
            limit=args.limit,
            workspace=args.workspace,
        )
        _print_result(res, args.as_json)
        return 0
    if args.adk_cmd == "voice-to-action":
        res = run_voice_to_action_tool(transcript=args.transcript, dry_run=args.dry_run)
        _print_result(res, args.as_json)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
