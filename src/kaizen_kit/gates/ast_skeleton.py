#!/usr/bin/env python3
"""
0-Token AST Skeleton Pruner (`ast_skeleton.py`) for Rust (`.rs`) and Java (`.java`).
Reduces context token footprint by ~80% by stripping function/method bodies while
preserving module docs, structs/records, traits/interfaces, and exact line ranges (`Lstart-Lend`).
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

RUST_SIG_RE = re.compile(
    r"^\s*(pub(?:\([^)]*\))?\s+)?(async\s+|const\s+)?(fn|struct|enum|trait|impl|type|mod)\s+([^{;]+)"
)
JAVA_SIG_RE = re.compile(
    r"^\s*(@\w+(?:\([^)]*\))?\s+)*(public|protected|private)?\s*(static\s+|final\s+|abstract\s+|sealed\s+)*(class|interface|record|enum|[\w<>\[\],\s]+\s+\w+\s*\([^)]*\))\s*([^{;]*)"
)


def extract_skeleton(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"// Error reading {path}: {e}"

    out = [f"// === AST SKELETON: {path.name} ({len(lines)} lines) ==="]
    brace_depth = 0
    skip_until_depth = None
    sig_start_line = 0
    sig_header = ""

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        open_b = line.count("{")
        close_b = line.count("}")

        if skip_until_depth is not None:
            brace_depth += open_b - close_b
            if brace_depth <= skip_until_depth:
                out.append(f"{sig_header} {{ ... }} // L{sig_start_line}-L{idx}")
                skip_until_depth = None
            continue

        if stripped.startswith("//!") or stripped.startswith("///"):
            out.append(f"L{idx}: {stripped}")
        elif path.suffix == ".rs" and RUST_SIG_RE.match(line):
            if " fn " in f" {stripped} " and open_b > close_b:
                sig_start_line = idx
                sig_header = line.split("{")[0].rstrip()
                skip_until_depth = brace_depth
            else:
                out.append(f"L{idx}: {stripped}")
        elif path.suffix == ".java" and JAVA_SIG_RE.match(line):
            if "(" in stripped and open_b > close_b and not any(k in stripped for k in ("class ", "interface ", "enum ")):
                sig_start_line = idx
                sig_header = line.split("{")[0].rstrip()
                skip_until_depth = brace_depth
            else:
                out.append(f"L{idx}: {stripped}")

        brace_depth += open_b - close_b

    raw_chars = sum(len(line) for line in lines)
    skel_chars = sum(len(line) for line in out)
    saved_pct = (1.0 - (skel_chars / max(raw_chars, 1))) * 100.0
    out.append(f"// === COMPRESSION: {raw_chars} -> {skel_chars} chars ({saved_pct:.1f}% token reduction) ===")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="0-Token AST Skeleton Pruner")
    parser.add_argument("target", help="File or directory to prune into AST skeleton")
    args = parser.parse_args(argv)

    p = Path(args.target)
    if p.is_file():
        print(extract_skeleton(p))
    elif p.is_dir():
        for f in sorted(p.rglob("*")):
            if f.suffix in (".rs", ".java") and "target" not in f.parts:
                print(extract_skeleton(f))
                print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
