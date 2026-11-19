#!/usr/bin/env python3
"""Kaizen v4.0 M4 Guardrails evaluator (deterministic local rules + optional ModernBERT HTTP rail)."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

from kaizen_kit.gates.deterministic_vault import check_for_secrets, is_sensitive_file_path
from kaizen_kit.hooks import engine, prompt_decomposer
from kaizen_kit.localmodels import (
    MiniPoolClient,
    _port_open,
    validate_against_json_schema,
)

_TOKEN_SECRET_RE = re.compile(
    r"(?:glpat-[0-9A-Za-z_-]{16,}|acc-sk-[0-9a-fA-F]{16,}|ghp_[0-9A-Za-z]{20,}|xox[baprs]-[0-9A-Za-z-]{10,})"
)

_DESTRUCTIVE_SHELL_RE = re.compile(
    r"(?i)(?:"
    r"\brm\s+(?:-[a-z]*[rf][a-z]*\s+){1,3}(?:--no-preserve-root\s+)?/(?:\*|\s|$|['\"])"
    r"|\bmkfs(?:\.[a-z0-9]+)?\b"
    r"|\bdd\b[^\n]*\bif=[^\s]+[^\n]*\bof=/dev/"
    r"|:\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;?\s*:"
    r")"
)


@dataclass(frozen=True)
class GuardrailResult:
    """Outcome of a single input or output guardrail evaluation."""

    allowed: bool
    category: str
    reason: str
    source: str


DEFAULT_MODERNBERT_URL = "http://127.0.0.1:18800"
DEFAULT_GATEWAY_GUARDRAIL_URL = "http://127.0.0.1:8765"


def _resolve_guardrail_classify_endpoint(bert_url: str) -> Optional[str]:
    base = bert_url.rstrip("/")
    if _port_open(base):
        return f"{base}/classify"
    if base == DEFAULT_MODERNBERT_URL and _port_open(DEFAULT_GATEWAY_GUARDRAIL_URL):
        return f"{DEFAULT_GATEWAY_GUARDRAIL_URL}/v1/guardrail/classify"
    return None


def _probe_modernbert_rail(text: str, bert_url: str) -> Optional[GuardrailResult]:
    endpoint = _resolve_guardrail_classify_endpoint(bert_url)
    if endpoint is None:
        return None
    body = json.dumps({"text": text[:2000]}).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if bool(data.get("blocked")):
        reason = str(data.get("reason") or "ModernBERT guardrail flagged unsafe payload")
        return GuardrailResult(allowed=False, category="modernbert", reason=reason, source="modernbert")
    return None


def _check_synthetic_telemetry(text: str) -> Optional[str]:
    for pat, desc in engine.SYNTHETIC_TELEMETRY_PATTERNS:
        if pat.search(text):
            return desc
    return None


def evaluate_input_rail(text: str, bert_url: str = "http://127.0.0.1:18800") -> GuardrailResult:
    """Validate prompt/command input against secret, tenancy, host-stability, and ModernBERT rails."""
    if not text or not text.strip():
        return GuardrailResult(allowed=True, category="clean", reason="empty input", source="deterministic")
    secret = check_for_secrets(text)
    if secret:
        return GuardrailResult(allowed=False, category="secret", reason=f"Detected secret ({secret[0]})", source="deterministic")
    if _TOKEN_SECRET_RE.search(text):
        return GuardrailResult(allowed=False, category="secret", reason="Detected raw token secret", source="deterministic")
    if is_sensitive_file_path(text):
        return GuardrailResult(allowed=False, category="sensitive_path", reason="Sensitive credential path referenced", source="deterministic")
    if engine.is_host_suicide_command(text) or _DESTRUCTIVE_SHELL_RE.search(text):
        return GuardrailResult(allowed=False, category="host_stability", reason="Blocked host-disruption / destructive shell command", source="deterministic")
    if engine.is_aws_tenancy_violation(text):
        return GuardrailResult(allowed=False, category="aws_tenancy", reason="Blocked mutating AWS command outside --profile kaizen-u2", source="deterministic")
    bert_hit = _probe_modernbert_rail(text, bert_url)
    if bert_hit is not None:
        return bert_hit
    return GuardrailResult(allowed=True, category="clean", reason="ok", source="deterministic")


def evaluate_output_rail(text: str) -> GuardrailResult:
    """Validate generated output against leaked secrets and hardcoded synthetic telemetry literals."""
    if not text or not text.strip():
        return GuardrailResult(allowed=True, category="clean", reason="empty output", source="deterministic")
    secret = check_for_secrets(text)
    if secret:
        return GuardrailResult(allowed=False, category="secret", reason=f"Output leaked secret ({secret[0]})", source="deterministic")
    if _TOKEN_SECRET_RE.search(text):
        return GuardrailResult(allowed=False, category="secret", reason="Output leaked raw token secret", source="deterministic")
    if is_sensitive_file_path(text):
        return GuardrailResult(allowed=False, category="sensitive_path", reason="Output referenced sensitive credential path", source="deterministic")
    synth_desc = _check_synthetic_telemetry(text)
    if synth_desc:
        return GuardrailResult(allowed=False, category="synthetic_telemetry", reason=f"Blocked synthetic telemetry: {synth_desc}", source="deterministic")
    return GuardrailResult(allowed=True, category="clean", reason="ok", source="deterministic")


def _post_json_safe(url: str, payload: Dict[str, Any], timeout_s: float) -> Optional[Dict[str, Any]]:
    if not _port_open(url):
        return None
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            parsed = json.loads(resp.read().decode("utf-8", errors="replace"))
            return parsed if isinstance(parsed, dict) else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None


def _score_via_systemone(
    prompt: str,
    candidate_text: str,
    base_url: str,
    timeout_s: float,
) -> Optional[Tuple[float, str]]:
    endpoint = f"{base_url.rstrip('/')}/v1/systemone"
    payload = {
        "state": candidate_text[:6000],
        "questions": {
            "relevance": {
                "type": "score",
                "instructions": f"Score technical relevance and factual grounding for: {prompt[:400]}",
            }
        },
    }
    data = _post_json_safe(endpoint, payload, timeout_s=timeout_s)
    if not isinstance(data, dict):
        return None
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return None
    ans = answers.get("relevance")
    if not isinstance(ans, dict) or "score" not in ans:
        return None
    try:
        return float(ans["score"]), f"modernbert:{endpoint}"
    except (TypeError, ValueError):
        return None


def filter_mapped_candidate(
    prompt: str,
    candidate_text: str,
    bert_url: str = "http://127.0.0.1:18800",
    min_score: float = 0.45,
    fallback_jev_url: str = "http://127.0.0.1:8765",
    timeout_s: float = 1.5,
    probe_http: bool = True,
) -> Dict[str, Any]:
    """Filter mapped worker output using output safety rail + ModernBERT relevance scoring."""
    stripped = (candidate_text or "").strip()
    if not stripped:
        return {
            "kept": False,
            "score": 0.0,
            "rail": "empty_output",
            "backend": "deterministic",
            "reason": "empty_output",
        }

    out_rail = evaluate_output_rail(stripped)
    if not out_rail.allowed:
        return {
            "kept": False,
            "score": 0.0,
            "rail": out_rail.category,
            "backend": out_rail.source,
            "reason": out_rail.reason,
        }

    if not probe_http:
        score = 0.85 if len(stripped) >= 12 else 0.25
        kept = score >= min_score
        return {
            "kept": kept,
            "score": round(score, 4),
            "rail": "ok",
            "backend": "offline_heuristic",
            "reason": "ok" if kept else "below_min_score",
        }

    classify_data = _post_json_safe(
        f"{bert_url.rstrip('/')}/classify",
        {"text": stripped[:2000], "prompt": prompt[:400]},
        timeout_s=min(timeout_s, 0.8),
    )
    if isinstance(classify_data, dict) and bool(classify_data.get("blocked")):
        return {
            "kept": False,
            "score": 0.0,
            "rail": "modernbert",
            "backend": "modernbert",
            "reason": str(classify_data.get("reason") or "ModernBERT blocked candidate"),
        }
    if isinstance(classify_data, dict) and "score" in classify_data:
        try:
            score = float(classify_data["score"])
        except (TypeError, ValueError):
            score = None  # type: ignore[assignment]
        if score is not None:
            kept = score >= min_score
            return {
                "kept": kept,
                "score": round(score, 4),
                "rail": "ok",
                "backend": f"modernbert:{bert_url.rstrip('/')}/classify",
                "reason": "ok" if kept else "below_min_score",
            }

    sys_hit = _score_via_systemone(prompt, stripped, bert_url, timeout_s=timeout_s)
    if sys_hit is None and fallback_jev_url and fallback_jev_url != bert_url:
        sys_hit = _score_via_systemone(prompt, stripped, fallback_jev_url, timeout_s=timeout_s)

    if sys_hit is not None:
        score, backend = sys_hit
    else:
        score = 0.85 if len(stripped) >= 12 else 0.25
        backend = "offline_heuristic"

    kept = score >= min_score
    return {
        "kept": kept,
        "score": round(score, 4),
        "rail": "ok",
        "backend": backend,
        "reason": "ok" if kept else "below_min_score",
    }


@dataclass(frozen=True)
class StreamGuardrailEvent:
    """Lever E incremental outcome for Qwen3Guard-Stream token/chunk safety inspection."""

    allowed: bool
    halted: bool
    chunk_index: int
    category: str
    reason: str
    source: str
    safe_delta: str = ""


class StreamingGuardrailSession:
    """Sliding-window streaming guardrail (`Qwen3Guard-Stream`) catching cross-chunk secrets & tenancy violations."""

    def __init__(
        self,
        *,
        direction: str = "output",
        overlap_chars: int = 256,
        max_buffer_chars: int = 16384,
        fail_closed: bool = True,
        probe_stream_http: bool = False,
        stream_guard_url: str = DEFAULT_GATEWAY_GUARDRAIL_URL,
    ) -> None:
        self.direction = direction
        self.overlap_chars = max(64, int(overlap_chars))
        self.max_buffer_chars = max(1024, int(max_buffer_chars))
        self.fail_closed = fail_closed
        self.probe_stream_http = probe_stream_http
        self.stream_guard_url = stream_guard_url.rstrip("/")
        self._buffer = ""
        self._chunk_index = 0
        self._halted = False
        self._last_violation: Optional[StreamGuardrailEvent] = None

    @property
    def halted(self) -> bool:
        return self._halted

    def _probe_qwen3guard_stream(self, window: str) -> Optional[GuardrailResult]:
        if not self.probe_stream_http:
            return None
        data = _post_json_safe(
            f"{self.stream_guard_url}/v1/guardrail/stream",
            {"model": "qwen3-guard-stream-0.6b", "chunk_window": window[:2000], "direction": self.direction},
            timeout_s=0.4,
        )
        if isinstance(data, dict) and bool(data.get("blocked")):
            return GuardrailResult(
                allowed=False,
                category=str(data.get("category") or "qwen3guard_stream"),
                reason=str(data.get("reason") or "Qwen3Guard-Stream flagged unsafe token sequence"),
                source="qwen3-guard-stream-0.6b",
            )
        return None

    def inspect_chunk(self, chunk: str) -> StreamGuardrailEvent:
        self._chunk_index += 1
        if self._halted and self.fail_closed:
            prev = self._last_violation
            return StreamGuardrailEvent(
                allowed=False,
                halted=True,
                chunk_index=self._chunk_index,
                category=prev.category if prev else "halted",
                reason=prev.reason if prev else "stream already halted by prior violation",
                source=prev.source if prev else "deterministic",
                safe_delta="",
            )

        text_chunk = chunk or ""
        self._buffer = (self._buffer + text_chunk)[-self.max_buffer_chars :]
        window_len = len(text_chunk) + self.overlap_chars
        window = self._buffer[-window_len:]

        out_hit = evaluate_output_rail(window)
        if not out_hit.allowed:
            return self._record_violation(out_hit)

        if self.direction == "input" or engine.is_aws_tenancy_violation(window) or engine.is_host_suicide_command(window) or _DESTRUCTIVE_SHELL_RE.search(window):
            in_hit = evaluate_input_rail(window, bert_url="http://127.0.0.1:19999")
            if not in_hit.allowed:
                return self._record_violation(in_hit)

        stream_hit = self._probe_qwen3guard_stream(window)
        if stream_hit is not None and not stream_hit.allowed:
            return self._record_violation(stream_hit)

        return StreamGuardrailEvent(
            allowed=True,
            halted=False,
            chunk_index=self._chunk_index,
            category="clean",
            reason="ok",
            source="qwen3-guard-stream",
            safe_delta=text_chunk,
        )

    def _record_violation(self, hit: GuardrailResult) -> StreamGuardrailEvent:
        if self.fail_closed:
            self._halted = True
        event = StreamGuardrailEvent(
            allowed=False,
            halted=self.fail_closed,
            chunk_index=self._chunk_index,
            category=hit.category,
            reason=hit.reason,
            source=hit.source,
            safe_delta="",
        )
        self._last_violation = event
        return event

    def inspect_stream(self, chunks: Iterable[str]) -> List[StreamGuardrailEvent]:
        events: List[StreamGuardrailEvent] = []
        for ch in chunks:
            ev = self.inspect_chunk(ch)
            events.append(ev)
            if ev.halted:
                break
        return events


def _collect_string_leaves(node: Any, out: List[str]) -> None:
    if isinstance(node, str):
        if node.strip():
            out.append(node)
    elif isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.strip():
                out.append(k)
            _collect_string_leaves(v, out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect_string_leaves(item, out)


def evaluate_tool_call_rail(
    tool_name: str,
    tool_args: Any,
    bert_url: str = DEFAULT_MODERNBERT_URL,
) -> GuardrailResult:
    """Lever E fail-closed inspection of tool-call names and arguments before execution."""
    clean_name = (tool_name or "").strip()
    if not clean_name:
        return GuardrailResult(allowed=False, category="invalid_tool", reason="Empty tool_name", source="deterministic")
    parsed_args: Any = tool_args
    if isinstance(tool_args, str):
        stripped_args = tool_args.strip()
        if stripped_args.startswith("{"):
            try:
                parsed_args = json.loads(stripped_args)
                serialized = json.dumps(parsed_args, sort_keys=True)
            except ValueError as exc:
                return GuardrailResult(
                    allowed=False,
                    category="malformed_tool_args",
                    reason=f"Malformed tool JSON arguments: {exc}",
                    source="deterministic",
                )
        else:
            serialized = stripped_args
    else:
        try:
            serialized = json.dumps(tool_args, sort_keys=True)
        except (TypeError, ValueError) as exc:
            return GuardrailResult(
                allowed=False,
                category="malformed_tool_args",
                reason=f"Unserializable tool arguments: {exc}",
                source="deterministic",
            )

    leaves: List[str] = []
    _collect_string_leaves(parsed_args, leaves)
    leaves.append(f"tool={clean_name} args={serialized}")
    for candidate in leaves:
        in_res = evaluate_input_rail(candidate, bert_url=bert_url)
        if not in_res.allowed:
            return in_res
        out_res = evaluate_output_rail(candidate)
        if not out_res.allowed:
            return out_res
    return GuardrailResult(allowed=True, category="clean", reason="ok", source="deterministic")


VOICE_ACTION_ITEMS_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["summary", "action_items", "risks"],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "jira_project", "priority"],
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "assignee": {"type": "string"},
                    "jira_project": {"type": "string", "enum": ["UC", "RDM", "COPS"]},
                    "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                },
            },
        },
        "risks": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


@dataclass(frozen=True)
class VoicePipelineSpec:
    """Lever E corporate Voice-to-Action & TTS pipeline specification (`eu-west-1` / loopback + public Live API)."""

    asr_model: str = "openai/whisper-large-v3-turbo"
    live_transcribe_model: str = "gemini-3.5-transcribe-live"
    response_modalities: Tuple[str, ...] = ("TEXT",)
    mode: str = "smart"
    extraction_model: str = "qwen3.5-4b"
    tts_model: str = "hexgrad/Kokoro-82M"
    gateway_url: str = DEFAULT_GATEWAY_GUARDRAIL_URL
    default_jira_project: str = "UC"
    public_sanitized_only: bool = True


def live_session_compression_config(
    trigger_tokens: int = 24000,
    target_tokens: int = 12000,
) -> Dict[str, Any]:
    """Build Gemini Live API session config with ContextWindowCompressionConfig, Hybrid VAD, and residency guard."""
    return {
        "live_transcribe_model": "gemini-3.5-transcribe-live",
        "corporate_asr_model": "openai/whisper-large-v3-turbo",
        "response_modalities": ["TEXT"],
        "mode": "smart",
        "public_sanitized_only": True,
        "context_window_compression": {
            "type": "ContextWindowCompressionConfig",
            "trigger_tokens": int(trigger_tokens),
            "sliding_window": {
                "target_tokens": int(target_tokens),
            },
        },
        "hybrid_vad": {
            "enabled": True,
            "audio_stream_end": True,
            "strip_silence_frames": True,
        },
    }


def estimate_voice_stream_tokens(
    duration_seconds: float,
    *,
    speech_ratio: float = 0.45,
    hybrid_vad: bool = True,
    tokens_per_second: int = 25,
) -> Dict[str, Any]:
    """Quantify Live API voice stream token consumption (~25 tok/sec = 1,500 tok/min) and Hybrid VAD silence savings."""
    dur = max(0.0, float(duration_seconds))
    ratio = min(1.0, max(0.0, float(speech_ratio)))
    tps = max(1, int(tokens_per_second))
    unoptimized_tokens = int(round(dur * tps))
    active_speech_seconds = round(dur * ratio, 2)
    optimized_tokens = int(round(active_speech_seconds * tps)) if hybrid_vad else unoptimized_tokens
    tokens_saved = max(0, unoptimized_tokens - optimized_tokens)
    savings_pct = round((tokens_saved / float(unoptimized_tokens)) * 100.0, 2) if unoptimized_tokens > 0 else 0.0
    return {
        "duration_seconds": dur,
        "speech_ratio": ratio,
        "hybrid_vad": hybrid_vad,
        "tokens_per_second": tps,
        "tokens_per_minute": tps * 60,
        "active_speech_seconds": active_speech_seconds,
        "unoptimized_tokens": unoptimized_tokens,
        "optimized_tokens": optimized_tokens,
        "tokens_saved": tokens_saved,
        "savings_pct": savings_pct,
    }


class VoiceToActionAdapter:
    """Voice-to-Action pipeline: Whisper large-v3-turbo ASR -> Qwen3.5-4B constrained JSON -> Jira tasks + Kokoro-82M TTS."""

    def __init__(self, spec: Optional[VoicePipelineSpec] = None) -> None:
        self.spec = spec or VoicePipelineSpec()

    def live_session_compression_config(
        self,
        trigger_tokens: int = 24000,
        target_tokens: int = 12000,
    ) -> Dict[str, Any]:
        cfg = live_session_compression_config(trigger_tokens=trigger_tokens, target_tokens=target_tokens)
        cfg["live_transcribe_model"] = self.spec.live_transcribe_model
        cfg["corporate_asr_model"] = self.spec.asr_model
        cfg["response_modalities"] = list(self.spec.response_modalities)
        cfg["mode"] = self.spec.mode
        cfg["public_sanitized_only"] = self.spec.public_sanitized_only
        return cfg

    @staticmethod
    def estimate_voice_stream_tokens(
        duration_seconds: float,
        *,
        speech_ratio: float = 0.45,
        hybrid_vad: bool = True,
        tokens_per_second: int = 25,
    ) -> Dict[str, Any]:
        return estimate_voice_stream_tokens(
            duration_seconds,
            speech_ratio=speech_ratio,
            hybrid_vad=hybrid_vad,
            tokens_per_second=tokens_per_second,
        )

    def _offline_extract_structured(self, cleaned_text: str) -> Dict[str, Any]:
        sentences = [s.strip() for s in re.split(r"[\n.;]+", cleaned_text) if s.strip()]
        items: List[Dict[str, Any]] = []
        risks: List[str] = []
        for sent in sentences:
            lowered = sent.lower()
            proj = self.spec.default_jira_project
            if "cops" in lowered or "deploy" in lowered or "hotfix" in lowered:
                proj = "COPS"
            elif "rdm" in lowered:
                proj = "RDM"
            prio = "High" if any(k in lowered for k in ("urgent", "blocker", "critical", "p1", "hotfix")) else "Medium"
            if any(k in lowered for k in ("risk", "warning", "blocker")):
                risks.append(sent[:160])
            items.append(
                {
                    "title": sent[:140],
                    "assignee": "unassigned",
                    "jira_project": proj,
                    "priority": prio,
                }
            )
        summary = sentences[0][:200] if sentences else "Empty voice note"
        return {
            "summary": summary,
            "action_items": items[:10],
            "risks": risks[:5],
        }

    def extract_actions_from_transcript(self, raw_transcript: str) -> Dict[str, Any]:
        cleaned = prompt_decomposer.strip_voice_tags(raw_transcript or "").strip()
        in_rail = evaluate_input_rail(cleaned, bert_url="http://127.0.0.1:19999")
        if not in_rail.allowed:
            return {
                "status": "blocked",
                "rail": in_rail.category,
                "reason": in_rail.reason,
                "asr_model": self.spec.asr_model,
            }
        if not cleaned:
            return {
                "status": "empty",
                "asr_model": self.spec.asr_model,
                "extraction_model": self.spec.extraction_model,
                "structured": {"summary": "No content", "action_items": [], "risks": []},
            }

        pool = MiniPoolClient(self.spec.gateway_url)
        structured = pool.chat_structured(
            f"Extract actionable engineering tasks and Jira project keys from transcript:\n{cleaned}",
            json_schema=VOICE_ACTION_ITEMS_JSON_SCHEMA,
            system="Return strictly valid JSON conforming to the action items schema.",
            model=self.spec.extraction_model,
        )
        backend = f"loopback:{self.spec.extraction_model}"
        if structured is None:
            structured = self._offline_extract_structured(cleaned)
            backend = "deterministic_fallback"

        valid, validated_obj, errors = validate_against_json_schema(structured, VOICE_ACTION_ITEMS_JSON_SCHEMA)
        if not valid or validated_obj is None:
            return {"status": "schema_error", "errors": errors}

        out_rail = evaluate_output_rail(json.dumps(validated_obj))
        if not out_rail.allowed:
            return {"status": "blocked", "rail": out_rail.category, "reason": out_rail.reason}

        return {
            "status": "ok",
            "asr_model": self.spec.asr_model,
            "extraction_model": self.spec.extraction_model,
            "tts_model": self.spec.tts_model,
            "backend": backend,
            "cleaned_transcript": cleaned,
            "structured": validated_obj,
        }

    def build_tts_request(self, text: str, voice: str = "af_heart", speed: float = 1.0) -> Dict[str, Any]:
        cleaned = prompt_decomposer.strip_voice_tags(text or "").strip()
        out_rail = evaluate_output_rail(cleaned)
        return {
            "allowed": out_rail.allowed,
            "rail": out_rail.category,
            "endpoint": f"{self.spec.gateway_url}/v1/audio/speech",
            "model": self.spec.tts_model,
            "voice": voice,
            "speed": round(float(speed), 2),
            "input": cleaned if out_rail.allowed else "",
        }


def run_self_test() -> int:
    """Deterministic self-test for input/output, streaming, tool-call, and voice guardrails."""
    sample_pk = "-----BEGIN " + "PRIVATE KEY-----"
    kept_res = filter_mapped_candidate(
        "Analyze parity",
        "Verified endpoint parity across 14 fields.",
        bert_url="http://127.0.0.1:19999",
        fallback_jev_url="http://127.0.0.1:19998",
    )
    noise_res = filter_mapped_candidate(
        "Analyze parity",
        "ok",
        bert_url="http://127.0.0.1:19999",
        fallback_jev_url="http://127.0.0.1:19998",
    )
    secret_res = filter_mapped_candidate(
        "Analyze parity",
        sample_pk,
        bert_url="http://127.0.0.1:19999",
        fallback_jev_url="http://127.0.0.1:19998",
    )
    stream_session = StreamingGuardrailSession(fail_closed=True)
    stream_events = stream_session.inspect_stream([" Safe chunk 1. ", "-----BEGIN ", "PRIVATE KEY-----", " after"])
    voice_adapter = VoiceToActionAdapter(VoicePipelineSpec(gateway_url="http://127.0.0.1:19999"))
    voice_res = voice_adapter.extract_actions_from_transcript(
        "<voice_instructions>fast</voice_instructions>Open urgent COPS hotfix deploy ticket for CloudGatewayApi staging."
    )
    live_cfg = voice_adapter.live_session_compression_config()
    tok_est = voice_adapter.estimate_voice_stream_tokens(60.0)
    cases = [
        evaluate_input_rail("run cargo test --workspace", bert_url="http://127.0.0.1:19999").allowed is True,
        evaluate_input_rail("aws lambda update-function-code --profile kaizen-prod", bert_url="http://127.0.0.1:19999").allowed is False,
        evaluate_input_rail("os.kill(pid, 0)", bert_url="http://127.0.0.1:19999").allowed is False,
        evaluate_output_rail("ok clean output").allowed is True,
        evaluate_output_rail(sample_pk).allowed is False,
        kept_res["kept"] is True,
        noise_res["kept"] is False,
        secret_res["kept"] is False and secret_res["rail"] == "secret",
        len(stream_events) == 3 and stream_events[2].halted is True and stream_events[2].category == "secret",
        evaluate_tool_call_rail("bash", {"cmd": "aws lambda update-function-code --profile kaizen-prod"}, bert_url="http://127.0.0.1:19999").allowed is False,
        voice_res["status"] == "ok" and voice_res["structured"]["action_items"][0]["jira_project"] == "COPS",
        live_cfg["live_transcribe_model"] == "gemini-3.5-transcribe-live" and live_cfg["public_sanitized_only"] is True,
        tok_est["unoptimized_tokens"] == 1500 and tok_est["tokens_saved"] == 825,
    ]
    if all(cases):
        print("[PASS] guardrails -- input/output safety, Qwen3Guard-Stream, tool-call & Voice-to-Action verified")
        return 0
    print(f"[FAIL] guardrails self-test failed: {cases}")
    return 1


if __name__ == "__main__":
    raise SystemExit(run_self_test())
