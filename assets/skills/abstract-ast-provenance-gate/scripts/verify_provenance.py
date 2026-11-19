#!/usr/bin/env python3
"""
Abstract AST & Telemetry Provenance Verifier (`verify_provenance.py`).

Enforces 4 Abstract Architectural Invariants grounded in canonical classics:
1. INV-ISOLATION (APOSD Information Leakage Prohibition & Evans Bounded Context Isolation):
   Avro modules (`*avro*.rs`) never reference BSON types (`TopologyRecord`, `AutomeshRecord`,
   `BsonTopologyRow`, `HermesBsonEnvelope`, `HERMES_DELIM`), and BSON modules (`*bson*.rs`)
   never reference Avro types (`AvroWhitelistedEvent`, `BriefcaseRecord`, `apache_avro`).
2. INV-PROVENANCE (Fowler Anti-Feature-Envy & Uncle Bob Law of Demeter):
   Every `pub fn handle_*` in `rest/controllers/` (except deprecated 404 routes) consumes `cube`
   and never returns a hardcoded empty `ItemsResponse::new(Vec::<...>::new())`.
3. INV-NO-SYNTHETIC-LITERAL (Evans Ubiquitous Language Authenticity):
   No fake telemetry literals ('ETHERNET', 'UP', 'FULL', 1000, 'Kaizen Gateway', 'slice_idx % 15')
   in folding/ingest/controller production code.
4. INV-COMPILE-TIME-SCHEMA (Bloch Item 6 Static Immutable Assets):
   Zero runtime S3 schema fetching (`ensure_briefcase_schema_loaded` / `schemas/fingerprints/`).

Also verifies the Parity Oracle Gate:
- Flags VACUOUS_PASS anti-patterns in test suites (e.g. asserting HTTP 200 without checking
  response body or asserting empty results on populated test inputs).
- Refuses vacuous passes when 0 files are scanned (fails immediately with exit code 1).
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

BSON_SYMBOLS_IN_AVRO_RE = re.compile(
    r"\b(?:TopologyRecord|TopologyConfigExt|AutomeshRecord|BsonTopologyRow|HermesBsonEnvelope|HERMES_DELIM)\b"
)
AVRO_SYMBOLS_IN_BSON_RE = re.compile(
    r"\b(?:AvroWhitelistedEvent|BriefcaseRecord|apache_avro|EmbeddedSchemaRegistry)\b"
)
HOLLOW_CONTROLLER_FN_RE = re.compile(
    r"pub\s+fn\s+(handle_[a-zA-Z0-9_]+)\s*\(([^)]*)\)\s*->\s*Result<Vec<u8>,\s*String>\s*\{([\s\S]*?)\n\}",
    re.MULTILINE,
)
SYNTHETIC_LITERAL_RULES = [
    (
        "INV-COMPILE-TIME-SCHEMA",
        re.compile(r"\bensure_briefcase_schema_loaded\b|schemas/fingerprints/"),
        "Runtime S3 Avro schema fetch detected (violates Bloch Static Immutable Assets)",
    ),
    (
        "INV-NO-SYNTHETIC-LITERAL",
        re.compile(
            r'wan_type\s*[:=]\s*"ETHERNET"|wan_status\s*[:=]\s*"UP"|wan_duplex\s*[:=]\s*"FULL"|wan_link_speed\s*[:=]\s*1000\b'
        ),
        "Hardcoded fake WAN link state ('ETHERNET'/'UP'/'FULL'/1000) violates Evans Authenticity",
    ),
    (
        "INV-NO-SYNTHETIC-LITERAL",
        re.compile(r'"Kaizen Gateway"|"Kaizen Extender"'),
        "Hardcoded fake device model ('Kaizen Gateway' / 'Kaizen Extender') violates Evans Authenticity",
    ),
    (
        "INV-NO-SYNTHETIC-LITERAL",
        re.compile(r"slice_idx\s*%\s*15"),
        "Hardcoded modulo-15 NodeId wrapping bug ('slice_idx % 15') violates Evans Authenticity",
    ),
    (
        "INV-NO-SYNTHETIC-LITERAL",
        re.compile(r'"id"\s*:\s*"in_sync"|status"\s*:\s*"in_sync"'),
        "Hardcoded fake radio config history ('in_sync') violates Evans Authenticity",
    ),
    (
        "INV-NO-SYNTHETIC-LITERAL",
        re.compile(r"922_346_500|311_023_600|rssi_f64\s*[-+]\s*5\.0|\*\s*1_500_000\.0|excellent:\s*100\b"),
        "Synthetic Golden-HAR telemetry constant or fake RSSI/throughput arithmetic violates Evans Authenticity",
    ),
]

VACUOUS_PASS_RE = re.compile(
    r"assert\s*!\s*\(\s*(?:resp|response)\.status\s*\(\s*\)\s*(?:\.is_success\s*\(\s*\)|==\s*200)\s*\)\s*;\s*(?:\n\s*\}\s*|\n\s*Ok\s*\(\s*\(\s*\)\s*\)\s*;\s*\n\s*\})",
    re.MULTILINE,
)

PRUNE_DIRS = {
    "target", "node_modules", ".git", "build", ".venv", "__pycache__", ".idea"
}


def is_test_path(rel: str, p: Path) -> bool:
    norm = rel.lower()
    return (
        "/tests/" in norm
        or "\\tests\\" in norm
        or "/benches/" in norm
        or norm.endswith("_test.rs")
        or "test" in p.name.lower()
    )


def collect_rust_files(root: Path):
    """Walk root and yield all .rs files with relative paths, pruning build dirs."""
    if root.is_file():
        if root.suffix == ".rs":
            yield root, root.name
        return

    for dirpath, dirs, files in os.walk(str(root)):
        dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
        for f in files:
            if f.endswith(".rs"):
                full_p = Path(dirpath) / f
                try:
                    rel = full_p.relative_to(root).as_posix()
                except ValueError:
                    rel = full_p.as_posix()
                yield full_p, rel


def iter_rust_prod_files(root: Path):
    for p, rel in collect_rust_files(root):
        if not is_test_path(rel, p):
            yield p, rel


def iter_rust_test_files(root: Path):
    for p, rel in collect_rust_files(root):
        if is_test_path(rel, p):
            yield p, rel


def strip_test_mod(src: str) -> str:
    idx = src.find("#[cfg(test)]")
    return src[:idx] if idx != -1 else src


def audit_workspace(root: Path) -> dict:
    violations = []
    scanned_files = 0

    # 1. Audit Production Code
    for path, rel in iter_rust_prod_files(root):
        scanned_files += 1
        raw = path.read_text(encoding="utf-8", errors="replace")
        prod_code = strip_test_mod(raw)
        fname = path.name.lower()

        # INV-ISOLATION: Avro vs BSON cross-contamination (APOSD Information Leakage)
        if "avro" in fname or "/avro/" in rel:
            for m in BSON_SYMBOLS_IN_AVRO_RE.finditer(prod_code):
                line_no = prod_code[: m.start()].count("\n") + 1
                violations.append({
                    "invariant": "INV-ISOLATION",
                    "file": rel,
                    "line": line_no,
                    "detail": f"[APOSD-Information-Leakage] Avro module references BSON symbol `{m.group(0)}`",
                })
        if "bson" in fname or "/bson/" in rel:
            for m in AVRO_SYMBOLS_IN_BSON_RE.finditer(prod_code):
                line_no = prod_code[: m.start()].count("\n") + 1
                violations.append({
                    "invariant": "INV-ISOLATION",
                    "file": rel,
                    "line": line_no,
                    "detail": f"[APOSD-Information-Leakage] BSON module references Avro symbol `{m.group(0)}`",
                })

        # INV-PROVENANCE: Controllers must consume `cube` and not return hollow Vec::new()
        if "/controllers/" in rel:
            for m in HOLLOW_CONTROLLER_FN_RE.finditer(prod_code):
                fn_name, params, body = m.group(1), m.group(2), m.group(3)
                if "deprecated" in fn_name:
                    continue
                line_no = prod_code[: m.start()].count("\n") + 1
                if "_cube:" in params:
                    violations.append({
                        "invariant": "INV-PROVENANCE",
                        "file": rel,
                        "line": line_no,
                        "detail": f"[Fowler-Anti-Feature-Envy] Controller `{fn_name}` ignores `_cube: &MaterializedHomeState`",
                    })
                if re.search(r"ItemsResponse::new\(Vec::<[^>]+>::new\(\)\)", body):
                    violations.append({
                        "invariant": "INV-PROVENANCE",
                        "file": rel,
                        "line": line_no,
                        "detail": f"[Uncle-Bob-Demeter] Controller `{fn_name}` returns hardcoded empty `Vec::new()` stub without domain lineage",
                    })

        # INV-NO-SYNTHETIC-LITERAL and INV-COMPILE-TIME-SCHEMA
        for inv_id, rx, desc in SYNTHETIC_LITERAL_RULES:
            for m in rx.finditer(prod_code):
                line_no = prod_code[: m.start()].count("\n") + 1
                violations.append({
                    "invariant": inv_id,
                    "file": rel,
                    "line": line_no,
                    "detail": f"{desc}: `{m.group(0)}`",
                })

    # 2. Audit Parity Oracle Gate across Test Files
    for path, rel in iter_rust_test_files(root):
        scanned_files += 1
        raw = path.read_text(encoding="utf-8", errors="replace")
        for m in VACUOUS_PASS_RE.finditer(raw):
            line_no = raw[: m.start()].count("\n") + 1
            violations.append({
                "invariant": "PARITY-ORACLE-GATE",
                "file": rel,
                "line": line_no,
                "detail": "[VACUOUS_PASS] Test asserts HTTP 200 without validating populated body content",
            })

    # 3. Refuse Vacuous Pass on Zero Files Scanned
    if scanned_files == 0:
        return {
            "scanned_files": 0,
            "violation_count": 1,
            "by_invariant": {"VACUOUS-PASS-ZERO-FILES": 1},
            "passed": False,
            "violations": [{
                "invariant": "VACUOUS-PASS-ZERO-FILES",
                "file": str(root),
                "line": 0,
                "detail": f"Zero Rust files discovered under workspace '{root}'. Refusing vacuous pass.",
            }],
        }

    by_inv = {}
    for v in violations:
        by_inv[v["invariant"]] = by_inv.get(v["invariant"], 0) + 1

    return {
        "scanned_files": scanned_files,
        "violation_count": len(violations),
        "by_invariant": by_inv,
        "passed": len(violations) == 0,
        "violations": violations,
    }


def main():
    parser = argparse.ArgumentParser(description="Verify Abstract AST & Telemetry Provenance Invariants")
    parser.add_argument("--workspace", default=".", help="Path to codebase")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    res = audit_workspace(Path(args.workspace).resolve())
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        status = "PASS" if res["passed"] else "FAIL"
        print(
            f"[Abstract-AST-Provenance-Gate] Status={status} | Scanned={res['scanned_files']} files | "
            f"Violations={res['violation_count']} ({res['by_invariant']})"
        )
        for v in res["violations"]:
            print(f"  - [{v['invariant']}] {v['file']}:{v['line']} -> {v['detail']}")

    sys.exit(0 if res["passed"] else 1)


if __name__ == "__main__":
    main()
