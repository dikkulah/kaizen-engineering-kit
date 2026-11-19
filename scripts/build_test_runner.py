#!/usr/bin/env python3
"""
build_test_runner.py - Polyglot Multi-Stack Pre-Flight Build & Test Runner.

Auto-detects repository technology stack (Maven Java, NPM/Vitest, Cargo Rust,
AWS SAM, Python), verifies build tool dependencies, constructs the appropriate
clean/compile/test pipeline, and executes pre-flight checks before Git push.
"""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional


def detect_project_stack(project_dir: Path) -> Optional[str]:
    """Auto-detect build technology stack from signature marker files."""
    if (project_dir / "pom.xml").exists():
        return "maven"
    if (project_dir / "Cargo.toml").exists():
        return "cargo"
    if (project_dir / "package.json").exists():
        return "npm"
    if (project_dir / "template.yaml").exists() or (project_dir / "template.yml").exists() or (project_dir / "samconfig.toml").exists():
        return "sam"
    if (project_dir / "pyproject.toml").exists() or (project_dir / "setup.py").exists() or (project_dir / "requirements.txt").exists():
        return "python"
    return None


def build_pipeline_commands(
    stack: str,
    clean: bool,
    skip_tests: bool,
    test_target: Optional[str],
) -> list[list[str]]:
    """Construct command pipeline based on technology stack and flags."""
    commands = []

    if stack == "maven":
        mvn_bin = "mvn.cmd" if sys.platform == "win32" else "mvn"
        base_cmd = [mvn_bin]
        if clean:
            commands.append(base_cmd + ["clean"])
        if skip_tests:
            commands.append(base_cmd + ["test-compile", "-DskipTests"])
        elif test_target:
            commands.append(base_cmd + ["test", f"-Dtest={test_target}"])
        else:
            commands.append(base_cmd + ["test-compile"])
            commands.append(base_cmd + ["test"])

    elif stack == "npm":
        npm_bin = "npm.cmd" if sys.platform == "win32" else "npm"
        if clean:
            commands.append([npm_bin, "run", "clean"])
        commands.append([npm_bin, "run", "typecheck"])
        if not skip_tests:
            test_cmd = [npm_bin, "test"]
            if test_target:
                test_cmd.extend(["--", test_target])
            commands.append(test_cmd)

    elif stack == "cargo":
        cargo_bin = "cargo.exe" if sys.platform == "win32" else "cargo"
        if clean:
            commands.append([cargo_bin, "clean"])
        commands.append([cargo_bin, "check"])
        if not skip_tests:
            test_cmd = [cargo_bin, "test"]
            if test_target:
                test_cmd.extend(["--test", test_target])
            commands.append(test_cmd)

    elif stack == "sam":
        sam_bin = "sam.cmd" if sys.platform == "win32" else "sam"
        commands.append([sam_bin, "validate"])
        commands.append([sam_bin, "build"])
        if not skip_tests and test_target:
            commands.append([sam_bin, "local", "invoke", test_target])

    elif stack == "python":
        py_bin = sys.executable
        if clean:
            commands.append([py_bin, "-c", "import shutil, pathlib; [shutil.rmtree(p) for p in pathlib.Path('.').glob('**/__pycache__')]"])
        if not skip_tests:
            if shutil.which("pytest"):
                test_cmd = ["pytest"]
                if test_target:
                    test_cmd.append(test_target)
                commands.append(test_cmd)
            else:
                test_cmd = [py_bin, "-m", "unittest"]
                if test_target:
                    test_cmd.extend(["-k", test_target])
                commands.append(test_cmd)

    return commands


def parse_arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Unified multi-stack pre-flight test and lint runner.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--project-dir",
        type=str,
        default=".",
        help="Directory of the target project to validate.",
    )
    parser.add_argument(
        "--stack",
        type=str,
        choices=["auto", "maven", "npm", "cargo", "sam", "python"],
        default="auto",
        help="Target build stack or 'auto' for automatic file detection.",
    )
    parser.add_argument(
        "--test-target",
        type=str,
        default=None,
        help="Specific test class, target file, or pattern to execute.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        default=False,
        help="Compile and lint only, skipping test execution.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        default=False,
        help="Execute clean lifecycle phase prior to compilation.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Inspect project and display command pipeline without executing commands.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Main execution entrypoint."""
    args = parse_arguments(argv)

    project_path = Path(args.project_dir).resolve()
    if not project_path.exists():
        print(f"[ERROR] Project directory does not exist: {args.project_dir}", file=sys.stderr)
        return 1

    if not project_path.is_dir():
        print(f"[ERROR] Project path is not a directory: {args.project_dir}", file=sys.stderr)
        return 1

    resolved_stack = args.stack
    if resolved_stack == "auto":
        detected = detect_project_stack(project_path)
        if not detected:
            print(f"[ERROR] Could not auto-detect build stack in '{project_path}'. Specify with --stack.", file=sys.stderr)
            return 1
        resolved_stack = detected

    commands = build_pipeline_commands(
        stack=resolved_stack,
        clean=args.clean,
        skip_tests=args.skip_tests,
        test_target=args.test_target,
    )

    if not commands:
        print(f"[ERROR] No pipeline commands constructed for stack '{resolved_stack}'.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("[DRY-RUN] Multi-Stack Build & Test Plan:")
        print(f"[DRY-RUN] - Target directory: {project_path}")
        print(f"[DRY-RUN] - Resolved stack: {resolved_stack}")
        print(f"[DRY-RUN] - Clean phase: {args.clean}")
        print(f"[DRY-RUN] - Skip tests: {args.skip_tests}")
        print(f"[DRY-RUN] - Test target: {args.test_target or 'all'}")
        print(f"[DRY-RUN] - Planned command pipeline ({len(commands)} step(s)):")
        for i, cmd in enumerate(commands, 1):
            print(f"[DRY-RUN]   {i}. {' '.join(cmd)}")
        print("[DRY-RUN] Simulation completed successfully.")
        return 0

    print(f"=== Multi-Stack Runner: {resolved_stack.upper()} ({project_path.name}) ===")
    for step_num, cmd in enumerate(commands, 1):
        cmd_str = " ".join(cmd)
        print(f"\n[STEP {step_num}/{len(commands)}] Running: {cmd_str}")

        # Check binary presence
        tool_name = cmd[0]
        if not shutil.which(tool_name):
            print(f"[ERROR] Required tool binary '{tool_name}' not found in PATH.", file=sys.stderr)
            return 1

        proc = subprocess.run(cmd, cwd=str(project_path), check=False)
        if proc.returncode != 0:
            print(f"[FAIL] Command failed with exit code {proc.returncode}: {cmd_str}", file=sys.stderr)
            return proc.returncode

    print(f"\n[SUCCESS] All pipeline steps completed successfully for {resolved_stack}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
