#!/usr/bin/env python3
"""
git_sync_rebase.py - Safe Git Branch Rebase and Pre-Flight Fast-Forward Checker.

Automates safe branch synchronization with upstream base branch, uncommitted work
preservation via stashing, rebase execution, and fast-forward validation to prevent
Jenkins CI Validation Gate 1 failures.
"""

import argparse
import re
import shutil
import subprocess
import sys
from typing import Optional


def validate_branch_name(branch_name: str) -> bool:
    """Validate git branch name syntax."""
    if not branch_name or not branch_name.strip():
        return False
    # Reject illegal git branch characters
    illegal_pattern = r"[\s~^:?*\[\\]|(\.\.)|(@\{)"
    if re.search(illegal_pattern, branch_name):
        return False
    return True


def run_git_cmd(cmd: list[str], dry_run: bool = False) -> tuple[int, str, str]:
    """Execute a git command or simulate in dry-run mode."""
    if dry_run:
        print(f"[DRY-RUN] Would execute: git {' '.join(cmd)}")
        return 0, "", ""
    proc = subprocess.run(
        ["git"] + cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def parse_arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Safely rebase current branch against upstream base branch with stash preservation.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base-branch",
        type=str,
        default="master",
        help="Upstream base branch to rebase against.",
    )
    parser.add_argument(
        "--remote",
        type=str,
        default="origin",
        help="Git remote name.",
    )
    parser.add_argument(
        "--auto-stash",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Automatically stash uncommitted changes before rebase and pop afterwards.",
    )
    parser.add_argument(
        "--check-fast-forward",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Validate that HEAD is a fast-forward descendant of upstream base.",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        default=False,
        help="Push rebased branch to remote using --force-with-lease.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate rebase actions without modifying repository state.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Main execution entrypoint."""
    args = parse_arguments(argv)

    if not validate_branch_name(args.base_branch):
        print(f"[ERROR] Invalid base branch name: '{args.base_branch}'", file=sys.stderr)
        return 1

    if not args.remote or not args.remote.strip():
        print(f"[ERROR] Remote name cannot be empty.", file=sys.stderr)
        return 1

    if not shutil.which("git"):
        print("[ERROR] Git binary not found in system PATH.", file=sys.stderr)
        return 1

    # In dry run mode, display simulated pipeline and return 0
    if args.dry_run:
        print("[DRY-RUN] Git Sync & Rebase Plan:")
        print(f"[DRY-RUN] 1. Check current branch status")
        if args.auto_stash:
            print(f"[DRY-RUN] 2. Stash unstaged/staged changes ('git stash push -u')")
        print(f"[DRY-RUN] 3. Fetch upstream branch: git fetch {args.remote} {args.base_branch}")
        print(f"[DRY-RUN] 4. Rebase against: git rebase {args.remote}/{args.base_branch}")
        if args.check_fast_forward:
            print(f"[DRY-RUN] 5. Verify fast-forward: git merge-base --is-ancestor {args.remote}/{args.base_branch} HEAD")
        if args.auto_stash:
            print(f"[DRY-RUN] 6. Pop stashed changes: git stash pop")
        if args.push:
            print(f"[DRY-RUN] 7. Push rebased commits: git push --force-with-lease {args.remote} HEAD")
        print("[DRY-RUN] Simulation completed successfully.")
        return 0

    # Check git repository
    rc, stdout, stderr = run_git_cmd(["rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        print(f"[ERROR] Not inside a git repository: {stderr}", file=sys.stderr)
        return 1

    # Check if working tree has changes
    rc, status_out, _ = run_git_cmd(["status", "--porcelain"])
    has_dirty_worktree = bool(status_out)
    stashed = False

    if has_dirty_worktree and args.auto_stash:
        print("[INFO] Working tree is dirty. Auto-stashing uncommitted changes...")
        rc, _, err = run_git_cmd(["stash", "push", "-u", "-m", "git_sync_rebase_autostash"])
        if rc != 0:
            print(f"[ERROR] Failed to stash changes: {err}", file=sys.stderr)
            return 1
        stashed = True

    # Fetch upstream
    print(f"[INFO] Fetching {args.remote} {args.base_branch}...")
    rc, _, err = run_git_cmd(["fetch", args.remote, args.base_branch])
    if rc != 0:
        print(f"[ERROR] Fetch failed: {err}", file=sys.stderr)
        if stashed:
            run_git_cmd(["stash", "pop"])
        return 1

    # Rebase
    upstream_ref = f"{args.remote}/{args.base_branch}"
    print(f"[INFO] Rebasing onto {upstream_ref}...")
    rc, _, err = run_git_cmd(["rebase", upstream_ref])
    if rc != 0:
        print(f"[ERROR] Rebase conflict or failure: {err}", file=sys.stderr)
        print("[INFO] Aborting rebase to preserve branch state...")
        run_git_cmd(["rebase", "--abort"])
        if stashed:
            run_git_cmd(["stash", "pop"])
        return 1

    # Check fast forward
    if args.check_fast_forward:
        print(f"[INFO] Validating fast-forward against {upstream_ref}...")
        rc, _, _ = run_git_cmd(["merge-base", "--is-ancestor", upstream_ref, "HEAD"])
        if rc != 0:
            print(f"[ERROR] Fast-forward verification failed: HEAD is not a descendant of {upstream_ref}", file=sys.stderr)
            if stashed:
                run_git_cmd(["stash", "pop"])
            return 1
        print("[INFO] Fast-forward verification PASSED.")

    # Pop stash if needed
    if stashed:
        print("[INFO] Restoring stashed changes...")
        rc, _, err = run_git_cmd(["stash", "pop"])
        if rc != 0:
            print(f"[WARNING] Conflict during stash pop. Stash preserved in stash list: {err}", file=sys.stderr)

    # Push if requested
    if args.push:
        print(f"[INFO] Pushing with --force-with-lease to {args.remote}...")
        rc, _, err = run_git_cmd(["push", "--force-with-lease", args.remote, "HEAD"])
        if rc != 0:
            print(f"[ERROR] Push failed: {err}", file=sys.stderr)
            return 1
        print("[INFO] Push completed successfully.")

    print("[SUCCESS] Branch synchronized and rebased successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
