#!/usr/bin/env python3
"""
Portable Selective Lossless Test & Compiler Output Distiller (`lossless_test_distiller.py`).

Selectively wraps pure test/compiler/linter commands (`pytest`, `unittest`, `cargo test|check|clippy|nextest`,
`mvn test|test-compile`, `go test`, `npm|pnpm|yarn|bun test|typecheck|lint`, `vitest`, `jest`,
`tsc`, `vue-tsc`, `ruff check`, `mypy`, `golangci-lint`, `opengrep`, `semgrep`), distilling
passing runs into a 2-line `[Test Distiller - PASS]` summary while preserving 100% of failure
traces and original exit codes. General shell commands, pipes, and redirections pass untouched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

TARGET_COMMAND_RE = re.compile(
    r"^\s*(?:(?:(?:\$env:[A-Za-z0-9_]+|(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:'[^']*'|\"[^\"]*\"|\S+))(?:\s*(?:;|&&)\s*|\s+)|(?:cd|Set-Location|Push-Location)\s+(?:'[^']*'|\"[^\"]*\"|[^;&|]+)\s*(?:;|&&)\s*)*"
    r"(?:&\s*)?(?:npx(?:\.cmd)?\s+|uv\s+run\s+|poetry\s+run\s+)?"
    r"(?P<tool>"
    r"pytest|py\.test|"
    r"(?:(?:'[^']*python(?:3|\d+(?:\.\d+)?)?(?:\.exe)?'|\"[^\"]*python(?:3|\d+(?:\.\d+)?)?(?:\.exe)?\"|(?:\S*[/\\])?python(?:3|\d+(?:\.\d+)?)?(?:\.exe)?)\s+-m\s+(?:pytest|unittest))|"
    r"cargo(?:\.exe)?\s+(?:test|check|clippy|nextest)|"
    r"mvn(?:\.cmd)?\s+(?:[^\r\n|]*?\b(?:test|test-compile)\b)|"
    r"go(?:\.exe)?\s+test|"
    r"(?:npm|pnpm|yarn|bun)(?:\.cmd)?\s+(?:run\s+)?(?:test|typecheck|lint)|"
    r"vitest|jest|vue-tsc|tsc|"
    r"golangci-lint|ruff\s+check|mypy|opengrep|semgrep"
    r")(?:\s|$)",
    re.IGNORECASE,
)

INTERACTIVE_WATCH_RE = re.compile(
    r"(?:^|\s)(?:--watch\b|--watchAll\b|-w\b|watch\b)",
    re.IGNORECASE,
)

SKIP_WRAP_PATTERNS = (
    "lossless_test_distiller",
    "rtk.exe",
    "rtk ",
    "@'",
    '@"',
)

NOISE_LINE_RE = re.compile(
    r"^(?:"
    r"\s*(?:Compiling|Downloading|Downloaded|Fresh|Checking|Locking|Finished)\s+\S+.*|"
    r".*\s+PASSED\s*(?:\[\s*\d+%\s*\])?\s*$|"
    r"^test\s+\S+\s+\.\.\.\s+ok\s*$|"
    r"^ok\s+\S+\s+(?:\(cached\)|\d+(?:\.\d+)?s).*|"
    r"^\?\s+\S+\s+\[no test files\]\s*$|"
    r"^\s*PASS\s+\S+.*|"
    r"^\[INFO\]\s+(?:Downloading|Downloaded|Building|Compiling|Copying)\b.*"
    r")$"
)

FAILURE_START_RE = re.compile(
    r"(?:"
    r"^=+ FAILURES =+|^=+ ERRORS =+|"
    r"^--- FAIL:|^FAIL\s+\S+|"
    r"^failures:$|^---- \S+ stdout ----|"
    r"^Traceback \(most recent call last\):|"
    r"^panic:|^thread '.*' panicked at|"
    r"^error(?:\[[A-Z0-9]+\])?:|"
    r"^\[ERROR\]\s+|"
    r"^\S+:\d+:\d+:\s*(?:error|fatal)|"
    r"\bAssertionError\b|\bFAILED\b|\bBUILD FAILURE\b"
    r")"
)

SUMMARY_LINE_RE = re.compile(
    r"(?:"
    r"=+\s*\d+\s+(?:passed|failed|error|skipped|warning).*?=+|"
    r"^test result:\s*(?:ok|FAILED)\..*|"
    r"^Test Suites:.*|^Tests:\s+\d+.*|"
    r"^Ran\s+\d+\s+tests?\s+in\s+[\d.]+s|"
    r"^\[INFO\]\s+BUILD SUCCESS\b.*|"
    r"^OK\s*(?:\(.*\))?$|"
    r"^ok\b|^FAIL\b"
    r")",
    re.IGNORECASE,
)

MIN_LINES_TO_DISTILL = 25
MIN_CHARS_TO_DISTILL = 1400
MAX_DISTILLED_CHARS = 9600
DEFAULT_OBSERVATION_THRESHOLD = 80
_OBSERVATION_ALERT_RE = re.compile(
    r"(?:\berror\b|\bwarn(?:ing)?\b|\bfail(?:ed|ure)?\b|\bexception\b|\bpanic\b|\btraceback\b|^\s*FAILED\b)",
    re.IGNORECASE,
)


def should_wrap_command(cmd: str) -> Optional[str]:
    """Returns normalized verification tag if `cmd` is eligible for lossless distillation, else None."""
    if os.environ.get("KAIZEN_DISABLE_DISTILLER") == "1":
        return None
    stripped = (cmd or "").strip()
    if not stripped or "\n" in stripped or "\r" in stripped:
        return None
    if "|" in stripped or ">" in stripped:
        return None
    lower = stripped.lower()
    if any(tok in lower for tok in SKIP_WRAP_PATTERNS):
        return None
    m = TARGET_COMMAND_RE.match(stripped)
    if not m:
        return None
    tail_after_match = stripped[m.start("tool") :]
    if ";" in tail_after_match[len(m.group("tool")) :] or "&&" in tail_after_match or "||" in tail_after_match:
        return None
    if INTERACTIVE_WATCH_RE.search(tail_after_match):
        return None
    raw_tool = re.sub(r"\s+", "_", m.group("tool").strip().lower())
    if raw_tool == "vitest" and not re.search(r"\b(?:run|--run|--version|-v|--help|-h)\b", tail_after_match, re.IGNORECASE):
        return None
    return re.sub(r"[^a-z0-9_-]", "", raw_tool)[:32] or "test"


def build_wrapped_command(cmd: str, verify_tag: str, script_path: Optional[Path] = None, shell: str = "auto") -> str:
    target_script = (script_path or Path(__file__)).resolve()
    py_exe = sys.executable or "python"
    encoded = base64.b64encode(cmd.encode("utf-8")).decode("ascii")
    use_ps_call = (shell == "powershell") or (shell == "auto" and os.name == "nt")
    shell_arg = "powershell" if use_ps_call else "bash"
    if use_ps_call:
        return f'& "{py_exe}" -X utf8 "{target_script}" --shell {shell_arg} --verify-tag {verify_tag} --exec-b64 {encoded}'
    py_posix = str(py_exe).replace("\\", "/")
    script_posix = str(target_script).replace("\\", "/")
    return f'"{py_posix}" -X utf8 "{script_posix}" --shell {shell_arg} --verify-tag {verify_tag} --exec-b64 {encoded}'


def has_real_failure(lines: List[str], returncode: int = 0) -> bool:
    if returncode != 0:
        return True
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if re.search(r"\b0\s+(?:failed|errors?|failures?)\b", stripped, re.IGNORECASE):
            if not re.search(r"\b[1-9]\d*\s+(?:failed|errors?|failures?)\b", stripped, re.IGNORECASE):
                continue
        if FAILURE_START_RE.search(stripped):
            return True
        if re.search(r"\b[1-9]\d*\s+(?:failed|errors?|failures?)\b", stripped, re.IGNORECASE):
            return True
    return False


def _save_raw_log(tag: str, command: str, raw_text: str) -> Path:
    digest = hashlib.sha1(f"{tag}:{command}".encode("utf-8", errors="replace")).hexdigest()[:10]
    log_path = Path(tempfile.gettempdir()) / f"kaizen_test_raw_{digest}.log"
    try:
        log_path.write_text(raw_text, encoding="utf-8", errors="replace")
    except OSError:
        pass
    return log_path


def distill_text(raw_output: str, command: str, tag: str = "local", returncode: int = 0) -> str:
    cleaned = ANSI_ESCAPE_RE.sub("", raw_output or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = cleaned.splitlines()
    if len(lines) < MIN_LINES_TO_DISTILL and len(cleaned) < MIN_CHARS_TO_DISTILL:
        return raw_output

    log_path = _save_raw_log(tag, command, cleaned)
    if not has_real_failure(lines, returncode=returncode):
        summary_candidates = [ln.strip() for ln in lines if SUMMARY_LINE_RE.search(ln.strip())]
        go_ok_count = sum(1 for ln in lines if re.match(r"^ok\s+\S+", ln))
        non_empty = [ln.strip() for ln in lines if ln.strip()]
        tail_summary = summary_candidates[-1] if summary_candidates else (non_empty[-1] if non_empty else "OK")
        extra = f" ({go_ok_count} Go packages ok)" if go_ok_count > 1 else ""
        return (
            f"[Test Distiller - PASS]{extra} {tail_summary}\n"
            f"({len(lines)} lines / {len(cleaned)} chars of passing build/test noise distilled; full raw log: {log_path})\n"
        )

    kept = [ln for ln in lines if not NOISE_LINE_RE.match(ln)]
    body = "\n".join(kept).strip()
    header = (
        f"[Test Distiller - FAIL/WARN] Passing test/compile noise stripped; "
        f"all failure & stack trace blocks preserved (full raw log: {log_path}):\n"
    )
    budget = MAX_DISTILLED_CHARS - len(header) - 96
    if len(body) > budget:
        head_part = body[: budget // 3]
        tail_part = body[-(budget - len(head_part)) :]
        body = f"{head_part}\n...[intermediate lines saved in {log_path}]...\n{tail_part}"
    return header + body + "\n"


distill_test_output = distill_text


def _save_observation_log(command: str, cleaned: str, log_dir: Optional[Path] = None) -> Path:
    digest = hashlib.sha1(f"{command}:{len(cleaned)}".encode("utf-8", errors="replace")).hexdigest()[:12]
    target_dir = Path(log_dir).expanduser().resolve() if log_dir is not None else Path(tempfile.gettempdir())
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        target_dir = Path(tempfile.gettempdir())
    log_path = target_dir / f"kaizen_obs_{digest}.log"
    try:
        log_path.write_text(cleaned, encoding="utf-8", errors="replace")
    except OSError:
        pass
    return log_path


def mask_large_observation(
    command: str,
    output: str,
    *,
    exit_code: int = 0,
    line_threshold: int = DEFAULT_OBSERVATION_THRESHOLD,
    head_lines: int = 15,
    tail_lines: int = 15,
    log_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Mask >80-line command/diff/log/JSON outputs while preserving raw log pointer and error lines."""
    cleaned = ANSI_ESCAPE_RE.sub("", output or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = cleaned.splitlines()
    threshold = max(1, int(line_threshold))
    if len(lines) <= threshold:
        return {
            "masked": False,
            "command": command,
            "exit_code": int(exit_code),
            "total_lines": len(lines),
            "raw_log_path": "",
            "tokens_saved_estimate": 0,
            "masked_text": cleaned,
        }

    head_n = max(1, int(head_lines))
    tail_n = max(1, int(tail_lines))
    raw_path = _save_observation_log(command, cleaned, log_dir=log_dir)
    head = lines[:head_n]
    tail_start = max(head_n, len(lines) - tail_n)
    tail = lines[tail_start:]
    alerts = [ln for ln in lines[head_n:tail_start] if _OBSERVATION_ALERT_RE.search(ln)][:25]

    sections = [
        f"[Observation Masked: cmd={command[:80]!r}, exit_code={int(exit_code)}, lines={len(lines)}, raw_log={raw_path}]",
        "--- HEAD ---",
        *head,
    ]
    if alerts:
        sections.extend(["--- ALERTS ---", *alerts])
    sections.extend([f"--- TAIL ({len(lines) - tail_start}L) ---", *tail])
    masked_text = "\n".join(sections)
    tokens_saved = max(0, (len(cleaned) - len(masked_text)) // 4)
    return {
        "masked": True,
        "command": command,
        "exit_code": int(exit_code),
        "total_lines": len(lines),
        "head_lines": head,
        "alert_lines": alerts,
        "tail_lines": tail,
        "raw_log_path": str(raw_path),
        "tokens_saved_estimate": tokens_saved,
        "masked_text": masked_text,
    }


def _is_tool_observation_msg(msg: Dict[str, Any]) -> bool:
    role = str(msg.get("role") or "").lower()
    mtype = str(msg.get("type") or "").lower()
    if role == "tool" or mtype in ("tool_result", "observation"):
        return True
    return bool(msg.get("tool_call_id") or ("tool" in msg and ("output" in msg or "content" in msg)))


def mask_stale_turn_observations(
    history: List[Dict[str, Any]],
    *,
    keep_recent_turns: int = 4,
) -> List[Dict[str, Any]]:
    """Replace tool outputs older than the last N turns with compact placeholder references."""
    if not history:
        return []
    keep_n = max(0, int(keep_recent_turns))
    obs_indices = [idx for idx, item in enumerate(history) if isinstance(item, dict) and _is_tool_observation_msg(item)]
    cutoff_indices = set(obs_indices[:-keep_n]) if keep_n > 0 else set(obs_indices)

    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(history):
        if not isinstance(item, dict):
            out.append(item)
            continue
        cloned = dict(item)
        if idx in cutoff_indices:
            tool_name = str(cloned.get("name") or cloned.get("tool") or "tool")
            raw_body = str(cloned.get("content") or cloned.get("output") or cloned.get("tool_output") or "")
            if raw_body and not raw_body.startswith("<Observation masked:"):
                first_line = next((ln.strip() for ln in raw_body.splitlines() if ln.strip()), "")[:80]
                ref = str(cloned.get("raw_log_path") or cloned.get("ref") or f"turn-{idx}")
                placeholder = f"<Observation masked: tool={tool_name}, summary={first_line!r}, ref={ref}>"
                if "content" in cloned:
                    cloned["content"] = placeholder
                elif "output" in cloned:
                    cloned["output"] = placeholder
                else:
                    cloned["tool_output"] = placeholder
        out.append(cloned)
    return out


def _build_windows_env() -> Dict[str, str]:
    env = dict(os.environ)
    extra_dirs: List[str] = []
    home = Path.home()
    for cand in (home / ".cargo" / "bin", home / ".local" / "bin", Path(env.get("APPDATA", "")) / "npm"):
        if str(cand) and cand.is_dir():
            extra_dirs.append(str(cand))
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            user_path, _ = winreg.QueryValueEx(key, "Path")
            if isinstance(user_path, str):
                for seg in user_path.split(";"):
                    expanded = os.path.expandvars(seg.strip())
                    if expanded and os.path.isdir(expanded):
                        extra_dirs.append(expanded)
    except Exception:
        pass
    cur_path = env.get("PATH") or env.get("Path") or ""
    existing = {p.strip().lower() for p in cur_path.split(";") if p.strip()}
    prepended = [d for d in extra_dirs if d.lower() not in existing]
    if prepended:
        env["PATH"] = ";".join(prepended + ([cur_path] if cur_path else []))
    return env


def _find_windows_bash() -> Optional[str]:
    for cand in (
        os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "").strip(),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        shutil.which("bash") or "",
    ):
        if cand and os.path.isfile(cand) and "system32\\bash.exe" not in cand.lower():
            return cand
    return None


def _execute_native_shell(command: str, shell_hint: str = "auto") -> subprocess.CompletedProcess[str]:
    is_posix_syntax = bool(
        shell_hint in ("bash", "sh", "posix")
        or ("&&" in command and "$env:" not in command)
        or re.match(r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*=\S+\s+\S+", command)
    )
    if os.name == "nt":
        win_env = _build_windows_env()
        if is_posix_syntax:
            bash_bin = _find_windows_bash()
            if bash_bin:
                return subprocess.run(
                    [bash_bin, "--noprofile", "--norc", "-c", command],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=win_env,
                    check=False,
                )
        ps_script = f"$ProgressPreference = 'SilentlyContinue'; {command}"
        ps_b64 = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand", ps_b64],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=win_env,
            check=False,
        )
    return subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def run_wrapped_command(b64_cmd: str, verify_tag: str = "test", shell_hint: str = "auto") -> int:
    try:
        command = base64.b64decode(b64_cmd.encode("ascii")).decode("utf-8")
    except Exception as exc:
        sys.stderr.write(f"lossless_test_distiller decode error: {exc}\n")
        return 2

    proc = _execute_native_shell(command, shell_hint=shell_hint)
    parts = [p for p in (proc.stdout, proc.stderr) if p]
    combined = "\n".join(parts)

    distilled = distill_text(
        raw_output=combined,
        command=command,
        tag=verify_tag,
        returncode=proc.returncode,
    )
    if distilled:
        out_txt = distilled if distilled.endswith("\n") else distilled + "\n"
        sys.stdout.buffer.write(out_txt.encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    return proc.returncode


def maybe_rewrite_antigravity_args(args: Dict[str, Any], shell: str = "auto") -> Optional[Dict[str, Any]]:
    cmd = str(args.get("CommandLine") or "").strip()
    tag = should_wrap_command(cmd)
    if not tag:
        return None
    updated = dict(args)
    updated["CommandLine"] = build_wrapped_command(cmd, tag, shell=shell)
    return updated


def run_self_test() -> int:
    if should_wrap_command("git status -sb") is not None:
        sys.stderr.write("[lossless_test_distiller FAIL] git status should never be wrapped\n")
        return 1
    for non_test_cmd in (
        "git commit -m 'fix pytest flakiness'",
        "Get-Content pytest.ini",
        "vitest",
        "vitest watch",
        "jest --watch",
        "cargo test --lib; git status",
    ):
        if should_wrap_command(non_test_cmd) is not None:
            sys.stderr.write(f"[lossless_test_distiller FAIL] {non_test_cmd!r} should NOT be wrapped\n")
            return 1
    if should_wrap_command("cargo test | Select-Object -First 20") is not None:
        sys.stderr.write("[lossless_test_distiller FAIL] piped command should not be wrapped\n")
        return 1
    for valid_test_cmd in (
        "$env:CARGO_BUILD_JOBS=4; cargo test --lib",
        "cd /workspace/service; $env:RUST_BACKTRACE=1; cargo clippy --all-targets",
        "npx vitest run",
    ):
        if should_wrap_command(valid_test_cmd) is None:
            sys.stderr.write(f"[lossless_test_distiller FAIL] expected {valid_test_cmd!r} to be matched\n")
            return 1

    noisy_pass = "\n".join(
        [f"   Compiling crate_{i} v0.1.0" for i in range(25)]
        + [f"test module::case_{i} ... ok" for i in range(30)]
        + ["test result: ok. 30 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s"]
    )
    d_pass = distill_text(noisy_pass, "cargo test", "selftest", returncode=0)
    if "[Test Distiller - PASS]" not in d_pass or "30 passed; 0 failed" not in d_pass or len(d_pass.splitlines()) > 3:
        sys.stderr.write(f"[lossless_test_distiller FAIL] Unexpected PASS distillation: {d_pass!r}\n")
        return 1

    noisy_fail = "\n".join(
        [f"   Compiling dep_{i} v0.1.0" for i in range(20)]
        + [f"test module::ok_{i} ... ok" for i in range(15)]
        + [
            "test module::broken_case ... FAILED",
            "failures:",
            "---- module::broken_case stdout ----",
            "thread 'module::broken_case' panicked at src/lib.rs:42:5:",
            "assertion `left == right` failed: expected 200, got 502",
            "test result: FAILED. 15 passed; 1 failed; 0 ignored",
        ]
    )
    d_fail = distill_text(noisy_fail, "cargo test", "selftest_fail", returncode=101)
    if "[Test Distiller - FAIL/WARN]" not in d_fail or "src/lib.rs:42:5" not in d_fail or "Compiling dep_0" in d_fail:
        sys.stderr.write(f"[lossless_test_distiller FAIL] Failure trace corrupted or noise kept: {d_fail!r}\n")
        return 1

    print("  [PASS] lossless_test_distiller -- selective wrapping, PASS compression & FAIL trace preservation verified")
    return 0


def _emit_ascii_json(obj: Dict[str, Any]) -> None:
    encoded = (json.dumps(obj, ensure_ascii=True) + "\n").encode("ascii")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main(argv: Optional[List[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--self-test" in args:
        return run_self_test()

    if "--exec-b64" in args:
        idx = args.index("--exec-b64")
        if idx + 1 >= len(args):
            return 2
        vtag = "test"
        if "--verify-tag" in args:
            t_idx = args.index("--verify-tag")
            if t_idx + 1 < len(args):
                vtag = args[t_idx + 1]
        shint = "auto"
        if "--shell" in args:
            s_idx = args.index("--shell")
            if s_idx + 1 < len(args):
                shint = args[s_idx + 1]
        return run_wrapped_command(args[idx + 1], verify_tag=vtag, shell_hint=shint)

    try:
        raw_stdin = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        payload = json.loads(raw_stdin) if raw_stdin.strip() else {}
        if not isinstance(payload, dict):
            _emit_ascii_json({"decision": "allow"})
            return 0
    except Exception:
        _emit_ascii_json({"decision": "allow"})
        return 0

    tc = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    if tc.get("name") == "run_command":
        tc_args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
        rewritten = maybe_rewrite_antigravity_args(tc_args)
        if rewritten is not None:
            _emit_ascii_json({"decision": "allow", "overwrite": rewritten})
            return 0

    _emit_ascii_json({"decision": "allow"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
