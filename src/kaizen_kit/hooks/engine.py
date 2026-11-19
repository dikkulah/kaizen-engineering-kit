#!/usr/bin/env python3
"""
Portable Kaizen v3.0 6-Event Closed-Loop Lifecycle Engine (`engine.py`).

Shared deterministic core powering BOTH:
  - Native Claude Code CLI (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `SubagentStop`, `Stop`)
  - Antigravity 2.0 (`SessionStart`, `PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, `Stop`)

Zero hardcoded machine paths or personal identifiers.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from kaizen_kit.gates import (
    check_clean_code,
    hansei_ledger,
    oss_ast_gate,
    senior_review_lint,
    verify_provenance,
)
from kaizen_kit.gates.deterministic_vault import (
    check_for_secrets,
    is_dangerous_command,
    is_sensitive_file_path,
)
from kaizen_kit.hooks import lossless_test_distiller, prompt_decomposer
from kaizen_kit.paths import (
    resolve_audit_log_path,
    resolve_repo_locks_dir,
    resolve_session_state_dir,
)

MAX_INVOCATIONS_PER_TURN = int(os.environ.get("AI_KIT_MAX_TURN_STEPS", "80"))
SUBAGENT_WARN_STEPS = 32
SUBAGENT_MAX_STEPS = 40
MAX_UNBOUNDED_READ_LINES = 300
MAX_AUDIT_LOG_BYTES = 2 * 1024 * 1024
KAIZEN_WORKSPACE_GROUPS = frozenset({
    "kaizen-core-backend",
    "kaizen-serverless-lambda",
    "kaizen-ui",
    "kaizen-infra-devops",
    "kaizen-keycloak-auth",
    "kaizen-eu-services",
    "edge-gerrit",
})

UNIVERSAL_AGENT_ROUTES = [
    (re.compile(r"\b(?:official\s+doc|documentation|rfc\s*\d*|jep\s*\d*|quota|limits|confluence\s+spec|refactoring\.guru)\b", re.I), "docs-spec-analyst"),
    (re.compile(r"\b(?:refactor|clean\s+code|clean\s+architecture|guard\s+clause|smell|solid|demeter|adp|sdp)\b", re.I), "refactoring-specialist"),
    (re.compile(r"\b(?:rust|lambda|graviton|arm64|tokio|clippy|simd|vector_cube|fold|avro|bson)\b", re.I), "systems-architect"),
    (re.compile(r"\b(?:java|spring|springboot|loom|virtual\s+threads|maven|archunit|restclient|dto)\b", re.I), "jvm-service-architect"),
    (re.compile(r"\b(?:sql|dag|parity|oracle|parquet|readmodel_service|har|telemetry|cube|matrix)\b", re.I), "data-sql-architect"),
    (re.compile(r"\b(?:uc-\d+|bug|triage|incident|rca|falsif|blame|regression|gerrit)\b", re.I), "incident-rca-falsifier"),
    (re.compile(r"\b(?:mr|deploy|jenkins|ci|cd|playwright|token|finops|hygiene|clean|pre-flight)\b", re.I), "release-hygiene-guardian"),
    (re.compile(r"\b(?:explore|ast|skeleton|blast-radius|survey|search|inventory|scan)\b", re.I), "code-explorer"),
]
DEFAULT_UNIVERSAL_AGENT = "e2e-systems-visionary"
GENERIC_SUBAGENT_TYPES = {"self", "inherited", "generic", "worker", "assistant", "general-purpose", ""}

# Constructed cleanly without embedding literal forbidden payloads in single contiguous strings
_SSID_FIXTURE_PAT = 'Kaizen_Air4960R_' + '8YD7_Cujo|"Home' + 'WiFi"|"A22D1306' + '7BF5"'
_HAR_ARITH_PAT = "922_346_" + "500|311_023_" + "600|rssi_f64\\s*[-+]\\s*5\\.0|\\*\\s*1_500_000\\.0|excellent:\\s*100\\b"
_CAP_BOOL_PAT = r"(?:supported_sgi|support_11v|support_11k)\.unwrap_or\(true\)"
_BAND_HEUR_PAT = r'if\s+is_5g_capable\s*\{\s*Some\("(?:80MHz|2)"'
_S3_SCHEMA_PAT = "ensure_briefcase_" + "schema_loaded|schemas/" + "fingerprints/"
_WAN_FAKE_PAT = (
    'wan_type\\s*[:=]\\s*"ETHER' + 'NET"|wan_status\\s*[:=]\\s*"U' + 'P"|'
    'wan_duplex\\s*[:=]\\s*"FU' + 'LL"|wan_link_speed\\s*[:=]\\s*1000\\b|'
    '"Kaizen ' + 'Gateway"|"Kaizen ' + 'Extender"|slice_idx\\s*%\\s*15|'
    + r'\\?"id\\?"\s*:\s*\\?"in_' + r'sync\\?"|status\\?"\s*:\s*\\?"in_' + r'sync\\?"'
)
_HOLLOW_CUBE_PAT = r"_cube:\s*&MaterializedHomeState[\s\S]{0,160}ItemsResponse::new\(Vec::<[^>]+>::new\(\)\)"

SYNTHETIC_TELEMETRY_PATTERNS = [
    (re.compile(_SSID_FIXTURE_PAT), "hardcoded test-home SSID/MAC fixture"),
    (re.compile(_HAR_ARITH_PAT), "synthetic Golden-HAR telemetry constant or fake RSSI/throughput arithmetic"),
    (re.compile(_CAP_BOOL_PAT), "fake capability boolean fallback (.unwrap_or(true) on sgi/11v/11k)"),
    (re.compile(_BAND_HEUR_PAT), "static band-heuristic capability hardcoding"),
    (re.compile(_S3_SCHEMA_PAT), "runtime S3 Avro schema fetching; embed at compile time"),
    (re.compile(_WAN_FAKE_PAT), "hardcoded fake WAN/Gateway/NodeId/RadioConfig fallback"),
    (re.compile(_HOLLOW_CUBE_PAT), "hollow stub controller ignoring `_cube` and returning empty `Vec::new()`"),
]

ORACLE_WEAKENING_PATTERNS = [
    (
        re.compile(r'return\s+\{\s*"class"\s*:\s*"(?:TOLERATED|S3_GAP|HONEST_TELEMETRY_GAP|PASS)"'),
        "masking parity diffs as TOLERATED/S3_GAP inside the strict comparator",
    ),
    (
        re.compile(r"FLOAT_EPSILON\s*=\s*(?:0\.[0-9]+|[1-9])"),
        "inflating FLOAT_EPSILON tolerance in parity comparator",
    ),
]

AWS_MUTATION_RE = re.compile(
    r"\baws(?:\.exe)?\s+(?:--[^\s]+\s+)*(?:lambda|elbv2|s3|s3api|cloudformation|ecs|ec2|iam|dynamodb|events|logs|ssm)\s+"
    r"(?:update-|create-|delete-|put-|publish-|modify-|terminate-|stop-instances|run-instances|send-command|start-session|rm\b|sync\b|deploy\b|cp\s+\S+\s+s3://)",
    re.IGNORECASE,
)
U2_PROFILE_RE = re.compile(
    r"(?:--profile[=\s]+(?:kaizen-u2(?=\s|$|[;&|])|'kaizen-u2'|\"kaizen-u2\")|"
    r"AWS_PROFILE\s*=\s*(?:kaizen-u2(?=\s|$|[;&|])|'kaizen-u2'|\"kaizen-u2\"))",
    re.IGNORECASE,
)
GIT_HOOK_BYPASS_RE = re.compile(
    r"\bgit(?:\.exe)?\b[^;&|\n]*(?:\s--no-verify(?=\s|$|[;&|])|\bcommit\b[^;&|\n]*\s-n(?=\s|$|[;&|])|"
    r"-c\s*['\"]?core\.hooksPath\s*=|\bconfig\b[^;&|\n]*\bcore\.hooksPath\b)",
    re.IGNORECASE,
)
_SHELL_WRAPPERS = frozenset({"env", "rtk", "rtk.exe", "command", "time", "nice"})
_REDIR_PREFIX_RE = re.compile(r"^(?:\d*[<>]+&\d+|\d*>>?\S*|\d*<\S*|&>>?\S*)$")
_SUBSHELL_BINARIES = frozenset(
    {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe", "bash", "bash.exe", "sh", "sh.exe"}
)
_SUBSHELL_CMD_FLAGS = frozenset({"/c", "/k", "-c", "-lc", "-command"})
_AWS_MUTATION_SERVICES = frozenset(
    {"lambda", "elbv2", "s3", "s3api", "cloudformation", "ecs", "ec2", "iam", "dynamodb", "events", "logs", "ssm"}
)
_AWS_GLOBAL_VALUE_FLAGS = frozenset(
    {
        "--profile",
        "--region",
        "--output",
        "--color",
        "--query",
        "--endpoint-url",
        "--ca-bundle",
        "--cli-read-timeout",
        "--cli-connect-timeout",
        "--cli-binary-format",
        "--cli-pager",
    }
)
_AWS_MUTATION_PREFIXES = (
    "update-",
    "create-",
    "delete-",
    "put-",
    "publish-",
    "modify-",
    "terminate-",
    "stop-instances",
    "run-instances",
    "send-command",
    "start-session",
    "cancel-command",
    "register-",
    "deregister-",
    "resume-session",
    "start-automation-",
    "stop-automation-",
    "send-automation-",
    "add-tags-",
    "remove-tags-",
    "label-parameter-",
    "unlabel-parameter-",
)
_GIT_GLOBAL_ARG_FLAGS = frozenset(
    {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env", "--exec-path"}
)
_COMMIT_VALUE_SHORT_FLAGS = frozenset("mFCct")
_COMMIT_VALUE_LONG_FLAGS = frozenset(
    {
        "--message",
        "--file",
        "--reuse-message",
        "--reedit-message",
        "--template",
        "--author",
        "--date",
        "--cleanup",
        "--fixup",
        "--squash",
        "--pathspec-from-file",
    }
)
_PROFILE_OR_ENV_RE = re.compile(
    r"(?:--profile[=\s]+|\bAWS_PROFILE\s*=\s*)(['\"]?)([^\s'\";&|]*)\1",
    re.IGNORECASE,
)
_UNSET_AWS_PROFILE_RE = re.compile(
    r"\b(?:unset\s+AWS_PROFILE|Remove-Item\s+Env:[/\\]?AWS_PROFILE)\b",
    re.IGNORECASE,
)
_EXPORT_AWS_PROFILE_RE = re.compile(
    r"^\s*(?:export\s+(?:\w+=\S+\s+)*AWS_PROFILE\s*=|\$env:AWS_PROFILE\s*=)\s*(['\"]?)([^\s'\";&|]*)\1\s*$",
    re.IGNORECASE,
)


def _delimiter_len(cmd_text: str, idx: int, at_seg_start: bool = False) -> int:
    ch = cmd_text[idx]
    if ch in ";\n|":
        return 2 if cmd_text[idx : idx + 2] == "||" else 1
    if ch != "&":
        return 0
    if cmd_text[idx : idx + 2] == "&&":
        return 2
    if at_seg_start or cmd_text[idx : idx + 2] == "&>":
        return 0
    if idx > 0 and cmd_text[idx - 1] in "><":
        return 0
    return 1


def _toggle_quote(quote: str, ch: str) -> str:
    return "" if quote == ch else ch


def _is_blank_buf(buf: list[str]) -> bool:
    return not "".join(buf).strip()


def _flush_segment(segments: list[str], buf: list[str]) -> None:
    seg = "".join(buf).strip()
    if seg:
        segments.append(seg)
    buf.clear()


def _split_shell_segments(cmd_text: str) -> list[str]:
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    idx = 0
    while idx < len(cmd_text):
        ch = cmd_text[idx]
        if ch == "\\" and quote != "'":
            buf.append(cmd_text[idx : idx + 2])
            idx += 2
            continue
        if ch in ("'", '"') and quote in ("", ch):
            quote = _toggle_quote(quote, ch)
            buf.append(ch)
            idx += 1
            continue
        sep_len = 0 if quote else _delimiter_len(cmd_text, idx, _is_blank_buf(buf))
        if sep_len > 0:
            _flush_segment(segments, buf)
            idx += sep_len
            continue
        buf.append(ch)
        idx += 1
    _flush_segment(segments, buf)
    return segments


def _flush_token(tokens: list[str], buf: list[str], has_token: bool) -> bool:
    if has_token:
        tokens.append("".join(buf))
        buf.clear()
    return False


def _tokenize_shell(seg: str) -> list[str]:
    tokens: list[str] = []
    buf: list[str] = []
    quote = ""
    has_token = False
    idx = 0
    while idx < len(seg):
        ch = seg[idx]
        if ch == "\\" and quote != "'" and seg[idx + 1 : idx + 2] in ('"', "'", "\\", " "):
            buf.append(seg[idx + 1 : idx + 2])
            has_token = True
            idx += 2
            continue
        if ch in ("'", '"') and quote in ("", ch):
            quote = _toggle_quote(quote, ch)
            has_token = True
            idx += 1
            continue
        if not quote and ch.isspace():
            has_token = _flush_token(tokens, buf, has_token)
            idx += 1
            continue
        buf.append(ch)
        has_token = True
        idx += 1
    _flush_token(tokens, buf, has_token)
    return tokens


def _is_binary_name(token: str, names: frozenset[str]) -> bool:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in names


def _find_cmd_index(tokens: list[str], names: frozenset[str]) -> int:
    for idx, tok in enumerate(tokens):
        if _is_binary_name(tok, names):
            return idx
        low = tok.lower()
        if idx == 0 and tok == "&":
            continue
        if low in _SHELL_WRAPPERS or ("=" in tok and not tok.startswith("-")) or _REDIR_PREFIX_RE.match(tok):
            continue
        return -1
    return -1


def _unwrap_shell_tokens(tokens: list[str]) -> tuple[int, list[str]]:
    wrapper_idx = _find_cmd_index(tokens, _SUBSHELL_BINARIES)
    if wrapper_idx < 0:
        return -1, []
    is_posix = _is_binary_name(tokens[wrapper_idx], frozenset({"bash", "bash.exe", "sh", "sh.exe"}))
    for idx in range(wrapper_idx + 1, len(tokens)):
        low = tokens[idx].lower()
        if low in _SUBSHELL_CMD_FLAGS:
            return wrapper_idx, tokens[idx + 1 :]
        if is_posix and low.startswith("-") and not low.startswith("--") and "c" in low[1:]:
            return wrapper_idx, tokens[idx + 1 :]
    return -1, []


def _is_global_hookspath(tok: str, nxt: str) -> bool:
    low = tok.lower()
    if "core.hookspath=" in low and (low.startswith("-c") or low.startswith("--config-env")):
        return True
    return low in {"-c", "--config-env"} and "core.hookspath=" in nxt.lower()


def _analyze_git_global_args(tokens: list[str], start: int) -> tuple[int, str, bool]:
    idx = start
    while idx < len(tokens):
        tok = tokens[idx]
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        if _is_global_hookspath(tok, nxt):
            return idx, "", True
        if tok in _GIT_GLOBAL_ARG_FLAGS:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        return idx + 1, tok.lower(), False
    return len(tokens), "", False


def _short_flag_status(tok: str) -> tuple[bool, bool]:
    for idx, ch in enumerate(tok[1:], start=1):
        if ch == "n":
            return True, False
        if ch in _COMMIT_VALUE_SHORT_FLAGS:
            return False, idx == len(tok) - 1
    return False, False


def _is_short_commit_flag(subcmd: str, tok: str) -> bool:
    return subcmd == "commit" and tok.startswith("-") and not tok.startswith("--") and len(tok) > 1


def _subcmd_has_hook_bypass(tokens: list[str], start: int, subcmd: str) -> bool:
    idx = start
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--":
            return False
        if tok.lower() == "--no-verify":
            return True
        if tok in _COMMIT_VALUE_LONG_FLAGS:
            idx += 2
            continue
        if not _is_short_commit_flag(subcmd, tok):
            idx += 1
            continue
        has_n, consumes_next = _short_flag_status(tok)
        if has_n:
            return True
        idx += 2 if consumes_next else 1
    return False


def _tokens_have_git_hook_bypass(tokens: list[str]) -> bool:
    git_idx = _find_cmd_index(tokens, frozenset({"git", "git.exe"}))
    if git_idx < 0:
        return False
    idx, subcmd, global_bypass = _analyze_git_global_args(tokens, git_idx + 1)
    if global_bypass:
        return True
    if subcmd == "config":
        return any("core.hookspath" in tok.lower() for tok in tokens[idx:])
    return _subcmd_has_hook_bypass(tokens, idx, subcmd)


def _check_tokens_git_bypass(tokens: list[str], depth: int = 0) -> bool:
    if _tokens_have_git_hook_bypass(tokens):
        return True
    if depth >= 3:
        return False
    _, inner = _unwrap_shell_tokens(tokens)
    if not inner:
        return False
    if len(inner) == 1:
        return any(_segment_has_git_hook_bypass(s, depth + 1) for s in _split_shell_segments(inner[0]))
    return _check_tokens_git_bypass(inner, depth + 1)


def _segment_has_git_hook_bypass(seg: str, depth: int = 0) -> bool:
    return _check_tokens_git_bypass(_tokenize_shell(seg), depth)


def is_git_hook_bypass(cmd_text: str) -> bool:
    return any(_segment_has_git_hook_bypass(seg) for seg in _split_shell_segments(cmd_text))


_HOST_SUICIDE_RE = re.compile(
    r"(?:"
    r"\bos\.kill\s*\([^\r\n;]*?,\s*(?:0\s*\)|signal\.CTRL_C_EVENT|signal\.CTRL_BREAK_EVENT)"
    r"|\bGenerateConsoleCtrlEvent\s*\("
    r"|\b(?:Stop-Process|taskkill(?:\.exe)?)\b[^\r\n;|&]*(?:language_server|Antigravity)"
    r"|\bwsl(?:\.exe)?\s+--shutdown\b"
    r")",
    re.IGNORECASE,
)


def is_host_suicide_command(cmd_text: str) -> bool:
    return bool(_HOST_SUICIDE_RE.search(cmd_text))


def _next_aws_positional(tokens: list[str], start: int) -> tuple[str, int]:
    idx = start
    while idx < len(tokens):
        tok = tokens[idx]
        low = tok.lower()
        if low in _AWS_GLOBAL_VALUE_FLAGS:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        return low, idx + 1
    return "", len(tokens)


def _is_aws_mutation_tokens(tokens: list[str], aws_idx: int) -> bool:
    if aws_idx < 0:
        return False
    service, idx = _next_aws_positional(tokens, aws_idx + 1)
    if service not in _AWS_MUTATION_SERVICES:
        return False
    op, op_next = _next_aws_positional(tokens, idx)
    if not op:
        return False
    if op.startswith(_AWS_MUTATION_PREFIXES) or op in {"rm", "sync", "deploy"}:
        return True
    if service == "s3" and op == "cp":
        _, src_next = _next_aws_positional(tokens, op_next)
        dst, _ = _next_aws_positional(tokens, src_next)
        return dst.startswith("s3://")
    return False


def _tokens_have_u2_profile(tokens: list[str]) -> bool:
    for idx, tok in enumerate(tokens):
        low = tok.lower()
        if low == "--profile=kaizen-u2":
            return True
        if low == "--profile" and idx + 1 < len(tokens) and tokens[idx + 1].lower() == "kaizen-u2":
            return True
    return False


def _has_u2_in_segment(seg: str, tokens: list[str], exported_u2: bool) -> bool:
    aws_idx = _find_cmd_index(tokens, frozenset({"aws", "aws.exe"}))
    if aws_idx < 0:
        return exported_u2
    if any(tok.lower() == "aws_profile=kaizen-u2" for tok in tokens[:aws_idx]):
        return True
    return exported_u2 or _tokens_have_u2_profile(tokens[aws_idx + 1 :])


def _token_is_non_u2(tokens: list[str], idx: int) -> bool:
    low = tokens[idx].lower()
    if low.startswith(("aws_profile=", "--profile=")):
        return low.split("=", 1)[1] != "kaizen-u2"
    if low == "--profile":
        return idx + 1 >= len(tokens) or tokens[idx + 1].lower() != "kaizen-u2"
    return False


def _has_non_u2_profile(seg: str, tokens: list[str]) -> bool:
    if any(val.lower() != "kaizen-u2" for _, val in _PROFILE_OR_ENV_RE.findall(seg)):
        return True
    return any(_token_is_non_u2(tokens, idx) for idx in range(len(tokens)))


def _update_exported_u2(seg: str, exported_u2: bool) -> bool:
    if _UNSET_AWS_PROFILE_RE.search(seg):
        return False
    export_match = _EXPORT_AWS_PROFILE_RE.match(seg)
    if export_match:
        return export_match.group(2).lower() == "kaizen-u2"
    for _, val in _PROFILE_OR_ENV_RE.findall(seg):
        if val.lower() != "kaizen-u2":
            return False
    return exported_u2


def _tokens_violate_aws_tenancy(seg: str, tokens: list[str], exported_u2: bool, depth: int = 0) -> bool:
    aws_idx = _find_cmd_index(tokens, frozenset({"aws", "aws.exe"}))
    if aws_idx >= 0:
        if not _is_aws_mutation_tokens(tokens, aws_idx):
            return False
        if _has_non_u2_profile(seg, tokens):
            return True
        return not _has_u2_in_segment(seg, tokens, exported_u2)
    if depth >= 3:
        return False
    wrapper_idx, inner = _unwrap_shell_tokens(tokens)
    if not inner:
        return False
    inner_u2 = exported_u2 or any(tok.lower() == "aws_profile=kaizen-u2" for tok in tokens[:wrapper_idx])
    if len(inner) == 1:
        return is_aws_tenancy_violation(inner[0], depth + 1, inner_u2)
    return _tokens_violate_aws_tenancy("", inner, inner_u2, depth + 1)


def _segment_violates_aws_tenancy(seg: str, exported_u2: bool, depth: int = 0) -> bool:
    return _tokens_violate_aws_tenancy(seg, _tokenize_shell(seg), exported_u2, depth)


def is_aws_tenancy_violation(cmd_text: str, depth: int = 0, exported_u2: bool = False) -> bool:
    if depth >= 4:
        return False
    for seg in _split_shell_segments(cmd_text):
        if _segment_violates_aws_tenancy(seg, exported_u2, depth):
            return True
        exported_u2 = _update_exported_u2(seg, exported_u2)
    return False
MUTATING_REPO_CMD_RE = re.compile(
    r"\b(?:git\s+(?:commit|checkout|reset|rebase|merge|cherry-pick|stash|clean|rm|mv|apply)|"
    r"Remove-Item|Move-Item|Copy-Item|Set-Content|Add-Content|rm\s+|mv\s+)\b",
    re.IGNORECASE,
)
VERIFICATION_CMD_RE = re.compile(
    r"\b(?:cargo\s+(?:check|test|clippy|build|nextest)|mvn\s+.*(?:compile|test)|pytest\b|unittest\b|"
    r"npm\s+run\s+(?:typecheck|lint|test)|verify_provenance|check_clean_code|"
    r"senior_review_lint|story_guard|kaizen-kit\s+check|lossless_test_distiller|opengrep|semgrep)\b",
    re.IGNORECASE,
)

TEST_DELETE_CMD_RE = re.compile(r"\b(?:rm|git\s+rm|Remove-Item|del|unlink)\b", re.IGNORECASE)
TEST_DELETE_TARGET_RE = re.compile(
    r"(?:deploy[/\\]parity|\badversarial_|\bparity_suite\b|crates[/\\][^/\\]+[/\\]tests[/\\]|test_.*\.py|\btests[/\\]|\btests\b|\bbenches[/\\]|\bbenches\b)",
    re.IGNORECASE,
)
SLEEP_CMD_RE = re.compile(r"^(?:sleep\s+\d+|python3?\s+-c\s+['\"]import time;\s*time\.sleep|Start-Sleep)", re.I)


def _rotate_audit_log_if_needed(log_path: Path) -> None:
    if not log_path.exists() or log_path.stat().st_size <= MAX_AUDIT_LOG_BYTES:
        return
    all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    backup_path = log_path.with_suffix(log_path.suffix + ".1")
    backup_path.write_text("".join(all_lines[:-1000] if len(all_lines) > 1000 else []), encoding="utf-8")
    log_path.write_text("".join(all_lines[-1000:]), encoding="utf-8")


def log_event(event_type: str, session_id: str, reason: str) -> None:
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log_path = resolve_audit_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_audit_log_if_needed(log_path)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{event_type}] session={session_id[:10]} | {reason}\n")
    except Exception:
        pass


def load_session_state(session_id: str) -> dict:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id or "default")
    p = resolve_session_state_dir() / f"{safe_id}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "turn_index": 0,
        "unverified_prod_edits": [],
        "hard_jidoka_violations": [],
        "pending_jidoka_warnings": [],
        "stop_gate_fired_turn": -1,
    }


def save_session_state(session_id: str, data: dict) -> None:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id or "default")
    p = resolve_session_state_dir() / f"{safe_id}.json"
    try:
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _fast_repo_name_from_parts(cur: Path) -> Optional[str]:
    parts = [seg for seg in re.split(r"[/\\]+", str(cur)) if seg]
    for idx, seg in enumerate(parts):
        if seg.lower() in KAIZEN_WORKSPACE_GROUPS and idx + 1 < len(parts):
            return parts[idx + 1]
        if seg.startswith("kaizen-") and idx > 0:
            return seg
    return None


def detect_git_repo_name(path_str: str, cwd_str: str = "", cmd_str: str = "") -> Optional[str]:
    """Resolve the enclosing git repository folder name for cross-session worktree locking."""
    candidates = []
    if path_str:
        candidates.append(Path(path_str))
    for token in re.findall(r"[A-Za-z0-9_./\\:-]+", cmd_str or ""):
        if "/" in token or "\\" in token:
            candidates.append(Path(token))
    if cwd_str and not path_str:
        candidates.append(Path(cwd_str))

    for cand in candidates:
        try:
            cur = cand.resolve() if cand.exists() else (Path(cwd_str) / cand).resolve() if cwd_str else cand
            fast = _fast_repo_name_from_parts(cur)
            if fast:
                return fast
            if cur.is_file():
                cur = cur.parent
            for parent in [cur, *list(cur.parents)[:6]]:
                if (parent / ".git").exists():
                    return parent.name
        except Exception:
            continue
    return None


def acquire_repo_lock(repo_name: str, root_session_id: str) -> tuple[bool, str]:
    locks_dir = resolve_repo_locks_dir()
    lock_path = locks_dir / f"{repo_name}.lock"
    now = time.time()
    try:
        if lock_path.exists():
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            holder = data.get("root_cid") or data.get("cid") or ""
            locked_at = float(data.get("time", 0))
            if holder and holder != root_session_id and (now - locked_at) < 1800:
                return False, holder
        lock_path.write_text(
            json.dumps({"cid": root_session_id, "root_cid": root_session_id, "repo": repo_name, "time": now}),
            encoding="utf-8",
        )
        return True, root_session_id
    except Exception:
        return True, root_session_id


# ==============================================================================
# Canonical Event Handlers (Runtime-Neutral Dicts)
# ==============================================================================

def evaluate_session_start(session_id: str, is_subagent: bool = False) -> dict:
    if is_subagent:
        return {"context": "", "position": "suffix", "capc_cache_safe": True}
    brief = hansei_ledger.build_session_brief(session_id)
    log_event("SESSION_START_BRIEF", session_id, "Injected Yokoten institutional memory briefing.")
    return {"context": brief, "position": "suffix", "capc_cache_safe": True}


def evaluate_user_prompt(session_id: str, prompt: str) -> dict:
    st = load_session_state(session_id)
    st["turn_index"] = int(st.get("turn_index", 0)) + 1
    save_session_state(session_id, st)

    sec_match = check_for_secrets(prompt)
    if sec_match:
        sec_type, sec_snippet = sec_match
        reason = (
            f"[CRITICAL SECURITY GUARD] Prompt blocked! Detected {sec_type} ({sec_snippet}) in prompt. "
            f"Remove raw credentials before submitting."
        )
        log_event("BLOCK_PROMPT_SECRET", session_id, reason)
        return {"blocked": True, "reason": reason, "context": "", "position": "suffix", "capc_cache_safe": True}

    voice_meta = prompt_decomposer.extract_voice_metadata(prompt)
    clean_prompt = voice_meta["clean_prompt"] or prompt
    decomp = prompt_decomposer.decompose_prompt(clean_prompt)
    directive = prompt_decomposer.format_workstream_directive(decomp) if decomp.get("is_complex") else ""
    return {
        "blocked": False,
        "reason": "",
        "context": directive,
        "voice_metadata": voice_meta,
        "position": "suffix",
        "capc_cache_safe": True,
    }


def _is_teamwork_dispatch_prompt(prompt_txt: str) -> bool:
    low = (prompt_txt or "").lower()
    return any(
        tok in low
        for tok in (
            "dispatch.md",
            ".agents/teamwork/",
            "original_request.md",
            "teamwork_",
            "resume work at",
            "handoff.md",
            "briefing.md",
        )
    )


def evaluate_pre_tool(
    session_id: str,
    root_session_id: str,
    canonical_tool: str,
    args: dict,
    cwd: str = "",
) -> dict:
    """
    Canonical tools: `write_file`, `edit_file`, `read_file`, `run_command`, `invoke_subagent`.
    Returns `{"allow": bool, "reason": str, "updated_args": Optional[dict]}`.
    """
    is_file_write = canonical_tool in ("edit_file", "write_file")
    cmd_text = (args.get("CommandLine") or "").strip() if canonical_tool == "run_command" else ""
    is_mutating_cmd = bool(canonical_tool == "run_command" and MUTATING_REPO_CMD_RE.search(cmd_text))

    # 1. Cross-Session Worktree Mutex on Write/Mutate
    if is_file_write or is_mutating_cmd:
        t_path = (args.get("Path") or "").replace("\\", "/")
        repo_name = detect_git_repo_name(t_path, cwd, cmd_text)
        if repo_name:
            acquired, holder = acquire_repo_lock(repo_name, root_session_id)
            if not acquired:
                reason = (
                    f"[Cross-Session Worktree Mutex] Repository '{repo_name}' is actively locked by parallel session "
                    f"('{holder[:8]}'). Wait for that session to complete or use an isolated git worktree."
                )
                log_event("DENY_CROSS_SESSION_COLLISION", session_id, reason)
                return {"allow": False, "reason": reason, "updated_args": None}

    # 2. File Write Guards
    if is_file_write:
        target_path = (args.get("Path") or "").replace("\\", "/")
        new_text = args.get("Content") or args.get("Replacement") or ""
        is_prod_src = (
            "/src/" in target_path
            and target_path.endswith(".rs")
            and not target_path.endswith("_test.rs")
            and "/tests/" not in target_path
        )
        if is_prod_src:
            for pat, desc in SYNTHETIC_TELEMETRY_PATTERNS:
                if pat.search(new_text):
                    reason = (
                        f"[Zero-Hardcode Guard] Blocked edit on '{os.path.basename(target_path)}': "
                        f"detected {desc}. Compute dynamically from Avro/BSON telemetry or emit None/0/UNKNOWN."
                    )
                    log_event("DENY_HARDCODED_TELEMETRY", session_id, reason)
                    return {"allow": False, "reason": reason, "updated_args": None}

        is_test_file = any(p in target_path.lower() for p in ("/tests/", "/test/", "_test.rs", "test_", "parity_suite"))
        if is_test_file and canonical_tool == "write_file" and len(new_text.strip()) < 50:
            reason = (
                f"[Immutable-Oracle Guard] Blocked overwriting test file '{os.path.basename(target_path)}' "
                f"with hollow content ({len(new_text.strip())} chars)."
            )
            log_event("DENY_TEST_HOLLOWING", session_id, reason)
            return {"allow": False, "reason": reason, "updated_args": None}

        if any(name in target_path for name in ("differ.py", "diff_engine.py", "parity_gate.py")):
            for pat, desc in ORACLE_WEAKENING_PATTERNS:
                if pat.search(new_text):
                    reason = f"[Immutable-Oracle Guard] Blocked weakening parity comparator ({desc})."
                    log_event("DENY_ORACLE_WEAKENING", session_id, reason)
                    return {"allow": False, "reason": reason, "updated_args": None}

        sec_check = check_for_secrets(new_text)
        if sec_check:
            sec_type, sec_snippet = sec_check
            reason = f"[Security Guard] Blocked writing {sec_type} ({sec_snippet}) into '{os.path.basename(target_path)}'."
            log_event("DENY_SECRET_WRITE", session_id, reason)
            return {"allow": False, "reason": reason, "updated_args": None}

    # 3. Sensitive File Read Protection
    if canonical_tool == "read_file":
        read_path = args.get("Path") or ""
        if is_sensitive_file_path(read_path):
            reason = (
                f"[Security Guard] Blocked reading '{os.path.basename(read_path)}'. "
                f"Credentials, .env files, private keys, and settings.xml must never enter AI context."
            )
            log_event("DENY_SENSITIVE_FILE_READ", session_id, reason)
            return {"allow": False, "reason": reason, "updated_args": None}

        start_l = args.get("StartLine") or args.get("offset")
        end_l = args.get("EndLine") or args.get("limit")
        if read_path and os.path.isfile(read_path) and start_l is None and end_l is None:
            try:
                with open(read_path, "r", encoding="utf-8", errors="replace") as rf:
                    line_count = sum(1 for _ in rf)
                if line_count > MAX_UNBOUNDED_READ_LINES:
                    ext = os.path.splitext(read_path)[1].lower()
                    if ext in (".rs", ".java", ".ts", ".tsx", ".vue"):
                        from kaizen_kit.gates import ast_skeleton
                        skel = ast_skeleton.extract_skeleton(Path(read_path))
                        reason = (
                            f"[Mandatory RAM Serena LSP & AST Gate] Full-file read blocked on '{os.path.basename(read_path)}' "
                            f"({line_count} lines > {MAX_UNBOUNDED_READ_LINES}L). Query symbols in RAM via `serena` MCP "
                            f"(`find_symbol`, `find_referencing_symbols`) or `lsp-query find <Symbol>`, or pass line offset/limit "
                            f"using the 0-token signature skeleton below:\n\n{skel}"
                        )
                        log_event("DENY_UNBOUNDED_LARGE_READ", session_id, f"Blocked unbounded read on {os.path.basename(read_path)} ({line_count}L)")
                        return {"allow": False, "reason": reason, "updated_args": None}
            except Exception:
                pass

    # 4. Subagent Auto-Routing, Zero Explorer-Committee & Mandatory RAM LSP Contract
    if canonical_tool == "invoke_subagent":
        updated = dict(args)
        sub_type = (updated.get("TypeName") or "").strip()
        prompt_txt = updated.get("Prompt") or ""
        is_teamwork_self = sub_type.lower() == "self" and _is_teamwork_dispatch_prompt(prompt_txt)
        if sub_type.lower() in GENERIC_SUBAGENT_TYPES and not is_teamwork_self:
            routed = DEFAULT_UNIVERSAL_AGENT
            for rx, target_agent in UNIVERSAL_AGENT_ROUTES:
                if rx.search(prompt_txt):
                    routed = target_agent
                    break
            updated["TypeName"] = routed
            sub_type = routed

        if sub_type.lower() in ("explore", "code-explorer", "research"):
            st = load_session_state(session_id)
            exp_turn = int(st.get("explorer_turn_idx", -1))
            cur_turn = int(st.get("turn_index", 0))
            count = int(st.get("explorers_in_turn", 0)) if exp_turn == cur_turn else 0
            if count >= 1:
                reason = (
                    "[Mandatory RAM Serena LSP Gate — Zero Explorer Committees] Spawning multiple Explorer subagents is FORBIDDEN. "
                    "Query the warm RAM Serena LSP daemon directly (`serena`: `find_symbol`, `find_referencing_symbols`, `get_symbols_overview`) "
                    "or run `lsp-query find <Symbol> --project <repo>`."
                )
                log_event("DENY_EXPLORER_COMMITTEE", session_id, reason)
                return {"allow": False, "reason": reason, "updated_args": None}
            st["explorer_turn_idx"] = cur_turn
            st["explorers_in_turn"] = count + 1
            save_session_state(session_id, st)
            updated["Model"] = "haiku"

        raw_prompt = updated.get("Prompt") or ""
        marker = "[ALWAYS-ON RECURSIVE SUBAGENT CONTRACT]"
        if raw_prompt and marker not in raw_prompt:
            updated["Prompt"] = (
                f"{raw_prompt}\n\n{marker}\n"
                "- MANDATORY RAM SERENA LSP FIRST: Query symbols in ~100ms from RAM via `serena` MCP or `lsp-query find <Symbol> --project <repo>` before grepping or reading >300L files.\n"
                "- ZERO HARDCODED TELEMETRY: Compute every field from real Avro/BSON state; emit None/0/UNKNOWN when absent.\n"
                "- ZERO SECRETS & TENANCY ISOLATION: Never read .env/credentials; all non-u2 AWS profiles are 100% READ-ONLY.\n"
                "- CLEAN CODE & POPULATED_MATCH: Guard clauses (nesting <= 1), compile-time schemas, never delete tests/parity."
            )
        if updated != args:
            return {"allow": True, "reason": "", "updated_args": updated}

    # 5. Shell Command Safety
    if canonical_tool == "run_command":
        if is_dangerous_command(cmd_text):
            reason = f"[Security Guard] Blocked environment/credential dump command '{cmd_text[:60]}'."
            log_event("DENY_DANGEROUS_COMMAND", session_id, reason)
            return {"allow": False, "reason": reason, "updated_args": None}

        if is_host_suicide_command(cmd_text):
            reason = (
                "[Poka-Yoke Host Stability Guard] Blocked command that sends CTRL_C_EVENT (`os.kill(..., 0)` == "
                "CTRL_C_EVENT on Windows) or disrupts Antigravity `language_server.exe` (`wsl --shutdown`). "
                "Use Win32 `OpenProcess(0x1000)` / `GetExitCodeProcess` instead."
            )
            log_event("DENY_HOST_SUICIDE", session_id, reason)
            return {"allow": False, "reason": reason, "updated_args": None}

        if is_git_hook_bypass(cmd_text):
            reason = (
                "[Poka-Yoke Git Hook Guard] Blocked git hook bypass (`--no-verify` or `core.hooksPath`). "
                "Never bypass local/pre-push validation hooks."
            )
            log_event("DENY_GIT_HOOK_BYPASS", session_id, reason)
            return {"allow": False, "reason": reason, "updated_args": None}

        if is_aws_tenancy_violation(cmd_text):
            reason = (
                "[SEC-AWS-TENANCY-ISOLATION Guard] Blocked mutating AWS CLI command without explicit `--profile kaizen-u2`. "
                "All 41 staging/production/operator AWS profiles are strictly 100% READ-ONLY."
            )
            log_event("DENY_AWS_TENANCY_MUTATION", session_id, reason)
            return {"allow": False, "reason": reason, "updated_args": None}

        if TEST_DELETE_CMD_RE.search(cmd_text) and TEST_DELETE_TARGET_RE.search(cmd_text):
            reason = "[Immutable-Oracle Guard] Blocked command attempting to delete test suites or parity verification files."
            log_event("DENY_TEST_DELETION", session_id, reason)
            return {"allow": False, "reason": reason, "updated_args": None}

        if SLEEP_CMD_RE.match(cmd_text):
            reason = "[Zero-Polling Guard] Blocked shell sleep command; never poll with sleep."
            log_event("DENY_SLEEP", session_id, reason)
            return {"allow": False, "reason": reason, "updated_args": None}

        if re.match(
            r"^\s*(?:Get-ChildItem\s+-Recurse.*Select-String|grep\s+-[a-zA-Z]*r[a-zA-Z]*\s+|findstr\s+/s\b)",
            cmd_text,
            re.IGNORECASE,
        ):
            reason = (
                "[Mandatory RAM LSP / Symbol-RAG / AST Bridge Gate] Blocked slow recursive text grep across directories. "
                "Route by query type instead: Known symbol -> `serena` MCP / `lsp-query find`; "
                "Conceptual -> `lsp-query concept`; AST/Hotspots -> `kaizen-kit ast-audit <repo> --bridge-pack`."
            )
            log_event("DENY_BLIND_RECURSIVE_GREP", session_id, reason)
            return {"allow": False, "reason": reason, "updated_args": None}

        rewritten = lossless_test_distiller.maybe_rewrite_antigravity_args(args)
        if rewritten is not None:
            log_event("OVERWRITE_LOSSLESS_DISTILLER", session_id, f"Wrapped test command: {cmd_text[:70]}")
            return {"allow": True, "reason": "", "updated_args": rewritten}

    return {"allow": True, "reason": "", "updated_args": None}


def evaluate_post_tool(session_id: str, canonical_tool: str, args: dict, had_error: bool = False) -> dict:
    if had_error:
        return {"warnings": [], "hard_violations": []}

    st = load_session_state(session_id)

    if canonical_tool in ("edit_file", "write_file"):
        raw_path = args.get("Path") or ""
        if not raw_path or not os.path.isfile(raw_path):
            return {"warnings": [], "hard_violations": []}
        p = Path(raw_path).resolve()
        norm = p.as_posix()
        suffix = p.suffix.lower()
        is_test = "/tests/" in norm.lower() or "/test/" in norm.lower() or norm.lower().endswith(("_test.rs", "test.java"))

        if suffix in (".rs", ".java", ".vue", ".ts") and not is_test:
            if norm not in st["unverified_prod_edits"]:
                st["unverified_prod_edits"].append(norm)

            warnings: list[str] = []
            hard_viols: list[str] = []

            if suffix == ".rs":
                rep = verify_provenance.audit_workspace(p)
                for v in rep.get("violations", []):
                    hard_viols.append(f"[{v['invariant']}] {p.name}:{v['line']} -> {v['detail']}")
                warnings.extend(check_clean_code.scan_rust_file(p)[:4])
            elif suffix == ".java":
                warnings.extend(check_clean_code.scan_java_file(p)[:4])

            for item in senior_review_lint.scan_file(p)[:4]:
                warnings.append(f"[{item['category']}:{item['rule']}] {p.name}:{item['line']} -> {item['detail']}")
            warnings.extend(oss_ast_gate.scan_single_file_ast_grep(p)[:3])

            st["hard_jidoka_violations"] = hard_viols[:8]
            st["pending_jidoka_warnings"] = (hard_viols + warnings)[:6]
            save_session_state(session_id, st)
            return {"warnings": st["pending_jidoka_warnings"], "hard_violations": hard_viols}

    elif canonical_tool == "run_command":
        cmd = (args.get("CommandLine") or "").strip()
        if VERIFICATION_CMD_RE.search(cmd):
            st["unverified_prod_edits"] = []
            save_session_state(session_id, st)
        raw_out = args.get("output") if args.get("output") is not None else args.get("Output")
        if isinstance(raw_out, str) and raw_out:
            exit_code = int(args.get("exit_code", 1 if had_error else 0))
            masked = lossless_test_distiller.mask_large_observation(cmd, raw_out, exit_code=exit_code)
            if masked.get("masked"):
                return {"warnings": [], "hard_violations": [], "masked_observation": masked}

    return {"warnings": [], "hard_violations": []}


def evaluate_stop(session_id: str, stop_hook_active: bool = False) -> dict:
    """
    Popperian Verification-Before-Completion Gate.
    Guaranteed zero infinite loops via BOTH `stop_hook_active` and `stop_gate_fired_turn`.
    """
    if stop_hook_active:
        return {"allow_stop": True, "reason": ""}

    st = load_session_state(session_id)
    turn_id = int(st.get("turn_index", 0))
    if st.get("stop_gate_fired_turn") == turn_id:
        return {"allow_stop": True, "reason": ""}

    hard_viols = st.get("hard_jidoka_violations") or []
    unverified = st.get("unverified_prod_edits") or []

    if hard_viols:
        st["stop_gate_fired_turn"] = turn_id
        save_session_state(session_id, st)
        reason = (
            f"[Jidoka Stop-the-Line Gate] Unresolved architectural/provenance violation(s): "
            f"{'; '.join(hard_viols[:3])}. Fix the violation before completing."
        )
        log_event("STOP_GATE_BLOCK_VIOLATION", session_id, reason)
        return {"allow_stop": False, "reason": reason}

    if unverified:
        st["stop_gate_fired_turn"] = turn_id
        save_session_state(session_id, st)
        short_files = ", ".join(os.path.basename(f) for f in unverified[:4])
        reason = (
            f"[Popperian Verification-Before-Completion Gate] Production code ({short_files}) was modified "
            f"without running a verification command (`cargo check/test`, `mvn test-compile`, or `kaizen-kit check`). "
            f"Run verification before concluding."
        )
        log_event("STOP_GATE_BLOCK_UNVERIFIED", session_id, reason)
        return {"allow_stop": False, "reason": reason}

    return {"allow_stop": True, "reason": ""}
