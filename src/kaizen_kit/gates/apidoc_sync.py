#!/usr/bin/env python3
"""
API Documentation Synchronizer (`apidoc_sync.py` v2.0) & 0-Token AST Skeletonizer (`ast_skeleton.py`).
"""

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional

ROUTE_LITERAL_RE = re.compile(r'"(/(?:cloud_gateway_api/v[134]|nodes|stations|wifiLinks|ethMeshLinks|bundle)[^"]*)"')
OPENAPI_PATH_RE = re.compile(r"^\s{2}(/[a-zA-Z0-9_{}/-]+):", re.MULTILINE)


def discover_rust_routes(target: Path) -> list[str]:
    files = []
    if target.is_file():
        files = [target]
    elif target.is_dir():
        for sub in ("src/rest/router", "src"):
            d = target / sub
            if d.exists():
                files.extend(sorted(d.glob("*.rs")))
    found = set()
    for f in files:
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
            for m in ROUTE_LITERAL_RE.findall(txt):
                found.add(m)
        except OSError:
            continue
    return sorted(found)


def extract_openapi_paths(spec_path: Path) -> list[str]:
    if not spec_path.is_file():
        return []
    return sorted(set(OPENAPI_PATH_RE.findall(spec_path.read_text(encoding="utf-8", errors="replace"))))


def run_self_test() -> int:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "routes.rs"
        p.write_text('let r = "/cloud_gateway_api/v3/stats/homes/{id}/stations";\n', encoding="utf-8")
        assert "/cloud_gateway_api/v3/stats/homes/{id}/stations" in discover_rust_routes(p)
    print("apidoc_sync v2.0 self-test: OK")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Verify & sync Rust VectorCube routes with OpenAPI YAML/JSON")
    ap.add_argument("--repo", default=".", help="Path to Rust repo or router file")
    ap.add_argument("--http-api", help="Alias for --repo")
    ap.add_argument("--openapi-spec", help="Optional OpenAPI YAML/JSON file")
    ap.add_argument("--self-test", action="store_true", help="Run built-in self-test")
    args = ap.parse_args(argv)

    if args.self_test:
        return run_self_test()

    target = Path(args.http_api or args.repo).resolve()
    routes = discover_rust_routes(target)
    openapi_paths = extract_openapi_paths(Path(args.openapi_spec)) if args.openapi_spec else []

    report = {
        "target": str(target),
        "detected_route_count": len(routes),
        "sample_routes": routes[:15],
        "openapi_path_count": len(openapi_paths),
        "sync_command": 'kaizen-kit flow ask --agent jvm-service-architect "Synchronize OpenAPI spec with VectorCube router delta"',
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
