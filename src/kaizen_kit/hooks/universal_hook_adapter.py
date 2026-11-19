#!/usr/bin/env python3
"""
Universal Multi-Harness Bidirectional Lifecycle Hook Adapter (`universal_hook_adapter.py`).

Normalizes lifecycle events & tool payloads without vendor lock-in across:
  1. `claude`      — Native Claude Code CLI (`settings.json` hooks)
  2. `codex`       — OpenAI Codex CLI (`~/.codex/` shell / apply_patch / function hooks)
  3. `opencode`    — OpenCode CLI (`~/.config/opencode/` plugin & tool hooks)
  4. `cursor`      — Cursor IDE (`.cursor/hooks.json` beforeShellExecution / beforeFileEdit / stop)
  5. `antigravity` — Antigravity 2.0 (`hooks.json` 6-event lifecycle)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_KIT_SRC = Path(__file__).resolve().parent.parent.parent
if str(_KIT_SRC) not in sys.path:
    sys.path.insert(0, str(_KIT_SRC))

from kaizen_kit.hooks import compact_state_guard, engine

SUPPORTED_HOOK_RUNTIMES: Tuple[str, ...] = (
    "auto",
    "claude",
    "codex",
    "opencode",
    "cursor",
    "antigravity",
)


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {}
    return {}


def _extract_shell_command(raw_cmd: Any) -> str:
    if isinstance(raw_cmd, str):
        return raw_cmd
    if isinstance(raw_cmd, (list, tuple)):
        parts = [str(x) for x in raw_cmd if x is not None]
        if len(parts) >= 3 and parts[0] in ("bash", "sh", "/bin/bash", "/bin/sh") and parts[1] in ("-c", "-lc"):
            return parts[2]
        return " ".join(parts)
    return ""


# ==============================================================================
# Claude Code <-> Canonical Tool Normalization
# ==============================================================================

def normalize_claude_tool(tool_name: str, tool_input: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    t = str(tool_name or "").strip()
    inp = _as_dict(tool_input)

    if t in ("Bash", "PowerShell"):
        return "run_command", {"CommandLine": _extract_shell_command(inp.get("command"))}

    if t == "Write":
        return "write_file", {
            "Path": str(inp.get("file_path") or inp.get("path") or ""),
            "Content": str(inp.get("content") or ""),
        }

    if t in ("Edit", "MultiEdit"):
        if "edits" in inp and isinstance(inp["edits"], list):
            joined_new = "\n".join(str(e.get("new_string", "")) for e in inp["edits"] if isinstance(e, dict))
        else:
            joined_new = str(inp.get("new_string") or inp.get("content") or "")
        return "edit_file", {
            "Path": str(inp.get("file_path") or inp.get("path") or ""),
            "Replacement": joined_new,
        }

    if t == "Read":
        return "read_file", {
            "Path": str(inp.get("file_path") or inp.get("path") or ""),
            "offset": inp.get("offset"),
            "limit": inp.get("limit"),
        }

    if t in ("Task", "Agent"):
        return "invoke_subagent", {
            "TypeName": str(inp.get("subagent_type") or inp.get("agent") or ""),
            "Prompt": str(inp.get("prompt") or inp.get("description") or ""),
            "Model": str(inp.get("model") or ""),
        }

    return t, dict(inp)


def denormalize_claude_updated_input(
    orig_tool: str, orig_input: Dict[str, Any], updated_canonical: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    if not updated_canonical:
        return None
    out = dict(orig_input)
    if orig_tool in ("Task", "Agent"):
        if "TypeName" in updated_canonical:
            out["subagent_type"] = updated_canonical["TypeName"]
        if "Prompt" in updated_canonical and "prompt" in out:
            out["prompt"] = updated_canonical["Prompt"]
        if updated_canonical.get("Model"):
            out["model"] = updated_canonical["Model"]
        return out
    if orig_tool in ("Bash", "PowerShell") and "CommandLine" in updated_canonical:
        cmd_out = str(updated_canonical["CommandLine"])
        if orig_tool == "Bash":
            if cmd_out.startswith("& "):
                cmd_out = cmd_out[2:]
            prefix_idx = cmd_out.find(" --verify-tag ")
            if prefix_idx > 0:
                cmd_out = cmd_out[:prefix_idx].replace("\\", "/") + cmd_out[prefix_idx:]
            cmd_out = cmd_out.replace(" --shell powershell ", " --shell bash ")
        out["command"] = cmd_out
        return out
    return None


def handle_claude_event(event_name: str, payload: Dict[str, Any]) -> Tuple[int, Optional[Dict[str, Any]], str]:
    """Returns `(exit_code, stdout_json_or_none, stderr_text)`."""
    payload = _as_dict(payload)
    session_id = str(payload.get("session_id") or "claude-session")
    cwd = str(payload.get("cwd") or "")
    ev = str(event_name or payload.get("hook_event_name") or "")

    if ev == "PreCompact":
        compact_state_guard.handle_claude_compact_event(payload)
        return 0, {}, ""

    if ev == "SessionStart":
        res = engine.evaluate_session_start(session_id, is_subagent=False)
        ctx = res.get("context", "")
        compact_ctx = compact_state_guard.handle_claude_compact_event(payload) or ""
        if compact_ctx:
            ctx = f"{ctx}\n\n{compact_ctx}".strip() if ctx else compact_ctx
        if "[MIRIX MEMORY CONTEXT" not in ctx:
            mirix_q = Path(cwd).name if cwd else "kaizen"
            mirix_ctx = compact_state_guard.fetch_mirix_context(mirix_q, budget_tokens=400, timeout_s=0.25)
            if mirix_ctx:
                ctx = f"{ctx}\n\n{mirix_ctx}".strip() if ctx else mirix_ctx
        if ctx:
            return 0, {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": ctx,
                }
            }, ""
        return 0, {}, ""

    if ev == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "")
        res = engine.evaluate_user_prompt(session_id, prompt)
        if res.get("blocked"):
            return 2, None, res.get("reason", "Blocked by security guard.")
        ctx = res.get("context", "")
        if ctx:
            return 0, {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": ctx,
                }
            }, ""
        return 0, {}, ""

    if ev == "PreToolUse":
        tool_name = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") or {}
        canonical_tool, canonical_args = normalize_claude_tool(tool_name, tool_input)
        res = engine.evaluate_pre_tool(
            session_id=session_id,
            root_session_id=session_id,
            canonical_tool=canonical_tool,
            args=canonical_args,
            cwd=cwd,
        )
        if not res["allow"]:
            return 0, {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": res["reason"],
                }
            }, ""
        updated_inp = denormalize_claude_updated_input(tool_name, tool_input, res.get("updated_args"))
        if updated_inp:
            return 0, {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": updated_inp,
                }
            }, ""
        return 0, {}, ""

    if ev == "PostToolUse":
        compact_state_guard.record_post_tool(payload)
        tool_name = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") or {}
        canonical_tool, canonical_args = normalize_claude_tool(tool_name, tool_input)
        res = engine.evaluate_post_tool(session_id, canonical_tool, canonical_args, had_error=False)
        warnings = res.get("warnings") or []
        if warnings:
            bullets = "\n".join(f"  - {w}" for w in warnings)
            return 0, {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"[Jidoka Real-Time Code Quality Sensor]\n{bullets}\n"
                        f"Resolve provenance/security violations immediately and keep nesting <= 1."
                    ),
                }
            }, ""
        return 0, {}, ""

    if ev in ("Stop", "SubagentStop"):
        stop_active = bool(payload.get("stop_hook_active", False))
        res = engine.evaluate_stop(session_id, stop_hook_active=stop_active)
        if not res["allow_stop"]:
            return 0, {"decision": "block", "reason": res["reason"]}, ""
        return 0, {}, ""

    return 0, {}, ""


# ==============================================================================
# Antigravity 2.0 Adapter
# ==============================================================================

def normalize_agy_tool(tool_name: str, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    t = str(tool_name or "").strip()
    safe_args = _as_dict(args)
    if t in ("write_file", "write_to_file"):
        return "write_file", {
            "Path": str(safe_args.get("Path") or safe_args.get("TargetFile") or ""),
            "Content": str(safe_args.get("Content") or safe_args.get("CodeContent") or ""),
        }
    if t in ("edit_file", "replace_file_content"):
        return "edit_file", {
            "Path": str(safe_args.get("Path") or safe_args.get("TargetFile") or ""),
            "Replacement": str(safe_args.get("Replacement") or safe_args.get("ReplacementContent") or ""),
        }
    if t in ("read_file", "view_file"):
        return "read_file", {"Path": str(safe_args.get("Path") or safe_args.get("AbsolutePath") or "")}
    return t, dict(safe_args)


def handle_antigravity_event(mode: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = _as_dict(payload)
    cid = str(payload.get("conversationId") or "agy-session")
    mode = str(mode or "").strip()

    if mode == "session_start":
        res = engine.evaluate_session_start(cid, is_subagent=False)
        ctx = res.get("context", "")
        ws_list = payload.get("workspacePaths")
        ws_q = Path(str(ws_list[0])).name if isinstance(ws_list, list) and ws_list else "kaizen"
        mirix_ctx = compact_state_guard.fetch_mirix_context(ws_q, budget_tokens=400, timeout_s=0.25)
        if mirix_ctx:
            ctx = f"{ctx}\n\n{mirix_ctx}".strip() if ctx else mirix_ctx
        if ctx:
            return {"injectSteps": [{"systemMessage": {"systemMessage": ctx}}]}
        return {}

    if mode == "pre_invocation":
        compact_msg = compact_state_guard.check_pre_invocation_compaction(payload)
        user_input = str(payload.get("lastUserInput") or "")
        if not user_input:
            if compact_msg:
                return {"injectSteps": [{"systemMessage": {"systemMessage": compact_msg}}]}
            return {}
        res = engine.evaluate_user_prompt(cid, user_input)
        if res.get("blocked"):
            return {
                "injectSteps": [{"errorMessage": {"userMessage": res["reason"]}}],
                "terminationBehavior": "terminate",
            }
        ctx_parts = [p for p in (compact_msg, res.get("context") or "") if p]
        if ctx_parts:
            return {"injectSteps": [{"systemMessage": {"systemMessage": "\n\n".join(ctx_parts)}}]}
        return {}

    if mode == "pre_tool":
        tc = _as_dict(payload.get("toolCall"))
        tname = str(tc.get("name") or "")
        targs = _as_dict(tc.get("args"))
        can_tool, can_args = normalize_agy_tool(tname, targs)
        res = engine.evaluate_pre_tool(
            session_id=cid,
            root_session_id=cid,
            canonical_tool=can_tool,
            args=can_args,
        )
        if not res["allow"]:
            return {"decision": "deny", "reason": res["reason"]}
        if res.get("updated_args") and tname in ("invoke_subagent", "run_command"):
            merged = dict(targs)
            merged.update(res["updated_args"])
            return {"decision": "allow", "overwrite": merged}
        return {"decision": "allow"}

    if mode == "post_tool":
        compact_state_guard.record_post_tool(payload)
        tc = _as_dict(payload.get("toolCall"))
        tname = str(tc.get("name") or "")
        targs = _as_dict(tc.get("args"))
        can_tool, can_args = normalize_agy_tool(tname, targs)
        engine.evaluate_post_tool(cid, can_tool, can_args, had_error=bool(payload.get("error")))
        return {}

    if mode == "post_invocation":
        st = engine.load_session_state(cid)
        warns = st.get("pending_jidoka_warnings") or []
        if warns:
            st["pending_jidoka_warnings"] = []
            engine.save_session_state(cid, st)
            bullets = "\n".join(f"  - {w}" for w in warns)
            return {
                "injectSteps": [{
                    "systemMessage": {
                        "systemMessage": f"[Jidoka Real-Time Code Quality Sensor]\n{bullets}"
                    }
                }]
            }
        return {}

    if mode == "stop":
        if payload.get("terminationReason") != "NO_TOOL_CALL" or not payload.get("fullyIdle", True):
            return {"decision": "allow"}
        res = engine.evaluate_stop(cid, stop_hook_active=False)
        if not res["allow_stop"]:
            return {"decision": "continue", "reason": res["reason"]}
        return {"decision": "allow"}

    return {}


# ==============================================================================
# Codex / OpenCode / Cursor Multi-Harness Normalization
# ==============================================================================

def normalize_codex_tool(tool_name: Any, tool_input: Any) -> Tuple[str, Dict[str, Any]]:
    t = str(tool_name or "").strip()
    low = t.lower()
    inp = _as_dict(tool_input)
    if low in ("shell", "local_shell", "exec_command", "container.exec", "bash", "run_command"):
        raw_cmd = inp.get("command") if "command" in inp else inp.get("cmd")
        if raw_cmd is None and isinstance(tool_input, str) and not tool_input.strip().startswith("{"):
            raw_cmd = tool_input
        return "run_command", {"CommandLine": _extract_shell_command(raw_cmd)}
    if low in ("apply_patch", "patch_file", "edit_file", "edit"):
        patch_text = str(inp.get("patch") or inp.get("new_string") or inp.get("content") or "")
        path_str = str(inp.get("file_path") or inp.get("path") or "")
        return "edit_file", {"Path": path_str, "Replacement": patch_text}
    if low in ("write_file", "create_file", "write"):
        return "write_file", {
            "Path": str(inp.get("file_path") or inp.get("path") or ""),
            "Content": str(inp.get("content") or ""),
        }
    if low in ("read_file", "view_file", "read"):
        return "read_file", {"Path": str(inp.get("file_path") or inp.get("path") or "")}
    return t, dict(inp)


def normalize_opencode_tool(tool_name: Any, tool_input: Any) -> Tuple[str, Dict[str, Any]]:
    t = str(tool_name or "").strip()
    low = t.lower()
    inp = _as_dict(tool_input)
    file_path = str(inp.get("filePath") or inp.get("file_path") or inp.get("path") or "")
    if low in ("bash", "shell", "run_command"):
        return "run_command", {"CommandLine": _extract_shell_command(inp.get("command") or inp.get("cmd"))}
    if low in ("write", "write_file"):
        return "write_file", {"Path": file_path, "Content": str(inp.get("content") or "")}
    if low in ("edit", "multiedit", "patch", "edit_file"):
        repl = str(inp.get("newString") or inp.get("new_string") or inp.get("content") or "")
        return "edit_file", {"Path": file_path, "Replacement": repl}
    if low in ("read", "read_file"):
        return "read_file", {"Path": file_path, "offset": inp.get("offset"), "limit": inp.get("limit")}
    if low in ("task", "agent", "subagent"):
        return "invoke_subagent", {
            "TypeName": str(inp.get("subagent_type") or inp.get("agent") or ""),
            "Prompt": str(inp.get("prompt") or inp.get("description") or ""),
            "Model": str(inp.get("model") or ""),
        }
    return t, dict(inp)


def normalize_cursor_tool(event_name: str, tool_name: Any, payload_or_input: Any) -> Tuple[str, Dict[str, Any]]:
    ev = str(event_name or "").strip()
    t = str(tool_name or "").strip()
    inp = _as_dict(payload_or_input)
    if ev in ("beforeShellExecution", "afterShellExecution") or t.lower() in ("shell", "bash", "terminal"):
        return "run_command", {"CommandLine": _extract_shell_command(inp.get("command") or inp.get("cmd"))}
    if ev in ("beforeFileEdit", "afterFileEdit") or t.lower() in ("edit", "edit_file"):
        edits = inp.get("edits")
        if isinstance(edits, list):
            joined = "\n".join(
                str(e.get("new_string") or e.get("newText") or "") for e in edits if isinstance(e, dict)
            )
        else:
            joined = str(inp.get("new_string") or inp.get("content") or "")
        return "edit_file", {"Path": str(inp.get("file_path") or inp.get("path") or ""), "Replacement": joined}
    if ev == "beforeReadFile" or t.lower() in ("read", "read_file"):
        return "read_file", {"Path": str(inp.get("file_path") or inp.get("path") or "")}
    if t:
        return normalize_claude_tool(t, inp)
    return ev or "unknown", dict(inp)


def detect_hook_runtime(event_name: str, payload: Any) -> str:
    data = _as_dict(payload)
    ev = str(event_name or data.get("hook_event_name") or data.get("event") or data.get("type") or "").strip()

    explicit = str(data.get("harness") or data.get("runtime") or "").strip().lower()
    if explicit in ("claude", "codex", "opencode", "cursor", "antigravity"):
        return explicit

    if ev in (
        "beforeShellExecution",
        "afterShellExecution",
        "beforeMCPExecution",
        "beforeFileEdit",
        "afterFileEdit",
        "beforeReadFile",
        "beforeSubmitPrompt",
    ) or "cursor_version" in data:
        return "cursor"

    if "sessionID" in data or ev.startswith(("tool.execute.", "session.", "chat.")):
        return "opencode"

    if "thread_id" in data or ev == "agent-turn-complete" or str(data.get("tool") or "").lower() in ("shell", "local_shell", "apply_patch"):
        return "codex"

    if "conversationId" in data or "toolCall" in data or ev in ("pre_tool", "post_tool", "pre_invocation", "post_invocation"):
        return "antigravity"

    if "hook_event_name" in data or "session_id" in data or ev in (
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact", "Stop", "SubagentStop"
    ):
        return "claude"

    return "antigravity"


def _canonicalize_event_name(raw_event: str) -> str:
    ev = (raw_event or "").strip()
    low = ev.lower()
    if low in ("sessionstart", "session_start", "session.created"):
        return "session_start"
    if low in ("userpromptsubmit", "user_prompt", "pre_invocation", "beforesubmitprompt", "chat.message", "turn_start"):
        return "user_prompt"
    if low in (
        "pretooluse",
        "pre_tool",
        "pre_tool_use",
        "before_tool_call",
        "tool.execute.before",
        "beforeshellexecution",
        "beforefileedit",
        "beforereadfile",
        "beforemcpexecution",
    ):
        return "pre_tool"
    if low in (
        "posttooluse",
        "post_tool",
        "post_tool_use",
        "after_tool_call",
        "tool.execute.after",
        "aftershellexecution",
        "afterfileedit",
        "agent-turn-complete",
    ):
        return "post_tool"
    if low in ("precompact", "pre_compact"):
        return "pre_compact"
    if low in ("stop", "subagentstop", "session.idle", "turn_complete"):
        return "stop"
    return "unknown"


def _extract_raw_tool_fields(runtime: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    if runtime == "antigravity":
        tc = _as_dict(data.get("toolCall"))
        return str(tc.get("name") or ""), _as_dict(tc.get("args"))
    fn = _as_dict(data.get("function"))
    inp_nested = _as_dict(data.get("input"))
    out_nested = _as_dict(data.get("output"))
    raw_name = str(
        data.get("tool_name")
        or data.get("tool")
        or fn.get("name")
        or inp_nested.get("tool")
        or ""
    ).strip()
    raw_input_candidate = (
        data.get("tool_input")
        if data.get("tool_input") is not None
        else data.get("args")
        if data.get("args") is not None
        else data.get("arguments")
        if data.get("arguments") is not None
        else fn.get("arguments")
        if fn.get("arguments") is not None
        else out_nested.get("args")
        if out_nested.get("args") is not None
        else data
    )
    return raw_name, _as_dict(raw_input_candidate)


def normalize_hook_payload(
    payload: Any,
    runtime: str = "auto",
    event_name: str = "",
) -> Dict[str, Any]:
    """Normalize hook payloads across `claude`, `codex`, `opencode`, `cursor`, and `antigravity` without crashing."""
    data = _as_dict(payload)
    resolved_runtime = detect_hook_runtime(event_name, data) if (not runtime or runtime == "auto") else runtime.strip().lower()
    raw_ev = str(event_name or data.get("hook_event_name") or data.get("event") or data.get("type") or "").strip()
    canonical_event = _canonicalize_event_name(raw_ev)

    session_id = str(
        data.get("session_id")
        or data.get("sessionID")
        or data.get("thread_id")
        or data.get("conversation_id")
        or data.get("conversationId")
        or f"{resolved_runtime}-session"
    )
    ws_paths = data.get("workspacePaths")
    fallback_cwd = str(ws_paths[0]) if isinstance(ws_paths, list) and ws_paths else ""
    cwd = str(data.get("cwd") or data.get("workdir") or fallback_cwd or "")
    prompt = str(data.get("prompt") or data.get("lastUserInput") or data.get("message") or "")

    raw_tool_name, raw_tool_input = _extract_raw_tool_fields(resolved_runtime, data)
    if resolved_runtime == "codex":
        raw_arg = data.get("arguments") if data.get("arguments") is not None else (raw_tool_input or data)
        can_tool, can_args = normalize_codex_tool(raw_tool_name, raw_arg)
    elif resolved_runtime == "opencode":
        can_tool, can_args = normalize_opencode_tool(raw_tool_name, raw_tool_input)
    elif resolved_runtime == "cursor":
        can_tool, can_args = normalize_cursor_tool(raw_ev, raw_tool_name, raw_tool_input or data)
    elif resolved_runtime == "antigravity":
        can_tool, can_args = normalize_agy_tool(raw_tool_name, raw_tool_input)
    else:
        can_tool, can_args = normalize_claude_tool(raw_tool_name, raw_tool_input)

    return {
        "runtime": resolved_runtime,
        "canonical_event": canonical_event,
        "session_id": session_id,
        "cwd": cwd,
        "prompt": prompt,
        "canonical_tool": can_tool,
        "canonical_args": can_args,
        "raw_tool_name": raw_tool_name,
        "raw_tool_input": raw_tool_input,
    }


def handle_harness_event(
    runtime: str,
    event_name: str,
    payload: Any,
) -> Tuple[int, Optional[Dict[str, Any]], str]:
    """Unified multi-harness lifecycle handler that never crashes on missing or unknown fields."""
    data = _as_dict(payload)
    norm = normalize_hook_payload(data, runtime=runtime, event_name=event_name)
    resolved = norm["runtime"]

    if resolved == "claude":
        return handle_claude_event(event_name, data)
    if resolved == "antigravity":
        return 0, handle_antigravity_event(event_name or "pre_tool", data), ""

    ev = norm["canonical_event"]
    sid = norm["session_id"]
    if ev == "session_start":
        res = engine.evaluate_session_start(sid, is_subagent=False)
        ctx = res.get("context") or ""
        return 0, ({"additionalContext": ctx} if ctx else {}), ""

    if ev == "user_prompt":
        res = engine.evaluate_user_prompt(sid, norm["prompt"])
        if res.get("blocked"):
            reason = str(res.get("reason") or "Blocked by security guard.")
            if resolved == "cursor":
                return 0, {"continue": False, "permission": "deny", "userMessage": reason}, ""
            return 2, None, reason
        ctx = res.get("context") or ""
        return 0, ({"additionalContext": ctx} if ctx else {}), ""

    if ev == "pre_tool":
        res = engine.evaluate_pre_tool(
            session_id=sid,
            root_session_id=sid,
            canonical_tool=norm["canonical_tool"],
            args=norm["canonical_args"],
            cwd=norm["cwd"],
        )
        if not res["allow"]:
            reason = str(res.get("reason") or "Denied by Kaizen guard.")
            return 0, {"decision": "deny", "permission": "deny", "reason": reason, "userMessage": reason}, ""
        return 0, {"decision": "allow", "permission": "allow"}, ""

    if ev == "post_tool":
        res = engine.evaluate_post_tool(sid, norm["canonical_tool"], norm["canonical_args"], had_error=False)
        warns = res.get("warnings") or []
        if warns:
            return 0, {"warnings": warns}, ""
        return 0, {}, ""

    if ev == "stop":
        stop_active = bool(data.get("stop_hook_active", False))
        res = engine.evaluate_stop(sid, stop_hook_active=stop_active)
        if not res["allow_stop"]:
            return 0, {"decision": "block", "reason": res["reason"]}, ""
        return 0, {}, ""

    return 0, {}, ""


def _write_utf8(stream: Any, text: str) -> None:
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        buf.write(text.encode("utf-8", errors="replace"))
        buf.flush()
    else:
        stream.write(text)
        stream.flush()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kaizen Universal Lifecycle Hook Adapter")
    ap.add_argument(
        "--runtime",
        choices=list(SUPPORTED_HOOK_RUNTIMES),
        default="auto",
        help="Hook runtime dialect (auto-detects from payload if omitted)",
    )
    ap.add_argument(
        "event",
        nargs="?",
        default="",
        help="Event name (e.g. SessionStart, PreToolUse, pre_tool, beforeShellExecution, stop)",
    )
    args = ap.parse_args(argv)

    raw_bytes = sys.stdin.buffer.read() if getattr(sys.stdin, "buffer", None) is not None else sys.stdin.read().encode("utf-8", errors="replace")
    raw_in = raw_bytes.decode("utf-8", errors="replace")
    try:
        loaded = json.loads(raw_in) if raw_in.strip() else {}
        payload = loaded if isinstance(loaded, dict) else {}
    except Exception:
        payload = {}

    try:
        code, out_json, err_txt = handle_harness_event(args.runtime, args.event, payload)
    except Exception:
        code, out_json, err_txt = 0, {}, ""

    if err_txt:
        _write_utf8(sys.stderr, err_txt + "\n")
    if out_json is not None:
        _write_utf8(sys.stdout, json.dumps(out_json, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
