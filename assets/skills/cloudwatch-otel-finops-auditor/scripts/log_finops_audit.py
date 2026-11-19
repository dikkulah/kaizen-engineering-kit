#!/usr/bin/env python3
"""
CloudWatch, OpenTelemetry & Logback FinOps Auditor (`log_finops_audit.py`).
Scans local workspaces for expensive logging patterns (DEBUG/TRACE levels, un-sampled hot-loop logs)
and optionally queries read-only AWS CloudWatch describe-log-groups for retention and storage FinOps ($).
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

DEBUG_XML_RE = re.compile(r'level\s*=\s*["\'](?:DEBUG|TRACE|ALL)["\']', re.IGNORECASE)
YAML_DEBUG_RE = re.compile(r":\s*(?:DEBUG|TRACE)\b")


def scan_workspace_logs(workspace: Path) -> dict:
    logback_files = []
    noisy_configs = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [
            d for d in dirs
            if d not in (".git", "target", "node_modules", "dist", ".cargo", "build", ".venv")
        ]
        for fname in files:
            low = fname.lower()
            fpath = Path(root) / fname
            if "logback" in low and low.endswith(".xml"):
                logback_files.append(str(fpath))
                txt = fpath.read_text(encoding="utf-8", errors="replace")
                hits = DEBUG_XML_RE.findall(txt)
                if hits:
                    noisy_configs.append((str(fpath), f"{len(hits)} DEBUG/TRACE logger rule(s)"))
            elif low.startswith("application") and low.endswith((".yml", ".yaml", ".properties")):
                txt = fpath.read_text(encoding="utf-8", errors="replace")
                if "logging" in txt.lower() and YAML_DEBUG_RE.search(txt):
                    noisy_configs.append((str(fpath), "YAML logging level set to DEBUG/TRACE"))
    return {
        "logback_count": len(logback_files),
        "noisy_configs": noisy_configs,
    }


def scan_cloudwatch_groups(profile: str) -> list:
    try:
        cmd = [
            "aws", "logs", "describe-log-groups",
            "--profile", profile,
            "--query", "logGroups[*].[logGroupName,storedBytes,retentionInDays]",
            "--output", "json",
        ]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if p.returncode != 0:
            return []
        import json
        rows = json.loads(p.stdout) or []
        rows.sort(key=lambda x: x[1] or 0, reverse=True)
        return rows[:15]
    except Exception:
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description="CloudWatch, OTEL & Logback FinOps Auditor")
    ap.add_argument("--workspace", default="${WORKSPACE_ROOT}", help="Workspace directory to scan")
    ap.add_argument("--aws-profile", default="", help="Optional read-only AWS profile for CloudWatch check")
    args = ap.parse_args()

    ws = Path(args.workspace)
    res = scan_workspace_logs(ws) if ws.exists() else {"logback_count": 0, "noisy_configs": []}

    print("=" * 95)
    print(f"CLOUDWATCH, OTEL & LOGBACK FINOPS AUDIT REPORT ({ws})")
    print("=" * 95)
    print(f"  Taranan logback.xml Dosya Sayısı : {res['logback_count']}")
    print(f"  Gürültülü / DEBUG/TRACE Konfig   : {len(res['noisy_configs'])}")
    for path_str, reason in res["noisy_configs"][:15]:
        print(f"    - {path_str}: {reason}")

    if args.aws_profile:
        cw = scan_cloudwatch_groups(args.aws_profile)
        print(f"\n  AWS CloudWatch Log Groups (Profile: {args.aws_profile}, İlk {len(cw)}):")
        for name, sbytes, ret in cw:
            gb = (sbytes or 0) / (1024 ** 3)
            ret_str = f"{ret}d" if ret else "NEVER_EXPIRE (!)"
            print(f"    - {name:52s} | {gb:8.2f} GB | Retention: {ret_str}")
    print("=" * 95)
    return 0


if __name__ == "__main__":
    sys.exit(main())
