#!/usr/bin/env python3
"""
Storybook Clean Architecture, Deep Module & Runtime Cache Linter (`story_guard.py` v3.0).

Supports BOTH:
  1. Single-Crate VectorCube Architecture (`src/{domain,codecs,cube,ingest,fanout,service,features,rest}`)
  2. Multi-Crate Workspace Architecture (`crates/*`)

Enforces 4 Architectural Gates grounded in Ousterhout (APOSD), Uncle Bob (Clean Architecture),
and Toyota Poka-Yoke:
  - Gate 1 (Inward Dependency Law): `src/domain/` never imports outer adapters.
  - Gate 2 (Ousterhout Deep Module & Facade Budget): `mod.rs` <= 160L, leaf modules <= 950L.
  - Gate 3 (Storybook Module Headers `C9`): Top-level `src/*/mod.rs` carry `//!` headers.
  - Gate 4 (Warm Singleton & Compile-Time Schema Law): Flags per-request `HomeOrchestratorService::new()`
    without `OnceLock` and bans runtime S3 schema fetches.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

DOMAIN_FORBIDDEN_IMPORTS_RE = re.compile(
    r"\b(?:crate::rest|crate::ingest|crate::fanout|crate::repository|aws_sdk_|lambda_runtime)\b"
)
PER_REQUEST_ORCHESTRATOR_RE = re.compile(r"\bHomeOrchestratorService::new\s*\(")
_SCHEMA_FETCH_PAT = "ensure_briefcase_" + "schema_loaded|schemas/" + "fingerprints/"
RUNTIME_SCHEMA_FETCH_RE = re.compile(_SCHEMA_FETCH_PAT)


def discover_ondemand_repo(explicit: str = "") -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_repo = os.environ.get("ONDEMAND_REPO", "").strip()
    if env_repo:
        return Path(env_repo).expanduser().resolve()
    return Path.cwd().resolve()


@dataclass
class GateResult:
    gate_id: str
    title: str
    status: str  # PASS, WARN, FAIL
    summary: str
    violations: List[str] = field(default_factory=list)


def iter_prod_rs(src_dir: Path):
    if not src_dir.exists():
        return
    for p in sorted(src_dir.rglob("*.rs")):
        norm = p.as_posix().lower()
        if "/tests/" in norm or norm.endswith(("tests.rs", "_test.rs")):
            continue
        yield p


def check_gate1_inward_dependency(repo: Path) -> GateResult:
    domain_dir = repo / "src" / "domain"
    if not domain_dir.exists():
        domain_dir = repo / "crates" / "core" / "src"
    violations = []
    scanned = 0
    for rs in iter_prod_rs(domain_dir):
        scanned += 1
        rel = rs.relative_to(repo).as_posix()
        text = rs.read_text(encoding="utf-8", errors="replace").split("#[cfg(test)]")[0]
        for idx, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("//"):
                continue
            m = DOMAIN_FORBIDDEN_IMPORTS_RE.search(line)
            if m:
                violations.append(f"{rel}:{idx} -> forbidden outer import `{m.group(0)}` in pure domain")
    status = "FAIL" if violations else ("PASS" if scanned > 0 else "WARN")
    return GateResult(
        "G1-INWARD-DEP",
        "Clean Architecture Inward Dependency Rule",
        status,
        f"Scanned {scanned} domain files; {len(violations)} outer-layer leak(s).",
        violations,
    )


def check_gate2_module_budgets(repo: Path) -> GateResult:
    src_dir = repo / "src" if (repo / "src").exists() else repo / "crates"
    warns = []
    scanned = 0
    for rs in iter_prod_rs(src_dir):
        scanned += 1
        rel = rs.relative_to(repo).as_posix()
        line_count = len(rs.read_text(encoding="utf-8", errors="replace").splitlines())
        if rs.name == "mod.rs" and line_count > 160:
            warns.append(f"{rel} ({line_count}L > 160L facade budget)")
        elif line_count > 950:
            warns.append(f"{rel} ({line_count}L > 950L leaf module budget)")
    status = "WARN" if warns else "PASS"
    return GateResult(
        "G2-DEEP-MODULES",
        "Ousterhout Facade & Module Cognitive Budget",
        status,
        f"Scanned {scanned} prod Rust files; {len(warns)} budget warning(s).",
        warns[:20],
    )


def check_gate3_storybook_headers(repo: Path) -> GateResult:
    src_dir = repo / "src"
    missing = []
    checked = 0
    if src_dir.exists():
        for mod_rs in sorted(src_dir.glob("*/mod.rs")):
            checked += 1
            head = "\n".join(mod_rs.read_text(encoding="utf-8", errors="replace").splitlines()[:8])
            if "//!" not in head:
                missing.append(mod_rs.relative_to(repo).as_posix())
    status = "WARN" if missing else "PASS"
    return GateResult(
        "G3-STORYBOOK-DOC",
        "Top-Level Layer `//!` Storybook Headers",
        status,
        f"Checked {checked} layer facades; {len(missing)} missing `//!` header(s).",
        missing,
    )


def check_gate4_singleton_and_schema(repo: Path) -> GateResult:
    src_dir = repo / "src" if (repo / "src").exists() else repo / "crates"
    violations = []
    warns = []
    for rs in iter_prod_rs(src_dir):
        rel = rs.relative_to(repo).as_posix()
        text = rs.read_text(encoding="utf-8", errors="replace").split("#[cfg(test)]")[0]
        for idx, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("//"):
                continue
            if RUNTIME_SCHEMA_FETCH_RE.search(line):
                violations.append(f"{rel}:{idx} -> runtime S3 schema fetch banned (embed at compile time)")
            if "/rest/" in rel and PER_REQUEST_ORCHESTRATOR_RE.search(line) and "OnceLock" not in text:
                warns.append(f"{rel}:{idx} -> per-request `HomeOrchestratorService::new()` wipes L1 RAM cache; wrap in `OnceLock`")
    status = "FAIL" if violations else ("WARN" if warns else "PASS")
    return GateResult(
        "G4-WARM-SINGLETON",
        "OnceLock Warm L1 Cache & Compile-Time Schema Invariant",
        status,
        f"{len(violations)} hard violation(s), {len(warns)} singleton warning(s).",
        violations + warns,
    )


def run_all_gates(repo: Path) -> dict:
    gates = [
        check_gate1_inward_dependency(repo),
        check_gate2_module_budgets(repo),
        check_gate3_storybook_headers(repo),
        check_gate4_singleton_and_schema(repo),
    ]
    hard_fail = any(g.status == "FAIL" for g in gates)
    return {
        "workspace": str(repo),
        "passed": not hard_fail,
        "gates": [asdict(g) for g in gates],
    }


def run_self_test() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src" / "domain").mkdir(parents=True)
        (root / "src" / "rest").mkdir(parents=True)
        (root / "src" / "domain" / "mod.rs").write_text("//! Pure domain\npub struct HomeId(String);\n", encoding="utf-8")
        (root / "src" / "rest" / "mod.rs").write_text("//! REST adapter\nuse std::sync::OnceLock;\n", encoding="utf-8")
        clean_res = run_all_gates(root)
        assert clean_res["passed"], clean_res
        (root / "src" / "domain" / "leak.rs").write_text("use crate::rest::Router;\n", encoding="utf-8")
        fail_res = run_all_gates(root)
        assert not fail_res["passed"], fail_res
    print("story_guard v3.0 self-test: OK")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Storybook Clean Architecture & VectorCube Guard v3.0")
    ap.add_argument("action", nargs="?", default="check", choices=["check", "self-test"])
    ap.add_argument("--workspace", default="", help="Target Rust repository")
    ap.add_argument("--json", action="store_true", help="Output JSON")
    args = ap.parse_args(argv)

    if args.action == "self-test":
        return run_self_test()

    repo = discover_ondemand_repo(args.workspace)
    rep = run_all_gates(repo)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        verdict = "PASS" if rep["passed"] else "FAIL"
        print(f"[Storybook-Guard v3.0] Verdict={verdict} | Workspace={rep['workspace']}")
        for g in rep["gates"]:
            print(f"  [{g['status']}] {g['gate_id']} — {g['summary']}")
            for v in g["violations"][:8]:
                print(f"      * {v}")
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
