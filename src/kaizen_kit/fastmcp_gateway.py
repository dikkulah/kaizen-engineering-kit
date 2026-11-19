#!/usr/bin/env python3
"""Unified FastMCP Gateway for EC2 Cloud-Brain services.

Mounts and proxies JEV Gateway (:8765), Code-RAG (:18808), Zoekt (:6070),
Serena MCP (:24280), and LiteLLM (:4000) behind one FastMCP Streamable HTTP
server (:8770 standalone and /mcp when proxied under JEV Gateway :8765).

Can run either as `kaizen_kit.fastmcp_gateway` or standalone on EC2
(`/opt/kaizen-mini-pool/fastmcp_gateway.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from fastmcp import FastMCP
from fastmcp.server.server import create_proxy
from starlette.requests import Request
from starlette.responses import JSONResponse

try:
    from kaizen_kit import carbon_orchestrator, gemini_cloud_client, gpu_burst_orchestrator, guardrails, mirix_memory, neon_night_shift
    from kaizen_kit.hooks import lossless_test_distiller
except ImportError:
    import carbon_orchestrator  # type: ignore[no-redef]
    import gpu_burst_orchestrator  # type: ignore[no-redef]
    import guardrails  # type: ignore[no-redef]
    import mirix_memory  # type: ignore[no-redef]
    from hooks import lossless_test_distiller  # type: ignore[no-redef]
    try:
        import gemini_cloud_client  # type: ignore[no-redef]
    except ImportError:
        gemini_cloud_client = None  # type: ignore[assignment]
    try:
        import neon_night_shift  # type: ignore[no-redef]
    except ImportError:
        neon_night_shift = None  # type: ignore[assignment]

_LOG = logging.getLogger("kaizen_kit.fastmcp_gateway")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _require_loopback(url: str) -> str:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"Gateway backend URL scheme must be http or https, got {scheme!r}")
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(f"Gateway backend URL must be loopback, got {host!r}")
    return url.rstrip("/")


@dataclass(frozen=True)
class GatewayConfig:
    """Loopback endpoint and timeout settings for the unified FastMCP gateway."""

    jev_url: str = field(default_factory=lambda: os.environ.get("KAIZEN_JEV_URL", "http://127.0.0.1:8765"))
    coderag_url: str = field(default_factory=lambda: os.environ.get("KAIZEN_CODERAG_URL", "http://127.0.0.1:18808"))
    zoekt_url: str = field(default_factory=lambda: os.environ.get("KAIZEN_ZOEKT_URL", "http://127.0.0.1:6070"))
    serena_url: str = field(default_factory=lambda: os.environ.get("KAIZEN_SERENA_URL", "http://127.0.0.1:24280/mcp"))
    litellm_url: str = field(default_factory=lambda: os.environ.get("KAIZEN_LITELLM_URL", "http://127.0.0.1:4000"))
    embed_url: str = field(default_factory=lambda: os.environ.get("KAIZEN_EMBED_URL", "http://127.0.0.1:8766"))
    mirix_dir: str = field(default_factory=lambda: os.environ.get("KAIZEN_MIRIX_DIR", ""))
    probe_timeout_s: float = 2.0
    rpc_timeout_s: float = 30.0
    chat_timeout_s: float = 90.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "jev_url", _require_loopback(self.jev_url))
        object.__setattr__(self, "coderag_url", _require_loopback(self.coderag_url))
        object.__setattr__(self, "zoekt_url", _require_loopback(self.zoekt_url))
        object.__setattr__(self, "serena_url", _require_loopback(self.serena_url))
        object.__setattr__(self, "litellm_url", _require_loopback(self.litellm_url))
        object.__setattr__(self, "embed_url", _require_loopback(self.embed_url))


async def _http_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 20.0,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 0, "backend": url, "error": str(exc) or type(exc).__name__}

    if resp.status_code >= 400:
        return {
            "ok": False,
            "status_code": resp.status_code,
            "backend": url,
            "error": resp.text[:400],
        }
    try:
        data = resp.json()
    except ValueError:
        return {"ok": True, "status_code": resp.status_code, "raw": resp.text[:2000]}
    if isinstance(data, dict):
        return data
    return {"ok": True, "data": data}


async def _probe_backend(name: str, url: str, timeout: float, accept_4xx: bool = False) -> tuple[str, str]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
    except httpx.HTTPError:
        return name, "DOWN"
    if accept_4xx and resp.status_code in (200, 400, 404, 405, 406):
        return name, "UP"
    return name, ("UP" if resp.status_code < 400 else f"HTTP_{resp.status_code}")


async def collect_backend_health(cfg: GatewayConfig) -> Dict[str, str]:
    checks = [
        _probe_backend("jev", f"{cfg.jev_url}/health", cfg.probe_timeout_s),
        _probe_backend("coderag", f"{cfg.coderag_url}/health", cfg.probe_timeout_s),
        _probe_backend("zoekt", f"{cfg.zoekt_url}/healthz", cfg.probe_timeout_s),
        _probe_backend("litellm", f"{cfg.litellm_url}/health/liveliness", cfg.probe_timeout_s),
        _probe_backend("serena", cfg.serena_url, cfg.probe_timeout_s, accept_4xx=True),
    ]
    results = await asyncio.gather(*checks)
    return dict(results)


def _build_coderag_server(cfg: GatewayConfig) -> FastMCP:
    srv = FastMCP("coderag", version="1.2.0")

    @srv.tool
    async def query(query: str, limit: int = 10) -> Dict[str, Any]:
        """Run hybrid vector + FTS + ModernBERT reranked code search on EC2 Code-RAG."""
        bounded = max(1, min(int(limit), 50))
        return await _http_json(
            "POST",
            f"{cfg.coderag_url}/v1/rag/query",
            payload={"query": query, "limit": bounded},
            timeout=cfg.rpc_timeout_s,
        )

    @srv.tool
    async def reindex(root: str = "/work-space") -> Dict[str, Any]:
        """Trigger background Code-RAG LanceDB reindex for a workspace path on EC2."""
        return await _http_json(
            "POST",
            f"{cfg.coderag_url}/v1/rag/reindex",
            payload={"root": root},
            timeout=cfg.rpc_timeout_s,
        )

    @srv.tool
    async def hybrid_search(query: str, limit: int = 10, rerank: bool = True) -> Dict[str, Any]:
        """Run Hybrid RAG (dense + FTS5 + RRF rerank) on EC2 Code-RAG (:18808)."""
        bounded = max(1, min(int(limit), 50))
        return await _http_json(
            "POST",
            f"{cfg.coderag_url}/v1/rag/query",
            payload={"query": query, "limit": bounded, "hybrid": True, "rerank": bool(rerank)},
            timeout=cfg.rpc_timeout_s,
        )

    @srv.tool
    async def ocr_probe(document_path: str, doc_type: str = "auto") -> Dict[str, Any]:
        """Probe EC2 Document OCR (Docling / olmOCR 2 / Gemma 4 12B Unified) for PDFs or screenshots."""
        try:
            from kaizen_kit import localmodels
        except ImportError:
            import localmodels  # type: ignore[no-redef]
        ocr_cls = getattr(localmodels, "DocumentOcrClient", None)
        if ocr_cls is not None:
            return await asyncio.to_thread(lambda: ocr_cls().parse_document(document_path, doc_type=doc_type))
        return await _http_json(
            "POST",
            f"{cfg.coderag_url}/v1/ocr/probe",
            payload={"document_path": document_path, "doc_type": doc_type},
            timeout=cfg.rpc_timeout_s,
        )

    return srv


def _build_zoekt_server(cfg: GatewayConfig) -> FastMCP:
    srv = FastMCP("zoekt", version="1.0.0")

    @srv.tool
    async def search(query: str, limit: int = 20) -> Dict[str, Any]:
        """Search indexed /work-space repositories via Zoekt trigram/regex engine."""
        bounded = max(1, min(int(limit), 100))
        payload = {"Q": query, "Opts": {"MaxDocDisplayCount": bounded}}
        return await _http_json("POST", f"{cfg.zoekt_url}/api/search", payload=payload, timeout=cfg.rpc_timeout_s)

    return srv


def _build_litellm_server(cfg: GatewayConfig) -> FastMCP:
    srv = FastMCP("litellm", version="1.0.0")

    @srv.tool
    async def models() -> Dict[str, Any]:
        """List available OSS models registered in EC2 LiteLLM (:4000)."""
        return await _http_json(
            "GET",
            f"{cfg.litellm_url}/v1/models",
            timeout=cfg.probe_timeout_s + 3.0,
            headers={"Authorization": "Bearer local"},
        )

    @srv.tool
    async def chat(
        prompt: str,
        model: str = "mini-pool",
        system: str = "",
        max_tokens: int = 512,
        temperature: float = 0.2,
        response_format: Optional[Dict[str, Any]] = None,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run a chat completion against an EC2 LiteLLM OSS pool model with optional Lever A constrained decoding."""
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max(1, min(int(max_tokens), 8192)),
            "temperature": float(temperature),
        }
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        elif json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "fastmcp_output", "schema": dict(json_schema), "strict": True},
            }
        return await _http_json(
            "POST",
            f"{cfg.litellm_url}/v1/chat/completions",
            payload=payload,
            timeout=cfg.chat_timeout_s,
            headers={"Authorization": "Bearer local"},
        )

    return srv


def _build_jev_decision_question(question_type: str, instructions: str, criteria: Optional[Any]) -> Dict[str, Any]:
    q_type = (question_type or "noul").strip().lower()
    if q_type == "choice":
        crit = dict(criteria) if isinstance(criteria, dict) else {}
        return {"type": "choice", "instructions": instructions, "criteria": crit}
    if q_type == "score":
        q: Dict[str, Any] = {"type": "score", "instructions": instructions}
        if criteria:
            q["rubric"] = criteria if isinstance(criteria, str) else ", ".join(str(x) for x in criteria)
        return q
    q_noul: Dict[str, Any] = {"type": "noul", "instructions": instructions}
    if isinstance(criteria, list):
        q_noul["rules"] = criteria
    return q_noul


def _run_gemini_decision(
    input_content: str,
    instructions: str,
    question_type: str,
    criteria: Optional[Any],
    data_classification: str,
    dry_run: bool,
) -> Dict[str, Any]:
    if gemini_cloud_client is None:
        return {"ok": False, "error": "gemini_cloud_client module is not available."}
    client = gemini_cloud_client.GeminiCloudClient()
    q_type = (question_type or "noul").strip().lower()
    if q_type == "choice":
        crit = dict(criteria) if isinstance(criteria, dict) else {}
        res = client.decide_choice(
            input_content,
            instructions,
            crit,
            data_classification=data_classification,
            dry_run=dry_run,
        )
    elif q_type == "score":
        levels = list(criteria) if isinstance(criteria, (list, tuple)) else [str(criteria or "low"), "high"]
        res = client.decide_score(
            input_content,
            instructions,
            levels,
            data_classification=data_classification,
            dry_run=dry_run,
        )
    else:
        res = client.decide_noul(
            input_content,
            instructions,
            data_classification=data_classification,
            dry_run=dry_run,
        )
    return {"ok": True, "backend": "gemini-decision-1", "data_classification": data_classification, **res}


def _build_neon_server(cfg: Optional[GatewayConfig] = None) -> FastMCP:
    active_cfg = cfg or GatewayConfig()
    srv = FastMCP("neon", version="2.0.0")

    @srv.tool
    async def decide(
        input_content: str,
        instructions: str,
        question_type: str = "noul",
        criteria: Optional[Any] = None,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Dispatch 0-output-token decisions to JEV :8765 (corporate) or gemini-decision-1 (public_sanitized)."""
        if data_classification == "public_sanitized":
            try:
                return await asyncio.to_thread(
                    _run_gemini_decision,
                    input_content,
                    instructions,
                    question_type,
                    criteria,
                    data_classification,
                    dry_run,
                )
            except PermissionError as exc:
                return {
                    "ok": False,
                    "blocked": True,
                    "backend": "gemini-decision-1",
                    "data_classification": data_classification,
                    "error": str(exc),
                }
            except (ValueError, RuntimeError) as exc:
                return {
                    "ok": False,
                    "backend": "gemini-decision-1",
                    "data_classification": data_classification,
                    "error": str(exc),
                }

        body = {
            "state": input_content[:6000],
            "questions": {"q": _build_jev_decision_question(question_type, instructions, criteria)},
        }
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "backend": "ec2_jev_8765",
                "url": f"{active_cfg.jev_url}/v1/systemone",
                "data_classification": data_classification,
                "expected_output_tokens": 0,
                "payload": body,
            }
        resp = await _http_json("POST", f"{active_cfg.jev_url}/v1/systemone", payload=body, timeout=active_cfg.rpc_timeout_s)
        return {"backend": "ec2_jev_8765", "data_classification": data_classification, **resp}

    @srv.tool
    async def batch_plan(
        prompts: List[str],
        model: str = "gemini-4-hydrogen",
        system_instruction: Optional[str] = None,
        cache_contents: Optional[str] = None,
        ttl: str = "3600s",
        service_tier: str = "flex",
        thinking_budget: Optional[int] = None,
        thinking_level: Optional[str] = None,
        response_json_schema: Optional[Dict[str, Any]] = None,
        display_name: str = "kaizen-neon-batch",
        data_classification: str = "corporate_proprietary",
    ) -> Dict[str, Any]:
        """Build dry-run JSONL + Context Cache + Flex/Batch plan via fail-closed gemini_cloud_client."""
        if gemini_cloud_client is None:
            return {"ok": False, "error": "gemini_cloud_client module is not available."}
        if not prompts:
            return {"ok": False, "error": "prompts must contain at least one item."}
        client = gemini_cloud_client.GeminiCloudClient()
        try:
            cache_plan: Optional[Dict[str, Any]] = None
            cached_ref: Optional[str] = None
            if cache_contents or system_instruction:
                try:
                    cache_plan = client.create_cache(
                        model=model,
                        contents=cache_contents,
                        system_instruction=system_instruction,
                        ttl=ttl,
                        display_name=f"{display_name}-cache",
                        data_classification=data_classification,
                        dry_run=True,
                    )
                    cached_ref = "cachedContents/dry-run-plan"
                except ValueError as exc:
                    if "EXPLICIT_CACHE_UNSUPPORTED" not in str(exc):
                        raise
                    cache_plan = {"caching_mode": "implicit_only", "model": model, "reason": str(exc)}
                    cached_ref = None

            items = [{"key": f"item-{idx}", "contents": p} for idx, p in enumerate(prompts)]
            jsonl_text = client.build_batch_jsonl(
                items,
                cached_content=cached_ref,
                thinking_budget=thinking_budget,
                thinking_level=thinking_level,
                response_json_schema=response_json_schema,
                system_instruction=None if cached_ref else system_instruction,
                service_tier=service_tier,
                data_classification=data_classification,
            )
            inline_requests = [
                {
                    "key": f"item-{idx}",
                    "contents": p,
                    "cached_content": cached_ref,
                    "thinking_budget": thinking_budget,
                    "thinking_level": thinking_level,
                    "response_json_schema": response_json_schema,
                    "system_instruction": None if cached_ref else system_instruction,
                    "service_tier": service_tier,
                }
                for idx, p in enumerate(prompts)
            ]
            inline_plan = client.submit_batch_inline(
                model=model,
                requests=inline_requests,
                display_name=display_name,
                data_classification=data_classification,
                dry_run=True,
            )
            return {
                "ok": True,
                "dry_run": True,
                "data_classification": data_classification,
                "model": inline_plan["model"],
                "service_tier": service_tier,
                "thinking_level": thinking_level,
                "cache_plan": cache_plan,
                "jsonl_lines": jsonl_text.splitlines(),
                "inline_batch_plan": inline_plan,
            }
        except PermissionError as exc:
            return {"ok": False, "blocked": True, "data_classification": data_classification, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "data_classification": data_classification, "error": str(exc)}

    @srv.tool
    async def plan(
        prompt: str,
        task_type: str = "auto",
        data_classification: str = "corporate_proprietary",
        context_tokens: int = 0,
        allow_gpu_burst: bool = False,
    ) -> Dict[str, Any]:
        """Build a Neon Two-Phase (Planner/Executor/Verifier) plan enforcing fail-closed data residency."""
        try:
            return await asyncio.to_thread(
                carbon_orchestrator.build_neon_phase_plan,
                prompt=prompt,
                task_type=task_type,
                data_classification=data_classification,
                context_tokens=context_tokens,
                allow_gpu_burst=allow_gpu_burst,
            )
        except PermissionError as exc:
            return {"ok": False, "blocked": True, "data_classification": data_classification, "error": str(exc)}

    @srv.tool
    async def night_shift(
        tasks: Optional[List[Dict[str, Any]]] = None,
        morning: bool = False,
        execute: bool = False,
        use_context_cache: bool = True,
        shared_rubric: str = "",
        allow_distributed_256gb: bool = False,
    ) -> Dict[str, Any]:
        """Run or collect the Neon Night-Shift (EC2 AutoDream, Best-of-N, QLoRA SFT/DPO & Gemini)."""
        if neon_night_shift is None:
            return {"ok": False, "error": "neon_night_shift module is not available."}
        if morning:
            return await asyncio.to_thread(neon_night_shift.collect_morning_report, execute=execute)
        parsed_tasks = (
            [neon_night_shift.NightShiftTask.from_mapping(item, idx) for idx, item in enumerate(tasks, start=1)]
            if tasks
            else None
        )
        return await asyncio.to_thread(
            neon_night_shift.run_night_shift,
            parsed_tasks,
            execute=execute,
            use_context_cache=use_context_cache,
            shared_rubric=shared_rubric,
            allow_distributed_256gb=allow_distributed_256gb,
        )

    @srv.tool
    async def best_of_n(
        prompt: str,
        n: int = 3,
        task_type: str = "code_generation",
        verify_cmd: str = "",
        json_schema: Optional[Dict[str, Any]] = None,
        data_classification: str = "corporate_proprietary",
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Run Lever D execution-verified Best-of-N cascade via CascadeRouter."""
        router = carbon_orchestrator.CascadeRouter()
        exec_fn = getattr(router, "execute_best_of_n", None)
        if not callable(exec_fn):
            dec = router.route_task(prompt, task_type=task_type, data_classification=data_classification)
            return {"ok": True, "dry_run": dry_run, "fallback": "route_task", "decision": asdict(dec)}
        return await asyncio.to_thread(
            exec_fn,
            prompt,
            n=max(1, int(n)),
            task_type=task_type,
            data_classification=data_classification,
            verify_cmd=verify_cmd,
            json_schema=json_schema,
            dry_run=dry_run,
        )

    @srv.tool
    async def gpu_burst_plan(
        tier: str = "distributed_256gb",
        nodes: int = 16,
        check_aws: bool = False,
    ) -> Dict[str, Any]:
        """Build a non-mutating ($0 idle STOPPED) AWS kaizen-u2 GPU Burst / distributed_256gb plan."""
        return await asyncio.to_thread(
            gpu_burst_orchestrator.plan_gpu_burst,
            tier=tier,
            nodes=max(1, int(nodes)),
            dry_run=True,
            execute_approved_u2=False,
            check_aws=bool(check_aws),
        )

    @srv.tool
    async def guardrail_stream(
        chunks: List[str],
        direction: str = "output",
    ) -> Dict[str, Any]:
        """Inspect streaming text chunks via StreamingGuardrailSession (Lever E, fail-closed)."""
        session_cls = getattr(guardrails, "StreamingGuardrailSession", None)
        if session_cls is not None:
            def _run_sess() -> Dict[str, Any]:
                sess = session_cls(direction=direction)
                raw_events = sess.inspect_stream(chunks)
                events = [asdict(ev) if hasattr(ev, "__dataclass_fields__") else dict(ev) for ev in raw_events]
                return {"ok": True, "halted": bool(sess.halted), "direction": direction, "events": events}

            return await asyncio.to_thread(_run_sess)
        combined = "".join(chunks)
        res = (
            guardrails.evaluate_input_rail(combined, bert_url="")
            if direction == "input"
            else guardrails.evaluate_output_rail(combined)
        )
        return {"ok": True, "halted": not res.allowed, "direction": direction, "events": [asdict(res)]}

    @srv.tool
    async def voice_to_action(
        transcript: str,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Convert voice transcript (Whisper/Kokoro spec) into structured action items."""
        adapter_cls = getattr(guardrails, "VoiceToActionAdapter", None)
        if adapter_cls is not None:
            res = await asyncio.to_thread(lambda: adapter_cls().extract_actions_from_transcript(transcript))
            return {"ok": res.get("status") in ("ok", "empty"), "dry_run": dry_run, **res}
        from kaizen_kit.hooks import prompt_decomposer
        cleaned = prompt_decomposer.strip_voice_tags(transcript)
        return {
            "ok": True,
            "dry_run": dry_run,
            "cleaned_transcript": cleaned,
            "decomposition": prompt_decomposer.decompose_prompt(cleaned),
        }

    @srv.tool
    async def mask_observation(
        command: str,
        output: str,
        exit_code: int = 0,
        line_threshold: int = 80,
        head_lines: int = 15,
        tail_lines: int = 15,
    ) -> Dict[str, Any]:
        """Mask oversized (>80L) command/diff/log outputs while preserving raw logs & alerts."""
        return await asyncio.to_thread(
            lossless_test_distiller.mask_large_observation,
            command,
            output,
            exit_code=exit_code,
            line_threshold=line_threshold,
            head_lines=head_lines,
            tail_lines=tail_lines,
        )

    @srv.tool
    async def status() -> Dict[str, Any]:
        """Probe Neon loopback daemons (0.25s timeout) and report boolean Gemini/Bedrock capability flags."""
        return await asyncio.to_thread(carbon_orchestrator.get_neon_live_status)

    return srv


def build_fastmcp_server(
    config: Optional[GatewayConfig] = None,
    include_serena: bool = True,
) -> FastMCP:
    """Build the unified FastMCP v6 gateway mounting Code-RAG, Zoekt, LiteLLM, Neon, MIRIX, and Serena."""
    cfg = config or GatewayConfig()
    root = FastMCP(
        name="kaizen-cloud-brain",
        version="6.0.0",
        instructions=(
            "Unified Kaizen Cloud-Brain MCP v6 gateway exposing MIRIX AutoDream, JEV/LiteLLM (:4000), "
            "Code-RAG (:18808), Zoekt (:6070), Neon Orchestrator & Night-Shift, and Serena LSP MCP (:24280)."
        ),
    )

    root.mount(_build_coderag_server(cfg), namespace="coderag")
    root.mount(_build_zoekt_server(cfg), namespace="zoekt")
    root.mount(_build_litellm_server(cfg), namespace="litellm")
    root.mount(_build_neon_server(cfg), namespace="neon")

    if include_serena:
        try:
            serena_proxy = create_proxy(cfg.serena_url, name="serena-mcp")
            root.mount(serena_proxy, namespace="serena")
        except Exception as exc:
            _LOG.warning("Skipping Serena proxy mount (%s): %s", cfg.serena_url, exc)

    def _get_mirix_store() -> mirix_memory.MirixStore:
        d = Path(cfg.mirix_dir) if cfg.mirix_dir else None
        return mirix_memory.get_store(data_dir=d, embed_url=cfg.embed_url, litellm_url=cfg.litellm_url)

    def _run_memory_ingest(items: List[Dict[str, Any]], scope: str = "", queue_jsonl_path: Optional[str] = None) -> Dict[str, Any]:
        st = _get_mirix_store()
        res = st.ingest(items, scope=scope) if items else {"ok": True, "ingested": 0, "rejected": 0, "ids": [], "rejected_reasons": []}
        if queue_jsonl_path:
            qres = st.ingest_jsonl(Path(queue_jsonl_path), scope=scope)
            res = {"ok": bool(res.get("ok", True) and qres.get("ok", True)), "ingested": int(res.get("ingested", 0)) + int(qres.get("ingested", 0)), "rejected": int(res.get("rejected", 0)) + int(qres.get("rejected", 0)), "ids": list(res.get("ids", [])) + list(qres.get("ids", [])), "rejected_reasons": list(res.get("rejected_reasons", [])) + list(qres.get("rejected_reasons", [])), "screen_capture": mirix_memory.SCREEN_CAPTURE_ENABLED}
        return res

    @root.tool
    async def memory_query(query: str, types: Optional[List[str]] = None, budget_tokens: int = 600) -> Dict[str, Any]:
        """Query MIRIX-2.0 Lite hybrid memory (LanceDB + SQLite FTS5) with strict token budget."""
        return await asyncio.to_thread(
            lambda: _get_mirix_store().query(query=query, types=types, budget_tokens=budget_tokens)
        )

    @root.tool
    async def memory_ingest(items: List[Dict[str, Any]], scope: str = "", queue_jsonl_path: Optional[str] = None) -> Dict[str, Any]:
        """Ingest memory records or drain an async JSONL queue with vault/secret filtering."""
        return await asyncio.to_thread(_run_memory_ingest, items, scope, queue_jsonl_path)

    @root.tool
    async def memory_autodream(max_records: int = 30) -> Dict[str, Any]:
        """Run asynchronous AutoDream consolidation via LiteLLM (:4000)."""
        return await asyncio.to_thread(
            mirix_memory.run_autodream,
            store=_get_mirix_store(),
            max_records=max_records,
            litellm_url=cfg.litellm_url,
        )

    @root.tool
    async def memory_stats() -> Dict[str, Any]:
        """Return MIRIX-2.0 Lite counts by 6 types, hypergraph edges, RSS MB, and screen_capture=False."""
        return await asyncio.to_thread(lambda: _get_mirix_store().stats())

    @root.tool
    async def gateway_health() -> Dict[str, Any]:
        """Check connectivity of all mounted EC2 Cloud-Brain backends and JEV worker pool."""
        backends, pool = await asyncio.gather(
            collect_backend_health(cfg),
            _http_json("GET", f"{cfg.jev_url}/v1/pool/status", timeout=cfg.probe_timeout_s + 1.0),
        )
        return {"status": "ok", "service": "kaizen-fastmcp", "backends": backends, "pool": pool}

    @root.custom_route("/health", methods=["GET"])
    async def http_health(_request: Request) -> JSONResponse:
        backends = await collect_backend_health(cfg)
        return JSONResponse({"status": "ok", "service": "kaizen-fastmcp", "backends": backends})

    @root.custom_route("/mirix/query", methods=["POST"])
    async def http_mirix_query(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("JSON object body required")
            raw_types = body.get("types")
            if raw_types is not None and not isinstance(raw_types, list):
                raise ValueError("types must be a list of strings")
            budget = 600 if body.get("budget_tokens") is None else int(body["budget_tokens"])
            limit = 12 if body.get("limit") is None else int(body["limit"])
            query_text = str(body.get("query") or "")
            res = await asyncio.to_thread(
                lambda: _get_mirix_store().query(query=query_text, types=raw_types, budget_tokens=budget, limit=limit)
            )
            return JSONResponse(res)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @root.custom_route("/mirix/ingest", methods=["POST"])
    async def http_mirix_ingest(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            if isinstance(body, list):
                items, scope, qpath = body, "", None
            elif isinstance(body, dict):
                raw_items = body.get("items")
                if raw_items is None:
                    items = []
                elif not isinstance(raw_items, list):
                    raise ValueError("items must be a list")
                else:
                    items = raw_items
                scope = str(body.get("scope") or "")
                qpath = body.get("queue_jsonl_path")
            else:
                raise ValueError("JSON object or list body required")
            if not all(isinstance(it, dict) for it in items):
                raise ValueError("each item must be a JSON object")
            res = await asyncio.to_thread(_run_memory_ingest, items, scope, qpath)
            return JSONResponse(res)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @root.custom_route("/mirix/autodream", methods=["POST"])
    async def http_mirix_autodream(request: Request) -> JSONResponse:
        try:
            raw = await request.body()
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            if not isinstance(body, dict):
                raise ValueError("JSON object body required")
            raw_max = body.get("max_records")
            max_records = 30 if raw_max is None else max(1, min(int(raw_max), 200))
            res = await asyncio.to_thread(
                mirix_memory.run_autodream,
                store=_get_mirix_store(),
                max_records=max_records,
                litellm_url=cfg.litellm_url,
            )
            return JSONResponse(res)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    return root


def _warmup_mirix(cfg: GatewayConfig) -> None:
    if os.environ.get("KAIZEN_MIRIX_SKIP_WARMUP", "") == "1":
        return
    try:
        d = Path(cfg.mirix_dir) if cfg.mirix_dir else None
        mirix_memory.get_store(data_dir=d, embed_url=cfg.embed_url, litellm_url=cfg.litellm_url).warmup()
    except Exception as exc:
        _LOG.warning("MIRIX warmup skipped: %s", exc)


def create_asgi_app(
    config: Optional[GatewayConfig] = None,
    path: str = "/mcp",
    stateless_http: bool = True,
    include_serena: bool = True,
) -> Any:
    """Export the FastMCP ASGI app mountable at `/mcp` or standalone on `:8770`."""
    cfg = config or GatewayConfig()
    _warmup_mirix(cfg)
    server = build_fastmcp_server(config=cfg, include_serena=include_serena)
    return server.http_app(path=path, stateless_http=stateless_http)


app = create_asgi_app()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Kaizen Cloud-Brain Unified FastMCP Gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--transport", choices=["http", "stdio"], default="http")
    parser.add_argument("--no-serena", action="store_true", help="Disable Serena MCP proxy mount")
    parser.add_argument("--list-tools", action="store_true", help="Print registered local tools and exit")
    args = parser.parse_args(argv)

    if args.list_tools:
        local_only = build_fastmcp_server(include_serena=False)
        tools = asyncio.run(local_only.list_tools())
        print(json.dumps([t.name for t in tools], indent=2))
        return 0

    server = build_fastmcp_server(include_serena=not args.no_serena)

    if args.transport == "stdio":
        server.run(transport="stdio")
        return 0

    import uvicorn

    _warmup_mirix(GatewayConfig())
    asgi = server.http_app(path="/mcp", stateless_http=True)
    uvicorn.run(asgi, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
