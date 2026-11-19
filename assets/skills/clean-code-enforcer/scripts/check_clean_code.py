#!/usr/bin/env python3
"""
Deterministic Clean Code & Cognitive Complexity Scanner for Rust (.rs) and Java (.java).
Authoritatively grounded in:
  - Refactoring.Guru 22 Code Smells & 66 Refactorings (Bloaters, OO Abusers, Couplers)
  - John Ousterhout's A Philosophy of Software Design (APOSD)
  - Robert C. Martin's Clean Code (Cognitive Complexity, SLAP, Small Functions)
  - Eric Evans' DDD (Ubiquitous Language, Value Objects over Primitive Obsession)
  - Joshua Bloch's Effective Java & Official Rust API Guidelines

Checks:
  1. Function/Method length > 55 lines (Bloaters: Long Method)
  2. Control-flow nesting depth > 3 (Cognitive Complexity & Pyramid code)
  3. Long Parameter List (> 4 parameters) (Bloaters: Long Parameter List)
  4. Primitive Obsession in signatures (>= 3 primitive parameters)
  5. Law of Demeter Message Chains (.getA().getB().getC())
  6. Public mutable fields in domain models (Bloaters: Data Class / Lack of Encapsulation)
  7. Production .unwrap() in Rust (outside #[cfg(test)] and test modules/files)
  8. Cryptic domain naming (sta, thpt, rad, ev, gw, etc.)
  9. Virtual-thread carrier pinning (`synchronized`) or field `@Autowired` in Java 21
  10. Swallowed exceptions in Java (`catch (...) {}`)
"""

import argparse
import os
import re
import sys
from pathlib import Path

CRYPTIC_IDENT_RE = re.compile(
    r"\b(?:let|mut|var|final)\s+(sta|thpt|trh|rad|ev|dur|gw|cnt|cap|tmp2|res2)\b"
)
RUST_FN_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([a-zA-Z0-9_]+)\s*\(")
JAVA_METHOD_RE = re.compile(
    r"^\s*(?:public|protected|private)\s+(?:static\s+|final\s+|synchronized\s+|abstract\s+)*[\w<>\[\],\s]+\s+([a-zA-Z0-9_]+)\s*\("
)
JAVA_PUBLIC_MUTABLE_FIELD_RE = re.compile(
    r"^\s*public\s+(?!static\s+|final\s+|class\s+|interface\s+|enum\s+|record\s+|@)[\w<>\[\]]+\s+[a-zA-Z0-9_]+\s*(?:=.*)?;"
)
MESSAGE_CHAIN_RE = re.compile(
    r"\.get[A-Z][a-zA-Z0-9_]*\(\)\.get[A-Z][a-zA-Z0-9_]*\(\)\.get[A-Z][a-zA-Z0-9_]*\(\)"
)

RUST_PRIMITIVES = {
    "String", "&str", "str", "u8", "u16", "u32", "u64", "u128", "usize",
    "i8", "i16", "i32", "i64", "i128", "isize", "f32", "f64", "bool",
}

JAVA_PRIMITIVES = {
    "String", "int", "long", "boolean", "double", "float", "short", "byte", "char",
    "Integer", "Long", "Boolean", "Double", "Float",
}

PRUNE_DIRS = {
    "target", "node_modules", ".git", "build", ".venv", "__pycache__", ".idea", ".gradle"
}


def is_test_file(path: Path) -> bool:
    norm = str(path).replace("\\", "/").lower()
    return (
        "/tests/" in norm
        or "/test/" in norm
        or norm.endswith("_test.rs")
        or "test" in path.name.lower()
    )


def split_params(param_str: str) -> list:
    """Split comma-separated parameters honoring generic brackets <...> and parentheses."""
    params = []
    current = []
    depth = 0
    for char in param_str:
        if char in ("<", "(", "["):
            depth += 1
            current.append(char)
        elif char in (">", ")", "]"):
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            p = "".join(current).strip()
            if p:
                params.append(p)
            current = []
        else:
            current.append(char)
    last = "".join(current).strip()
    if last:
        params.append(last)
    return params


def scan_rust_file(path: Path) -> list:
    issues = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return issues

    in_test_mod = False
    in_struct_domain = False
    fn_name = None
    fn_start = 0
    brace_depth = 0
    fn_base_depth = 0
    max_control_nesting = 0
    is_domain = "domain" in str(path).lower() or "core" in str(path).lower()
    is_test = is_test_file(path)

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        if "#[cfg(test)]" in stripped or "mod tests" in stripped:
            in_test_mod = True

        if not in_test_mod and not is_test and ".unwrap()" in stripped:
            issues.append(f"{path}:{idx} [RUST-UNWRAP] Avoid `.unwrap()` in production code; use `?` or `let ... else`.")

        m_crypt = CRYPTIC_IDENT_RE.search(stripped)
        if m_crypt:
            issues.append(f"{path}:{idx} [CLEAN-NAMING] Cryptic variable `{m_crypt.group(1)}`; use full domain noun.")

        # Message Chain check
        if MESSAGE_CHAIN_RE.search(stripped):
            issues.append(f"{path}:{idx} [MESSAGE-CHAIN] Chained call `.getA().getB().getC()` violates Law of Demeter; hide delegate or move method.")

        # Rust domain public field check
        if is_domain and not in_test_mod and not is_test:
            if re.match(r"^\s*pub\s+struct\s+[a-zA-Z0-9_]+", line):
                in_struct_domain = True
            elif in_struct_domain and stripped.startswith("pub ") and ":" in stripped and "(" not in stripped:
                issues.append(f"{path}:{idx} [PUBLIC-FIELD] Public field in domain struct; encapsulate or use NewType pattern.")
            elif in_struct_domain and "}" in stripped:
                in_struct_domain = False

        m_fn = RUST_FN_RE.match(line)
        if m_fn and fn_name is None:
            fn_name = m_fn.group(1)
            fn_start = idx
            fn_base_depth = brace_depth
            max_control_nesting = 0

            # Collect signature lines if multiline
            sig_lines = [line]
            curr_idx = idx
            while ")" not in "".join(sig_lines) and curr_idx < len(lines):
                curr_idx += 1
                sig_lines.append(lines[curr_idx - 1])
            sig_text = " ".join(sig_lines)
            paren_start = sig_text.find("(")
            paren_end = sig_text.find(")", paren_start)
            if paren_start != -1 and paren_end != -1:
                sig_params = sig_text[paren_start + 1:paren_end]
                params = [p for p in split_params(sig_params) if p not in ("self", "&self", "&mut self")]
                if not in_test_mod and not is_test and len(params) > 4:
                    issues.append(
                        f"{path}:{idx} [LONG-PARAMETER-LIST] Function `{fn_name}` has {len(params)} parameters (budget <= 4); introduce parameter object or builder."
                    )
                # Check primitive obsession
                prim_count = 0
                for p in params:
                    if ":" in p:
                        ptype = p.split(":", 1)[1].strip().split()[0]
                        if ptype in RUST_PRIMITIVES:
                            prim_count += 1
                if not in_test_mod and not is_test and prim_count >= 3:
                    issues.append(
                        f"{path}:{idx} [PRIMITIVE-OBSESSION] Function `{fn_name}` has {prim_count} primitive parameters; use Value Objects or NewTypes."
                    )

        if fn_name and re.match(r"^(if|for|while|match)\b", stripped):
            rel_depth = brace_depth - fn_base_depth
            if rel_depth > max_control_nesting:
                max_control_nesting = rel_depth

        brace_depth += line.count("{") - line.count("}")
        if fn_name and brace_depth <= fn_base_depth and idx > fn_start:
            fn_len = idx - fn_start + 1
            if not in_test_mod and not is_test and fn_len > 55:
                issues.append(f"{path}:{fn_start} [FN-LENGTH] Function `{fn_name}` is {fn_len} lines (budget <= 55L); extract helpers.")
            if not in_test_mod and not is_test and max_control_nesting > 3:
                issues.append(f"{path}:{fn_start} [COGNITIVE-PYRAMID] Function `{fn_name}` has control nesting depth {max_control_nesting}; flatten with guard clauses.")
            fn_name = None

    return issues


def scan_java_file(path: Path) -> list:
    issues = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return issues

    is_test = is_test_file(path)
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue

        if not is_test and "@Autowired" in stripped:
            issues.append(f"{path}:{idx} [JAVA-DI] Avoid `@Autowired` field injection; use constructor injection with `private final` fields.")
        if not is_test and re.search(r"\bsynchronized\b", stripped):
            issues.append(f"{path}:{idx} [LOOM-PINNING] Avoid `synchronized` in Java 21 Virtual Threads (causes carrier pinning); use `ReentrantLock`.")
        if re.search(r"catch\s*\([^)]+\)\s*\{\s*\}", stripped):
            issues.append(f"{path}:{idx} [SWALLOWED-EXCEPTION] Empty `catch` block swallows exceptions.")

        m_crypt = CRYPTIC_IDENT_RE.search(stripped)
        if m_crypt:
            issues.append(f"{path}:{idx} [CLEAN-NAMING] Cryptic variable `{m_crypt.group(1)}`; use full domain noun.")

        # Message Chain check
        if MESSAGE_CHAIN_RE.search(stripped):
            issues.append(f"{path}:{idx} [MESSAGE-CHAIN] Chained call `.getA().getB().getC()` violates Law of Demeter; hide delegate or move method.")

        # Public mutable fields in non-test Java classes
        if not is_test and JAVA_PUBLIC_MUTABLE_FIELD_RE.match(line):
            issues.append(f"{path}:{idx} [MUTABLE-PUBLIC-FIELD] Public mutable field; encapsulate with private final fields or use Java 21 record.")

        # Java method parameter checks
        m_meth = JAVA_METHOD_RE.match(line)
        if m_meth and not is_test:
            meth_name = m_meth.group(1)
            sig_lines = [line]
            curr_idx = idx
            while ")" not in "".join(sig_lines) and curr_idx < len(lines):
                curr_idx += 1
                sig_lines.append(lines[curr_idx - 1])
            sig_text = " ".join(sig_lines)
            paren_start = sig_text.find("(")
            paren_end = sig_text.find(")", paren_start)
            if paren_start != -1 and paren_end != -1:
                sig_params = sig_text[paren_start + 1:paren_end]
                params = split_params(sig_params)
                if len(params) > 4:
                    issues.append(
                        f"{path}:{idx} [LONG-PARAMETER-LIST] Method `{meth_name}` has {len(params)} parameters (budget <= 4); introduce parameter record or builder."
                    )
                prim_count = 0
                for p in params:
                    tokens = p.strip().split()
                    for t in tokens:
                        if t in JAVA_PRIMITIVES:
                            prim_count += 1
                            break
                if prim_count >= 3:
                    issues.append(
                        f"{path}:{idx} [PRIMITIVE-OBSESSION] Method `{meth_name}` has {prim_count} primitive parameters; use Value Objects or Java 21 records."
                    )

    return issues


def collect_source_files(target: Path) -> list:
    """Efficiently collect .rs and .java files, pruning build and VCS directories early."""
    if target.is_file():
        return [target] if target.suffix in (".rs", ".java") else []

    collected = []
    for root, dirs, files in os.walk(str(target)):
        # In-place prune directories so os.walk does not descend into target, .git, etc.
        dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
        for f in files:
            if f.endswith(".rs") or f.endswith(".java"):
                collected.append(Path(root) / f)
    return collected


def main():
    parser = argparse.ArgumentParser(description="Clean Code & Cognitive Complexity Scanner")
    parser.add_argument("target", nargs="?", default=".", help="File or directory to scan")
    parser.add_argument("--max-issues", type=int, default=30)
    parser.add_argument(
        "--exit-zero",
        "--allow-violations",
        dest="exit_zero",
        action="store_true",
        help="Exit with 0 even if violations are found (advisory mode)",
    )
    args = parser.parse_args()

    target = Path(args.target).resolve()
    files = collect_source_files(target)
    all_issues = []

    for f in files:
        if f.suffix == ".rs":
            all_issues.extend(scan_rust_file(f))
        elif f.suffix == ".java":
            all_issues.extend(scan_java_file(f))

    if all_issues:
        print(f"[CLEAN-CODE] Found {len(all_issues)} findings (showing top {args.max_issues}):")
        for issue in all_issues[: args.max_issues]:
            print(f"  - {issue}")
        if not args.exit_zero:
            sys.exit(1)
        sys.exit(0)
    else:
        print("[CLEAN-CODE] Zero Clean Code violations found.")
        sys.exit(0)


if __name__ == "__main__":
    main()
