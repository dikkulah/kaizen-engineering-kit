#!/usr/bin/env python3
"""
Portable Smart Compaction State Recovery Guard (`compact_state_guard.py`).

Maintains a lightweight per-session checkpoint in `resolve_state_dir() / "compact_checkpoints"`
containing verified Serena/LSP symbols, active modified files, and unresolved errors.
Injects a one-shot recovery summary upon ~135K context compaction (`initialNumSteps` drop >= 4
from `max_seen_steps >= 8`), explicit `/compact`, or Claude Code `SessionStart(source="compact")`.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

_KIT_SRC = Path(__file__).resolve().parent.parent.parent
if str(_KIT_SRC) not in sys.path:
    sys.path.insert(0, str(_KIT_SRC))

from kaizen_kit.gates.deterministic_vault import DeterministicVault, check_for_secrets
from kaizen_kit.paths import resolve_state_dir, resolve_vault_path

_MIRIX_VAULT: Optional[DeterministicVault] = None
_MIRIX_VAULT_ATEXIT_REGISTERED = False
_MIRIX_QUEUE_LOCK = threading.Lock()
DEFAULT_FASTMCP_URL = os.environ.get("KAIZEN_FASTMCP_URL", "http://127.0.0.1:8770").rstrip("/")
MIRIX_HOOK_TOKEN_BUDGET = 400

MAX_SYMBOLS = 18
MAX_MODIFIED_FILES = 12
MAX_ERROR_CHARS = 360
CHECKPOINT_TTL_SECONDS = 6 * 3600
STEP_DROP_THRESHOLD = 4
MIN_STEPS_BEFORE_DROP_DETECT = 8
PROACTIVE_COMPACT_STEP_THRESHOLD = 65
PROACTIVE_COMPACT_STEP_INTERVAL = 65
PROACTIVE_COMPACT_BYTES_THRESHOLD = 800 * 1024
PROACTIVE_COMPACT_BYTES_INTERVAL = 500 * 1024
NATURAL_REWIND_RE = re.compile(
    r"\b(oto\s*rewind|auto\s*rewind|rewind\s+yap|kompakt\s+yap|bağlamı\s+özetle|contexti\s+küçült|başa\s+sar)\b",
    re.IGNORECASE,
)

NUDGE_INPUTS = {
    "continue",
    "devam",
    "devam et",
    "go on",
    "proceed",
    "ok",
    "tamam",
}

LSP_CLI_SYM_RE = re.compile(
    r"\blsp-query(?:\.cmd)?\s+(?P<subcmd>find|refs|symbols|search|concept)\s+(?:'([^']+)'|\"([^\"]+)\"|([^\s;|]+))",
    re.IGNORECASE,
)
CLAUDE_RAM_SYM_RE = re.compile(
    r"\bclaude-ram(?:\.cmd)?\s+(?:map|slice|ask)\s+([^\r\n;|]{1,90})",
    re.IGNORECASE,
)


def _default_state_root() -> Path:
    env_gemini = os.environ.get("GEMINI_HOME", "").strip()
    if env_gemini and not os.environ.get("AI_KIT_STATE_DIR", "").strip():
        return Path(env_gemini).expanduser().resolve() / "antigravity" / "kaizen_state" / "compact_checkpoints"
    return resolve_state_dir() / "compact_checkpoints"


def _safe_conv_key(payload: Dict[str, Any]) -> str:
    raw = str(payload.get("conversationId") or payload.get("session_id") or "default")
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
    return cleaned[:64] or "default"


def resolve_workspace(payload: Dict[str, Any]) -> Path:
    ws_paths = payload.get("workspacePaths")
    if isinstance(ws_paths, list) and ws_paths:
        first = str(ws_paths[0]).strip()
        if first:
            return Path(first).expanduser().resolve()
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    cwd = payload.get("cwd")
    if cwd:
        return Path(str(cwd)).expanduser().resolve()
    return Path.cwd().resolve()


def checkpoint_file(conv_key: str, state_root: Optional[Path] = None) -> Path:
    root = state_root or _default_state_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"compact_checkpoint_{conv_key}.json"


def rewind_capsule_file(conv_key: str, state_root: Optional[Path] = None) -> Path:
    root = state_root or _default_state_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"rewind_capsule_{conv_key}.md"


def _empty_checkpoint() -> Dict[str, Any]:
    return {
        "verified_serena_symbols": [],
        "edited_files": [],
        "unresolved_error": "",
        "last_goal": "",
        "max_seen_steps": 0,
        "last_initial_steps": 0,
        "last_injected_step": -1,
        "last_proactive_step": 0,
        "last_proactive_bytes": 0,
        "last_natural_rewind_input": "",
        "updated_at": time.time(),
    }


def load_checkpoint(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return _empty_checkpoint()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base = _empty_checkpoint()
            base.update(data)
            return base
    except (OSError, json.JSONDecodeError):
        pass
    return _empty_checkpoint()


def save_checkpoint(path: Path, state: Dict[str, Any]) -> None:
    state["updated_at"] = time.time()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")
    except OSError:
        pass


def _append_unique(items: List[str], value: str, limit: int) -> List[str]:
    clean = value.strip()
    if not clean:
        return items
    filtered = [x for x in items if x != clean]
    filtered.append(clean)
    return filtered[-limit:]


def _git_branch_and_dirty(workspace: Path) -> Tuple[str, List[str]]:
    if not workspace.is_dir() or not (workspace / ".git").exists():
        return "", []
    branch = ""
    dirty: List[str] = []
    try:
        br = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=0.8,
            check=False,
        )
        if br.returncode == 0:
            branch = br.stdout.strip()
        st = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=0.8,
            check=False,
        )
        if st.returncode == 0:
            dirty = [ln.strip() for ln in st.stdout.splitlines() if len(ln.strip()) > 2][:MAX_MODIFIED_FILES]
    except (OSError, subprocess.SubprocessError):
        pass
    return branch, dirty


def _extract_tool_and_args(payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    tc = payload.get("toolCall")
    if isinstance(tc, dict):
        raw_args = tc.get("args") or tc.get("arguments") or {}
        return str(tc.get("name") or ""), dict(raw_args) if isinstance(raw_args, dict) else {}
    if "tool_name" in payload:
        t_inp = payload.get("tool_input")
        return str(payload.get("tool_name") or ""), dict(t_inp) if isinstance(t_inp, dict) else {}
    raw_args = payload.get("arguments") or payload.get("args") or {}
    return str(payload.get("toolName") or payload.get("name") or ""), dict(raw_args) if isinstance(raw_args, dict) else {}


def _extract_serena_label(tool_name: str, args: Dict[str, Any]) -> str:
    lower_name = tool_name.lower()
    is_mcp_serena = (
        "serena" in lower_name
        or (tool_name == "call_mcp_tool" and str(args.get("ServerName") or "").lower() == "serena")
        or any(k in args for k in ("name_path", "name_path_pattern", "symbol_name", "substring_pattern"))
    )
    if not is_mcp_serena:
        return ""
    mcp_args = args.get("Arguments") if isinstance(args.get("Arguments"), dict) else args
    sym = (
        mcp_args.get("name_path")
        or mcp_args.get("name_path_pattern")
        or mcp_args.get("symbol_name")
        or mcp_args.get("substring_pattern")
        or mcp_args.get("symbol")
        or args.get("ToolName")
        or ""
    )
    rel = (
        mcp_args.get("relative_path")
        or mcp_args.get("TargetFile")
        or mcp_args.get("Path")
        or mcp_args.get("file_path")
        or ""
    )
    if not sym and not rel:
        return ""
    short_rel = os.path.basename(str(rel)) if rel else ""
    return f"{sym} ({short_rel})" if (sym and short_rel) else str(sym or short_rel)


def _flush_mirix_vault() -> None:
    if _MIRIX_VAULT is not None:
        _MIRIX_VAULT.flush()


def _get_mirix_vault() -> DeterministicVault:
    global _MIRIX_VAULT, _MIRIX_VAULT_ATEXIT_REGISTERED
    expected_path = resolve_vault_path()
    if _MIRIX_VAULT is None or _MIRIX_VAULT.vault_path != expected_path:
        if _MIRIX_VAULT is not None:
            _MIRIX_VAULT.flush()
        _MIRIX_VAULT = DeterministicVault(vault_path=expected_path, auto_save=False)
        if not _MIRIX_VAULT_ATEXIT_REGISTERED:
            atexit.register(_flush_mirix_vault)
            _MIRIX_VAULT_ATEXIT_REGISTERED = True
    return _MIRIX_VAULT


def _is_port_reachable(host: str, port: int, timeout_s: float) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        err = sock.connect_ex((host, port))
        if err == 0:
            return True
        _, writable, exceptional = select.select([], [sock], [sock], max(0.01, min(0.08, timeout_s)))
        if exceptional or not writable:
            return False
        return sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0
    except OSError:
        return False
    finally:
        sock.close()


def default_mirix_queue_path() -> Path:
    return resolve_state_dir() / "mirix_queue.jsonl"


def enqueue_mirix_event(
    event: Dict[str, Any],
    queue_path: Optional[Path] = None,
    scope: str = "",
) -> bool:
    """Zero-tax (<5 ms) async JSONL enqueue with secret rejection and vault anonymization."""
    if not isinstance(event, dict):
        return False
    raw_blob = json.dumps(event, ensure_ascii=False)
    if check_for_secrets(raw_blob):
        return False
    vault = _get_mirix_vault()
    sc = str(event.get("scope") or scope or "")
    sanitized = dict(event)
    for key in ("summary", "content", "tags", "source_ref"):
        if key in sanitized and isinstance(sanitized[key], str):
            sanitized[key] = vault.anonymize_text(sanitized[key], scope=sc)
    if isinstance(sanitized.get("provenance"), dict):
        sanitized["provenance"] = {
            k: vault.anonymize_text(str(v), scope=sc) if isinstance(v, str) else v
            for k, v in sanitized["provenance"].items()
        }
    line = (json.dumps(sanitized, ensure_ascii=False) + "\n").encode("utf-8")
    qpath = queue_path or default_mirix_queue_path()
    try:
        with _MIRIX_QUEUE_LOCK:
            if not qpath.parent.exists():
                qpath.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
            fd = os.open(str(qpath), flags, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        return True
    except OSError:
        return False


def fetch_mirix_context(
    query: str,
    budget_tokens: int = MIRIX_HOOK_TOKEN_BUDGET,
    timeout_s: float = 0.25,
    gateway_url: Optional[str] = None,
) -> str:
    """Fetch <=400 tokens of MIRIX memory from FastMCP :8770 with silent fast fallback."""
    if not query or not query.strip():
        return ""
    budget = max(1, min(int(budget_tokens), MIRIX_HOOK_TOKEN_BUDGET))
    base_url = (gateway_url or DEFAULT_FASTMCP_URL).rstrip("/")
    payload = {"query": query.strip()[:400], "budget_tokens": budget, "limit": 6}
    data: Optional[Dict[str, Any]] = None
    try:
        httpx_mod = sys.modules.get("httpx")
        is_mocked = httpx_mod is not None and not getattr(httpx_mod.Client, "__module__", "").startswith("httpx")
        if not is_mocked:
            parsed = urlparse(base_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if not _is_port_reachable(host, port, float(timeout_s)):
                return ""
        import httpx

        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(f"{base_url}/mirix/query", json=payload)
            if resp.status_code == 200:
                data = resp.json()
    except Exception:
        return ""
    items = (data or {}).get("items") or []
    if not items:
        return ""
    lines = ["[MIRIX MEMORY CONTEXT (<=400 tok)]"]
    max_chars = budget * 4
    total_chars = len(lines[0])
    for it in items:
        snippet = f"- [{it.get('mem_type', 'mem')}] {it.get('summary', '')}: {str(it.get('content', ''))[:220]}"
        if total_chars + len(snippet) + 1 > max_chars:
            break
        lines.append(snippet)
        total_chars += len(snippet) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


def _read_nonempty_lines(path: Path) -> List[str]:
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]


def _merge_lines_to_queue(prefix_lines: List[str], qpath: Path) -> int:
    with _MIRIX_QUEUE_LOCK:
        tail_lines = _read_nonempty_lines(qpath)
        combined = [*prefix_lines, *tail_lines]
        if not combined:
            if qpath.is_file():
                qpath.unlink()
            return 0
        tmp = qpath.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(combined) + "\n", encoding="utf-8")
        tmp.replace(qpath)
        return len(combined)


def flush_mirix_queue(
    queue_path: Optional[Path] = None,
    gateway_url: Optional[str] = None,
    batch_size: int = 50,
    timeout_s: float = 3.0,
) -> Dict[str, Any]:
    """Drain queued MIRIX JSONL events out-of-band to FastMCP :8770 (/mirix/ingest)."""
    qpath = queue_path or default_mirix_queue_path()
    drain_path = qpath.with_suffix(".jsonl.draining")
    with _MIRIX_QUEUE_LOCK:
        if not drain_path.is_file():
            if not qpath.is_file():
                return {"ok": True, "flushed": 0, "remaining": 0}
            qpath.replace(drain_path)
    lines = _read_nonempty_lines(drain_path)
    if not lines:
        if drain_path.is_file():
            drain_path.unlink()
        return {"ok": True, "flushed": 0, "remaining": len(_read_nonempty_lines(qpath))}
    cap = max(1, int(batch_size))
    batch_lines, rest_lines = lines[:cap], lines[cap:]
    items: List[Dict[str, Any]] = []
    for ln in batch_lines:
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                items.append(obj)
        except json.JSONDecodeError:
            continue
    if items:
        base_url = (gateway_url or DEFAULT_FASTMCP_URL).rstrip("/")
        try:
            import httpx

            with httpx.Client(timeout=timeout_s) as client:
                resp = client.post(f"{base_url}/mirix/ingest", json={"items": items})
                if resp.status_code >= 400:
                    total_rem = _merge_lines_to_queue(lines, qpath)
                    if drain_path.is_file():
                        drain_path.unlink()
                    return {"ok": False, "flushed": 0, "remaining": total_rem, "status": resp.status_code}
        except Exception as exc:
            total_rem = _merge_lines_to_queue(lines, qpath)
            if drain_path.is_file():
                drain_path.unlink()
            return {"ok": False, "flushed": 0, "remaining": total_rem, "error": str(exc)}
    total_rem = _merge_lines_to_queue(rest_lines, qpath)
    if drain_path.is_file():
        drain_path.unlink()
    _flush_mirix_vault()
    return {"ok": True, "flushed": len(items), "remaining": total_rem}


def record_post_tool(payload: Dict[str, Any], state_root: Optional[Path] = None) -> None:
    """Updates conversation checkpoint after any tool execution (`<3ms`)."""
    ckpt_path = checkpoint_file(_safe_conv_key(payload), state_root=state_root)
    state = load_checkpoint(ckpt_path)
    tool_name, args = _extract_tool_and_args(payload)

    step_idx = int(payload.get("stepIdx") or payload.get("invocationNum") or 0)
    if step_idx > int(state.get("max_seen_steps") or 0):
        state["max_seen_steps"] = step_idx

    last_input = str(payload.get("lastUserInput") or "").strip()
    if last_input and not last_input.startswith("/"):
        state["last_goal"] = " ".join(last_input.split())[:220]

    serena_label = _extract_serena_label(tool_name, args)
    if serena_label:
        state["verified_serena_symbols"] = _append_unique(
            list(state.get("verified_serena_symbols") or []),
            serena_label,
            MAX_SYMBOLS,
        )

    if tool_name in ("edit_file", "write_file", "write_to_file", "replace_file_content", "multi_replace_file_content", "Edit", "Write") or "replace_symbol" in tool_name.lower():
        target = str(args.get("Path") or args.get("TargetFile") or args.get("relative_path") or args.get("file_path") or "")
        if target:
            state["edited_files"] = _append_unique(
                list(state.get("edited_files") or []),
                target.replace("\\", "/"),
                MAX_MODIFIED_FILES,
            )

    if tool_name in ("run_command", "Bash", "PowerShell"):
        cmd = str(args.get("CommandLine") or args.get("command") or "").strip()
        for m_lsp in LSP_CLI_SYM_RE.finditer(cmd):
            subcmd = m_lsp.group("subcmd").lower()
            target_val = (m_lsp.group(2) or m_lsp.group(3) or m_lsp.group(4) or "").strip()
            if target_val and len(target_val) <= 64 and not target_val.startswith("-"):
                state["verified_serena_symbols"] = _append_unique(
                    list(state.get("verified_serena_symbols") or []),
                    f"lsp-{subcmd}:{target_val}",
                    MAX_SYMBOLS,
                )
        m_ram = CLAUDE_RAM_SYM_RE.search(cmd)
        if m_ram:
            state["verified_serena_symbols"] = _append_unique(
                list(state.get("verified_serena_symbols") or []),
                f"ram:{m_ram.group(1).strip()[:50]}",
                MAX_SYMBOLS,
            )
        err_txt = str(payload.get("error") or "").strip()
        state["unresolved_error"] = f"{cmd[:80]} -> {err_txt}"[:MAX_ERROR_CHARS] if err_txt else ""

    save_checkpoint(ckpt_path, state)
    edited_last = (state.get("edited_files") or [])[-1:]
    sym_last = (state.get("verified_serena_symbols") or [])[-1:]
    err_now = str(state.get("unresolved_error") or "")
    if edited_last or sym_last or err_now:
        q_target = (state_root / "mirix_queue.jsonl") if state_root else None
        enqueue_mirix_event(
            {
                "mem_type": "episodic",
                "summary": f"Tool {tool_name}: {(edited_last or sym_last or [err_now])[0][:100]}",
                "content": f"goal={state.get('last_goal', '')} files={state.get('edited_files', [])[-3:]} symbols={state.get('verified_serena_symbols', [])[-3:]} err={err_now}",
                "tags": f"post_tool,{tool_name}",
                "source_ref": f"conv:{_safe_conv_key(payload)}",
                "provenance": {
                    "olay": f"post_tool:{tool_name}",
                    "bilesen": tool_name,
                    "dosya_yolu": ",".join(edited_last),
                    "raw_source_ptr": str(ckpt_path),
                    "raw_source_type": "hook_post_tool",
                },
            },
            queue_path=q_target,
        )


def build_recovery_message(state: Dict[str, Any], workspace: Path) -> str:
    branch, dirty = _git_branch_and_dirty(workspace)
    lines: List[str] = [
        "[SERENA COMPACT STATE GUARD -- DO NOT RE-READ VERIFIED SYMBOLS/FILES FROM SCRATCH]"
    ]
    if branch:
        lines.append(f"- Git branch: {branch}")
    if state.get("last_goal"):
        lines.append(f"- Active task goal: {state['last_goal']}")

    symbols = state.get("verified_serena_symbols") or []
    if symbols:
        lines.append("- Verified warm Serena/RAM symbols: " + "; ".join(symbols))

    edited = state.get("edited_files") or []
    merged_files = edited if edited else dirty
    if merged_files:
        lines.append("- Modified / active files: " + ", ".join(merged_files[:10]))

    if state.get("unresolved_error"):
        lines.append(f"- Unresolved last command error: {state['unresolved_error']}")

    return "\n".join(lines) if len(lines) > 1 else ""


def write_rewind_capsule(
    conv_key: str,
    state: Dict[str, Any],
    workspace: Path,
    reason: str,
    state_root: Optional[Path] = None,
) -> Path:
    branch, dirty = _git_branch_and_dirty(workspace)
    edited = list(state.get("edited_files") or [])
    symbols = list(state.get("verified_serena_symbols") or [])
    merged_files = (edited if edited else dirty)[:12]
    lines = [
        f"# Kaizen Auto-Rewind Capsule (`{conv_key}`)",
        f"- **Updated At**: `{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}`",
        f"- **Trigger Reason**: {reason}",
        f"- **Active Goal**: {state.get('last_goal') or '(none recorded)'}",
        f"- **Git Branch**: `{branch or 'unknown'}`",
        f"- **Verified Warm Serena/RAM Symbols**: {', '.join(symbols) if symbols else '(none)'}",
        f"- **Modified / Dirty Files**: {', '.join(merged_files) if merged_files else '(none)'}",
        f"- **Unresolved Error**: {state.get('unresolved_error') or '(none)'}",
    ]
    cap_path = rewind_capsule_file(conv_key, state_root=state_root)
    try:
        cap_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    q_target = (state_root / "mirix_queue.jsonl") if state_root else None
    enqueue_mirix_event(
        {
            "mem_type": "resource",
            "summary": f"Rewind capsule ({reason}): {state.get('last_goal') or conv_key}",
            "content": f"branch={branch or 'unknown'} files={merged_files[:6]} symbols={symbols[:6]} err={state.get('unresolved_error') or ''}",
            "tags": "rewind_capsule,compact",
            "source_ref": str(cap_path),
            "provenance": {
                "olay": f"rewind:{reason}",
                "bilesen": branch or "workspace",
                "dosya_yolu": ",".join(merged_files[:4]),
                "raw_source_ptr": str(cap_path),
                "raw_source_type": "rewind_capsule",
            },
        },
        queue_path=q_target,
    )
    return cap_path


def check_pre_invocation_compaction(
    payload: Dict[str, Any],
    observed_planner_steps: int = 0,
    state_root: Optional[Path] = None,
    transcript_bytes: int = 0,
    total_tx_steps: int = 0,
) -> Optional[str]:
    conv_key = _safe_conv_key(payload)
    ckpt_path = checkpoint_file(conv_key, state_root=state_root)
    state = load_checkpoint(ckpt_path)
    updated_at = float(state.get("updated_at") or 0.0)
    if time.time() - updated_at > CHECKPOINT_TTL_SECONDS:
        return None

    has_initial = "initialNumSteps" in payload and payload.get("initialNumSteps") is not None
    initial_steps = int(payload.get("initialNumSteps") or 0) if has_initial else observed_planner_steps
    effective_steps = max(initial_steps, int(observed_planner_steps or 0), int(total_tx_steps or 0))
    tx_bytes = max(int(transcript_bytes or 0), int(payload.get("transcriptBytes") or 0))
    prev_initial = int(state.get("last_initial_steps") or 0)
    max_seen = max(prev_initial, int(state.get("max_seen_steps") or 0), observed_planner_steps if not has_initial else 0)
    last_user = str(payload.get("lastUserInput") or "").strip()
    last_user_lower = last_user.lower()

    step_dropped = (
        has_initial
        and max_seen >= MIN_STEPS_BEFORE_DROP_DETECT
        and initial_steps <= max_seen - STEP_DROP_THRESHOLD
    )
    explicit_cmd = (
        last_user_lower.startswith("/compact")
        or "[durum-yukle]" in last_user_lower
        or "[restore-state]" in last_user_lower
    )
    natural_rewind = bool(
        last_user
        and NATURAL_REWIND_RE.search(last_user)
        and state.get("last_natural_rewind_input") != last_user
    )
    explicit_compact = explicit_cmd or natural_rewind
    last_proactive_step = int(state.get("last_proactive_step") or 0)
    last_proactive_bytes = int(state.get("last_proactive_bytes") or 0)
    proactive_step_trigger = (
        effective_steps >= PROACTIVE_COMPACT_STEP_THRESHOLD
        and (last_proactive_step == 0 or (effective_steps - last_proactive_step) >= PROACTIVE_COMPACT_STEP_INTERVAL)
    )
    proactive_bytes_trigger = (
        tx_bytes >= PROACTIVE_COMPACT_BYTES_THRESHOLD
        and (last_proactive_bytes == 0 or (tx_bytes - last_proactive_bytes) >= PROACTIVE_COMPACT_BYTES_INTERVAL)
    )

    if initial_steps > max_seen:
        state["max_seen_steps"] = initial_steps
    state["last_initial_steps"] = initial_steps
    if last_user and not last_user.startswith("/") and last_user_lower not in NUDGE_INPUTS:
        state["last_goal"] = " ".join(last_user.split())[:220]

    if step_dropped or explicit_compact or proactive_step_trigger or proactive_bytes_trigger:
        if step_dropped or explicit_compact:
            state["max_seen_steps"] = initial_steps
            state["last_injected_step"] = initial_steps
        if natural_rewind:
            state["last_natural_rewind_input"] = last_user
        state["last_proactive_step"] = effective_steps
        state["last_proactive_bytes"] = tx_bytes

        reasons: List[str] = []
        if step_dropped:
            reasons.append("compaction_step_drop")
        if explicit_compact:
            reasons.append("explicit_compact_or_rewind")
        if proactive_step_trigger:
            reasons.append(f"proactive_steps>={PROACTIVE_COMPACT_STEP_THRESHOLD}")
        if proactive_bytes_trigger:
            reasons.append(f"proactive_bytes>={PROACTIVE_COMPACT_BYTES_THRESHOLD // 1024}KB")

        workspace = resolve_workspace(payload)
        capsule_path = write_rewind_capsule(conv_key, state, workspace, ", ".join(reasons), state_root=state_root)
        base_msg = build_recovery_message(state, workspace)
        if step_dropped or explicit_compact:
            mirix_q = str(state.get("last_goal") or last_user or workspace.name or "").strip()
            mirix_ctx = fetch_mirix_context(mirix_q, budget_tokens=MIRIX_HOOK_TOKEN_BUDGET, timeout_s=0.25)
            if mirix_ctx:
                base_msg = f"{base_msg}\n\n{mirix_ctx}" if base_msg else mirix_ctx
        save_checkpoint(ckpt_path, state)

        if proactive_step_trigger or proactive_bytes_trigger:
            banner = (
                f"[KAIZEN AUTO-REWIND & COMPACT ENGINE -- PROACTIVE CONTEXT WATERMARK "
                f"(steps={effective_steps}, kb={tx_bytes // 1024})]\n"
                f"- Rewind Capsule saved: {capsule_path}\n"
                "- Rely on this Rewind Capsule & warm Serena symbols; DO NOT re-read earlier chat logs or full files.\n"
                "- Offload multi-file reads via `claude-ram ask` or specialized subagents; user can run `/compact` anytime with zero state loss."
            )
            return f"{banner}\n{base_msg}" if base_msg else banner
        if explicit_compact:
            prefix = f"- Rewind Capsule saved: {capsule_path}"
            return f"{base_msg}\n{prefix}" if base_msg else prefix
        return base_msg or None

    save_checkpoint(ckpt_path, state)
    return None


def handle_claude_compact_event(payload: Dict[str, Any], state_root: Optional[Path] = None) -> Optional[str]:
    event = str(payload.get("hook_event_name") or "")
    ckpt_path = checkpoint_file(_safe_conv_key(payload), state_root=state_root)
    if event == "PreCompact":
        state = load_checkpoint(ckpt_path)
        save_checkpoint(ckpt_path, state)
        q_target = (state_root / "mirix_queue.jsonl") if state_root else None
        enqueue_mirix_event(
            {
                "mem_type": "episodic",
                "summary": f"PreCompact snapshot: {state.get('last_goal') or _safe_conv_key(payload)}",
                "content": f"files={state.get('edited_files', [])[:6]} symbols={state.get('verified_serena_symbols', [])[:6]} err={state.get('unresolved_error', '')}",
                "tags": "pre_compact",
                "source_ref": str(ckpt_path),
            },
            queue_path=q_target,
        )
        return None
    if event == "SessionStart" and str(payload.get("source") or "") == "compact":
        state = load_checkpoint(ckpt_path)
        ws = resolve_workspace(payload)
        msg = build_recovery_message(state, ws)
        mirix_ctx = fetch_mirix_context(str(state.get("last_goal") or ws.name or "kaizen"), budget_tokens=MIRIX_HOOK_TOKEN_BUDGET, timeout_s=0.25)
        if mirix_ctx:
            msg = f"{msg}\n\n{mirix_ctx}" if msg else mirix_ctx
        return msg or None
    return None


def run_self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="kaizen_compact_test_") as tmp:
        root = Path(tmp)
        cid = "conv-compact-01"
        record_post_tool(
            {
                "conversationId": cid,
                "stepIdx": 14,
                "lastUserInput": "Refactor VectorCube station folding",
                "toolCall": {
                    "name": "call_mcp_tool",
                    "args": {
                        "ServerName": "serena",
                        "ToolName": "find_symbol",
                        "Arguments": {"name_path_pattern": "fold_station_event", "relative_path": "src/ingest/fold_avro.rs"},
                    },
                },
            },
            state_root=root,
        )
        record_post_tool(
            {
                "conversationId": cid,
                "stepIdx": 15,
                "toolCall": {"name": "edit_file", "args": {"Path": "src/ingest/fold_avro.rs"}},
            },
            state_root=root,
        )
        none_msg = check_pre_invocation_compaction(
            {"conversationId": cid, "initialNumSteps": 16, "lastUserInput": "Continue"},
            state_root=root,
        )
        if none_msg is not None:
            sys.stderr.write(f"[compact_state_guard FAIL] Expected None on normal turn, got: {none_msg}\n")
            return 1
        rec_msg = check_pre_invocation_compaction(
            {"conversationId": cid, "initialNumSteps": 3, "lastUserInput": "Continue after compact"},
            state_root=root,
        )
        if not rec_msg or "fold_station_event" not in rec_msg or "src/ingest/fold_avro.rs" not in rec_msg:
            sys.stderr.write(f"[compact_state_guard FAIL] Missing symbols/files in recovery message: {rec_msg}\n")
            return 1
        second_msg = check_pre_invocation_compaction(
            {"conversationId": cid, "initialNumSteps": 4, "lastUserInput": "Next step"},
            state_root=root,
        )
        if second_msg is not None:
            sys.stderr.write("[compact_state_guard FAIL] Recovery message fired twice without new compaction\n")
            return 1
        proactive_msg = check_pre_invocation_compaction(
            {"conversationId": cid, "lastUserInput": "Continue work"},
            state_root=root,
            total_tx_steps=70,
        )
        cap_file = root / f"rewind_capsule_{cid}.md"
        if not proactive_msg or "[KAIZEN AUTO-REWIND & COMPACT ENGINE" not in proactive_msg or not cap_file.is_file():
            sys.stderr.write(f"[compact_state_guard FAIL] Proactive auto-rewind did not trigger cleanly: {proactive_msg}\n")
            return 1
        quiet_msg = check_pre_invocation_compaction(
            {"conversationId": cid, "lastUserInput": "Next work"},
            state_root=root,
            total_tx_steps=72,
        )
        if quiet_msg is not None:
            sys.stderr.write(f"[compact_state_guard FAIL] Proactive watermark spammed at step 72: {quiet_msg}\n")
            return 1
    print("  [PASS] compact_state_guard -- one-shot compaction recovery & proactive auto-rewind verified")
    return 0


def format_capc_suffix_hook(
    message: str,
    *,
    reason: str = "threshold_breach",
    hook_name: str = "compact_state_guard",
) -> Dict[str, Any]:
    """Wrap dynamic hook context as a CAPC cache-safe suffix payload."""
    return {
        "text": message,
        "position": "suffix",
        "capc_cache_safe": True,
        "reason": reason,
        "hook": hook_name,
    }


def _emit_ascii_json(obj: Dict[str, Any]) -> None:
    encoded = (json.dumps(obj, ensure_ascii=True) + "\n").encode("ascii")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main(argv: Optional[List[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--self-test" in args:
        return run_self_test()

    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            _emit_ascii_json({})
            return 0
    except Exception:
        _emit_ascii_json({})
        return 0

    if "hook_event_name" in payload:
        msg_cc = handle_claude_compact_event(payload)
        if msg_cc:
            _emit_ascii_json(
                {
                    "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg_cc},
                    "position": "suffix",
                    "capc_cache_safe": True,
                }
            )
            return 0
        _emit_ascii_json({})
        return 0

    if "toolCall" in payload or "toolName" in payload:
        record_post_tool(payload)
        _emit_ascii_json({})
        return 0

    msg = check_pre_invocation_compaction(payload)
    if msg:
        _emit_ascii_json(
            {
                "injectSteps": [{"systemMessage": {"systemMessage": msg}}],
                "position": "suffix",
                "capc_cache_safe": True,
            }
        )
        return 0

    _emit_ascii_json({})
    return 0


if __name__ == "__main__":
    sys.exit(main())
