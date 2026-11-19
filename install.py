#!/usr/bin/env python3
"""
Zero-Dependency Bootstrap Installer for `kaizen-engineering-kit`.

Usage:
  python install.py                  # Installs Claude Code + Cursor targets (preserving auth!)
  python install.py --target claude  # Installs Native Claude Code CLI target only
  python install.py --self-test      # Runs full doctor + all 7 gate self-tests
  python install.py --uninstall      # Cleanly removes kit hooks/agents while preserving auth
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from kaizen_kit import cli  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Bootstrap installer for Kaizen v4.0 AI Engineering Kit")
    ap.add_argument("--target", choices=["all", "claude", "cursor", "antigravity", "git-hooks"], default="all")
    ap.add_argument("--home", default="", help="Optional custom HOME path")
    ap.add_argument("--repo", default=".", help="Target repo for --target git-hooks")
    ap.add_argument("--self-test", action="store_true", help="Run doctor and all deterministic gate self-tests (including QLoRA pipeline)")
    ap.add_argument("--uninstall", action="store_true", help="Uninstall kit hooks cleanly")
    args = ap.parse_args()

    if args.self_test:
        return cli.main(["doctor", "--self-test"])
    if args.uninstall:
        cmd = ["uninstall"] + (["--home", args.home] if args.home else [])
        return cli.main(cmd)

    cmd = ["install", "--target", args.target]
    if args.home:
        cmd.extend(["--home", args.home])
    if args.target == "git-hooks":
        cmd.extend(["--repo", args.repo])
    rc = cli.main(cmd)
    if rc == 0 and not args.home:
        print("\n[OK] Installation complete. Running health check...")
        return cli.main(["doctor"])
    return rc


if __name__ == "__main__":
    sys.exit(main())
