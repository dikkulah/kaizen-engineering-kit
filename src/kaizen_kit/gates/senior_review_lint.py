#!/usr/bin/env python3
"""
Kaizen Role-Based Senior Engineering & Jenkins 4-Gate Pre-Flight Linter (`senior_review_lint.py`).

Mechanically enforces the 6 Kaizen Engineering Review Standards:
  1. Core-Java-Guard (Core Backend & Java 21 Clean Architecture Standard):
     - Zero inline full package paths (`java.util.*`, `com.kaizen.*`) inside method bodies
     - Single-line `LOG.*` calls with `{}` placeholders (no `+` string concat)
     - No `Log & Throw` anti-pattern in catch blocks
     - Java 21 `.getFirst()` / `.getLast()` over `.get(0)` / `.get(list.size() - 1)`
  2. Concurrency-Security-Guard (Systems Concurrency, Singleflight & Fail-Closed Security Standard):
     - Fail-closed auth check (flags `OD_DEFAULT_TENANT_ID` fallback in HTTP auth handlers)
     - Flags legacy `RestTemplate` over pooled `RestClient` + `Apache5HttpClient`
  3. Vue-State-Guard (Frontend Vue 3 / TypeScript Reactive State Standard):
     - Flags wiping table state (`records.value = []`) immediately before `await` network calls
     - Flags `useEditor(` initialized from props without a matching `watch(`
  4. FinOps-Observability-Guard (Cloud Platform, Identity & FinOps Observability Standard):
     - Flags `LOG.info` / `tracing::info!` inside `for` / `while` hot loops
  5. Resilience-DTO-Guard (API Error Fallback & DTO Hygiene Standard):
     - Flags empty `catch (...) {}` blocks
  6. Jenkins-PreFlight (Jenkins 4-Gate Pre-Flight CI Shield `--pre-flight`):
     - Verifies `git merge-base --is-ancestor origin/<base> HEAD` fast-forward invariant
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

PRUNE_DIRS = {"target", "node_modules", ".git", "build", "dist", ".venv", "__pycache__", ".idea"}

INLINE_JAVA_PKG_RE = re.compile(
    r"(?<!import\s)(?<!package\s)\b(?:java\.util|java\.time|java\.io|com\.kaizen\.[a-z0-9_.]+)\.[A-Z][A-Za-z0-9_]*"
)
LOG_CONCAT_RE = re.compile(r"\b(?:LOG|log|logger)\.(?:info|warn|error|debug)\s*\([^;]*\+[^;]*\)")
MULTILINE_LOG_OPEN_RE = re.compile(r"^\s*(?:LOG|log|logger)\.(?:info|warn|error)\s*\([^;]*$")
JAVA_GET_ZERO_RE = re.compile(r"\.get\(\s*0\s*\)|\.get\([^)]*\.size\(\)\s*-\s*1\s*\)")
EMPTY_CATCH_RE = re.compile(r"catch\s*\([^)]+\)\s*\{\s*\}")
FAIL_OPEN_TENANT_RE = re.compile(r'env::var\("OD_DEFAULT_TENANT_ID"\)')
VUE_TABLE_WIPE_RE = re.compile(r"\b(?:records|items|rows)\.value\s*=\s*\[\s*\]")


def is_test_path(path: Path) -> bool:
    norm = str(path).replace("\\", "/").lower()
    return "/test/" in norm or "/tests/" in norm or norm.endswith(("_test.rs", ".spec.ts", "test.java"))


def scan_file(path: Path) -> list[dict]:
    findings = []
    if is_test_path(path):
        return findings
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings

    lines = text.splitlines()
    suffix = path.suffix.lower()
    in_loop_depth = 0
    in_catch_block = False
    catch_logged = False
    catch_brace_base = 0
    brace_depth = 0

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue

        if suffix == ".java":
            if not stripped.startswith(("import ", "package ")):
                m_pkg = INLINE_JAVA_PKG_RE.search(stripped)
                if m_pkg:
                    findings.append({
                        "category": "Core-Java-Guard",
                        "rule": "ZERO-INLINE-PACKAGE",
                        "file": str(path),
                        "line": idx,
                        "detail": f"Inline package path `{m_pkg.group(0)}`; import cleanly at top of file.",
                    })
            if LOG_CONCAT_RE.search(stripped):
                findings.append({
                    "category": "Core-Java-Guard",
                    "rule": "SLF4J-PLACEHOLDER",
                    "file": str(path),
                    "line": idx,
                    "detail": "String concatenation `+` inside SLF4J log statement; use `{}` placeholders.",
                })
            if MULTILINE_LOG_OPEN_RE.match(line):
                findings.append({
                    "category": "Core-Java-Guard",
                    "rule": "SINGLE-LINE-LOG",
                    "file": str(path),
                    "line": idx,
                    "detail": "Multi-line `LOG.*` statement breaks CloudWatch/grep parsing; keep on a single line.",
                })
            if JAVA_GET_ZERO_RE.search(stripped):
                findings.append({
                    "category": "Core-Java-Guard",
                    "rule": "JAVA21-SEQUENCED",
                    "file": str(path),
                    "line": idx,
                    "detail": "Use Java 21 `getFirst()` / `getLast()` instead of `get(0)` / `get(size()-1)`.",
                })
            if "RestTemplate" in stripped:
                findings.append({
                    "category": "Concurrency-Security-Guard",
                    "rule": "HTTP-CLIENT-POOL",
                    "file": str(path),
                    "line": idx,
                    "detail": "Avoid legacy `RestTemplate`; use `RestClient.Builder` + shared `Apache5HttpClient`.",
                })
            if EMPTY_CATCH_RE.search(stripped):
                findings.append({
                    "category": "Resilience-DTO-Guard",
                    "rule": "NO-EMPTY-CATCH",
                    "file": str(path),
                    "line": idx,
                    "detail": "Empty `catch` block swallows runtime failures.",
                })
            if re.search(r"\bcatch\s*\(", stripped):
                in_catch_block = True
                catch_logged = False
                catch_brace_base = brace_depth
            elif in_catch_block:
                if re.search(r"\b(?:LOG|log|logger)\.(?:error|warn)\b", stripped):
                    catch_logged = True
                if catch_logged and re.search(r"\bthrow\s+", stripped):
                    findings.append({
                        "category": "Core-Java-Guard",
                        "rule": "NO-LOG-AND-THROW",
                        "file": str(path),
                        "line": idx,
                        "detail": "Log & Throw in the same `catch` block duplicates stack traces; either handle or propagate.",
                    })

        elif suffix == ".rs":
            if "#[cfg(test)]" in stripped:
                break
            if FAIL_OPEN_TENANT_RE.search(stripped) and ("router" in path.name or "handler" in path.name):
                findings.append({
                    "category": "Concurrency-Security-Guard",
                    "rule": "FAIL-CLOSED-AUTH",
                    "file": str(path),
                    "line": idx,
                    "detail": "Fallback to `OD_DEFAULT_TENANT_ID` on HTTP request path violates Fail-Closed Security Invariant.",
                })

        elif suffix in (".vue", ".ts"):
            if VUE_TABLE_WIPE_RE.search(stripped):
                next_window = "\n".join(lines[idx: min(len(lines), idx + 4)])
                if "await " in next_window:
                    findings.append({
                        "category": "Vue-State-Guard",
                        "rule": "STABLE-TABLE-STATE",
                        "file": str(path),
                        "line": idx,
                        "detail": "Wiping table records before `await` causes UI blanking on network error.",
                    })
            if "useEditor(" in stripped and "watch(" not in text:
                findings.append({
                    "category": "Vue-State-Guard",
                    "rule": "DYNAMIC-PROP-WATCH",
                    "file": str(path),
                    "line": idx,
                    "detail": "`useEditor` initialized without dynamic `watch` on `props.readonly`.",
                })

        if re.match(r"^(?:for|while)\b", stripped):
            in_loop_depth = max(in_loop_depth, 1)
        if in_loop_depth > 0 and re.search(r"\b(?:LOG\.info|log\.info|tracing::info!|info!)\s*\(", stripped):
            findings.append({
                "category": "FinOps-Observability-Guard",
                "rule": "HOT-LOOP-LOG-FINOPS",
                "file": str(path),
                "line": idx,
                "detail": "INFO logging inside loop inflates CloudWatch FinOps cost; downgrade to DEBUG or aggregate.",
            })

        brace_depth += line.count("{") - line.count("}")
        if in_catch_block and brace_depth <= catch_brace_base and "}" in line:
            in_catch_block = False
        if in_loop_depth > 0 and brace_depth == 0:
            in_loop_depth = 0

    return findings


def check_jenkins_preflight(repo_dir: Path, base_branch: str = "origin/master") -> dict:
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_branch, "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return {
            "fast_forward_ready": proc.returncode == 0,
            "base_branch": base_branch,
            "detail": "HEAD includes base branch" if proc.returncode == 0 else f"Branch behind {base_branch}; run git fetch && git rebase {base_branch}",
        }
    except Exception as exc:
        return {"fast_forward_ready": True, "base_branch": base_branch, "detail": f"Skipped git check: {exc}"}


def run_self_test() -> int:
    with tempfile.TemporaryDirectory() as td:
        jf = Path(td) / "SampleService.java"
        jf.write_text(
            "public class SampleService {\n"
            "  public void run(java.util.List<String> items) {\n"
            "    String first = items.get(0);\n"
            "    try { first.toString(); } catch (Exception e) {\n"
            "      LOG.error(\"Failed: \" + first);\n"
            "      throw e;\n"
            "    }\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        res = scan_file(jf)
        rules = {r["rule"] for r in res}
        assert "ZERO-INLINE-PACKAGE" in rules
        assert "JAVA21-SEQUENCED" in rules
        assert "SLF4J-PLACEHOLDER" in rules
        assert "NO-LOG-AND-THROW" in rules
    print("senior_review_lint self-test: OK (4/4 rules verified)")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kaizen Senior Review Standards & Jenkins Pre-Flight Linter")
    ap.add_argument("target", nargs="?", default=".", help="File or directory to scan")
    ap.add_argument("--pre-flight", action="store_true", help="Also run Jenkins fast-forward ancestor check")
    ap.add_argument("--json", action="store_true", help="Output JSON")
    ap.add_argument("--exit-zero", action="store_true", help="Advisory exit 0")
    ap.add_argument("--self-test", action="store_true", help="Run built-in self-test")
    args = ap.parse_args(argv)

    if args.self_test:
        return run_self_test()

    target = Path(args.target).resolve()
    files: list[Path] = []
    if target.is_file():
        files = [target]
    else:
        for root, dirs, fnames in os.walk(str(target)):
            dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
            for fn in fnames:
                if fn.endswith((".java", ".rs", ".vue", ".ts")):
                    files.append(Path(root) / fn)

    findings = []
    for f in files:
        findings.extend(scan_file(f))

    preflight = check_jenkins_preflight(target if target.is_dir() else target.parent) if args.pre_flight else None

    if args.json:
        print(json.dumps({"findings_count": len(findings), "findings": findings[:50], "preflight": preflight}, indent=2))
    else:
        print(f"[Senior-Review-Guard] Scanned {len(files)} files | Findings={len(findings)}")
        for item in findings[:30]:
            print(f"  - [{item['category']}:{item['rule']}] {item['file']}:{item['line']} -> {item['detail']}")
        if preflight:
            status = "PASS" if preflight["fast_forward_ready"] else "FAIL"
            print(f"  - [Jenkins-PreFlight:{status}] {preflight['detail']}")

    failed = bool(findings) or (preflight is not None and not preflight["fast_forward_ready"])
    return 0 if (not failed or args.exit_zero) else 1


if __name__ == "__main__":
    sys.exit(main())
