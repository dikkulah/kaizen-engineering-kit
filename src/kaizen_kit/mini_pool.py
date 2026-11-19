#!/usr/bin/env python3
"""EC2 32-core parallel mini-AI pool & Windows SSM loopback tunnel manager."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .localmodels import BitNetEmbedClient, JevClient, MiniPoolClient, _port_open, status as local_status
from .paths import resolve_state_dir

DEFAULT_INSTANCE_ID = os.environ.get("KAIZEN_EC2_INSTANCE_ID", "i-0123456789abcdef0")
DEFAULT_REGION = os.environ.get("KAIZEN_EC2_REGION", "eu-west-1")
DEFAULT_PROFILE = os.environ.get("KAIZEN_EC2_PROFILE", "kaizen-u2")
FASTMCP_PORT = 8770
TUNNEL_PORTS = (8765, 8766, 4000, 6070, 8770, 18808, 24280)
LEGACY_WORKER_TUNNEL_PORTS = (18800, 18801, 18802, 18803, 18804, 18805, 18806, 18807)


def _tunnel_state_path() -> Path:
    return resolve_state_dir() / "mini_pool_tunnels.json"


def _load_tunnels() -> Dict[str, int]:
    p = _tunnel_state_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.items()}
    except Exception:
        return {}


def _save_tunnels(pids: Dict[str, int]) -> None:
    _tunnel_state_path().write_text(json.dumps(pids, indent=2), encoding="utf-8")


def _win32_pid_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok and code.value == 259)
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win32_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _win_subprocess_kwargs() -> Dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _win32_collect_descendants(root_pid: int) -> List[int]:
    """Return `root_pid` and all living descendants in bottom-up (leaves-first) order."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_char * 260),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if snap in (0, -1, ctypes.c_void_p(-1).value):
        return [root_pid]

    children: Dict[int, List[int]] = {}
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        ok = kernel32.Process32First(snap, ctypes.byref(entry))
        while ok:
            children.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
            ok = kernel32.Process32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)

    order: List[int] = []
    seen = {root_pid}
    queue = [root_pid]
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for child in children.get(cur, ()):
            if child not in seen and child > 0:
                seen.add(child)
                queue.append(child)
    return list(reversed(order))


def _win32_terminate_pid(pid: int) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x0001 | 0x1000, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


def _win32_kill_process_tree(pid: int) -> None:
    for target_pid in _win32_collect_descendants(pid):
        _win32_terminate_pid(target_pid)


def _kill_pid(pid: int) -> None:
    if pid <= 0 or not _pid_alive(pid):
        return
    if sys.platform == "win32":
        _win32_kill_process_tree(pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _aws_cmd(profile: str, region: str, *args: str) -> List[str]:
    return ["aws", "--profile", profile, "--region", region, *args]


def ensure_ec2_running(instance_id: str, profile: str, region: str, timeout_s: int = 180) -> str:
    state = subprocess.check_output(
        _aws_cmd(
            profile,
            region,
            "ec2",
            "describe-instances",
            "--instance-ids",
            instance_id,
            "--query",
            "Reservations[0].Instances[0].State.Name",
            "--output",
            "text",
        ),
        text=True,
        **_win_subprocess_kwargs(),
    ).strip()
    if state != "running":
        subprocess.run(
            _aws_cmd(profile, region, "ec2", "start-instances", "--instance-ids", instance_id),
            check=True,
            **_win_subprocess_kwargs(),
        )
        subprocess.run(
            _aws_cmd(profile, region, "ec2", "wait", "instance-running", "--instance-ids", instance_id),
            check=True,
            **_win_subprocess_kwargs(),
        )

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ping = subprocess.check_output(
            _aws_cmd(
                profile,
                region,
                "ssm",
                "describe-instance-information",
                "--filters",
                f"Key=InstanceIds,Values={instance_id}",
                "--query",
                "InstanceInformationList[0].PingStatus",
                "--output",
                "text",
            ),
            text=True,
            **_win_subprocess_kwargs(),
        ).strip()
        if ping == "Online":
            return "Online"
        time.sleep(3)
    raise TimeoutError(f"SSM did not become Online for {instance_id} within {timeout_s}s")


OPTIONAL_TUNNEL_PORTS = (18081,)
BURST_TUNNEL_PORTS = (8000, 8001, 8002)


def start_burst_tunnels(
    instance_id: str,
    profile: str = DEFAULT_PROFILE,
    region: str = DEFAULT_REGION,
    ports: tuple[int, ...] = BURST_TUNNEL_PORTS,
) -> Dict[str, int]:
    """Forward ephemeral GPU Spot burst ports (8000..8002) over SSM without altering base TUNNEL_PORTS."""
    pending_cmds: Dict[int, List[str]] = {}
    active: Dict[str, int] = {}
    for port in ports:
        if _port_open(f"http://127.0.0.1:{port}"):
            continue
        params = json.dumps({"portNumber": [str(port)], "localPortNumber": [str(port)]})
        pending_cmds[port] = _aws_cmd(
            profile,
            region,
            "ssm",
            "start-session",
            "--target",
            instance_id,
            "--document-name",
            "AWS-StartPortForwardingSession",
            "--parameters",
            params,
        )
    wmi_pids = _win32_spawn_detached_tunnels(pending_cmds)
    for port, cmd in pending_cmds.items():
        key = str(port)
        if key in wmi_pids:
            active[key] = wmi_pids[key]
            continue
        creationflags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        active[key] = proc.pid
    (resolve_state_dir() / "burst_tunnels.json").write_text(json.dumps(active, indent=2), encoding="utf-8")
    return active


def _win32_spawn_detached_tunnels(port_cmds: Dict[int, List[str]]) -> Dict[str, int]:
    """Spawn SSM tunnel commands outside the caller's Windows Job Object via WMI."""
    if sys.platform != "win32" or not port_cmds:
        return {}
    tunnels_dir = resolve_state_dir() / "tunnels"
    tunnels_dir.mkdir(parents=True, exist_ok=True)
    aws_exe = shutil.which("aws") or "aws"
    local_bin = str(Path.home() / ".local" / "bin")

    items: List[Dict[str, str]] = []
    for port, raw_cmd in port_cmds.items():
        resolved = [aws_exe if i == 0 and part == "aws" else part for i, part in enumerate(raw_cmd)]
        runner = tunnels_dir / f"tunnel_{port}.cmd"
        log_file = tunnels_dir / f"tunnel_{port}.log"
        cmd_line = subprocess.list2cmdline(resolved)
        runner.write_text(
            "@echo off\r\n"
            f'set "PATH={local_bin};%PATH%"\r\n'
            f'{cmd_line} > "{log_file}" 2>&1\r\n',
            encoding="utf-8",
        )
        items.append({"port": str(port), "cmd": str(runner)})

    items_json = json.dumps(items)
    cwd_str = str(tunnels_dir).replace("'", "''")
    ps_script = (
        f"$items = '{items_json.replace(chr(39), chr(39)*2)}' | ConvertFrom-Json; "
        "$si = ([wmiclass]'Win32_ProcessStartup').CreateInstance(); $si.ShowWindow = 0; "
        "$out = @{}; "
        "foreach ($it in $items) { "
        "$cmd = 'cmd.exe /c \"' + $it.cmd + '\"'; "
        f"$r = ([wmiclass]'Win32_Process').Create($cmd, '{cwd_str}', $si); "
        "if ($r.ReturnValue -eq 0) { $out[$it.port] = [int]$r.ProcessId } "
        "}; "
        "$out | ConvertTo-Json -Compress"
    )
    res = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        check=False,
        **_win_subprocess_kwargs(),
    )
    if res.returncode != 0 or not res.stdout.strip():
        return {}
    try:
        parsed = json.loads(res.stdout.strip())
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): int(v) for k, v in parsed.items() if int(v) > 0}


def start_tunnels(
    instance_id: str,
    profile: str,
    region: str,
    include_bench: bool = False,
) -> Dict[str, int]:
    existing = _load_tunnels()
    active: Dict[str, int] = {}
    creationflags = (
        (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        if sys.platform == "win32"
        else 0
    )

    ports = (*TUNNEL_PORTS, *OPTIONAL_TUNNEL_PORTS) if include_bench else TUNNEL_PORTS
    pending_cmds: Dict[int, List[str]] = {}
    for port in ports:
        key = str(port)
        if _port_open(f"http://127.0.0.1:{port}"):
            if key in existing and _pid_alive(existing[key]):
                active[key] = existing[key]
            continue
        if key in existing and _pid_alive(existing[key]):
            _kill_pid(existing[key])

        params = json.dumps({"portNumber": [str(port)], "localPortNumber": [str(port)]})
        pending_cmds[port] = _aws_cmd(
            profile,
            region,
            "ssm",
            "start-session",
            "--target",
            instance_id,
            "--document-name",
            "AWS-StartPortForwardingSession",
            "--parameters",
            params,
        )

    wmi_pids = _win32_spawn_detached_tunnels(pending_cmds)
    for port, cmd in pending_cmds.items():
        key = str(port)
        if key in wmi_pids:
            active[key] = wmi_pids[key]
            continue
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        active[key] = proc.pid

    _save_tunnels(active)
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if all(_port_open(f"http://127.0.0.1:{p}") for p in TUNNEL_PORTS):
            break
        time.sleep(0.5)
    return active


def stop_tunnels() -> int:
    existing = _load_tunnels()
    count = 0
    for pid in existing.values():
        if _pid_alive(pid):
            _kill_pid(pid)
            count += 1
    _save_tunnels({})
    return count


def stop_ec2(instance_id: str, profile: str, region: str) -> None:
    subprocess.run(
        _aws_cmd(profile, region, "ec2", "stop-instances", "--instance-ids", instance_id),
        check=True,
        **_win_subprocess_kwargs(),
    )


def scale_workers(
    workers: int,
    instance_id: str = DEFAULT_INSTANCE_ID,
    profile: str = DEFAULT_PROFILE,
    region: str = DEFAULT_REGION,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if workers < 1 or workers > 10:
        raise ValueError(f"workers must be between 1 and 10, got {workers}")
    shell_cmd = f"python3 /opt/kaizen-mini-pool/autoscaler.py --scale {workers}"
    if dry_run:
        return {
            "status": "dry_run",
            "workers": workers,
            "instance_id": instance_id,
            "command": shell_cmd,
        }
    out = subprocess.check_output(
        _aws_cmd(
            profile,
            region,
            "ssm",
            "send-command",
            "--instance-ids",
            instance_id,
            "--document-name",
            "AWS-RunShellScript",
            "--parameters",
            json.dumps({"commands": [shell_cmd]}),
            "--query",
            "Command.CommandId",
            "--output",
            "text",
        ),
        text=True,
        **_win_subprocess_kwargs(),
    ).strip()
    return {"status": "scaled", "workers": workers, "instance_id": instance_id, "command_id": out}


def get_status() -> Dict[str, Any]:
    tunnels = _load_tunnels()
    tunnel_state = {
        port: {
            "pid": pid,
            "pid_alive": _pid_alive(pid),
            "port_open": _port_open(f"http://127.0.0.1:{port}"),
        }
        for port, pid in tunnels.items()
    }
    pool = MiniPoolClient()
    return {
        "tunnels": tunnel_state,
        "localmodels": local_status(),
        "pool": pool.pool_status() if pool.healthy() else None,
    }


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kaizen-kit mini-pool", description="Manage EC2 32-core parallel mini-AI pool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("up", "tunnel"):
        p = sub.add_parser(name)
        p.add_argument("--instance-id", default=DEFAULT_INSTANCE_ID)
        p.add_argument("--profile", default=DEFAULT_PROFILE)
        p.add_argument("--region", default=DEFAULT_REGION)
        p.add_argument("--include-bench", action="store_true", help="Also forward optional EC2 :18081 benchmark port")

    p_down = sub.add_parser("down")
    p_down.add_argument("--keep-ec2", action="store_true")
    p_down.add_argument("--instance-id", default=DEFAULT_INSTANCE_ID)
    p_down.add_argument("--profile", default=DEFAULT_PROFILE)
    p_down.add_argument("--region", default=DEFAULT_REGION)

    p_scale = sub.add_parser("scale")
    p_scale.add_argument("--workers", type=int, required=True, help="Target dynamic worker count (1..10)")
    p_scale.add_argument("--dry-run", action="store_true")
    p_scale.add_argument("--instance-id", default=DEFAULT_INSTANCE_ID)
    p_scale.add_argument("--profile", default=DEFAULT_PROFILE)
    p_scale.add_argument("--region", default=DEFAULT_REGION)

    sub.add_parser("status")

    p_sys = sub.add_parser("systemone")
    p_sys.add_argument("mode", choices=["choice", "score", "noul"])
    p_sys.add_argument("--state", required=True)
    p_sys.add_argument("--instructions", default="Evaluate the state.")
    p_sys.add_argument("--criteria", default="{}", help="JSON dict for choice mode")
    p_sys.add_argument("--rules", default="[]", help="JSON list for noul mode")

    p_ask = sub.add_parser("ask")
    p_ask.add_argument("prompt")
    p_ask.add_argument(
        "--model",
        default="mini-pool",
        help="Model alias (mini-pool, qwen3-30b-a3b, qwen3-coder-30b-a3b, qwen3.5-35b-a3b, glm-4.7-flash, ling-mini-2.0, gemma-4-26b-a4b, gemma-4-12b-unified, qwen3.5-4b, granite-4-h-tiny, bitnet-2b)",
    )
    p_ask.add_argument("--system", default="")
    p_ask.add_argument("--max-tokens", type=int, default=512)
    p_ask.add_argument("--json-schema", default="", help="Optional JSON Schema string for Lever A constrained decoding")

    p_mr = sub.add_parser("map-reduce")
    p_mr.add_argument("items", nargs="+")
    p_mr.add_argument("--system", default="Summarize concisely.")
    p_mr.add_argument(
        "--model",
        default="mini-pool",
        help="Model alias (mini-pool, qwen3-30b-a3b, qwen3-coder-30b-a3b, qwen3.5-35b-a3b, glm-4.7-flash, ling-mini-2.0, gemma-4-26b-a4b, gemma-4-12b-unified, qwen3.5-4b, granite-4-h-tiny, bitnet-2b)",
    )
    return parser


def _handle_systemone_command(args: argparse.Namespace) -> int:
    jev = JevClient()
    if args.mode == "choice":
        res = jev.choose(args.state, args.instructions, json.loads(args.criteria))
    elif args.mode == "score":
        res = jev.score(args.state, args.instructions)
    else:
        res = jev.noul(args.state, args.instructions, json.loads(args.rules))
    print(json.dumps(res.__dict__ if res else None, indent=2))
    return 0


def _handle_cli_command(args: argparse.Namespace) -> int:
    if args.cmd == "up":
        ensure_ec2_running(args.instance_id, args.profile, args.region)
        pids = start_tunnels(args.instance_id, args.profile, args.region, include_bench=args.include_bench)
        print(json.dumps({"status": "up", "tunnels": pids, "pool": MiniPoolClient().pool_status()}, indent=2))
        return 0
    if args.cmd == "tunnel":
        pids = start_tunnels(args.instance_id, args.profile, args.region, include_bench=args.include_bench)
        print(json.dumps({"status": "tunneled", "tunnels": pids}, indent=2))
        return 0
    if args.cmd == "down":
        closed = stop_tunnels()
        if not args.keep_ec2:
            stop_ec2(args.instance_id, args.profile, args.region)
        print(json.dumps({"status": "down", "closed_tunnels": closed, "ec2_stopped": not args.keep_ec2}, indent=2))
        return 0
    if args.cmd == "scale":
        res = scale_workers(args.workers, args.instance_id, args.profile, args.region, dry_run=args.dry_run)
        print(json.dumps(res, indent=2))
        return 0
    if args.cmd == "status":
        print(json.dumps(get_status(), indent=2))
        return 0
    if args.cmd == "systemone":
        return _handle_systemone_command(args)
    if args.cmd == "ask":
        schema_str = getattr(args, "json_schema", "") or ""
        if schema_str.strip():
            out = MiniPoolClient().chat(
                args.prompt,
                system=args.system,
                model=args.model,
                max_tokens=args.max_tokens,
                json_schema=json.loads(schema_str),
            )
        else:
            out = MiniPoolClient().chat(args.prompt, system=args.system, model=args.model, max_tokens=args.max_tokens)
        print(out or "")
        return 0 if out is not None else 1
    if args.cmd == "map-reduce":
        outs = MiniPoolClient().parallel_map(args.items, system=args.system, model=args.model)
        print(json.dumps(outs, indent=2))
        return 0
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_cli_parser().parse_args(argv)
    return _handle_cli_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
