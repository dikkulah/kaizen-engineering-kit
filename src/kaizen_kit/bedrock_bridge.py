#!/usr/bin/env python3
"""
Kaizen AWS Bedrock Claude Bridge (`bedrock_bridge.py`).

Exposes an OpenAI-compatible `/v1/chat/completions` and `/v1/models` server on `127.0.0.1:18889`.
Routes requests directly to Kaizen AWS Bedrock Claude models in `eu-west-1` via `boto3`
using the official `kaizen-claude` AWS profile (`KAIZEN_CLAUDE_PROFILE` override supported).

Keeps 100% of proprietary source code and telemetry within the Kaizen AWS tenant boundary.
"""

import argparse
import http.server
import json
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_KIT_SRC = Path(__file__).resolve().parent.parent
if str(_KIT_SRC) not in sys.path:
    sys.path.insert(0, str(_KIT_SRC))

from kaizen_kit.paths import resolve_state_dir

try:
    import boto3
    from botocore.config import Config
except ImportError:
    boto3 = None  # type: ignore
    Config = None  # type: ignore

DEFAULT_PORT = int(os.environ.get("KAIZEN_BEDROCK_PORT", "18889"))
DEFAULT_HOST = "127.0.0.1"
AWS_REGION = os.environ.get("AWS_REGION", "eu-west-1")
AWS_PROFILE = os.environ.get("KAIZEN_CLAUDE_PROFILE", "kaizen-claude")

BEDROCK_MODELS: Dict[str, str] = {
    "sonnet": "arn:aws:bedrock:eu-west-1:111122223333:application-inference-profile/kc24iucvezii",
    "claude-sonnet-5-5": "arn:aws:bedrock:eu-west-1:111122223333:application-inference-profile/kc24iucvezii",
    "haiku": "arn:aws:bedrock:eu-west-1:111122223333:application-inference-profile/eqo5vwaht638",
    "claude-haiku-5-5": "arn:aws:bedrock:eu-west-1:111122223333:application-inference-profile/eqo5vwaht638",
    "opus": "arn:aws:bedrock:eu-west-1:111122223333:application-inference-profile/4hvxx85xmzh0",
    "claude-opus-5-5": "arn:aws:bedrock:eu-west-1:111122223333:application-inference-profile/4hvxx85xmzh0",
    "fable": "arn:aws:bedrock:eu-west-1:111122223333:application-inference-profile/5nduit33hj5o",
    "claude-fable-5": "arn:aws:bedrock:eu-west-1:111122223333:application-inference-profile/5nduit33hj5o",
}


def _pid_file(port: int = DEFAULT_PORT, pid_file: Optional[Path] = None) -> Path:
    if pid_file is not None:
        return pid_file
    env_pf = os.environ.get("KAIZEN_BEDROCK_PID_FILE", "").strip()
    if env_pf:
        return Path(env_pf).expanduser().resolve()
    if port == DEFAULT_PORT:
        return resolve_state_dir() / "bedrock_bridge.pid"
    return resolve_state_dir() / f"bedrock_bridge_{port}.pid"


class BedrockClientHolder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client = None

    def get_client(self):
        with self._lock:
            if self._client is None:
                if boto3 is None:
                    raise RuntimeError(
                        "boto3 is required for Kaizen Bedrock Bridge. Install via `pip install boto3` or `pip install '.[bedrock]'`."
                    )
                session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
                boto_cfg = Config(
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=15,
                    read_timeout=180,
                )
                self._client = session.client("bedrock-runtime", config=boto_cfg)
            return self._client


CLIENT_HOLDER = BedrockClientHolder()


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content) if content is not None else ""


def _extract_content_blocks_with_cache(content: Any, for_system: bool = False) -> List[Dict[str, Any]]:
    """Extracts Bedrock text blocks + `cachePoint` markers (`1h` or `5m` TTL) from OpenAI/Anthropic content."""
    if isinstance(content, str):
        if not content.strip() and for_system:
            return []
        blocks: List[Dict[str, Any]] = [{"text": content}] if content else []
        if for_system and len(content) >= 4096:
            blocks.append({"cachePoint": {"type": "default", "ttl": "1h"}})
        return blocks

    if isinstance(content, list):
        out: List[Dict[str, Any]] = []
        for item in content:
            if isinstance(item, dict):
                txt = str(item.get("text", "")) if ("text" in item or item.get("type") == "text") else ""
                if txt:
                    out.append({"text": txt})
                cc = item.get("cache_control") or item.get("cachePoint")
                if cc:
                    ttl = cc.get("ttl", "1h") if isinstance(cc, dict) else "1h"
                    cp: Dict[str, Any] = {"type": "default"}
                    if ttl in ("5m", "1h"):
                        cp["ttl"] = ttl
                    out.append({"cachePoint": cp})
            elif isinstance(item, str) and item:
                out.append({"text": item})
        return out

    s = str(content) if content is not None else ""
    return [{"text": s}] if s else []


def _parse_tool_call_input(raw_args: Any) -> Dict[str, Any]:
    if raw_args is None or raw_args == "":
        return {}
    if isinstance(raw_args, dict):
        return raw_args
    if not isinstance(raw_args, str):
        return {"value": raw_args}
    try:
        parsed = json.loads(raw_args)
    except Exception:
        return {"raw": raw_args}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _append_converse_message(
    converse_messages: List[Dict[str, Any]],
    role: str,
    blocks: List[Dict[str, Any]],
) -> None:
    if converse_messages and converse_messages[-1]["role"] == role:
        converse_messages[-1]["content"].extend(blocks)
        return
    converse_messages.append({"role": role, "content": blocks})


def _build_tool_use_blocks(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        blocks.append({
            "toolUse": {
                "toolUseId": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                "name": fn.get("name", "tool"),
                "input": _parse_tool_call_input(fn.get("arguments")),
            }
        })
    return blocks


def _build_tool_result_block(msg: Dict[str, Any]) -> Dict[str, Any]:
    tool_call_id = msg.get("tool_call_id") or "tool_call_1"
    raw_content = msg.get("content")
    if isinstance(raw_content, dict):
        content_text = json.dumps(raw_content, default=str)
    else:
        content_text = _extract_text(raw_content)
    return {
        "toolResult": {
            "toolUseId": tool_call_id,
            "content": [{"text": content_text or "OK"}],
        }
    }


def _append_openai_msg_to_converse(
    msg: Dict[str, Any],
    system_prompts: List[Dict[str, Any]],
    converse_messages: List[Dict[str, Any]],
) -> None:
    role = msg.get("role", "user")
    if role in ("system", "developer"):
        system_prompts.extend(_extract_content_blocks_with_cache(msg.get("content"), for_system=True))
        return
    if role == "tool":
        _append_converse_message(converse_messages, "user", [_build_tool_result_block(msg)])
        return
    bedrock_role = "assistant" if role == "assistant" else "user"
    blocks = _extract_content_blocks_with_cache(msg.get("content"), for_system=False)
    if role == "assistant" and msg.get("tool_calls"):
        blocks.extend(_build_tool_use_blocks(msg["tool_calls"]))
    _append_converse_message(converse_messages, bedrock_role, blocks or [{"text": " "}])


def _build_bedrock_tool_config(tools_in: Any) -> Optional[Dict[str, Any]]:
    if not tools_in or not isinstance(tools_in, list):
        return None
    bedrock_tools: List[Dict[str, Any]] = []
    for item in tools_in:
        fn = item.get("function", item)
        name = fn.get("name")
        if not name:
            continue
        raw_schema = fn.get("parameters")
        schema = dict(raw_schema) if isinstance(raw_schema, dict) and raw_schema else {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        bedrock_tools.append({
            "toolSpec": {
                "name": name,
                "description": fn.get("description") or f"Tool {name}",
                "inputSchema": {"json": schema},
            }
        })
    return {"tools": bedrock_tools} if bedrock_tools else None


def _resolve_bedrock_arn(req_body: Dict[str, Any], model_alias: str) -> str:
    target_alias = (model_alias or req_body.get("model") or "sonnet").lower()
    arn = BEDROCK_MODELS.get(target_alias, target_alias)
    if arn.startswith("arn:aws:bedrock:") or arn.startswith("anthropic."):
        return arn
    return BEDROCK_MODELS["sonnet"]


def openai_to_bedrock_converse(req_body: Dict[str, Any], model_alias: str = "") -> Tuple[str, Dict[str, Any]]:
    system_prompts: List[Dict[str, Any]] = []
    converse_messages: List[Dict[str, Any]] = []
    for msg in req_body.get("messages", []):
        _append_openai_msg_to_converse(msg, system_prompts, converse_messages)

    if not converse_messages:
        converse_messages.append({"role": "user", "content": [{"text": "Hello"}]})

    max_tokens = int(req_body.get("max_tokens") or req_body.get("max_completion_tokens") or 8192)
    inference_cfg: Dict[str, Any] = {"maxTokens": max_tokens}
    if req_body.get("temperature") is not None:
        inference_cfg["temperature"] = float(req_body["temperature"])

    resp_fmt = req_body.get("response_format")
    if isinstance(resp_fmt, dict):
        fmt_type = str(resp_fmt.get("type") or "").strip().lower()
        if fmt_type == "json_schema":
            js_block = resp_fmt.get("json_schema") or {}
            schema_obj = js_block.get("schema") if isinstance(js_block, dict) else None
            schema_note = (
                "Return ONLY valid JSON conforming strictly to this JSON Schema (no markdown fences): "
                + json.dumps(schema_obj, ensure_ascii=False)
                if schema_obj
                else "Return ONLY valid JSON conforming strictly to the requested JSON Schema (no markdown fences)."
            )
            system_prompts.append({"text": schema_note})
        elif fmt_type == "json_object":
            system_prompts.append({"text": "Return ONLY a valid JSON object (no markdown fences)."})

    converse_kwargs: Dict[str, Any] = {"messages": converse_messages, "inferenceConfig": inference_cfg}
    if system_prompts:
        converse_kwargs["system"] = system_prompts
    tool_cfg = _build_bedrock_tool_config(req_body.get("tools"))
    if tool_cfg:
        converse_kwargs["toolConfig"] = tool_cfg
    return _resolve_bedrock_arn(req_body, model_alias), converse_kwargs


def _downgrade_or_strip_cache_points(converse_kwargs: Dict[str, Any], strip_all: bool = False) -> bool:
    changed = False

    def _process_list(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        nonlocal changed
        out: List[Dict[str, Any]] = []
        for blk in items:
            if "cachePoint" in blk:
                if strip_all:
                    changed = True
                    continue
                cp = dict(blk["cachePoint"])
                if "ttl" in cp:
                    cp.pop("ttl", None)
                    changed = True
                out.append({"cachePoint": cp})
            else:
                out.append(blk)
        return out

    if "system" in converse_kwargs and isinstance(converse_kwargs["system"], list):
        converse_kwargs["system"] = _process_list(converse_kwargs["system"])
    for m in converse_kwargs.get("messages", []):
        if isinstance(m.get("content"), list):
            m["content"] = _process_list(m["content"])
    return changed


def _retry_without_ttl(client: Any, arn: str, converse_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return client.converse(modelId=arn, **converse_kwargs)
    except Exception:
        _downgrade_or_strip_cache_points(converse_kwargs, strip_all=True)
        return client.converse(modelId=arn, **converse_kwargs)


def _call_converse_with_fallback(client: Any, arn: str, converse_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return client.converse(modelId=arn, **converse_kwargs)
    except Exception as exc:
        err_msg = str(exc)
        low_err = err_msg.lower()
        if ("cachepoint" in low_err or "ttl" in low_err) and _downgrade_or_strip_cache_points(converse_kwargs, strip_all=False):
            return _retry_without_ttl(client, arn, converse_kwargs)
        if "cachepoint" in low_err and _downgrade_or_strip_cache_points(converse_kwargs, strip_all=True):
            return client.converse(modelId=arn, **converse_kwargs)
        if "ExpiredTokenException" in err_msg or "security token" in low_err:
            raise RuntimeError(
                f"AWS Bedrock Token Expired for profile '{AWS_PROFILE}'. Run `aws sso login --profile {AWS_PROFILE}`."
            ) from exc
        raise


def _extract_output_parts(output_msg: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, Any]]]:
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for blk in output_msg.get("content", []):
        if "text" in blk:
            text_parts.append(blk["text"])
            continue
        if "toolUse" not in blk:
            continue
        tu = blk["toolUse"]
        tool_input = tu.get("input") if tu.get("input") is not None else {}
        tool_calls.append({
            "id": tu.get("toolUseId") or f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": tu.get("name", ""), "arguments": json.dumps(tool_input)},
        })
    return text_parts, tool_calls


def _bedrock_response_to_openai(resp_data: Dict[str, Any], model_alias: str) -> Dict[str, Any]:
    output_msg = resp_data.get("output", {}).get("message", {})
    text_parts, tool_calls = _extract_output_parts(output_msg)
    stop_reason = resp_data.get("stopReason", "end_turn")
    finish_reason = "tool_calls" if (stop_reason == "tool_use" or tool_calls) else "stop"
    usage = resp_data.get("usage", {})
    in_tok = int(usage.get("inputTokens", 0))
    out_tok = int(usage.get("outputTokens", 0))
    cache_read_tok = int(usage.get("cacheReadInputTokens", 0))
    cache_write_tok = int(usage.get("cacheWriteInputTokens", 0))
    assistant_msg: Dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else None}
    if tool_calls:
        assistant_msg["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_alias or "sonnet",
        "choices": [{"index": 0, "message": assistant_msg, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": in_tok,
            "completion_tokens": out_tok,
            "total_tokens": in_tok + out_tok + cache_read_tok + cache_write_tok,
            "cache_read_input_tokens": cache_read_tok,
            "cache_write_input_tokens": cache_write_tok,
        },
    }


def invoke_bedrock(req_body: Dict[str, Any], model_alias: str = "") -> Dict[str, Any]:
    client = CLIENT_HOLDER.get_client()
    arn, converse_kwargs = openai_to_bedrock_converse(req_body, model_alias=model_alias)
    resp_data = _call_converse_with_fallback(client, arn, converse_kwargs)
    return _bedrock_response_to_openai(resp_data, model_alias)


class BedrockBridgeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[BedrockBridge] {self.address_string()} - {fmt % args}\n")

    def do_GET(self) -> None:
        if self.path in ("/v1/models", "/models"):
            models_list = [{"id": m, "object": "model", "owned_by": "kaizen-aws-bedrock"} for m in BEDROCK_MODELS]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"object": "list", "data": models_list}).encode("utf-8"))
            return
        if self.path in ("/health", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "profile": AWS_PROFILE, "region": AWS_REGION}).encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path in ("/v1/chat/completions", "/chat/completions"):
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                req_data = json.loads(self.rfile.read(content_len).decode("utf-8"))
                resp_json = invoke_bedrock(req_data, model_alias=req_data.get("model", "sonnet"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(resp_json).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": str(e), "type": "bedrock_error"}}).encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def ensure_bridge_running(port: int = DEFAULT_PORT) -> str:
    url = f"http://{DEFAULT_HOST}:{port}"
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=1.0) as resp:
            if resp.status == 200:
                return f"{url}/v1"
    except Exception:
        pass
    cmd_start(port)
    time.sleep(0.6)
    return f"{url}/v1"


def _is_port_open(port: int, host: str = DEFAULT_HOST, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _is_bedrock_health(port: int, host: str = DEFAULT_HOST, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return isinstance(data, dict) and data.get("status") == "ok" and "region" in data and "profile" in data
    except Exception:
        return False


def _proc_net_listening_pids(port: int) -> List[int]:
    """Zero-lsof Linux fallback scanning `/proc/net/tcp{,6}` + `/proc/<pid>/fd`."""
    my_pid = os.getpid()
    inodes: set[str] = set()
    hex_port = f"{int(port):04X}"
    for net_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        if not os.path.isfile(net_file):
            continue
        try:
            with open(net_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f.readlines()[1:]:
                    cols = line.split()
                    if len(cols) >= 10 and cols[3] == "0A" and cols[1].upper().endswith(f":{hex_port}"):
                        inodes.add(cols[9])
        except OSError:
            continue
    if not inodes:
        return []
    found: List[int] = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid <= 0 or pid == my_pid:
                continue
            fd_dir = f"/proc/{entry}/fd"
            try:
                for fd_name in os.listdir(fd_dir):
                    target = os.readlink(f"{fd_dir}/{fd_name}")
                    if target.startswith("socket:[") and target[8:-1] in inodes:
                        found.append(pid)
                        break
            except OSError:
                continue
    except OSError:
        pass
    return found


def _find_listening_pids(port: int) -> List[int]:
    my_pid = os.getpid()
    pids: List[int] = []
    if sys.platform == "win32":
        pat = re.compile(
            rf"^\s*TCP\s+(?:127\.0\.0\.1|0\.0\.0\.0|\[::1?\]):{int(port)}\s+"
            r"(?:0\.0\.0\.0:0|\*:0|\[::\]:0)\s+\S+\s+(\d+)\s*$",
            re.IGNORECASE,
        )
        try:
            proc = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10)
            for line in (proc.stdout or "").splitlines():
                m = pat.match(line)
                if m:
                    pid = int(m.group(1))
                    if pid > 0 and pid != my_pid and pid not in pids:
                        pids.append(pid)
        except Exception:
            pass
    else:
        try:
            proc = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for tok in (proc.stdout or "").split():
                if tok.isdigit():
                    pid = int(tok)
                    if pid > 0 and pid != my_pid and pid not in pids:
                        pids.append(pid)
        except Exception:
            pass
        if not pids:
            for pid in _proc_net_listening_pids(port):
                if pid not in pids:
                    pids.append(pid)
    return pids


def _win32_descendant_pids(root_pid: int) -> List[int]:
    if sys.platform != "win32" or root_pid <= 0:
        return []
    try:
        import ctypes
        import ctypes.wintypes as wt

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wt.DWORD),
                ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
        k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32FirstW.restype = wt.BOOL
        k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.restype = wt.BOOL
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.CloseHandle.restype = wt.BOOL

        invalid_handle = ctypes.c_void_p(-1).value
        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
        if not snap or snap == invalid_handle:
            return []
        children_map: Dict[int, List[int]] = {}
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if k32.Process32FirstW(snap, ctypes.byref(pe)):
                while True:
                    children_map.setdefault(int(pe.th32ParentProcessID), []).append(int(pe.th32ProcessID))
                    if not k32.Process32NextW(snap, ctypes.byref(pe)):
                        break
        finally:
            k32.CloseHandle(snap)

        descendants: List[int] = []
        queue = list(children_map.get(root_pid, []))
        seen = {root_pid}
        while queue:
            cur = queue.pop(0)
            if cur in seen or cur <= 0:
                continue
            seen.add(cur)
            descendants.append(cur)
            queue.extend(children_map.get(cur, []))
        return descendants
    except Exception:
        return []


def _terminate_single_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
            k32.OpenProcess.restype = wt.HANDLE
            k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
            k32.TerminateProcess.restype = wt.BOOL
            k32.CloseHandle.argtypes = [wt.HANDLE]
            k32.CloseHandle.restype = wt.BOOL

            handle = k32.OpenProcess(0x0001, False, int(pid))
            if handle:
                try:
                    k32.TerminateProcess(handle, 1)
                finally:
                    k32.CloseHandle(handle)
                return
        except Exception:
            pass
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def _terminate_pid_tree(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    for child_pid in reversed(_win32_descendant_pids(pid)):
        _terminate_single_pid(child_pid)
    _terminate_single_pid(pid)


def cmd_serve(port: int = DEFAULT_PORT, pid_file: Optional[Path] = None) -> int:
    server = ThreadingHTTPServer((DEFAULT_HOST, port), BedrockBridgeHandler)
    pf = _pid_file(port, pid_file)
    try:
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    print(f"[BedrockBridge] Listening on http://{DEFAULT_HOST}:{port} (profile={AWS_PROFILE}, region={AWS_REGION})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_start(port: int = DEFAULT_PORT, pid_file: Optional[Path] = None) -> int:
    pf = _pid_file(port, pid_file)
    pf.parent.mkdir(parents=True, exist_ok=True)
    if _is_port_open(port, timeout=0.5):
        listeners = _find_listening_pids(port)
        if listeners:
            pf.write_text(str(listeners[0]), encoding="utf-8")
        print(f"[BedrockBridge] Already listening on http://{DEFAULT_HOST}:{port}")
        return 0
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    env = dict(os.environ)
    src_root = str(Path(__file__).resolve().parent.parent)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src_root}{os.pathsep}{existing_pp}" if existing_pp else src_root
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "serve", "--port", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
        env=env,
    )
    pf.write_text(str(proc.pid), encoding="utf-8")
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if _is_port_open(port, timeout=0.15):
            listeners = _find_listening_pids(port)
            if listeners:
                pf.write_text(str(listeners[0]), encoding="utf-8")
            break
        time.sleep(0.08)
    print(f"[BedrockBridge] Started on http://{DEFAULT_HOST}:{port} (PID={pf.read_text(encoding='utf-8').strip()})")
    return 0


def cmd_stop(port: int = DEFAULT_PORT, pid_file: Optional[Path] = None) -> int:
    pf = _pid_file(port, pid_file)
    had_pf = pf.exists()
    recorded_pid: Optional[int] = None
    if had_pf:
        try:
            pid_str = pf.read_text(encoding="utf-8").strip()
            if pid_str.isdigit():
                recorded_pid = int(pid_str)
        except OSError:
            pass
        pf.unlink(missing_ok=True)

    port_is_bedrock = _is_bedrock_health(port) or (port == DEFAULT_PORT and _is_port_open(port, timeout=0.2))
    listening_pids = _find_listening_pids(port) if port_is_bedrock else []

    target_pids: List[int] = []
    if recorded_pid and recorded_pid > 0:
        descendants = set(_win32_descendant_pids(recorded_pid))
        if recorded_pid in listening_pids or bool(descendants.intersection(listening_pids)) or (port_is_bedrock and not listening_pids) or not _is_port_open(port, timeout=0.15):
            # Only trust recorded_pid when port is not open or belongs to Bedrock (prevents killing unrelated port owners)
            if port_is_bedrock or recorded_pid in listening_pids or bool(descendants.intersection(listening_pids)):
                target_pids.append(recorded_pid)
    for lpid in listening_pids:
        if lpid not in target_pids:
            target_pids.append(lpid)

    if not target_pids:
        print("[BedrockBridge] Not running.")
        return 0

    for tpid in target_pids:
        _terminate_pid_tree(tpid)

    deadline = time.time() + 3.0
    while time.time() < deadline and _is_port_open(port, timeout=0.1):
        for rem_pid in _find_listening_pids(port):
            _terminate_pid_tree(rem_pid)
            if rem_pid not in target_pids:
                target_pids.append(rem_pid)
        time.sleep(0.08)

    print(f"[BedrockBridge] Stopped PID(s): {', '.join(str(p) for p in target_pids)}.")
    return 0


def cmd_status(port: int = DEFAULT_PORT) -> int:
    try:
        with urllib.request.urlopen(f"http://{DEFAULT_HOST}:{port}/health", timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"[BedrockBridge] HEALTHY on port {port}: {data}")
            return 0
    except Exception as e:
        print(f"[BedrockBridge] OFFLINE on port {port} ({e})")
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kaizen AWS Bedrock OpenAI-Compatible Bridge")
    sub = ap.add_subparsers(dest="command")
    for name in ("serve", "start", "stop", "status"):
        p = sub.add_parser(name)
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    cmd = args.command or "status"
    if cmd == "serve":
        return cmd_serve(args.port)
    if cmd == "start":
        return cmd_start(args.port)
    if cmd == "stop":
        return cmd_stop(args.port)
    return cmd_status(getattr(args, "port", DEFAULT_PORT))


if __name__ == "__main__":
    sys.exit(main())
