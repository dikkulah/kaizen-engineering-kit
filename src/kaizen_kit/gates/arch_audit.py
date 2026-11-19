#!/usr/bin/env python3
"""
Deterministic Clean Architecture & Refactoring.Guru Structural Smell Engine (`arch_audit.py`).

Implements mathematical & AST-heuristic enforcement of:
  1. `[CA-ADP]` Acyclic Dependencies Principle — Cycle detection across Rust/Java/TS components
  2. `[CA-DEP-RULE]` Inward Dependency Law — Domain/Entity/Cube isolation from HTTP/AWS/Web frameworks
  3. `[CA-SDP]` & `[CA-SAP]` Robert C. Martin Main Sequence Metrics — Ca, Ce, I = Ce/(Ca+Ce), A = Na/Nc, D = |A+I-1|
  4. `[CC-OD3]` / `[RG-SM21]` Law of Demeter — Train-wreck message chains (`a.b().c().d()`)
  5. `[CC-F3]` / `[RG-RF46]` Flag Arguments — Public functions accepting raw `bool`/`boolean` selector flags
  6. `[RG-SM05]` Data Clumps — Repeated 3+ parameter groups across >= 3 functions
"""

import argparse
import itertools
import json
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

SKIP_DIRS = {
    ".git",
    "target",
    "node_modules",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    ".idea",
    ".vscode",
}

INNER_DOMAIN_COMPONENTS = {"domain", "entity", "entities", "valueobject", "vo"}
PURE_MODEL_COMPONENTS = {"domain", "entity", "entities", "cube"}
OUTER_ADAPTER_COMPONENTS = {
    "http",
    "rest",
    "controller",
    "controllers",
    "web",
    "servlet",
    "ui",
    "handler",
    "handlers",
    "router",
}

FORBIDDEN_OUTER_IMPORTS_IN_PURE = re.compile(
    r"\b(?:aws_sdk_[a-z0-9_]+|lambda_http|lambda_runtime|actix_web|axum|reqwest|"
    r"org\.springframework\.web|jakarta\.servlet|javax\.servlet|org\.apache\.hc|"
    r"software\.amazon\.awssdk|axios)\b"
)

FLUENT_SAFE_METHODS = {
    # Rust iterators / Option / Result / builders / string views
    "iter",
    "iter_mut",
    "into_iter",
    "par_iter",
    "filter",
    "filter_map",
    "flat_map",
    "map",
    "map_err",
    "map_or",
    "map_or_else",
    "and_then",
    "or_else",
    "unwrap_or",
    "unwrap_or_default",
    "unwrap_or_else",
    "ok",
    "ok_or",
    "ok_or_else",
    "collect",
    "cloned",
    "copied",
    "enumerate",
    "take",
    "skip",
    "zip",
    "chain",
    "peekable",
    "fold",
    "reduce",
    "sum",
    "count",
    "any",
    "all",
    "find",
    "position",
    "max",
    "min",
    "as_ref",
    "as_deref",
    "as_str",
    "as_bytes",
    "as_slice",
    "to_string",
    "to_owned",
    "to_lowercase",
    "to_ascii_lowercase",
    "to_uppercase",
    "trim",
    "trim_start",
    "trim_end",
    "split",
    "lines",
    "chars",
    "bytes",
    "clone",
    "into",
    "from",
    "with_capacity",
    "entry",
    "or_insert",
    "or_insert_with",
    "or_default",
    # Java Streams / Optional / Builders / String
    "stream",
    "parallelStream",
    "of",
    "ofNullable",
    "orElse",
    "orElseGet",
    "orElseThrow",
    "isPresent",
    "isEmpty",
    "toList",
    " builder",
    "builder",
    "build",
    "append",
    "toString",
    # JS/TS Array/Promise chains
    "then",
    "catch",
    "finally",
    "slice",
    "join",
    "includes",
    "forEach",
}

RUST_USE_CRATE_RE = re.compile(r"\buse\s+crate::([a-zA-Z_][a-zA-Z0-9_]*)")
RUST_ABSTRACT_RE = re.compile(r"\bpub(?:\([^)]*\))?\s+(?:trait|enum)\s+[A-Z][A-Za-z0-9_]*")
RUST_CONCRETE_RE = re.compile(r"\bpub(?:\([^)]*\))?\s+struct\s+[A-Z][A-Za-z0-9_]*")
RUST_PUB_FN_RE = re.compile(r"\bpub\s+(?:async\s+)?fn\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:<[^>]*>)?\s*\(([^)]*)\)")

JAVA_PKG_RE = re.compile(r"^\s*package\s+([a-zA-Z0-9_.]+)\s*;", re.MULTILINE)
JAVA_IMP_RE = re.compile(r"^\s*import\s+(?:static\s+)?([a-zA-Z0-9_.]+)\s*;", re.MULTILINE)
JAVA_ABSTRACT_RE = re.compile(r"\b(?:interface|abstract\s+class|sealed\s+interface|record)\s+[A-Z][A-Za-z0-9_]*")
JAVA_CONCRETE_RE = re.compile(r"\b(?<!abstract\s)class\s+[A-Z][A-Za-z0-9_]*")
JAVA_PUB_METHOD_RE = re.compile(
    r"\bpublic\s+(?!class\b|interface\b|record\b|enum\b)(?:static\s+|final\s+|synchronized\s+)*"
    r"[A-Za-z0-9_<>, ?\[\]]+\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(([^)]*)\)"
)

TS_IMP_RE = re.compile(r"""import\s+.*?\s+from\s+['"](@/[^'"]+|\.\.?/[^'"]+)['"]""")

# Matches `ident.m1(...).m2(...).m3(...)` (3+ chained method invocations)
METHOD_CHAIN_RE = re.compile(
    r"\b([a-z_][a-zA-Z0-9_]*)"
    r"(?:\s*\.\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\([^()\n]*\))"
    r"(?:\s*\.\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\([^()\n]*\))"
    r"(?:\s*\.\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\([^()\n]*\))+"
)
CHAIN_STEP_RE = re.compile(r"\.\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")


def is_test_file(path: Path) -> bool:
    norm = path.as_posix().lower()
    return (
        "/tests/" in norm
        or "/test/" in norm
        or norm.endswith("_test.rs")
        or norm.endswith("test.java")
        or norm.endswith("tests.java")
        or norm.endswith(".spec.ts")
        or norm.endswith(".test.ts")
    )


def strip_comments_and_rust_test_mod(text: str, ext: str) -> str:
    if ext == ".rs":
        m = re.search(r"#\[cfg\(test\)\]", text)
        if m:
            text = text[: m.start()]
    # Strip line comments while preserving line numbers
    cleaned = []
    for line in text.splitlines():
        idx = line.find("//")
        cleaned.append(line[:idx] if idx >= 0 else line)
    return "\n".join(cleaned)


def iter_source_files(root: Path) -> List[Path]:
    if root.is_file():
        return [root]
    files: List[Path] = []
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file() and p.suffix in (".rs", ".java", ".ts", ".vue"):
            files.append(p)
    return sorted(files)


def rust_component_name(file_path: Path, root: Path) -> Optional[str]:
    try:
        rel = file_path.relative_to(root).parts
    except ValueError:
        rel = file_path.parts
    if "src" in rel:
        idx = rel.index("src")
        after = rel[idx + 1 :]
        if not after:
            return None
        if len(after) == 1:
            stem = Path(after[0]).stem
            return "root" if stem in ("lib", "main") else stem
        return after[0]
    return file_path.stem


def java_component_and_prefix(text: str) -> Tuple[Optional[str], Optional[str]]:
    m = JAVA_PKG_RE.search(text)
    if not m:
        return None, None
    pkg = m.group(1)
    parts = pkg.split(".")
    if len(parts) >= 4:
        return parts[3], ".".join(parts[:3])
    return parts[-1], ".".join(parts[:-1])


def ts_component_name(file_path: Path, root: Path) -> str:
    try:
        rel = file_path.relative_to(root).parts
    except ValueError:
        rel = file_path.parts
    if "src" in rel:
        idx = rel.index("src")
        after = rel[idx + 1 :]
        if after:
            return Path(after[0]).stem if len(after) == 1 else after[0]
    return file_path.parent.name or file_path.stem


def find_cycles(graph: Dict[str, Set[str]]) -> List[List[str]]:
    visited: Set[str] = set()
    on_stack: List[str] = []
    stack_set: Set[str] = set()
    cycles: List[List[str]] = []
    seen_signatures: Set[Tuple[str, ...]] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        on_stack.append(node)
        stack_set.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt == node:
                continue
            if nxt not in visited:
                dfs(nxt)
            elif nxt in stack_set:
                idx = on_stack.index(nxt)
                cyc = on_stack[idx:] + [nxt]
                core = cyc[:-1]
                min_i = min(range(len(core)), key=lambda i: core[i])
                canon = tuple(core[min_i:] + core[:min_i])
                if canon not in seen_signatures:
                    seen_signatures.add(canon)
                    cycles.append(list(canon) + [canon[0]])
        on_stack.pop()
        stack_set.remove(node)

    for n in sorted(graph.keys()):
        if n not in visited:
            dfs(n)
    return cycles


def analyze_architecture(target_path: Path) -> Dict[str, Any]:
    root = target_path.resolve()
    base_dir = root.parent if root.is_file() else root
    files = iter_source_files(root)

    comp_deps: Dict[str, Set[str]] = defaultdict(set)
    comp_abstract: Dict[str, int] = defaultdict(int)
    comp_concrete: Dict[str, int] = defaultdict(int)
    violations: List[Dict[str, Any]] = []
    advisories: List[Dict[str, Any]] = []
    param_tuples: Dict[Tuple[str, ...], List[str]] = defaultdict(list)

    for fp in files:
        if is_test_file(fp):
            continue
        try:
            raw_text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ext = fp.suffix
        code = strip_comments_and_rust_test_mod(raw_text, ext)
        rel_str = fp.relative_to(base_dir).as_posix() if fp.is_relative_to(base_dir) else fp.as_posix()

        comp: Optional[str] = None
        if ext == ".rs":
            comp = rust_component_name(fp, base_dir)
            if comp and comp != "root":
                comp_deps.setdefault(comp, set())
                comp_abstract[comp] += len(RUST_ABSTRACT_RE.findall(code))
                comp_concrete[comp] += len(RUST_CONCRETE_RE.findall(code))
                for dep in RUST_USE_CRATE_RE.findall(code):
                    if dep != comp and dep != "root":
                        comp_deps[comp].add(dep)

            # Flag arguments & Data Clumps in Rust
            for m in RUST_PUB_FN_RE.finditer(code):
                fn_name, raw_params = m.group(1), m.group(2)
                line_no = code.count("\n", 0, m.start()) + 1
                params = [p.strip() for p in raw_params.split(",") if p.strip() and not p.strip().startswith("&self") and not p.strip().startswith("mut self") and p.strip() != "self"]
                if not fn_name.startswith(("set_", "with_", "from_")):
                    for p in params:
                        if re.search(r":\s*bool\b", p):
                            advisories.append({
                                "rule": "CC-F3",
                                "severity": "ADVISORY",
                                "file": rel_str,
                                "line": line_no,
                                "message": f"Public function `{fn_name}` accepts raw boolean flag `{p}` -> split method or pass 2-variant enum (`[RG-RF46]`).",
                            })
                names = tuple(sorted(set(p.split(":")[0].strip().lstrip("mut ").strip() for p in params if ":" in p)))
                if 3 <= len(names) <= 8:
                    for sub3 in itertools.combinations(names, 3):
                        param_tuples[sub3].append(f"{rel_str}:{line_no} ({fn_name})")

        elif ext == ".java":
            comp, prefix = java_component_and_prefix(code)
            if comp:
                comp_deps.setdefault(comp, set())
                comp_abstract[comp] += len(JAVA_ABSTRACT_RE.findall(code))
                comp_concrete[comp] += len(JAVA_CONCRETE_RE.findall(code))
                for imp in JAVA_IMP_RE.findall(code):
                    if prefix and imp.startswith(prefix + "."):
                        rem = imp[len(prefix) + 1 :].split(".")
                        if rem and rem[0] != comp:
                            comp_deps[comp].add(rem[0])

            for m in JAVA_PUB_METHOD_RE.finditer(code):
                fn_name, raw_params = m.group(1), m.group(2)
                line_no = code.count("\n", 0, m.start()) + 1
                params = [p.strip() for p in raw_params.split(",") if p.strip()]
                if not fn_name.startswith(("set", "with", "is", "has")):
                    for p in params:
                        if re.match(r"^(?:final\s+)?boolean\s+\w+", p):
                            advisories.append({
                                "rule": "CC-F3",
                                "severity": "ADVISORY",
                                "file": rel_str,
                                "line": line_no,
                                "message": f"Public method `{fn_name}` accepts boolean flag `{p}` -> replace with explicit methods or domain enum (`[RG-RF46]`).",
                            })
                names = tuple(sorted(set(p.split()[-1] for p in params if len(p.split()) >= 2)))
                if 3 <= len(names) <= 8:
                    for sub3 in itertools.combinations(names, 3):
                        param_tuples[sub3].append(f"{rel_str}:{line_no} ({fn_name})")

        elif ext in (".ts", ".vue"):
            comp = ts_component_name(fp, base_dir)
            comp_deps.setdefault(comp, set())
            for imp in TS_IMP_RE.findall(code):
                if imp.startswith("@/"):
                    dep = imp[2:].split("/")[0]
                    if dep and dep != comp:
                        comp_deps[comp].add(dep)

        # Check 1: Pure inner domain/cube importing outer HTTP/SDK/Framework (`[CA-DEP-RULE]`)
        if comp and comp.lower() in PURE_MODEL_COMPONENTS:
            fm = FORBIDDEN_OUTER_IMPORTS_IN_PURE.search(code)
            if fm:
                line_no = code.count("\n", 0, fm.start()) + 1
                violations.append({
                    "rule": "CA-DEP-RULE",
                    "severity": "ERROR",
                    "file": rel_str,
                    "line": line_no,
                    "message": f"Inner `{comp}` module imports outer framework/SDK `{fm.group(0)}` (violates Clean Architecture Inward Dependency Rule).",
                })
            for dep in comp_deps.get(comp, ()):
                if dep.lower() in OUTER_ADAPTER_COMPONENTS or (comp.lower() in INNER_DOMAIN_COMPONENTS and dep.lower() == "codecs"):
                    violations.append({
                        "rule": "CA-DEP-RULE",
                        "severity": "ERROR",
                        "file": rel_str,
                        "line": 1,
                        "message": f"Inner component `{comp}` depends outward on `{dep}` (dependencies must point strictly inward).",
                    })

        # Check 2: Law of Demeter Train Wrecks (`[CC-OD3]` / `[RG-SM21]`)
        for line_idx, line in enumerate(code.splitlines(), start=1):
            if "LOG." in line or "log::" in line or "tracing::" in line:
                continue
            for cm in METHOD_CHAIN_RE.finditer(line):
                snippet = cm.group(0)
                methods = CHAIN_STEP_RE.findall(snippet)
                non_fluent = [m for m in methods if m not in FLUENT_SAFE_METHODS and not m.startswith(("with_", "set_"))]
                if len(non_fluent) >= 3:
                    advisories.append({
                        "rule": "CC-OD3",
                        "severity": "ADVISORY",
                        "file": rel_str,
                        "line": line_idx,
                        "message": f"Law of Demeter train wreck (`{len(non_fluent)}` chained calls: `{'.'.join(non_fluent)}`) -> apply Hide Delegate (`[RG-RF14]`) or Move Method (`[RG-RF10]`).",
                    })

    # Check 3: `[CA-ADP]` Acyclic Dependencies Principle
    # Ensure all referenced internal nodes exist in graph keys if defined in project
    known_comps = set(comp_deps.keys())
    internal_graph = {c: {d for d in deps if d in known_comps} for c, deps in comp_deps.items()}
    cycles = find_cycles(internal_graph)
    for cyc in cycles:
        violations.append({
            "rule": "CA-ADP",
            "severity": "ERROR",
            "file": "<component-graph>",
            "line": 0,
            "message": f"Component dependency cycle detected: `{' -> '.join(cyc)}` (violates Acyclic Dependencies Principle; break via DIP trait/interface or shared leaf Value Object).",
        })

    # Check 4: `[CA-SDP]` & `[CA-SAP]` Main Sequence Metrics
    ca_counts: Dict[str, int] = defaultdict(int)
    for src_c, targets in internal_graph.items():
        for dst_c in targets:
            ca_counts[dst_c] += 1

    metrics: Dict[str, Dict[str, Any]] = {}
    for c in sorted(known_comps):
        ce = len(internal_graph.get(c, ()))
        ca = ca_counts.get(c, 0)
        instability = round(ce / (ca + ce), 3) if (ca + ce) > 0 else 0.0
        na = comp_abstract.get(c, 0)
        nc_total = na + comp_concrete.get(c, 0)
        abstractness = round(na / nc_total, 3) if nc_total > 0 else 0.0
        distance = round(abs(abstractness + instability - 1.0), 3)
        metrics[c] = {
            "Ca": ca,
            "Ce": ce,
            "I": instability,
            "A": abstractness,
            "D": distance,
            "depends_on": sorted(internal_graph.get(c, ())),
        }

    for src_c, targets in internal_graph.items():
        i_src = metrics[src_c]["I"]
        ca_src = metrics[src_c]["Ca"]
        for dst_c in targets:
            i_dst = metrics[dst_c]["I"]
            if ca_src >= 2 and i_src + 0.35 < i_dst:
                advisories.append({
                    "rule": "CA-SDP",
                    "severity": "ADVISORY",
                    "file": f"<component:{src_c}>",
                    "line": 0,
                    "message": f"Stable Dependencies Warning: stable `{src_c}` (I={i_src}, Ca={ca_src}) depends on more volatile `{dst_c}` (I={i_dst}). Invert via DIP trait if `{dst_c}` churns frequently.",
                })

    # Check 5: `[RG-SM05]` Data Clumps
    for names_tuple, locs in sorted(param_tuples.items()):
        if len(locs) >= 3:
            advisories.append({
                "rule": "RG-SM05",
                "severity": "ADVISORY",
                "file": locs[0].split(":")[0],
                "line": 0,
                "message": f"Data Clump parameter tuple `({', '.join(names_tuple)})` repeated across {len(locs)} functions ({', '.join(locs[:3])}) -> Introduce Parameter Object (`[RG-RF49]`).",
            })

    return {
        "passed": len(violations) == 0,
        "target": str(root),
        "components_analyzed": len(metrics),
        "cycles": cycles,
        "violations": violations,
        "advisories": advisories,
        "metrics": metrics,
    }


def run_self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src" / "domain").mkdir(parents=True)
        (root / "src" / "http").mkdir(parents=True)
        (root / "src" / "service").mkdir(parents=True)

        # Intentional cycle (`service <-> http`) + inward violation (`domain -> http` + `aws_sdk_s3`) + LoD + Flag arg
        (root / "src" / "domain" / "mod.rs").write_text(
            "use crate::http::Router;\n"
            "use aws_sdk_s3::Client;\n"
            "pub struct Node { pub id: String }\n"
            "pub fn compute(flag: bool, a: u32, b: u32, c: u32) {\n"
            "    let _x = ctx.gateway().radio().channel().width();\n"
            "}\n",
            encoding="utf-8",
        )
        (root / "src" / "http" / "mod.rs").write_text(
            "use crate::service::Svc;\npub struct Router;\npub fn f1(a: u32, b: u32, c: u32) {}\n",
            encoding="utf-8",
        )
        (root / "src" / "service" / "mod.rs").write_text(
            "use crate::http::Router;\npub struct Svc;\npub fn f2(a: u32, b: u32, c: u32) {}\n",
            encoding="utf-8",
        )

        rep = analyze_architecture(root)
        rules_err = {v["rule"] for v in rep["violations"]}
        rules_adv = {a["rule"] for a in rep["advisories"]}
        assert not rep["passed"], "Expected architectural violations to fail"
        assert "CA-ADP" in rules_err, f"Expected CA-ADP cycle detection, got {rules_err}"
        assert "CA-DEP-RULE" in rules_err, f"Expected CA-DEP-RULE detection, got {rules_err}"
        assert "CC-OD3" in rules_adv, f"Expected CC-OD3 Law of Demeter detection, got {rules_adv}"
        assert "CC-F3" in rules_adv, f"Expected CC-F3 Flag Argument detection, got {rules_adv}"
        assert "RG-SM05" in rules_adv, f"Expected RG-SM05 Data Clump detection, got {rules_adv}"

        # Clean DAG verification
        clean = root / "clean_proj"
        (clean / "src" / "domain").mkdir(parents=True)
        (clean / "src" / "service").mkdir(parents=True)
        (clean / "src" / "domain" / "mod.rs").write_text("pub struct MacAddress([u8; 6]);\n", encoding="utf-8")
        (clean / "src" / "service" / "mod.rs").write_text("use crate::domain::MacAddress;\npub struct Orchestrator;\n", encoding="utf-8")
        clean_rep = analyze_architecture(clean)
        assert clean_rep["passed"], f"Clean project should pass: {clean_rep}"
        assert clean_rep["metrics"]["domain"]["I"] == 0.0
        assert clean_rep["metrics"]["service"]["I"] == 1.0

    print("arch_audit v1.0 self-test: OK")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Clean Architecture & Refactoring.Guru Structural Smell Engine")
    ap.add_argument("path", nargs="?", default=".", help="Workspace, module, or file path to audit")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON report")
    ap.add_argument("--strict-advisories", action="store_true", help="Fail exit code on advisories as well as hard errors")
    ap.add_argument("--self-test", action="store_true", help="Run built-in verification suite")
    args = ap.parse_args(argv)

    if args.self_test:
        return run_self_test()

    rep = analyze_architecture(Path(args.path))
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        status = "PASS" if rep["passed"] else "FAIL"
        print(f"[arch-audit] {status} | Components: {rep['components_analyzed']} | Cycles: {len(rep['cycles'])} | Violations: {len(rep['violations'])} | Advisories: {len(rep['advisories'])}")
        if rep["metrics"]:
            print("  Component Metrics (Ca, Ce, Instability I, Abstractness A, Distance D):")
            for comp, m in rep["metrics"].items():
                print(f"    - {comp:<18} Ca={m['Ca']:<2} Ce={m['Ce']:<2} I={m['I']:<5} A={m['A']:<5} D={m['D']:<5} -> {m['depends_on']}")
        for v in rep["violations"]:
            print(f"  [ERROR:{v['rule']}] {v['file']}:{v['line']} — {v['message']}")
        for a in rep["advisories"][:25]:
            print(f"  [ADVISORY:{a['rule']}] {a['file']}:{a['line']} — {a['message']}")
        if len(rep["advisories"]) > 25:
            print(f"  ... ({len(rep['advisories']) - 25} additional advisories omitted; pass --json for full list)")

    if not rep["passed"]:
        return 1
    if args.strict_advisories and rep["advisories"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
