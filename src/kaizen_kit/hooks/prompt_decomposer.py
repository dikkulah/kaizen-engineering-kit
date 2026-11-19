#!/usr/bin/env python3
"""
Lightweight Natural-Language Workstream Decomposer (`prompt_decomposer.py`).

Decomposes compound stream-of-consciousness prompts into dependency-ordered atomic
workstreams (`WS-1..WS-N`) mapped to the 11 Canonical Specialist Agents.
Zero database or IDE dependencies — runs in <5ms across Claude Code and Antigravity.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

TRIVIAL_CONFIRM_RE = re.compile(
    r"^(?:yap|devam|devam\s+et|evet|tamam|ok|olur|basla|başla|uygula|onay|peki|yes|proceed|go)[\s.!]*$",
    re.IGNORECASE,
)

VOICE_TAG_TOKEN_RE = re.compile(
    r"</?(?:voice_instructions|talker_agent_context)\b[^>]*>",
    re.IGNORECASE,
)

MULTI_INTENT_SPLIT_RE = re.compile(
    r"(?:\n\s*\n|\n[-*•]\s+|\n\d+[.)]\s+|"
    r"\s{2,}(?=(?:birde|bir\s+de|ayrıca|ayrica|ondan\s+sonra|sonrasında|ikinci\s+olarak|ek\s+olarak|also|additionally|next|finally)\b))",
    re.IGNORECASE,
)

AGENT_ROUTING_RULES = [
    (re.compile(r"\b(?:official\s+doc|documentation|rfc|jep|quota|specification|refactoring\.guru)\b", re.I), "docs-spec-analyst", 1),
    (re.compile(r"\b(?:rust|lambda|graviton|arm64|tokio|clippy|vector_cube|avro|bson|oncelock)\b", re.I), "systems-architect", 2),
    (re.compile(r"\b(?:java|spring|loom|virtual\s+threads|maven|restclient|openapi)\b", re.I), "jvm-service-architect", 2),
    (re.compile(r"\b(?:sql|parity|populated_match|har|rollup|telemetry|bucket)\b", re.I), "data-sql-architect", 2),
    (re.compile(r"\b(?:uc-\d+|bug|triage|incident|rca|cloudwatch|falsif|blame)\b", re.I), "incident-rca-falsifier", 3),
    (re.compile(r"\b(?:mr|deploy|jenkins|ci|cd|gerrit|cloud_compose|pre-flight|compose)\b", re.I), "release-hygiene-guardian", 4),
    (re.compile(r"\b(?:refactor|clean\s+code|clean\s+architecture|guard\s+clause|complexity|smell|solid|demeter)\b", re.I), "refactoring-specialist", 3),
    (re.compile(r"\b(?:explore|skeleton|blast-radius|serena|zoekt|coderag|inventory|scan)\b", re.I), "code-explorer", 1),
]


def classify_segment(segment: str) -> tuple[str, int]:
    for rx, agent, order in AGENT_ROUTING_RULES:
        if rx.search(segment):
            return agent, order
    return "e2e-systems-visionary", 1


def _pop_matched_open(
    stack: List[Tuple[str, int]],
    open_counts: Dict[str, int],
    tag_name: str,
) -> Optional[int]:
    if open_counts.get(tag_name, 0) <= 0:
        return None
    while stack:
        popped_name, open_start = stack.pop()
        open_counts[popped_name] -= 1
        if popped_name == tag_name:
            return open_start
    return None


def _record_span(spans: List[Tuple[int, int]], start: int, end: int) -> None:
    while spans and spans[-1][0] >= start:
        spans.pop()
    spans.append((start, end))


def _parse_voice_token(tok: str) -> Tuple[bool, bool, str]:
    is_close = tok.startswith("</")
    is_self_closing = tok.endswith("/>") and not is_close
    tag_char = tok[2].lower() if is_close else tok[1].lower()
    return is_close, is_self_closing, tag_char


def _collect_voice_tag_spans(text: str) -> List[Tuple[int, int]]:
    stack: List[Tuple[str, int]] = []
    open_counts: Dict[str, int] = {}
    spans: List[Tuple[int, int]] = []
    for match in VOICE_TAG_TOKEN_RE.finditer(text):
        is_close, is_self_closing, tag_name = _parse_voice_token(match.group(0))
        if is_close:
            open_start = _pop_matched_open(stack, open_counts, tag_name)
            if open_start is not None:
                _record_span(spans, open_start, match.end())
            continue
        if is_self_closing:
            _record_span(spans, match.start(), match.end())
            continue
        stack.append((tag_name, match.start()))
        open_counts[tag_name] = open_counts.get(tag_name, 0) + 1
    return spans


def strip_voice_tags(text: str) -> str:
    raw = text or ""
    spans = _collect_voice_tag_spans(raw)
    if not spans:
        return raw.strip()
    parts: List[str] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            parts.append(raw[cursor:start])
        cursor = end
    if cursor < len(raw):
        parts.append(raw[cursor:])
    return "".join(parts).strip()


def extract_voice_metadata(text: str) -> Dict[str, Any]:
    """Strip voice wrapper tags and report whether Voice-to-Action metadata was present."""
    raw = text or ""
    spans = _collect_voice_tag_spans(raw)
    cleaned = strip_voice_tags(raw)
    return {
        "has_voice_tags": bool(spans),
        "span_count": len(spans),
        "stripped_chars": max(0, len(raw.strip()) - len(cleaned)),
        "clean_prompt": cleaned,
    }


def decompose_prompt(prompt: str) -> Dict:
    clean = strip_voice_tags(prompt or "")
    if not clean or len(clean) < 90 or TRIVIAL_CONFIRM_RE.match(clean):
        return {"is_complex": False, "workstreams": []}

    raw_parts = [p.strip() for p in MULTI_INTENT_SPLIT_RE.split(clean) if len(p.strip()) >= 20]
    if len(raw_parts) < 2:
        return {"is_complex": False, "workstreams": []}

    enriched = []
    for part in raw_parts[:6]:
        agent, order = classify_segment(part)
        enriched.append({"order": order, "agent": agent, "task": part[:180]})

    enriched.sort(key=lambda item: item["order"])
    workstreams: List[Dict] = []
    for idx, item in enumerate(enriched, start=1):
        workstreams.append({
            "id": f"WS-{idx}",
            "agent": item["agent"],
            "task": item["task"],
        })
    return {"is_complex": True, "workstreams": workstreams}


def format_workstream_directive(res: Dict) -> str:
    if not res.get("is_complex"):
        return ""
    lines = ["[Kaizen Heijunka Workstream Plan — Verify Every WS Before Completion]"]
    for ws in res.get("workstreams", []):
        lines.append(f"  - {ws['id']} (@{ws['agent']}): {ws['task']}")
    return "\n".join(lines)
