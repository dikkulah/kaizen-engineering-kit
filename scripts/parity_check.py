#!/usr/bin/env python3
"""
parity_check.py - Popperian Response Parity Oracle (Legacy vs. Target).

Enforces strict parity matching between legacy and target service responses,
segregating POPULATED_MATCH from EMPTY_BOTH_200 false positives (ADR-004),
ignoring non-deterministic trace headers, and generating structural discrepancy reports.
"""

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Optional


def load_source_data(source_arg: str) -> tuple[Optional[Any], Optional[str]]:
    """Load JSON from a file path or direct JSON string."""
    path = Path(source_arg)
    if path.exists() and path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f), None
        except Exception as exc:
            return None, f"Failed to parse file '{source_arg}': {exc}"

    # Try parsing direct JSON string
    try:
        return json.loads(source_arg), None
    except Exception as exc:
        return None, f"Source is neither an existing file nor valid JSON string: '{source_arg}'"


def strip_ignored_fields(obj: Any, ignore_keys: set[str]) -> Any:
    """Recursively strip ignored keys from dictionaries and lists."""
    if isinstance(obj, dict):
        return {
            k: strip_ignored_fields(v, ignore_keys)
            for k, v in obj.items()
            if k not in ignore_keys
        }
    if isinstance(obj, list):
        return [strip_ignored_fields(elem, ignore_keys) for elem in obj]
    return obj


def is_empty_payload(data: Any) -> bool:
    """Check if payload is empty (None, empty list, empty dict, empty string)."""
    if data is None:
        return True
    if isinstance(data, (list, dict, str)) and len(data) == 0:
        return True
    return False


def compute_deep_diff(legacy: Any, target: Any, path: str = "root") -> list[str]:
    """Perform recursive structural diff between legacy and target objects."""
    diffs = []
    if type(legacy) != type(target):
        diffs.append(f"{path}: type mismatch (legacy={type(legacy).__name__}, target={type(target).__name__})")
        return diffs

    if isinstance(legacy, dict):
        legacy_keys = set(legacy.keys())
        target_keys = set(target.keys())
        for k in legacy_keys - target_keys:
            diffs.append(f"{path}.{k}: missing in target (legacy value={legacy[k]!r})")
        for k in target_keys - legacy_keys:
            diffs.append(f"{path}.{k}: unexpected in target (target value={target[k]!r})")
        for k in legacy_keys & target_keys:
            sub_diffs = compute_deep_diff(legacy[k], target[k], f"{path}.{k}")
            diffs.extend(sub_diffs)
        return diffs

    if isinstance(legacy, list):
        if len(legacy) != len(target):
            diffs.append(f"{path}: list length mismatch (legacy={len(legacy)}, target={len(target)})")
        for i, (leg_item, tgt_item) in enumerate(zip(legacy, target)):
            sub_diffs = compute_deep_diff(leg_item, tgt_item, f"{path}[{i}]")
            diffs.extend(sub_diffs)
        return diffs

    if legacy != target:
        diffs.append(f"{path}: value mismatch (legacy={legacy!r}, target={target!r})")

    return diffs


def parse_arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Verify response parity between legacy and target telemetry services (ADR-004).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--legacy-source",
        type=str,
        required=True,
        help="Legacy response JSON file path or raw JSON string.",
    )
    parser.add_argument(
        "--target-source",
        type=str,
        required=True,
        help="Target response JSON file path or raw JSON string.",
    )
    parser.add_argument(
        "--key-fields",
        type=str,
        default="mac,timestamp,ssid",
        help="Comma-separated key fields for record alignment.",
    )
    parser.add_argument(
        "--ignore-fields",
        type=str,
        default="traceId,responseTime,serverTiming",
        help="Comma-separated volatile fields excluded from parity comparison.",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        default=False,
        help="Allow empty responses ([] == []) to pass as valid parity (forbidden by default per ADR-004).",
    )
    parser.add_argument(
        "--report-path",
        type=str,
        default=None,
        help="Optional path to output markdown discrepancy report.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate source readability and parameters without deep field diff.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Main execution entrypoint."""
    args = parse_arguments(argv)

    legacy_data, leg_err = load_source_data(args.legacy_source)
    if leg_err:
        print(f"[ERROR] Legacy source error: {leg_err}", file=sys.stderr)
        return 1

    target_data, tgt_err = load_source_data(args.target_source)
    if tgt_err:
        print(f"[ERROR] Target source error: {tgt_err}", file=sys.stderr)
        return 1

    ignore_keys = {k.strip() for k in args.ignore_fields.split(",") if k.strip()}

    if args.dry_run:
        print("[DRY-RUN] Parity Check Simulation Plan:")
        print(f"[DRY-RUN] - Legacy source loaded successfully (type: {type(legacy_data).__name__})")
        print(f"[DRY-RUN] - Target source loaded successfully (type: {type(target_data).__name__})")
        print(f"[DRY-RUN] - Ignore fields: {ignore_keys}")
        print(f"[DRY-RUN] - Allow empty: {args.allow_empty}")
        print(f"[DRY-RUN] - Report destination: {args.report_path or 'console'}")
        print("[DRY-RUN] Simulation completed successfully.")
        return 0

    clean_legacy = strip_ignored_fields(legacy_data, ignore_keys)
    clean_target = strip_ignored_fields(target_data, ignore_keys)

    legacy_is_empty = is_empty_payload(clean_legacy)
    target_is_empty = is_empty_payload(clean_target)

    # ADR-004 / DE-004 check: Empty responses must not masquerade as populated matches
    if legacy_is_empty and target_is_empty:
        status_msg = "EMPTY_BOTH_200: Both legacy and target returned empty responses."
        if not args.allow_empty:
            print(f"[FAIL] {status_msg}", file=sys.stderr)
            print("[ADR-004 VIOLATION] Empty responses cannot count as POPULATED_MATCH. Check upstream ingestion.", file=sys.stderr)
            return 1
        print(f"[WARNING] {status_msg} (Permitted via --allow-empty)")
        return 0

    if legacy_is_empty or target_is_empty:
        print(f"[FAIL] ASYMMETRIC_EMPTY: Legacy empty={legacy_is_empty}, Target empty={target_is_empty}", file=sys.stderr)
        return 1

    diffs = compute_deep_diff(clean_legacy, clean_target)

    report_lines = [
        "# Parity Oracle Verification Report",
        "",
        f"- **Legacy Source**: `{args.legacy_source}`",
        f"- **Target Source**: `{args.target_source}`",
        f"- **Ignored Fields**: `{args.ignore_fields}`",
        f"- **Discrepancies Found**: {len(diffs)}",
    ]

    if diffs:
        report_lines.append("")
        report_lines.append("## Discrepancies")
        for d in diffs[:50]:
            report_lines.append(f"- `{d}`")
        if len(diffs) > 50:
            report_lines.append(f"- ... and {len(diffs) - 50} more discrepancies.")

        report_content = "\n".join(report_lines)
        if args.report_path:
            out_file = Path(args.report_path)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(report_content, encoding="utf-8")
            print(f"[INFO] Discrepancy report written to {out_file}")

        print(f"[FAIL] MISMATCH: Found {len(diffs)} parity differences.", file=sys.stderr)
        for d in diffs[:10]:
            print(f"  - {d}", file=sys.stderr)
        return 1

    report_lines.append("")
    report_lines.append("## Result: POPULATED_MATCH")
    report_lines.append("All fields and records match with 100% parity.")

    if args.report_path:
        out_file = Path(args.report_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("\n".join(report_lines), encoding="utf-8")

    print("[SUCCESS] STATUS: POPULATED_MATCH. Full recursive parity verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
