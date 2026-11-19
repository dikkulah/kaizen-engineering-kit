#!/usr/bin/env python3
"""Effort AutoSense Engine (`effort_autosense.py`).

Uses Kahneman System 1 (fast, intuitive heuristics & ultra-light sub-1B/4B models)
to automatically calibrate reasoning / thinking effort (`minimal`, `low`, `medium`,
`high`, `xhigh` / thinking budget 0-32k tokens) before invoking heavy System 2
frontier models (OpenAI o3/GPT-5, Anthropic Claude 5.5, Gemini 4, Bedrock).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

_LOG = logging.getLogger("kaizen_kit.effort_autosense")

# Effort to token budget mappings
EFFORT_BUDGET_MAP: Dict[str, int] = {
    "MINIMAL": 0,
    "LOW": 1024,
    "MEDIUM": 4096,
    "HIGH": 16384,
    "XHIGH": 32768,
}

EFFORT_NORMALIZATION: Dict[str, str] = {
    "minimal": "MINIMAL",
    "min": "MINIMAL",
    "none": "MINIMAL",
    "zero": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "med": "MEDIUM",
    "standard": "MEDIUM",
    "high": "HIGH",
    "xhigh": "XHIGH",
    "max": "XHIGH",
    "maximum": "XHIGH",
    "extreme": "XHIGH",
}

# Domain keyword weights for System 1 fast heuristics
_KEYWORD_WEIGHTS: Dict[str, int] = {
    # XHIGH indicators (+4)
    "race condition": 4,
    "deadlock": 4,
    "memory leak": 4,
    "data corruption": 4,
    "formal verification": 4,
    "distributed consensus": 4,
    "zero-day": 4,
    "byzantine": 4,
    "lock-free": 4,
    # HIGH indicators (+3)
    "concurrency": 3,
    "synchronization": 3,
    "clean architecture": 3,
    "reentrantlock": 3,
    "multithreading": 3,
    "asynchronous race": 3,
    "cross-repo": 3,
    "blast radius": 3,
    "parity gate": 3,
    "security vulnerability": 3,
    "authorization bypass": 3,
    "sql injection": 3,
    "crypto": 3,
    "jwt signature": 3,
    "performance degradation": 3,
    "p99 latency": 3,
    "refactor pyramid": 3,
    # MEDIUM indicators (+2)
    "refactor": 2,
    "optimize": 2,
    "implement feature": 2,
    "new endpoint": 2,
    "state machine": 2,
    "schema migration": 2,
    "integration test": 2,
    "unit test suite": 2,
    "error handling": 2,
    "cache invalidation": 2,
    "backoff": 2,
    # LOW / MINIMAL indicators (-2 to -4)
    "typo": -3,
    "spelling": -3,
    "format": -3,
    "lint": -3,
    "docstring": -3,
    "comment": -3,
    "rename variable": -2,
    "version bump": -3,
    "readme update": -3,
    "license": -2,
}


@dataclass(frozen=True)
class EffortSenseResult:
    """Calibrated thinking effort decided by System 1 model / heuristics."""

    level: str  # "MINIMAL" | "LOW" | "MEDIUM" | "HIGH" | "XHIGH"
    effort: str  # "minimal" | "low" | "medium" | "high" | "xhigh"
    thinking_budget: int  # 0 | 1024 | 4096 | 16384 | 32768
    rationale: str
    system_1_model: str  # e.g. "heuristic_fast" | "qwen3.5-0.8b" | "gemini-decision-1"
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        """Serialize result to JSON dictionary."""
        return asdict(self)


def _compute_structural_score(code_snippet: str) -> Tuple[int, List[str]]:
    """Compute structural score based on line count and nesting depth."""
    score = 0
    reasons = []
    lines = code_snippet.splitlines()
    total_lines = len(lines)

    if total_lines > 200:
        score += 3
        reasons.append(f"large context ({total_lines} lines)")
    elif total_lines > 60:
        score += 1
        reasons.append(f"moderate context ({total_lines} lines)")
    elif total_lines < 10 and total_lines > 0:
        score -= 1
        reasons.append(f"short snippet ({total_lines} lines)")

    # Indentation nesting depth check (indent > 12 spaces or 3 tabs)
    max_indent = 0
    for line in lines:
        stripped = line.lstrip()
        if stripped and not stripped.startswith(("#", "//", "/*", "*")):
            indent = len(line) - len(stripped)
            if indent > max_indent:
                max_indent = indent
    if max_indent >= 16:
        score += 3
        reasons.append(f"deep nesting depth ({max_indent} spaces)")
    elif max_indent >= 12:
        score += 2
        reasons.append(f"nested pyramid indentation ({max_indent} spaces)")

    return score, reasons


def autosense_effort(
    prompt_or_code: str,
    *,
    task_type: str = "auto",
    system_1_model: str = "heuristic_fast",
    decision_client: Optional[Any] = None,
) -> EffortSenseResult:
    """Calibrate reasoning effort using System 1 fast heuristics or ultra-light model."""
    text = (prompt_or_code or "").strip()
    if not text:
        return EffortSenseResult(
            level="MINIMAL",
            effort="minimal",
            thinking_budget=0,
            rationale="Empty input defaults to minimal effort.",
            system_1_model=system_1_model,
            confidence=1.0,
        )

    # 1. Keyword scoring
    text_lower = text.lower()
    keyword_score = 0
    matched_keywords = []

    for kw, weight in _KEYWORD_WEIGHTS.items():
        if kw in text_lower:
            keyword_score += weight
            matched_keywords.append(f"{kw} ({weight:+d})")

    # 2. Structural scoring
    struct_score, struct_reasons = _compute_structural_score(text)
    total_score = keyword_score + struct_score

    # 3. Optional System 1 Decision Gate (0 output tokens via JEV or Gemini Decision-1)
    if decision_client is not None and hasattr(decision_client, "decide_choice"):
        try:
            choices = {
                "MINIMAL": "Simple cosmetic, syntax, typo, docstring, or 1-line change",
                "LOW": "Straightforward localized method edit or simple bugfix",
                "MEDIUM": "Standard feature implementation or multi-function refactor",
                "HIGH": "Complex concurrency, distributed state, architecture violation, or security issue",
                "XHIGH": "Formal verification, distributed deadlock, or multi-repo blast radius",
            }
            res = decision_client.decide_choice(
                input_content=text[:4000],
                instructions="Determine the required reasoning/thinking effort level for an engineering assistant.",
                choices=choices,
                dry_run=False,
            )
            top_choice = str(res.get("top_choice") or res.get("choice") or "").upper()
            if top_choice in EFFORT_BUDGET_MAP:
                conf = float(res.get("confidence") or 0.95)
                return EffortSenseResult(
                    level=top_choice,
                    effort=top_choice.lower(),
                    thinking_budget=EFFORT_BUDGET_MAP[top_choice],
                    rationale=f"System 1 decision gate classified as {top_choice} (conf={conf:.2f}).",
                    system_1_model="gemini-decision-1",
                    confidence=conf,
                )
        except Exception as exc:
            _LOG.warning("System 1 model decision fallback to fast heuristics: %s", exc)

    # 4. Map total score to effort level
    all_reasons = matched_keywords + struct_reasons
    rationale_str = f"Score={total_score}: " + (", ".join(all_reasons[:4]) if all_reasons else "standard baseline")

    if total_score >= 6:
        level = "XHIGH"
        confidence = 0.92
    elif total_score >= 3:
        level = "HIGH"
        confidence = 0.88
    elif total_score >= 1:
        level = "MEDIUM"
        confidence = 0.85
    elif total_score <= -2:
        level = "MINIMAL"
        confidence = 0.90
    else:
        level = "LOW"
        confidence = 0.82

    return EffortSenseResult(
        level=level,
        effort=level.lower(),
        thinking_budget=EFFORT_BUDGET_MAP[level],
        rationale=rationale_str,
        system_1_model=system_1_model,
        confidence=confidence,
    )
