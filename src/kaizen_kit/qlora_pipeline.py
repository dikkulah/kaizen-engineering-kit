#!/usr/bin/env python3
"""
Universal Execution-Oracle QLoRA & Schema Prompt-Cache Pipeline (`qlora_pipeline.py`).

Provides open-source Lever D fine-tuning & prompt-caching primitives:
  1. `SchemaPromptCacheManager`: Deterministic JSON / SQL / Rust / Java schema compressor
     producing byte-stable static prefixes for KV / implicit / explicit prompt caching.
  2. `ExecutionOracleGate` & `synthesize_verified_sft_dpo_records()`: Best-of-N candidate
     verification emitting paired SFT + hard-negative DPO JSONL with `DeterministicVault`
     PII pseudonymization and fail-closed secret quarantine.
  3. `split_train_eval_by_entity()`: Entity-grouped leak-free train/eval split preventing
     template or entity leakage across splits.
  4. `emit_training_scripts()`: Dynamic QLoRA (`r=16`, `SFTTrainer` + `DPOTrainer`), vLLM
     multi-LoRA, and `llama.cpp` GGUF-LoRA script generator driven by
     `gpu_burst_orchestrator.LORA_PORTFOLIO` with SM75 (`fp16`) / SM89 (`bf16`) auto-detection.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from kaizen_kit import gpu_burst_orchestrator
from kaizen_kit.gates.deterministic_vault import DeterministicVault, check_for_secrets

EMAIL_PII_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_SLASH_COMMENT_RE = re.compile(r"(?m)(?<!:)//[^\n]*")
SQL_LINE_COMMENT_RE = re.compile(r"(?m)--[^\n]*")
MULTI_SPACE_RE = re.compile(r"[ \t]+")
BLANK_LINES_RE = re.compile(r"\n{2,}")
DESTRUCTIVE_SQL_RE = re.compile(r"\b(?:DROP\s+DATABASE|TRUNCATE\s+TABLE)\b", re.IGNORECASE)

DEFAULT_TARGET_MODULES: Tuple[str, ...] = (
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
)


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scrub_extended_pii(text: str, vault: DeterministicVault) -> str:
    if not text:
        return ""
    scrubbed = vault.anonymize_text(text)
    return EMAIL_PII_REGEX.sub("redacted@example.invalid", scrubbed)


@dataclass(frozen=True)
class CachedSchemaEntry:
    """Immutable content-addressed compressed schema entry for prompt caching."""

    name: str
    kind: str
    compressed_text: str
    sha256: str
    raw_tokens_est: int
    compressed_tokens_est: int
    savings_ratio: float


class SchemaPromptCacheManager:
    """
    Deterministic schema compressor for JSON, SQL DDL, Rust types, and Java records/classes.

    Produces byte-exact canonical static prefixes suitable for CAPC, vLLM `--enable-prefix-caching`,
    `llama.cpp` `--cache-reuse`, and cloud implicit/explicit KV caches.
    """

    SUPPORTED_KINDS = ("auto", "json", "sql", "rust", "java")

    def __init__(self) -> None:
        self._registry: Dict[str, CachedSchemaEntry] = {}

    @staticmethod
    def detect_schema_kind(raw_schema: str) -> str:
        stripped = (raw_schema or "").strip()
        if stripped.startswith(("{", "[")):
            return "json"
        upper = stripped.upper()
        if any(tok in upper for tok in ("CREATE TABLE", "CREATE VIEW", "ALTER TABLE", "PRIMARY KEY")):
            return "sql"
        if re.search(r"\b(?:pub\s+)?(?:struct|enum|trait)\s+\w+", stripped):
            return "rust"
        if re.search(r"\b(?:public\s+|private\s+|protected\s+)?(?:record|class|interface)\s+\w+", stripped):
            return "java"
        return "text"

    @classmethod
    def _compress_json_schema(cls, raw_schema: str) -> str:
        parsed = json.loads(raw_schema)
        pruned = cls._prune_json_descriptions(parsed)
        return json.dumps(pruned, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @classmethod
    def _prune_json_descriptions(cls, node: Any) -> Any:
        if isinstance(node, dict):
            cleaned: Dict[str, Any] = {}
            for key in sorted(node.keys()):
                if key in ("$comment", "examples") and len(node) > 1:
                    continue
                if key == "description" and isinstance(node[key], str) and len(node[key]) > 96:
                    cleaned[key] = node[key][:93].rstrip() + "..."
                    continue
                cleaned[key] = cls._prune_json_descriptions(node[key])
            return cleaned
        if isinstance(node, list):
            return [cls._prune_json_descriptions(item) for item in node]
        return node

    @staticmethod
    def _compress_code_or_sql(raw_schema: str, kind: str) -> str:
        no_block = BLOCK_COMMENT_RE.sub("", raw_schema)
        if kind == "sql":
            no_comments = SQL_LINE_COMMENT_RE.sub("", no_block)
        else:
            no_comments = LINE_SLASH_COMMENT_RE.sub("", no_block)
        lines: List[str] = []
        for raw_line in no_comments.splitlines():
            compact = MULTI_SPACE_RE.sub(" ", raw_line).strip()
            if compact:
                lines.append(compact)
        joined = "\n".join(lines)
        joined = re.sub(r"\s*([,;{}()])\s*", r"\1", joined)
        return BLANK_LINES_RE.sub("\n", joined).strip()

    def compress_schema(self, raw_schema: str, kind: str = "auto") -> Tuple[str, str]:
        if kind not in self.SUPPORTED_KINDS and kind != "text":
            raise ValueError(f"Unsupported schema kind '{kind}'. Expected one of {self.SUPPORTED_KINDS}.")
        resolved_kind = self.detect_schema_kind(raw_schema) if kind == "auto" else kind
        if resolved_kind == "json":
            try:
                return self._compress_json_schema(raw_schema), "json"
            except json.JSONDecodeError:
                resolved_kind = "text"
        return self._compress_code_or_sql(raw_schema, resolved_kind), resolved_kind

    def register_schema(self, name: str, raw_schema: str, kind: str = "auto") -> CachedSchemaEntry:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Schema name must be non-empty.")
        compressed, resolved_kind = self.compress_schema(raw_schema, kind=kind)
        raw_tokens = _estimate_tokens(raw_schema)
        comp_tokens = _estimate_tokens(compressed)
        savings = max(0.0, round(1.0 - (comp_tokens / max(1, raw_tokens)), 4))
        entry = CachedSchemaEntry(
            name=clean_name,
            kind=resolved_kind,
            compressed_text=compressed,
            sha256=_sha256_hex(compressed),
            raw_tokens_est=raw_tokens,
            compressed_tokens_est=comp_tokens,
            savings_ratio=savings,
        )
        self._registry[clean_name] = entry
        return entry

    def get_entry(self, name: str) -> Optional[CachedSchemaEntry]:
        return self._registry.get(name)

    def build_static_cache_prefix(
        self,
        schemas: Optional[Sequence[Tuple[str, str, str]]] = None,
        system_preamble: str = "You are a deterministic systems engineering assistant. Follow the canonical schemas strictly.",
    ) -> Dict[str, Any]:
        if schemas:
            entries = [self.register_schema(name, raw, kind) for name, raw, kind in schemas]
        else:
            entries = [self._registry[k] for k in sorted(self._registry.keys())]
        ordered = sorted(entries, key=lambda e: e.name)
        blocks = [system_preamble.strip(), "=== CANONICAL_SCHEMA_CACHE_BLOCK ==="]
        for item in ordered:
            blocks.append(f"[SCHEMA:{item.name}|KIND:{item.kind}|SHA256:{item.sha256[:12]}]\n{item.compressed_text}")
        blocks.append("=== END_CANONICAL_SCHEMA_CACHE_BLOCK ===")
        prefix_text = "\n".join(blocks)
        return {
            "static_prefix": prefix_text,
            "cache_key": _sha256_hex(prefix_text),
            "schema_count": len(ordered),
            "prefix_tokens_est": _estimate_tokens(prefix_text),
            "entries": ordered,
        }

    def build_cached_messages(
        self,
        user_query: str,
        schemas: Optional[Sequence[Tuple[str, str, str]]] = None,
        system_preamble: str = "You are a deterministic systems engineering assistant. Follow the canonical schemas strictly.",
    ) -> Dict[str, Any]:
        bundle = self.build_static_cache_prefix(schemas=schemas, system_preamble=system_preamble)
        messages = [
            {"role": "system", "content": bundle["static_prefix"]},
            {"role": "user", "content": user_query.strip()},
        ]
        return {
            "cache_key": bundle["cache_key"],
            "static_prefix": bundle["static_prefix"],
            "prefix_tokens_est": bundle["prefix_tokens_est"],
            "messages": messages,
        }


@dataclass(frozen=True)
class CandidateEvaluation:
    """Deterministic Execution-Oracle verdict for a single model completion candidate."""

    candidate_id: str
    response: str
    passed: bool
    score: float
    quarantined_secret: bool
    oracle_checks: Dict[str, bool] = field(default_factory=dict)
    failure_reason: str = ""


VerifierCallable = Callable[[str], Union[bool, Tuple[bool, float, str]]]


class ExecutionOracleGate:
    """
    Deterministic Best-of-N candidate verifier for synthetic SFT & hard-negative DPO data.

    Checks secret leakage (fail-closed), language syntax/structure, required/forbidden patterns,
    and an optional deterministic verifier callback (unit test / schema / AST oracle).
    """

    def __init__(
        self,
        language: str = "text",
        required_patterns: Sequence[str] = (),
        forbidden_patterns: Sequence[str] = (),
        verifier_fn: Optional[VerifierCallable] = None,
    ) -> None:
        self.language = (language or "text").strip().lower()
        self.required_patterns = tuple(required_patterns)
        self.forbidden_patterns = tuple(forbidden_patterns)
        self.verifier_fn = verifier_fn

    @staticmethod
    def _balanced_delimiters(text: str) -> bool:
        pairs = {")": "(", "]": "[", "}": "{"}
        stack: List[str] = []
        in_str: Optional[str] = None
        escaped = False
        for ch in text:
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
            if ch in "([{":
                stack.append(ch)
            elif ch in pairs:
                if not stack or stack[-1] != pairs[ch]:
                    return False
                stack.pop()
        return not stack and in_str is None

    def _check_syntax(self, candidate: str) -> Tuple[bool, str]:
        stripped = (candidate or "").strip()
        if not stripped:
            return False, "empty_candidate"
        if self.language == "python":
            try:
                ast.parse(stripped)
                return True, ""
            except SyntaxError as exc:
                return False, f"python_syntax_error:{exc.msg}"
        if self.language == "json":
            try:
                json.loads(stripped)
                return True, ""
            except json.JSONDecodeError as exc:
                return False, f"json_decode_error:{exc.msg}"
        if self.language == "sql":
            if DESTRUCTIVE_SQL_RE.search(stripped):
                return False, "destructive_sql_blocked"
            if not self._balanced_delimiters(stripped):
                return False, "unbalanced_sql_delimiters"
            return True, ""
        if self.language in ("rust", "java"):
            if not self._balanced_delimiters(stripped):
                return False, f"unbalanced_{self.language}_delimiters"
            if "{" not in stripped or "}" not in stripped:
                return False, f"missing_{self.language}_body_braces"
            return True, ""
        return True, ""

    def _check_contracts(self, candidate: str) -> Tuple[bool, float, str]:
        matched = 0
        for pat in self.required_patterns:
            if not re.search(pat, candidate, re.MULTILINE):
                return False, 0.0, f"missing_required_pattern:{pat}"
            matched += 1
        for pat in self.forbidden_patterns:
            if re.search(pat, candidate, re.MULTILINE):
                return False, 0.0, f"matched_forbidden_pattern:{pat}"
        bonus = min(0.25, 0.05 * matched)
        return True, bonus, ""

    def evaluate_candidate(self, candidate: str, candidate_id: str = "c0") -> CandidateEvaluation:
        raw = candidate or ""
        secret_hit = check_for_secrets(raw)
        if secret_hit is not None:
            kind, _ = secret_hit
            return CandidateEvaluation(
                candidate_id=candidate_id,
                response=raw,
                passed=False,
                score=0.0,
                quarantined_secret=True,
                oracle_checks={
                    "secret_free": False,
                    "syntax_valid": False,
                    "contract_valid": False,
                    "verifier_passed": False,
                },
                failure_reason=f"secret_detected:{kind}",
            )

        syntax_ok, syntax_reason = self._check_syntax(raw)
        contract_ok, contract_bonus, contract_reason = self._check_contracts(raw)

        verifier_ok = True
        verifier_score = 0.75 if (syntax_ok and contract_ok) else 0.25
        verifier_reason = ""
        if self.verifier_fn is not None and syntax_ok:
            verdict = self.verifier_fn(raw)
            if isinstance(verdict, tuple):
                verifier_ok, verifier_score, verifier_reason = bool(verdict[0]), float(verdict[1]), str(verdict[2])
            else:
                verifier_ok = bool(verdict)
                verifier_score = 0.85 if verifier_ok else 0.15
                verifier_reason = "" if verifier_ok else "custom_verifier_rejected"

        passed = syntax_ok and contract_ok and verifier_ok
        base_score = verifier_score + (contract_bonus if contract_ok else 0.0)
        if not syntax_ok:
            base_score = min(base_score, 0.1)
        elif not contract_ok:
            base_score = min(base_score, 0.35)
        elif not verifier_ok:
            base_score = min(base_score, 0.45)

        reason = syntax_reason or contract_reason or verifier_reason
        return CandidateEvaluation(
            candidate_id=candidate_id,
            response=raw,
            passed=passed,
            score=round(max(0.0, min(1.0, base_score)), 4),
            quarantined_secret=False,
            oracle_checks={
                "secret_free": True,
                "syntax_valid": syntax_ok,
                "contract_valid": contract_ok,
                "verifier_passed": verifier_ok,
            },
            failure_reason="" if passed else (reason or "oracle_rejected"),
        )

    def evaluate_best_of_n(
        self,
        candidates: Sequence[str],
    ) -> Tuple[Optional[CandidateEvaluation], List[CandidateEvaluation]]:
        evaluations = [self.evaluate_candidate(text, candidate_id=f"c{idx}") for idx, text in enumerate(candidates)]
        passing = [ev for ev in evaluations if ev.passed and not ev.quarantined_secret]
        if not passing:
            return None, evaluations
        best = max(passing, key=lambda ev: (ev.score, -int(ev.candidate_id[1:])))
        return best, evaluations


@dataclass(frozen=True)
class SynthesisReport:
    """Result bundle produced by `synthesize_verified_sft_dpo_records()`."""

    sft_records: List[Dict[str, Any]]
    dpo_records: List[Dict[str, Any]]
    quarantined_count: int
    skipped_unverified_count: int
    sft_path: Optional[str] = None
    dpo_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sft_records": self.sft_records,
            "dpo_records": self.dpo_records,
            "quarantined_count": self.quarantined_count,
            "skipped_unverified_count": self.skipped_unverified_count,
            "sft_path": self.sft_path,
            "dpo_path": self.dpo_path,
        }


def _select_hard_negative(
    evaluations: Sequence[CandidateEvaluation],
    winner: CandidateEvaluation,
) -> Optional[CandidateEvaluation]:
    safe_pool = [ev for ev in evaluations if not ev.quarantined_secret and ev.candidate_id != winner.candidate_id]
    failed = [ev for ev in safe_pool if not ev.passed and ev.response.strip()]
    if failed:
        return max(failed, key=lambda ev: ev.score)
    lower_passing = [ev for ev in safe_pool if ev.passed and ev.score + 0.05 < winner.score]
    if lower_passing:
        return min(lower_passing, key=lambda ev: ev.score)
    return None


def synthesize_verified_sft_dpo_records(
    tasks: Sequence[Mapping[str, Any]],
    oracle_gate: Optional[ExecutionOracleGate] = None,
    vault: Optional[DeterministicVault] = None,
    system_prompt: str = "You are a senior systems engineer. Produce minimal, verified, clean code.",
    default_adapter: Optional[str] = None,
    output_dir: Optional[Union[str, Path]] = None,
) -> SynthesisReport:
    """
    Verify Best-of-N candidate completions per task and emit scrubbed SFT + hard-negative DPO JSONL.

    Each item in `tasks` supports:
      - `entity_id` (str): Grouping identifier for leak-free train/eval splitting.
      - `prompt` (str): Task instruction / context.
      - `candidates` (Sequence[str]): N candidate model completions.
      - `adapter` (optional str): Target LoRA adapter ID (defaults to first entry in `LORA_PORTFOLIO`).
      - `language`, `required_patterns`, `forbidden_patterns`, `verifier_fn`: Optional per-task gate overrides.
    """
    active_vault: Optional[DeterministicVault] = vault
    temp_vault_dir: Optional[tempfile.TemporaryDirectory[str]] = None
    if active_vault is None:
        temp_vault_dir = tempfile.TemporaryDirectory()
        active_vault = DeterministicVault(vault_path=Path(temp_vault_dir.name) / "vault.json", auto_save=False)

    fallback_adapter = default_adapter or (
        gpu_burst_orchestrator.LORA_PORTFOLIO[0]["adapter"]
        if gpu_burst_orchestrator.LORA_PORTFOLIO
        else "lora-default"
    )

    sft_records: List[Dict[str, Any]] = []
    dpo_records: List[Dict[str, Any]] = []
    quarantined_count = 0
    skipped_unverified_count = 0

    try:
        for idx, item in enumerate(tasks):
            raw_prompt = str(item.get("prompt", ""))
            if check_for_secrets(raw_prompt) is not None:
                quarantined_count += 1
                continue

            gate = oracle_gate or ExecutionOracleGate(
                language=str(item.get("language", "text")),
                required_patterns=tuple(item.get("required_patterns", ())),
                forbidden_patterns=tuple(item.get("forbidden_patterns", ())),
                verifier_fn=item.get("verifier_fn"),
            )
            candidates = list(item.get("candidates", ()))
            winner, evaluations = gate.evaluate_best_of_n(candidates)
            quarantined_count += sum(1 for ev in evaluations if ev.quarantined_secret)

            if winner is None:
                skipped_unverified_count += 1
                continue

            entity_id = str(item.get("entity_id") or f"entity_{idx:04d}")
            adapter_name = gpu_burst_orchestrator.resolve_lora_adapter(str(item.get("adapter") or fallback_adapter))
            clean_system = _scrub_extended_pii(system_prompt, active_vault)
            clean_prompt = _scrub_extended_pii(raw_prompt, active_vault)
            clean_chosen = _scrub_extended_pii(winner.response, active_vault)

            sft_records.append(
                {
                    "entity_id": entity_id,
                    "adapter": adapter_name,
                    "oracle_score": winner.score,
                    "messages": [
                        {"role": "system", "content": clean_system},
                        {"role": "user", "content": clean_prompt},
                        {"role": "assistant", "content": clean_chosen},
                    ],
                }
            )

            hard_neg = _select_hard_negative(evaluations, winner)
            if hard_neg is not None:
                clean_rejected = _scrub_extended_pii(hard_neg.response, active_vault)
                if clean_rejected != clean_chosen:
                    dpo_records.append(
                        {
                            "entity_id": entity_id,
                            "adapter": adapter_name,
                            "prompt": f"{clean_system}\n\n{clean_prompt}".strip(),
                            "chosen": clean_chosen,
                            "rejected": clean_rejected,
                            "chosen_score": winner.score,
                            "rejected_score": hard_neg.score,
                            "rejected_reason": hard_neg.failure_reason or "suboptimal_candidate",
                        }
                    )

        sft_file_str: Optional[str] = None
        dpo_file_str: Optional[str] = None
        if output_dir is not None:
            out_path = Path(output_dir)
            out_path.mkdir(parents=True, exist_ok=True)
            sft_file = out_path / "sft_verified.jsonl"
            dpo_file = out_path / "dpo_hard_negatives.jsonl"
            with open(sft_file, "w", encoding="utf-8", newline="\n") as fsft:
                for row in sft_records:
                    fsft.write(json.dumps(row, ensure_ascii=True) + "\n")
            with open(dpo_file, "w", encoding="utf-8", newline="\n") as fdpo:
                for row in dpo_records:
                    fdpo.write(json.dumps(row, ensure_ascii=True) + "\n")
            sft_file_str = str(sft_file)
            dpo_file_str = str(dpo_file)

        return SynthesisReport(
            sft_records=sft_records,
            dpo_records=dpo_records,
            quarantined_count=quarantined_count,
            skipped_unverified_count=skipped_unverified_count,
            sft_path=sft_file_str,
            dpo_path=dpo_file_str,
        )
    finally:
        if temp_vault_dir is not None:
            temp_vault_dir.cleanup()


def split_train_eval_by_entity(
    records: Sequence[Mapping[str, Any]],
    entity_key: str = "entity_id",
    eval_ratio: float = 0.2,
    seed: int = 42,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Deterministically partition dataset records into leak-free `(train, eval)` splits by `entity_key`.

    Guarantees zero entity overlap (`set(train_entities).isdisjoint(set(eval_entities))`) across OS/Python runs.
    """
    if not (0.0 < eval_ratio < 1.0):
        raise ValueError(f"eval_ratio must be strictly between 0.0 and 1.0, got {eval_ratio}.")
    if not records:
        return [], []

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for idx, rec in enumerate(records):
        copied = dict(rec)
        raw_entity = str(copied.get(entity_key) or "").strip()
        if not raw_entity:
            raw_entity = f"anon_entity_{idx:06d}"
            copied[entity_key] = raw_entity
        grouped.setdefault(raw_entity, []).append(copied)

    ordered_entities = sorted(
        grouped.keys(),
        key=lambda eid: _sha256_hex(f"{seed}:{eid}"),
    )
    if len(ordered_entities) == 1:
        return list(grouped[ordered_entities[0]]), []

    total_rows = len(records)
    target_eval_rows = max(1, round(total_rows * eval_ratio))

    eval_records: List[Dict[str, Any]] = []
    train_records: List[Dict[str, Any]] = []
    for pos, eid in enumerate(ordered_entities):
        remaining_entities = len(ordered_entities) - pos - 1
        bucket = grouped[eid]
        # Always reserve at least one entity group for train.
        if len(eval_records) < target_eval_rows and (len(train_records) > 0 or remaining_entities >= 1):
            eval_records.extend(bucket)
        else:
            train_records.extend(bucket)

    if not train_records and eval_records:
        last_eid = ordered_entities[-1]
        moved = grouped[last_eid]
        eval_records = [r for r in eval_records if r[entity_key] != last_eid]
        train_records.extend(moved)

    return train_records, eval_records


def detect_gpu_training_profile(sm_capability: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
    """
    Resolve precision & attention kernel config for Turing SM75 (`fp16`) vs Ada/Ampere SM80+/SM89 (`bf16`).
    """
    major, minor = sm_capability if sm_capability is not None else (7, 5)
    is_sm80_plus = major >= 8
    if is_sm80_plus:
        return {
            "sm_arch": f"sm_{major}{minor}",
            "fp16": False,
            "bf16": True,
            "bnb_4bit_compute_dtype": "bfloat16",
            "torch_dtype": "bfloat16",
            "attn_implementation": "flash_attention_2",
        }
    return {
        "sm_arch": f"sm_{major}{minor}",
        "fp16": True,
        "bf16": False,
        "bnb_4bit_compute_dtype": "float16",
        "torch_dtype": "float16",
        "attn_implementation": "sdpa",
    }


def _render_python_qlora_trainer_script(lora_rank: int) -> str:
    target_mods = json.dumps(list(DEFAULT_TARGET_MODULES))
    return f'''#!/usr/bin/env python3
"""Auto-generated Two-Stage QLoRA Trainer (`SFTTrainer` + `DPOTrainer`, r={lora_rank})."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import torch
from datasets import load_dataset
from peft import LoraConfig, PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from trl import DPOConfig, DPOTrainer, SFTConfig, SFTTrainer


def resolve_hardware_precision() -> dict:
    if torch.cuda.is_available():
        major, minor = torch.cuda.get_device_capability(0)
        if major >= 8:
            # SM80 / SM89 (A10G / L4 / L40S) -> native bf16
            return {{"sm": f"sm_{{major}}{{minor}}", "fp16": False, "bf16": True, "dtype": torch.bfloat16, "attn": "sdpa"}}
        # SM75 (T4 / g4dn.*) -> strict fp16 (bf16 unsupported natively on Turing)
        return {{"sm": f"sm_{{major}}{{minor}}", "fp16": True, "bf16": False, "dtype": torch.float16, "attn": "sdpa"}}
    return {{"sm": "cpu", "fp16": False, "bf16": False, "dtype": torch.float32, "attn": "eager"}}


def main() -> None:
    ap = argparse.ArgumentParser(description="Two-stage SFT + DPO QLoRA fine-tuning runner")
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--base-model", required=True)
    ap.add_argument("--sft-train", required=True)
    ap.add_argument("--sft-eval", default="")
    ap.add_argument("--dpo-train", default="")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--lora-rank", type=int, default={lora_rank})
    args = ap.parse_args()

    hw = resolve_hardware_precision()
    out_root = Path(args.output_dir) / args.adapter
    sft_out = out_root / "sft_checkpoint"
    final_out = out_root / "final_adapter"
    out_root.mkdir(parents=True, exist_ok=True)

    bnb_cfg = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=hw["dtype"],
    )
    lora_cfg = LoraConfig(
        r=args.lora_rank,
        lora_alpha=args.lora_rank * 2,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules={target_mods},
    )

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        quantization_config=bnb_cfg,
        torch_dtype=hw["dtype"],
        attn_implementation=hw["attn"],
        device_map="auto",
    )

    sft_ds = load_dataset("json", data_files=args.sft_train, split="train")
    eval_ds = load_dataset("json", data_files=args.sft_eval, split="train") if args.sft_eval else None
    sft_args = SFTConfig(
        output_dir=str(sft_out),
        per_device_train_batch_size=2,
        gradient_accumulation_steps=8,
        learning_rate=2e-4,
        num_train_epochs=2,
        fp16=hw["fp16"],
        bf16=hw["bf16"],
        gradient_checkpointing=True,
        max_seq_length=4096,
        logging_steps=10,
        save_strategy="epoch",
    )
    sft_trainer = SFTTrainer(
        model=model,
        args=sft_args,
        train_dataset=sft_ds,
        eval_dataset=eval_ds,
        peft_config=lora_cfg,
        processing_class=tokenizer,
    )
    sft_trainer.train()
    sft_trainer.save_model(str(sft_out))

    dpo_path = Path(args.dpo_train) if args.dpo_train else None
    if dpo_path is not None and dpo_path.is_file() and dpo_path.stat().st_size > 0:
        dpo_ds = load_dataset("json", data_files=str(dpo_path), split="train")
        peft_model = PeftModel.from_pretrained(model, str(sft_out), is_trainable=True)
        dpo_args = DPOConfig(
            output_dir=str(final_out),
            per_device_train_batch_size=1,
            gradient_accumulation_steps=8,
            learning_rate=5e-6,
            num_train_epochs=1,
            beta=0.1,
            fp16=hw["fp16"],
            bf16=hw["bf16"],
            gradient_checkpointing=True,
            logging_steps=10,
        )
        dpo_trainer = DPOTrainer(
            model=peft_model,
            ref_model=None,
            args=dpo_args,
            train_dataset=dpo_ds,
            processing_class=tokenizer,
        )
        dpo_trainer.train()
        dpo_trainer.save_model(str(final_out))
    else:
        sft_trainer.save_model(str(final_out))

    (out_root / "training_manifest.json").write_text(
        json.dumps({{"adapter": args.adapter, "base_model": args.base_model, "rank": args.lora_rank, "hw": str(hw["sm"])}}, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
'''


def emit_training_scripts(
    output_dir: Optional[Union[str, Path]] = None,
    portfolio: Optional[Sequence[Mapping[str, str]]] = None,
    lora_rank: int = 16,
    dataset_dir: str = "./artifacts/qlora_data",
    adapters_dir: str = "./artifacts/lora_adapters",
) -> Dict[str, Any]:
    """
    Generate QLoRA `SFTTrainer` + `DPOTrainer`, vLLM multi-LoRA serving, and GGUF-LoRA export scripts
    dynamically from `gpu_burst_orchestrator.LORA_PORTFOLIO`.
    """
    active_portfolio = [dict(entry) for entry in (portfolio or gpu_burst_orchestrator.LORA_PORTFOLIO)]
    if not active_portfolio:
        raise ValueError("LoRA portfolio must contain at least one adapter specification.")

    adapters_manifest: List[Dict[str, Any]] = []
    lora_module_args: List[str] = []
    gguf_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'LLAMA_CPP_DIR="${LLAMA_CPP_DIR:-/opt/llama.cpp}"',
        f'ADAPTERS_DIR="${{ADAPTERS_DIR:-{adapters_dir}}}"',
        "",
    ]

    grouped_by_base: Dict[str, List[Dict[str, Any]]] = {}
    for entry in active_portfolio:
        adapter_id = entry["adapter"]
        rank = int(entry.get("max_lora_rank", lora_rank))
        base_model = entry["base_model"]
        spec = {
            "adapter": adapter_id,
            "base_model": base_model,
            "fallback_base_model": entry.get("fallback_base_model", base_model),
            "quantization": entry.get("quantization", "awq"),
            "lora_rank": rank,
            "tp": int(entry.get("tp", "1")),
            "gpus": entry.get("gpus", "0"),
            "adapter_path": f"{adapters_dir}/{adapter_id}/final_adapter",
            "gguf_lora_path": f"{adapters_dir}/{adapter_id}/{adapter_id}-f16.gguf",
        }
        adapters_manifest.append(spec)
        grouped_by_base.setdefault(base_model, []).append(spec)
        lora_module_args.append(f"{adapter_id}={spec['adapter_path']}")
        gguf_lines.append(
            f'python3 "${{LLAMA_CPP_DIR}}/convert_lora_to_gguf.py" '
            f'--base "{base_model}" '
            f'--outfile "${{ADAPTERS_DIR}}/{adapter_id}/{adapter_id}-f16.gguf" '
            f'--outtype f16 "${{ADAPTERS_DIR}}/{adapter_id}/final_adapter"'
        )

    vllm_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "# Auto-generated vLLM Multi-LoRA Serving Launcher (SM75 float16 / SM89 bfloat16 auto-detect)",
        'GPU_SM=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 | tr -d "." || echo "75")',
        'if [ "${GPU_SM:-75}" -ge 80 ]; then DTYPE="bfloat16"; else DTYPE="float16"; fi',
        "",
    ]
    base_port = 8000
    for idx, (base_model, specs) in enumerate(grouped_by_base.items()):
        port = base_port + idx
        tp = max(s["tp"] for s in specs)
        gpus = specs[0]["gpus"]
        max_rank = max(s["lora_rank"] for s in specs)
        mod_flags = " ".join(f'"{s["adapter"]}={s["adapter_path"]}"' for s in specs)
        vllm_lines.append(
            f'CUDA_VISIBLE_DEVICES="{gpus}" vllm serve "{base_model}" '
            f"--port {port} --tensor-parallel-size {tp} "
            f'--dtype "${{DTYPE}}" --enable-prefix-caching '
            f"--enable-lora --max-lora-rank {max_rank} "
            f"--lora-modules {mod_flags} &"
        )
    vllm_lines.append("wait")

    trainer_py = _render_python_qlora_trainer_script(lora_rank=lora_rank)
    vllm_sh = "\n".join(vllm_lines) + "\n"
    gguf_sh = "\n".join(gguf_lines) + "\n"

    sm75_profile = detect_gpu_training_profile((7, 5))
    sm89_profile = detect_gpu_training_profile((8, 9))

    written_files: List[str] = []
    if output_dir is not None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        files_to_write = {
            "train_qlora_sft_dpo.py": trainer_py,
            "serve_vllm_multilora.sh": vllm_sh,
            "export_gguf_lora.sh": gguf_sh,
            "lora_portfolio_manifest.json": json.dumps(
                {
                    "dataset_dir": dataset_dir,
                    "lora_rank": lora_rank,
                    "compute_profiles": {"sm75": sm75_profile, "sm89": sm89_profile},
                    "adapters": adapters_manifest,
                },
                indent=2,
            )
            + "\n",
        }
        for fname, content in files_to_write.items():
            fpath = out / fname
            with open(fpath, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            written_files.append(str(fpath))

    return {
        "lora_rank": lora_rank,
        "adapters": adapters_manifest,
        "compute_profiles": {"sm75": sm75_profile, "sm89": sm89_profile},
        "train_script": trainer_py,
        "vllm_multilora_script": vllm_sh,
        "gguf_export_script": gguf_sh,
        "lora_module_args": lora_module_args,
        "written_files": written_files,
    }


def plan_or_launch_cluster(
    profile: Optional[str] = None,
    region: Optional[str] = None,
    bucket: Optional[str] = None,
    s3_prefix: str = "telemetry-lora",
    cpu_hours: float = 40.0,
    gpu_hours: float = 12.0,
    idle_minutes: Optional[int] = None,
    execute_approved_u2: bool = False,
) -> Dict[str, Any]:
    """Build a dry-run or STS-verified Spot QLoRA training cluster plan via `gpu_burst_orchestrator`."""
    resolved_profile = profile or gpu_burst_orchestrator.DEFAULT_PROFILE
    resolved_region = region or gpu_burst_orchestrator.DEFAULT_REGION
    resolved_bucket = bucket or gpu_burst_orchestrator.DEFAULT_BUCKET
    resolved_idle = int(idle_minutes if idle_minutes is not None else gpu_burst_orchestrator.DEFAULT_IDLE_STOP_MINUTES)

    gpu_burst_orchestrator.validate_u2_profile(resolved_profile)
    unified = gpu_burst_orchestrator.plan_gpu_burst(
        tier="large",
        region=resolved_region,
        profile=resolved_profile,
        nodes=1,
        bucket=resolved_bucket,
        checkpoint_prefix=s3_prefix,
        idle_stop_minutes=resolved_idle,
        dry_run=not execute_approved_u2,
        execute_approved_u2=execute_approved_u2,
        check_aws=False,
        gpu_hours=gpu_hours,
        cpu_hours=cpu_hours,
    )
    clean_prefix = s3_prefix.strip("/")
    nvme_workspace = "/opt/dlami/nvme/telemetry-lora"
    userdata = gpu_burst_orchestrator.build_spot_userdata(
        run_id="telemetry-lora",
        bucket=resolved_bucket,
        checkpoint_prefix=clean_prefix,
        region=resolved_region,
        idle_stop_minutes=resolved_idle,
        nvme_dir=nvme_workspace,
        encode_base64=False,
        checkpoint_subpath="checkpoints",
    ) + f'\naws s3 sync "{nvme_workspace}/datasets" "s3://{resolved_bucket}/{clean_prefix}/datasets/" --region {resolved_region} || true\n'

    unified_finops = unified["finops_estimate"]
    selected_types = unified["region_mapping"].get("selected_instance_types") or ["g4dn.12xlarge"]
    finops_types = (
        [unified_finops["primary_instance_type"]]
        if unified_finops.get("primary_instance_type")
        else selected_types
    )
    burst_est = gpu_burst_orchestrator.estimate_burst_finops(
        instance_types=tuple(finops_types),
        nodes=1,
        gpu_hours=gpu_hours,
        cpu_hours=0.0,
        s3_gb=25.0,
        ebs_gb=100.0,
        region=resolved_region,
        spot_price_override=unified_finops.get("hourly_spot_usd_per_node"),
    )
    cpu_cost = round(max(0.0, cpu_hours) * 0.045, 2)
    finops = {
        "note": burst_est["note"],
        "profile": resolved_profile,
        "region": resolved_region,
        "primary_instance_type": burst_est.get("primary_instance_type", "g4dn.12xlarge"),
        "hourly_spot_usd_per_node": burst_est.get("hourly_spot_usd_per_node", 1.9612),
        "tier_a_cpu_spot_usd": cpu_cost,
        "tier_b_gpu_spot_usd": burst_est["ec2_spot_usd"],
        "s3_storage_usd": burst_est["s3_storage_usd"],
        "ebs_snapshot_usd": burst_est["ebs_usd"],
        "estimated_total_usd": round(
            cpu_cost + burst_est["ec2_spot_usd"] + burst_est["s3_storage_usd"] + burst_est["ebs_usd"],
            2,
        ),
        "assumptions": {"cpu_hours": cpu_hours, "gpu_hours": gpu_hours, "s3_gb": 25.0, "ebs_gb": 100.0},
    }

    plan: Dict[str, Any] = {
        "mode": "APPROVED_LIVE_LAUNCH" if execute_approved_u2 else "DRY_RUN_PLAN_ONLY",
        "aws_mutated": False,
        "default_state": unified["default_state"],
        "idle_stop_minutes": resolved_idle,
        "profile": resolved_profile,
        "region": resolved_region,
        "security_group": gpu_burst_orchestrator.DEFAULT_SG,
        "iam_instance_profile": gpu_burst_orchestrator.DEFAULT_IAM_PROFILE,
        "private_subnets": list(gpu_burst_orchestrator.DEFAULT_PRIVATE_SUBNETS),
        "s3_sync_target": f"s3://{resolved_bucket}/{clean_prefix}/checkpoints/",
        "region_mapping": unified["region_mapping"],
        "sparsity_config": unified["sparsity_config"],
        "quota_check": unified["quota_check"],
        "lora_portfolio": list(gpu_burst_orchestrator.LORA_PORTFOLIO),
        "finops_estimate": finops,
        "gpu_userdata_preview": userdata,
    }
    if not execute_approved_u2:
        return plan

    import boto3

    session = boto3.Session(profile_name=resolved_profile, region_name=resolved_region)
    caller = session.client("sts").get_caller_identity()
    plan["caller_account"] = caller.get("Account")
    plan["launch_ready_verified"] = True
    return plan


def run_self_test() -> int:
    """Deterministic offline self-test exercising all 4 universal QLoRA & schema-cache primitives."""
    mgr = SchemaPromptCacheManager()
    json_entry = mgr.register_schema(
        "order_event",
        '{\n  "$comment": "strip me",\n  "type": "object",\n  "properties": {\n    "order_id": {"type": "string"}\n  }\n}',
    )
    sql_entry = mgr.register_schema(
        "orders_ddl",
        "-- DDL comment\nCREATE TABLE orders (\n    order_id TEXT PRIMARY KEY,\n    total_cents BIGINT NOT NULL\n);\n",
    )
    rust_entry = mgr.register_schema(
        "order_struct",
        "// Rust domain struct\npub struct OrderRecord {\n    pub order_id: String,\n    pub total_cents: i64,\n}\n",
    )
    java_entry = mgr.register_schema(
        "order_record",
        "/* Java DTO */\npublic record OrderDto(\n    String orderId,\n    long totalCents\n) {}\n",
    )
    assert json_entry.kind == "json" and "$comment" not in json_entry.compressed_text
    assert sql_entry.kind == "sql" and "DDL comment" not in sql_entry.compressed_text
    assert rust_entry.kind == "rust" and java_entry.kind == "java"

    prefix_a = mgr.build_static_cache_prefix()
    prefix_b = mgr.build_static_cache_prefix()
    assert prefix_a["cache_key"] == prefix_b["cache_key"]
    assert prefix_a["schema_count"] == 4

    with tempfile.TemporaryDirectory() as td:
        vault = DeterministicVault(vault_path=Path(td) / "vault.json", auto_save=False)
        fake_aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
        tasks = [
            {
                "entity_id": "svc_checkout",
                "prompt": "Parse node 10.24.8.19 MAC AA:BB:CC:11:22:33 safely in Python.",
                "language": "python",
                "required_patterns": [r"def parse_node\("],
                "candidates": [
                    "def parse_node(x: str) -> dict:\n    return {'host': '10.24.8.19', 'mac': 'AA:BB:CC:11:22:33'}\n",
                    "def broken_syntax(:\n    pass",
                    f"API_KEY = '{fake_aws_key}'\ndef parse_node(x): return x\n",
                ],
            },
            {
                "entity_id": "svc_inventory",
                "prompt": "Return inventory health JSON.",
                "language": "json",
                "candidates": ['{"status": "ok", "healthy": true}', "{malformed_json"],
            },
            {
                "entity_id": "svc_checkout",
                "prompt": "Validate port range in Python.",
                "language": "python",
                "required_patterns": [r"def valid_port\("],
                "candidates": ["def valid_port(p: int) -> bool:\n    return 1 <= p <= 65535\n"],
            },
        ]
        report = synthesize_verified_sft_dpo_records(tasks, vault=vault, output_dir=Path(td) / "data")
        assert len(report.sft_records) == 3
        assert len(report.dpo_records) == 2
        assert report.quarantined_count == 1
        sft_blob = json.dumps(report.sft_records)
        assert "10.24.8.19" not in sft_blob and "AA:BB:CC:11:22:33" not in sft_blob
        assert fake_aws_key not in sft_blob and fake_aws_key not in json.dumps(report.dpo_records)

        train_rows, eval_rows = split_train_eval_by_entity(report.sft_records, eval_ratio=0.34, seed=7)
        train_entities = {r["entity_id"] for r in train_rows}
        eval_entities = {r["entity_id"] for r in eval_rows}
        assert train_entities and eval_entities and train_entities.isdisjoint(eval_entities)

        bundle = emit_training_scripts(output_dir=Path(td) / "scripts")
        assert len(bundle["adapters"]) == len(gpu_burst_orchestrator.LORA_PORTFOLIO)
        assert bundle["compute_profiles"]["sm75"]["fp16"] is True
        assert bundle["compute_profiles"]["sm75"]["bf16"] is False
        assert bundle["compute_profiles"]["sm89"]["bf16"] is True
        assert "SFTTrainer" in bundle["train_script"] and "DPOTrainer" in bundle["train_script"]
        assert "--enable-lora" in bundle["vllm_multilora_script"]
        assert "convert_lora_to_gguf.py" in bundle["gguf_export_script"]

    print("[PASS] qlora_pipeline -- schema cache, execution oracle SFT/DPO, entity split & SM75/SM89 QLoRA scripts verified")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Universal QLoRA & Schema Prompt-Cache Pipeline")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("self-test", help="Run deterministic offline QLoRA & schema-cache self-test")
    emit_p = sub.add_parser("emit-scripts", help="Emit SFT+DPO trainer, vLLM multi-LoRA & GGUF export scripts")
    emit_p.add_argument("--output-dir", required=True, help="Target directory for generated scripts")
    emit_p.add_argument("--rank", type=int, default=16, help="LoRA rank r (default: 16)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.cmd in (None, "self-test"):
        return run_self_test()
    if args.cmd == "emit-scripts":
        bundle = emit_training_scripts(output_dir=args.output_dir, lora_rank=args.rank)
        print(json.dumps({"adapters": len(bundle["adapters"]), "written_files": bundle["written_files"]}, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
