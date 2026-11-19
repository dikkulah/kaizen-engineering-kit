"""Shared loopback model clients for Carbon, ADK workflows, and Antigravity.

Services run on the EC2 Cloud-Brain (`m7g.8xlarge`) via SSM loopback tunnels:
  - jev-style decision model  POST {JEV_STYLE_URL}/v1/systemone      (default :8765)
  - bitnet/TEI embeddings     POST {KAIZEN_BITNET_EMBED_URL}         (default :8766/embedding)
"""

import json
import logging
import os
import re
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

_LOG = logging.getLogger(__name__)

JEV_DEFAULT_URL = "http://127.0.0.1:8765"
BITNET_DEFAULT_URL = "http://127.0.0.1:8766/embedding"
RERANK_DEFAULT_URL = "http://127.0.0.1:8766/rerank"
MINI_POOL_DEFAULT_URL = "http://127.0.0.1:8765"
AVAILABLE_POOL_MODELS = (
    "mini-pool",
    "bitnet-2b",
    "bitnet",
    "qwen3.5-4b",
    "granite-4-h-tiny",
    "gemma-4-26b-a4b",
    "gemma-4-e2b",
    "gemma-4-12b-unified",
    "qwen3-30b-a3b",
    "qwen3-coder-30b-a3b",
    "qwen3.5-35b-a3b",
    "glm-4.7-flash",
    "ling-mini-2.0",
    "qwen3-guard-0.6b",
    "qwen3-guard-stream-0.6b",
)
DEPRECATED_POOL_MODEL_ALIASES: Dict[str, str] = {
    "qwen3b": "qwen3.5-4b",
    "qwen2.5-coder-3b": "qwen3.5-4b",
    "qwen-coder": "qwen3-coder-30b-a3b",
    "phi4": "qwen3.5-4b",
    "phi-4-mini": "qwen3.5-4b",
    "phi-4-reasoning-plus": "qwen3.5-35b-a3b",
    "gemma3": "gemma-4-26b-a4b",
    "gemma-3": "gemma-4-26b-a4b",
    "nemotron": "granite-4-h-tiny",
    "nemotron-mini": "granite-4-h-tiny",
    "deepseek-r1-14b": "qwen3.5-35b-a3b",
    "ring-mini-2.0": "ling-mini-2.0",
    "granite-4.0-h-tiny": "granite-4-h-tiny",
}
HYBRID_EMBED_MODELS = ("bge-m3", "qwen3-embed-0.6b")
HYBRID_RERANK_MODELS = ("qwen3-reranker-0.6b", "bge-reranker-v2-m3", "colbert-v2")
DOC_OCR_MODELS = ("docling-v2", "olmocr-2-7b", "gemma-4-12b-unified")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
PROBE_TIMEOUT_S = 0.3
DECIDE_TIMEOUT_S = 20.0
EMBED_TIMEOUT_S = 5.0
CHAT_TIMEOUT_S = 60.0
MAX_STATE_CHARS = 6000


def normalize_pool_model(model: str) -> str:
    """Resolve retired CPU dense aliases to active EC2 Cloud-Brain MoE/small model names."""
    cleaned = (model or "mini-pool").strip()
    if not cleaned:
        return "mini-pool"
    return DEPRECATED_POOL_MODEL_ALIASES.get(cleaned, cleaned)


@dataclass(frozen=True)
class ConstrainedDecodingSpec:
    """Lever A specification for XGrammar / llguidance / GBNF zero-syntax-retry structured decoding."""

    mode: str = "json_schema"
    schema_name: str = "structured_output"
    json_schema: Optional[Dict[str, Any]] = None
    gbnf_grammar: Optional[str] = None
    strict: bool = True
    backend: str = "auto"


GBNF_PRESETS: Dict[str, str] = {
    "json_object": (
        'root ::= object\n'
        'value ::= object | array | string | number | ("true" | "false" | "null") ws\n'
        'object ::= "{" ws ( string ":" ws value ("," ws string ":" ws value)* )? "}" ws\n'
        'array ::= "[" ws ( value ("," ws value)* )? "]" ws\n'
        'string ::= "\\"" ([^"\\\\\\x7F\\x00-\\x1F] | "\\\\" (["\\\\/bfnrt] | "u" [0-9a-fA-F]{4}))* "\\"" ws\n'
        'number ::= ("-"? ([0-9] | [1-9] [0-9]{0,15})) ("." [0-9]+)? ([eE] [-+]? [0-9] [1-9]{0,15})? ws\n'
        'ws ::= | " " | "\\n" [ \\t]{0,20}'
    ),
    "unified_diff": (
        'root ::= ("--- " [a-zA-Z0-9_./-]+ "\\n+++ " [a-zA-Z0-9_./-]+ "\\n" hunk+)+\n'
        'hunk ::= "@@ -" [0-9,]+ " +" [0-9,]+ " @@" [^\\n]* "\\n" line+\n'
        'line ::= (" " | "+" | "-") [^\\n]* "\\n"'
    ),
    "duckdb_sql_select": (
        'root ::= ("SELECT " | "WITH " [^;]+ " SELECT ") [^;\\n]+ ";"? "\\n"?'
    ),
}


def build_constrained_payload(
    spec: Optional[ConstrainedDecodingSpec] = None,
    *,
    response_format: Optional[Dict[str, Any]] = None,
    json_schema: Optional[Dict[str, Any]] = None,
    gbnf_grammar: Optional[str] = None,
    guided_json: Optional[Dict[str, Any]] = None,
    target_engine: str = "auto",
) -> Dict[str, Any]:
    """Build OpenAI / LiteLLM / ik_llama.cpp / vLLM constrained decoding payload fields."""
    payload: Dict[str, Any] = {}
    eff_schema = json_schema or guided_json or (spec.json_schema if spec else None)
    eff_grammar = gbnf_grammar or (spec.gbnf_grammar if spec else None)
    if eff_grammar and eff_grammar in GBNF_PRESETS:
        eff_grammar = GBNF_PRESETS[eff_grammar]

    if response_format is not None:
        payload["response_format"] = dict(response_format)
    elif eff_schema is not None:
        schema_name = spec.schema_name if spec else "structured_output"
        strict = spec.strict if spec else True
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": strict,
                "schema": eff_schema,
            },
        }
        if target_engine in ("vllm", "xgrammar", "llguidance") or guided_json is not None:
            payload["guided_json"] = eff_schema
            backend = (spec.backend if spec and spec.backend != "auto" else "xgrammar")
            payload["guided_decoding_backend"] = backend
    elif spec and spec.mode == "json_object":
        payload["response_format"] = {"type": "json_object"}

    if eff_grammar:
        if target_engine in ("vllm", "xgrammar", "llguidance"):
            payload["guided_grammar"] = eff_grammar
        else:
            payload["grammar"] = eff_grammar
    return payload


def _strip_json_code_fence(raw_text: str) -> str:
    stripped = (raw_text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return stripped


def _check_json_type(val: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(val, dict)
    if expected_type == "array":
        return isinstance(val, list)
    if expected_type == "string":
        return isinstance(val, str)
    if expected_type == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if expected_type == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if expected_type == "boolean":
        return isinstance(val, bool)
    if expected_type == "null":
        return val is None
    return True


def _validate_node_against_schema(node: Any, schema: Dict[str, Any], path: str, errors: List[str]) -> None:
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _check_json_type(node, expected_type):
        errors.append(f"{path}: expected type '{expected_type}', got {type(node).__name__}")
        return
    if "enum" in schema and node not in schema["enum"]:
        errors.append(f"{path}: value {node!r} not in enum {schema['enum']!r}")
    if isinstance(node, str) and "minLength" in schema and len(node) < int(schema["minLength"]):
        errors.append(f"{path}: string length {len(node)} < minLength {schema['minLength']}")
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        if "minimum" in schema and node < float(schema["minimum"]):
            errors.append(f"{path}: value {node} < minimum {schema['minimum']}")
        if "maximum" in schema and node > float(schema["maximum"]):
            errors.append(f"{path}: value {node} > maximum {schema['maximum']}")
    if isinstance(node, dict):
        for req_key in schema.get("required") or ():
            if req_key not in node:
                errors.append(f"{path}: missing required key '{req_key}'")
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is False and props:
            for k in node:
                if k not in props:
                    errors.append(f"{path}: unexpected additional key '{k}'")
        for k, sub_schema in props.items():
            if k in node and isinstance(sub_schema, dict):
                _validate_node_against_schema(node[k], sub_schema, f"{path}.{k}", errors)
    elif isinstance(node, list) and isinstance(schema.get("items"), dict):
        item_schema = schema["items"]
        for idx, item in enumerate(node):
            _validate_node_against_schema(item, item_schema, f"{path}[{idx}]", errors)


def validate_against_json_schema(
    raw_text: Any,
    schema: Dict[str, Any],
) -> tuple[bool, Optional[Dict[str, Any]], List[str]]:
    """Deterministically parse and validate JSON output against a JSON Schema without heavy external deps."""
    if isinstance(raw_text, dict):
        parsed: Any = raw_text
    elif isinstance(raw_text, str):
        cleaned = _strip_json_code_fence(raw_text)
        if not cleaned:
            return False, None, ["empty output"]
        try:
            parsed = json.loads(cleaned)
        except ValueError as exc:
            return False, None, [f"invalid JSON: {exc}"]
    else:
        return False, None, [f"unsupported input type: {type(raw_text).__name__}"]

    errors: List[str] = []
    if isinstance(schema, dict) and schema:
        _validate_node_against_schema(parsed, schema, "$", errors)
    if errors:
        return False, None, errors
    if isinstance(parsed, dict):
        return True, parsed, []
    return True, {"value": parsed}, []


def _require_loopback(url: str) -> str:
    host = urlparse(url).hostname or ""
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"local model URL must be loopback, got host {host!r}")
    return url.rstrip("/")


def _port_open(url: str) -> bool:
    # Raw TCP: on Windows a refused loopback connect is retried by the OS, so an HTTP probe
    # with timeout=1 s blocks ~1.5 s. HTTP-level errors surface later in _post_json.
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _post_json(url: str, body: dict, timeout: float) -> Optional[dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:300]
        except OSError:
            pass
        _LOG.warning("POST %s failed with HTTP %s: %s", url, exc.code, err_body)
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _LOG.warning("POST %s failed: %s", url, exc)
        return None


@dataclass(frozen=True)
class Choice:
    label: str
    confidence: float
    probabilities: Dict[str, float]

    @property
    def top_probability(self) -> float:
        return self.probabilities.get(self.label, 0.0)

    @property
    def margin(self) -> float:
        """Gap between the best and second-best option (the top probability when only one option)."""
        ranked = sorted(self.probabilities.values(), reverse=True)
        if not ranked:
            return 0.0
        return ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0]


@dataclass(frozen=True)
class ScoreResult:
    score: float
    confidence: float
    reason: str = ""


@dataclass(frozen=True)
class NoulResult:
    passed: bool
    confidence: float
    issues: List[str]


class JevClient:
    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = _require_loopback(base_url or os.environ.get("JEV_STYLE_URL") or JEV_DEFAULT_URL)
        self._healthy: Optional[bool] = None

    def healthy(self) -> bool:
        if self._healthy is None:
            self._healthy = _port_open(self.base_url)
        return self._healthy

    def choose(self, state: str, instructions: str, criteria: Dict[str, str]) -> Optional[Choice]:
        if not criteria or not self.healthy():
            return None
        body = {
            "state": state[:MAX_STATE_CHARS],
            "questions": {"q": {"type": "choice", "instructions": instructions, "criteria": criteria}},
        }
        answer = _post_json(f"{self.base_url}/v1/systemone", body, DECIDE_TIMEOUT_S)
        if not answer:
            self._healthy = False
            return None
        q = answer.get("answers", {}).get("q") or {}
        label = q.get("choice")
        if label not in criteria:
            return None
        return Choice(label=label, confidence=float(q.get("confidence", 0.0)), probabilities=dict(q.get("probabilities", {})))

    def batch(self, state: str, questions: Dict[str, dict]) -> Optional[Dict[str, dict]]:
        if not questions or not self.healthy():
            return None
        body = {"state": state[:MAX_STATE_CHARS], "questions": questions}
        answer = _post_json(f"{self.base_url}/v1/systemone", body, DECIDE_TIMEOUT_S)
        if not answer:
            self._healthy = False
            return None
        return answer.get("answers") or {}

    def score(self, state: str, instructions: str, rubric: Optional[str] = None) -> Optional[ScoreResult]:
        q: Dict[str, Any] = {"type": "score", "instructions": instructions}
        if rubric:
            q["rubric"] = rubric
        answers = self.batch(state, {"q": q})
        if not answers or "q" not in answers:
            return None
        item = answers["q"] or {}
        return ScoreResult(
            score=float(item.get("score", 0.0)),
            confidence=float(item.get("confidence", 0.0)),
            reason=str(item.get("reason", "")),
        )

    def noul(self, state: str, instructions: str, rules: Optional[List[str]] = None) -> Optional[NoulResult]:
        q: Dict[str, Any] = {"type": "noul", "instructions": instructions}
        if rules:
            q["rules"] = list(rules)
        answers = self.batch(state, {"q": q})
        if not answers or "q" not in answers:
            return None
        item = answers["q"] or {}
        return NoulResult(
            passed=bool(item.get("passed", False)),
            confidence=float(item.get("confidence", 0.0)),
            issues=[str(x) for x in (item.get("issues") or [])],
        )


class MiniPoolClient:
    """Loopback client for the EC2 32-core parallel mini-AI pool via LiteLLM/Gateway (:8765)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = _require_loopback(base_url or os.environ.get("KAIZEN_MINI_POOL_URL") or MINI_POOL_DEFAULT_URL)
        self._healthy: Optional[bool] = None

    def healthy(self) -> bool:
        if self._healthy is None:
            self._healthy = _port_open(self.base_url)
        return self._healthy

    def pool_status(self) -> Optional[dict]:
        if not self.healthy():
            return None
        req = urllib.request.Request(f"{self.base_url}/v1/pool/status", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def chat(
        self,
        prompt: str,
        system: str = "",
        model: str = "mini-pool",
        max_tokens: int = 512,
        temperature: float = 0.2,
        *,
        response_format: Optional[Dict[str, Any]] = None,
        json_schema: Optional[Dict[str, Any]] = None,
        gbnf_grammar: Optional[str] = None,
        guided_json: Optional[Dict[str, Any]] = None,
        constrained_spec: Optional[ConstrainedDecodingSpec] = None,
    ) -> Optional[str]:
        if not self.healthy():
            return None
        resolved_model = normalize_pool_model(model)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: Dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        constrained_fields = build_constrained_payload(
            constrained_spec,
            response_format=response_format,
            json_schema=json_schema,
            gbnf_grammar=gbnf_grammar,
            guided_json=guided_json,
        )
        body.update(constrained_fields)
        data = _post_json(f"{self.base_url}/v1/chat/completions", body, CHAT_TIMEOUT_S)
        if not data:
            return None
        choices = data.get("choices") or []
        if not choices:
            return None
        msg = choices[0].get("message") or {}
        return msg.get("content")

    def chat_structured(
        self,
        prompt: str,
        json_schema: Dict[str, Any],
        system: str = "",
        model: str = "qwen3.5-4b",
        max_tokens: int = 512,
        temperature: float = 0.0,
        gbnf_grammar: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Execute constrained JSON-schema chat and deterministically validate the returned object."""
        raw = self.chat(
            prompt,
            system=system,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            json_schema=json_schema,
            gbnf_grammar=gbnf_grammar,
        )
        if raw is None:
            return None
        ok, parsed, _ = validate_against_json_schema(raw, json_schema)
        return parsed if ok else None

    def parallel_map(
        self,
        items: List[str],
        system: str = "",
        model: str = "mini-pool",
        max_tokens: int = 512,
        max_workers: int = 6,
    ) -> List[Optional[str]]:
        if not items or not self.healthy():
            return [None for _ in items]
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(items)))) as pool:
            futures = [pool.submit(self.chat, item, system, model, max_tokens) for item in items]
            return [f.result() for f in futures]


def reciprocal_rank_fusion(
    ranked_lists: List[List[str]],
    k: int = 60,
    weights: Optional[List[float]] = None,
) -> List[tuple[str, float]]:
    """Fuse multiple ranked candidate ID lists (dense + sparse BM25 + AST sketch) via Reciprocal Rank Fusion."""
    if not ranked_lists:
        return []
    eff_k = max(1, int(k))
    eff_weights = weights if weights and len(weights) == len(ranked_lists) else [1.0] * len(ranked_lists)
    scores: Dict[str, float] = {}
    for list_idx, items in enumerate(ranked_lists):
        w = float(eff_weights[list_idx])
        seen_in_list = set()
        for rank_idx, item_id in enumerate(items, 1):
            if not item_id or item_id in seen_in_list:
                continue
            seen_in_list.add(item_id)
            scores[item_id] = scores.get(item_id, 0.0) + (w / float(eff_k + rank_idx))
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


class HybridEmbedRerankClient:
    """Lever B loopback client for Hybrid Dense+Sparse Embeddings (BGE-M3 / Qwen3-Embed) & Cross-Encoder Reranking (:8766)."""

    def __init__(
        self,
        embed_url: Optional[str] = None,
        rerank_url: Optional[str] = None,
    ) -> None:
        self.embed_url = _require_loopback(embed_url or os.environ.get("KAIZEN_BITNET_EMBED_URL") or BITNET_DEFAULT_URL)
        self.rerank_url = _require_loopback(rerank_url or os.environ.get("KAIZEN_RERANK_URL") or RERANK_DEFAULT_URL)
        self._healthy: Optional[bool] = None

    def healthy(self) -> bool:
        if self._healthy is None:
            self._healthy = _port_open(self.embed_url)
        return self._healthy

    @staticmethod
    def compute_sparse_lexical_weights(text: str) -> Dict[str, float]:
        tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9_]{2,}", text or "")]
        if not tokens:
            return {}
        counts: Dict[str, int] = {}
        for tok in tokens:
            counts[tok] = counts.get(tok, 0) + 1
        total = float(len(tokens))
        return {tok: round(cnt / total, 4) for tok, cnt in counts.items()}

    def embed_hybrid(self, text: str, model: str = "bge-m3") -> Dict[str, Any]:
        sparse = self.compute_sparse_lexical_weights(text)
        dense: Optional[List[float]] = None
        source = "offline_sparse_only"
        if self.healthy():
            data = _post_json(
                self.embed_url,
                {"content": text, "input": text, "model": model, "return_sparse": True},
                EMBED_TIMEOUT_S,
            )
            if data is not None:
                dense = extract_embedding(data)
                if isinstance(data, dict) and isinstance(data.get("sparse_weights"), dict):
                    sparse = {str(k): float(v) for k, v in data["sparse_weights"].items()}
                source = f"loopback:{self.embed_url}"
            else:
                self._healthy = False
        return {
            "model": model,
            "dense": dense,
            "sparse": sparse,
            "source": source,
        }

    def rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        text_key: str = "signature",
        top_k: int = 10,
        model: str = "qwen3-reranker-0.6b",
    ) -> List[Dict[str, Any]]:
        if not candidates:
            return []
        if self.healthy():
            texts = [str(c.get(text_key) or c.get("name") or "") for c in candidates]
            data = _post_json(
                self.rerank_url,
                {"query": query, "texts": texts, "model": model, "top_n": top_k},
                EMBED_TIMEOUT_S,
            )
            results = data.get("results") if isinstance(data, dict) else (data if isinstance(data, list) else None)
            if isinstance(results, list) and results:
                reranked: List[Dict[str, Any]] = []
                for item in results:
                    if not isinstance(item, dict) or "index" not in item:
                        continue
                    idx = int(item["index"])
                    if 0 <= idx < len(candidates):
                        entry = dict(candidates[idx])
                        entry["rerank_score"] = round(float(item.get("score", item.get("relevance_score", 0.0))), 4)
                        entry["reranker"] = model
                        reranked.append(entry)
                if reranked:
                    reranked.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
                    return reranked[:top_k]
        return [dict(c) for c in candidates[:top_k]]


@dataclass(frozen=True)
class DocumentOcrSpec:
    """Lever B routing spec for technical docs, scanned PDFs, and UI screenshots."""

    doc_type: str
    engine: str
    output_format: str = "markdown"
    supports_tables: bool = True
    supports_vision: bool = False


class DocumentOcrClient:
    """Adapter for Docling (layout/table PDFs), olmOCR 2 (dense scanned PDFs), and Gemma 4 12B Unified (vision/UI)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = _require_loopback(base_url or os.environ.get("KAIZEN_MINI_POOL_URL") or MINI_POOL_DEFAULT_URL)

    @staticmethod
    def select_engine(source_hint: str, doc_type: str = "auto") -> DocumentOcrSpec:
        hint = (source_hint or "").lower()
        dtype = (doc_type or "auto").lower()
        if dtype in ("screenshot", "ui", "diagram", "image") or hint.endswith((".png", ".jpg", ".jpeg", ".webp")):
            return DocumentOcrSpec(
                doc_type="multimodal_visual",
                engine="gemma-4-12b-unified",
                output_format="markdown",
                supports_tables=True,
                supports_vision=True,
            )
        if dtype in ("scanned_pdf", "ocr", "equation") or "scan" in hint:
            return DocumentOcrSpec(
                doc_type="scanned_document",
                engine="olmocr-2-7b",
                output_format="markdown",
                supports_tables=True,
                supports_vision=True,
            )
        return DocumentOcrSpec(
            doc_type="structured_document",
            engine="docling-v2",
            output_format="markdown",
            supports_tables=True,
            supports_vision=False,
        )

    def parse_document(
        self,
        content_or_path: str,
        doc_type: str = "auto",
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        spec = self.select_engine(content_or_path, doc_type=doc_type)
        pool = MiniPoolClient(self.base_url)
        if spec.engine == "gemma-4-12b-unified" and pool.healthy():
            if json_schema is not None:
                structured = pool.chat_structured(
                    f"Extract structured document content from: {content_or_path}",
                    json_schema=json_schema,
                    model="gemma-4-12b-unified",
                )
                if structured is not None:
                    return {"status": "ok", "engine": spec.engine, "doc_type": spec.doc_type, "structured": structured}
            out = pool.chat(
                f"Convert document/image to clean GitHub-Flavored Markdown preserving tables: {content_or_path}",
                model="gemma-4-12b-unified",
            )
            if out:
                return {"status": "ok", "engine": spec.engine, "doc_type": spec.doc_type, "markdown": out}
        return {
            "status": "spec_ready",
            "engine": spec.engine,
            "doc_type": spec.doc_type,
            "output_format": spec.output_format,
            "supports_tables": spec.supports_tables,
            "supports_vision": spec.supports_vision,
            "source": content_or_path,
        }


class BitNetEmbedClient:
    """llama.cpp-style `/embedding` server from bitnet.cpp (BitNet-embedding-0.6B)."""

    def __init__(self, url: Optional[str] = None) -> None:
        self.url = _require_loopback(url or os.environ.get("KAIZEN_BITNET_EMBED_URL") or BITNET_DEFAULT_URL)
        self._healthy: Optional[bool] = None

    def healthy(self) -> bool:
        if self._healthy is None:
            self._healthy = _port_open(self.url)
        return self._healthy

    def embed(self, text: str) -> Optional[List[float]]:
        if not self.healthy():
            return None
        data = _post_json(self.url, {"content": text, "input": text}, EMBED_TIMEOUT_S)
        if data is None:
            self._healthy = False
            return None
        return extract_embedding(data)


def extract_embedding(data) -> Optional[List[float]]:
    """Accept llama.cpp (`{"embedding": [...]}` or `[{"embedding": [[...]]}]`) and OpenAI (`data[0].embedding`) shapes."""
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return None
    vec = data.get("embedding")
    if vec is None and data.get("data"):
        vec = data["data"][0].get("embedding")
    if vec and isinstance(vec[0], list):
        vec = vec[0]
    return [float(x) for x in vec] if vec else None


def status() -> Dict[str, str]:
    """One-line engine report for both runtimes; never blocks more than ~0.6 s."""
    jev = JevClient()
    bitnet = BitNetEmbedClient()
    return {
        "jev-style": f"UP {jev.base_url}" if jev.healthy() else f"DOWN {jev.base_url} (routing -> orchestrator)",
        "bitnet-embed": f"UP {bitnet.url}" if bitnet.healthy() else f"DOWN {bitnet.url} (concept search -> 1-bit hash sketch)",
    }


def accept_route(choice: Optional[Choice], min_confidence: float = 0.55, min_margin: float = 0.15) -> bool:
    """Decide whether a local routing answer is trusted, or the prompt goes to the Bedrock orchestrator.

    A wrong confident route sends work to the wrong specialist (wasted Bedrock tokens, wrong
    domain rules); a rejected route only costs one orchestrator turn.
    """
    if choice is None:
        return False
    return choice.confidence >= min_confidence and choice.margin >= min_margin


TELEMETRY_STAGE_CRITERIA: Dict[str, str] = {
    "avro_wire": "Raw Avro .avsc envelope decoding, C avro-parse, allow_keys filtering, or Rust codecs/avro.",
    "bson_tree": "Raw BSON document tree parsing, TR-181 path extraction, or node/radio BSON field mapping.",
    "state_fold_sql": "LegacyBatchWorker PipelineDB/MySQL tables (tbl_automesh, od-02..20), CloudGatewayApi SQL DAOs, or Rust Semilattice folding.",
    "scoring_qoe": "WFEI, QualityScore, BRM, BLM, BAM, DLM, ALM scoring parity between edge-gerrit-core/avro-parse and Rust scoring.",
}

TELEMETRY_DOMAIN_CRITERIA: Dict[str, str] = {
    "topology_node": "Device identity, ownaddr, gateway/repeater role, firmware, reboot, or vital temperature.",
    "radio_channel": "Radio capabilities, band 2g/5g/6g, CCA medium availability, ACS, or channel selection.",
    "station_mld": "Client station MAC, MLD links, RSSI, headroom, PHY tx/rx rates, or airtime metrics.",
    "mesh_backhaul": "Automesh topology links, eth_mesh_links, backhaul steering, or BTM transitions.",
    "events_alarms": "Home/node events, alarm catalogs, detection subtypes, ChronoLog, or REST DTO framing.",
}

STAGE_TO_LORA_SPEC: Dict[str, tuple[str, str]] = {
    "avro_wire": ("lora-rust-vector-parity", "Qwen/Qwen3-Coder-30B-A3B-Instruct"),
    "bson_tree": ("lora-rust-vector-parity", "Qwen/Qwen3-Coder-30B-A3B-Instruct"),
    "state_fold_sql": ("lora-rust-vector-parity", "Qwen/Qwen3-Coder-30B-A3B-Instruct"),
    "scoring_qoe": ("lora-rust-vector-parity", "Qwen/Qwen3-Coder-30B-A3B-Instruct"),
}

_STAGE_KEYWORDS: Dict[str, tuple[str, ...]] = {
    "avro_wire": (
        "avro", "avsc", "allow_keys", "avro-parse", "envelope", "codec",
        "wire_events", "raw_events", "rawevents", "get raw events", "raw events",
        "getrawevents", "getevents", "gettimelineevents", "timeline-events",
        "timeline events", "getreboots", "gethomereboots", "briefcase", "fingerprint",
    ),
    "bson_tree": (
        "bson", "hermes_bson", "hermes", "tr-181", "tr181", "document",
        "beacon", "capabilities", "automesh", "topology_line",
    ),
    "state_fold_sql": (
        "legacy_batch_worker", "pipelinedb", "mysql", "tbl_", "cache_", "sql", "semilattice",
        "lww", "rollup", "vector_cube", "fold", "cloud_gateway_api", "nodes", "stations",
        "links", "ssid", "afc", "fwa", "wan", "speedtest", "timeline",
        "getnodes", "gethomesummary", "getstations", "getwifilinks", "getethlinks",
        "getethmeshlinks", "getethmeshdatausage", "getmeshdatausage",
        "getstationperformance", "getstationphystats", "getstationdatausage",
        "getstationestimatedthr", "getwifiphystats", "getssids", "getnodescca",
        "getnodesspectrum", "getafchome", "getafcnode", "getafcnodecapability",
        "getcommands", "getwanstats", "getprioritysessions", "getfwastate",
        "getfwastats", "getfwaissues", "getfwaconnectionmetrics", "getfwadatatraffic",
        "getfwastatehistory", "getfwaapn",
    ),
    "scoring_qoe": (
        "wfei", "quality_score", "wuxi", "brm", "blm", "bam", "dlm", "alm",
        "score", "scoring", "qoe", "core-score", "alarm", "issue",
        "diagnostics", "ecomode", "getwfei", "getwfeiseries", "getnodesalm",
        "gethomeblm", "gethomedlm", "gethomebam", "gethomebrm",
        "gethomeissues", "gethomealarms", "getalarmtypes", "getnodeeventalarmtypes",
        "getecomode",
    ),
}

_DOMAIN_KEYWORDS: Dict[str, tuple[str, ...]] = {
    "topology_node": ("topology", "node", "ownaddr", "gateway", "repeater", "firmware", "reboot", "vital", "device"),
    "radio_channel": ("radio", "channel", "cca", "band", "acs", "medium", "bandwidth", "2g", "5g", "6g", "ssid", "spectrum"),
    "station_mld": ("station", "mld", "rssi", "headroom", "phy", "txpr", "rxpr", "airtime", "client", "estimated"),
    "mesh_backhaul": ("mesh", "backhaul", "automesh", "btm", "steer", "eth_mesh", "link", "blm", "dlm", "bam", "brm"),
    "events_alarms": ("event", "alarm", "chronolog", "subtype", "detection", "dto", "catalog", "uc-", "issue"),
}


@dataclass(frozen=True)
class TelemetryRoute:
    """Two-hop telemetry routing decision mapping queries to Dual-32B Multi-LoRA specialists."""

    stage: str
    domain: str
    lora_adapter: str
    judge_adapter: str
    base_model: str
    engine_used: str
    stage_confidence: float
    domain_confidence: float
    trusted: bool


def _keyword_matches(lowered: str, kw: str) -> bool:
    if kw.endswith(("_", "-")):
        return kw in lowered
    return re.search(rf"(?<![a-z0-9_]){re.escape(kw)}(?![a-z0-9_])", lowered) is not None


def _score_keywords(text: str, table: Dict[str, tuple[str, ...]], default_key: str) -> tuple[str, float]:
    lowered = text.lower()
    scores: Dict[str, int] = {
        k: sum(1 for kw in kws if _keyword_matches(lowered, kw))
        for k, kws in table.items()
    }
    best = max(scores.items(), key=lambda item: (item[1], item[0] == default_key))
    total = sum(scores.values())
    if best[1] == 0 or total == 0:
        return default_key, 0.25
    return best[0], round(best[1] / max(total, 1), 3)


def route_telemetry_2hop(
    query: str,
    state_summary: str = "",
    jev: Optional[JevClient] = None,
) -> TelemetryRoute:
    """Route a telemetry/parity query in 2 bounded hops (4 stages x 5 domains) without 28-way timeouts."""
    client = jev or JevClient()
    combined_state = f"QUERY: {query}\nCONTEXT: {state_summary}".strip()

    if client.healthy():
        hop1 = client.choose(
            combined_state,
            "Select the telemetry pipeline stage most responsible for this question or divergence.",
            TELEMETRY_STAGE_CRITERIA,
        )
        hop2 = None
        if accept_route(hop1):
            hop2 = client.choose(
                f"{combined_state}\nSELECTED_STAGE: {hop1.label}",
                "Select the specific telemetry domain family within the selected pipeline stage.",
                TELEMETRY_DOMAIN_CRITERIA,
            )
        if accept_route(hop1) and accept_route(hop2):
            assert hop1 is not None and hop2 is not None
            lora_adapter, base_model = STAGE_TO_LORA_SPEC[hop1.label]
            return TelemetryRoute(
                stage=hop1.label,
                domain=hop2.label,
                lora_adapter=lora_adapter,
                judge_adapter="lora-system-rca-triage",
                base_model=base_model,
                engine_used="jev-style-2hop",
                stage_confidence=round(hop1.confidence, 3),
                domain_confidence=round(hop2.confidence, 3),
                trusted=True,
            )

    stage, stage_conf = _score_keywords(combined_state, _STAGE_KEYWORDS, "state_fold_sql")
    domain, domain_conf = _score_keywords(combined_state, _DOMAIN_KEYWORDS, "topology_node")
    lora_adapter, base_model = STAGE_TO_LORA_SPEC[stage]
    return TelemetryRoute(
        stage=stage,
        domain=domain,
        lora_adapter=lora_adapter,
        judge_adapter="lora-system-rca-triage",
        base_model=base_model,
        engine_used="deterministic-lexical-fallback",
        stage_confidence=stage_conf,
        domain_confidence=domain_conf,
        trusted=False,
    )
