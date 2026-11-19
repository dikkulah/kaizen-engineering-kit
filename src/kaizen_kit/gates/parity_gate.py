#!/usr/bin/env python3
"""
Deterministic L1-L4 Endpoint Parity, POPULATED_MATCH Oracle & Self-Comparison Detector (`parity_gate.py` v2.0).

Upgrades grounded in Karl Popper Falsification & Anti-Illusion Telemetry Laws:
  1. Separates `POPULATED_MATCH` vs `EMPTY_BOTH_200` so empty `{}` / `{"items":[]}` responses
     NEVER inflate headline parity percentages.
  2. Order-insensitive identity-key array alignment (`mac`, `stationMac`, `bssid`, `ssid`, `id`,
     `nodeId`, `deviceId`, `timestamp`, `startTime`).
  3. Detects `Lambda == Lambda` ALB routing loop traps via `Server-Timing` inspection.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Optional

NA_SENTINELS = {"n/a", "na", "null", "none", "unknown", ""}
IDENTITY_KEYS = ("mac", "stationMac", "bssid", "ssid", "nodeId", "deviceId", "id", "timestamp", "startTime", "band")


def is_na_value(val) -> bool:
    if val is None:
        return True
    return isinstance(val, str) and val.strip().lower() in NA_SENTINELS


def is_effectively_empty_payload(obj) -> bool:
    if obj is None or obj == {} or obj == []:
        return True
    if isinstance(obj, dict):
        for k in ("items", "data", "events", "nodes", "stations", "links", "history"):
            if k in obj and isinstance(obj[k], list) and len(obj[k]) == 0 and len(obj) <= 2:
                return True
        if all(v in (None, [], {}, "") for v in obj.values()):
            return True
    return False


def extract_identity_key(elem) -> Optional[str]:
    if not isinstance(elem, dict):
        return None
    for k in IDENTITY_KEYS:
        if k in elem and elem[k] is not None:
            return f"{k}={str(elem[k]).strip().lower()}"
    return None


def load_payload_with_meta(path_str: str) -> tuple[object, bool]:
    p = Path(path_str)
    raw = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    self_loop_detected = False
    if isinstance(raw, dict) and "log" in raw and "entries" in raw["log"]:
        extracted = {}
        for entry in raw["log"]["entries"]:
            req_url = (entry.get("request") or {}).get("url", "")
            resp = entry.get("response") or {}
            headers = {(h.get("name") or "").lower(): (h.get("value") or "") for h in (resp.get("headers") or [])}
            st = headers.get("server-timing", "").lower()
            if "l2_tmp_mmap" in st or "l1_ram" in st or "l3_s3_snapshot" in st:
                self_loop_detected = True
            content = (resp.get("content") or {}).get("text", "")
            if content and "application/json" in (resp.get("content") or {}).get("mimeType", ""):
                try:
                    short_key = req_url.split("?")[0].split("/homes/")[-1] if "/homes/" in req_url else req_url.split("?")[0]
                    extracted[short_key] = json.loads(content)
                except Exception:
                    pass
        return extracted, self_loop_detected
    return raw, self_loop_detected


def compare_nodes(leg, tgt, path="root", report=None):
    if report is None:
        report = {
            "oracle_classification": "UNKNOWN",
            "populated_matches": 0,
            "empty_both_200": 0,
            "self_comparison_trap": False,
            "l2_missing_keys": [],
            "l2_type_mismatches": [],
            "l3_empty_array_regressions": [],
            "l3_row_count_diffs": [],
            "l4_na_regressions": [],
            "l4_value_mismatches": [],
        }

    if isinstance(leg, dict) and isinstance(tgt, dict):
        for k, l_val in leg.items():
            sub_path = f"{path}.{k}"
            if k not in tgt:
                report["l2_missing_keys"].append(sub_path)
                continue
            compare_nodes(l_val, tgt[k], sub_path, report)
    elif isinstance(leg, list) and isinstance(tgt, list):
        if len(leg) > 0 and len(tgt) == 0:
            report["l3_empty_array_regressions"].append(f"{path} (legacy={len(leg)} rows -> target=0 rows)")
            return report
        if len(leg) != len(tgt):
            report["l3_row_count_diffs"].append(f"{path} (legacy={len(leg)} vs target={len(tgt)})")

        leg_keyed = {extract_identity_key(e): e for e in leg}
        tgt_keyed = {extract_identity_key(e): e for e in tgt}
        if leg and tgt and None not in leg_keyed and None not in tgt_keyed and len(leg_keyed) == len(leg):
            for ikey, l_elem in leg_keyed.items():
                if ikey not in tgt_keyed:
                    report["l4_value_mismatches"].append(f"{path}[{ikey}] missing in target")
                else:
                    compare_nodes(l_elem, tgt_keyed[ikey], f"{path}[{ikey}]", report)
        else:
            for idx in range(min(len(leg), len(tgt), 10)):
                compare_nodes(leg[idx], tgt[idx], f"{path}[{idx}]", report)
    else:
        if type(leg) is not type(tgt) and leg is not None and tgt is not None and not (
            isinstance(leg, (int, float)) and isinstance(tgt, (int, float))
        ):
            report["l2_type_mismatches"].append(f"{path} (legacy={type(leg).__name__} vs target={type(tgt).__name__})")
        elif not is_na_value(leg) and is_na_value(tgt):
            report["l4_na_regressions"].append(f"{path} (legacy={leg!r} -> target={tgt!r})")
        elif isinstance(leg, (int, float)) and isinstance(tgt, (int, float)):
            if abs(float(leg) - float(tgt)) > 1e-4:
                report["l4_value_mismatches"].append(f"{path} (legacy={leg} vs target={tgt})")
        elif leg != tgt:
            report["l4_value_mismatches"].append(f"{path} (legacy={leg!r} vs target={tgt!r})")

    return report


def finalize_classification(leg, tgt, report: dict) -> dict:
    failures = (
        len(report["l2_missing_keys"])
        + len(report["l2_type_mismatches"])
        + len(report["l3_empty_array_regressions"])
        + len(report["l3_row_count_diffs"])
        + len(report["l4_na_regressions"])
        + len(report["l4_value_mismatches"])
    )
    if report.get("self_comparison_trap"):
        report["oracle_classification"] = "SELF_COMPARISON_TRAP_INVALID"
    elif failures > 0:
        report["oracle_classification"] = "REAL_PARITY_FAILURE"
    elif is_effectively_empty_payload(leg) and is_effectively_empty_payload(tgt):
        report["oracle_classification"] = "EMPTY_BOTH_200"
        report["empty_both_200"] = 1
    else:
        report["oracle_classification"] = "POPULATED_MATCH"
        report["populated_matches"] = 1
    return report


def run_self_test() -> int:
    r_empty = finalize_classification({"items": []}, {"items": []}, compare_nodes({"items": []}, {"items": []}))
    assert r_empty["oracle_classification"] == "EMPTY_BOTH_200"

    leg = {"stations": [{"mac": "AA:BB", "rssi": -55}, {"mac": "CC:DD", "rssi": -68}]}
    tgt = {"stations": [{"mac": "CC:DD", "rssi": -68}, {"mac": "AA:BB", "rssi": -55}]}
    r_pop = finalize_classification(leg, tgt, compare_nodes(leg, tgt))
    assert r_pop["oracle_classification"] == "POPULATED_MATCH"

    tgt_bad = {"stations": [{"mac": "AA:BB", "rssi": "N/A"}, {"mac": "CC:DD", "rssi": -68}]}
    r_bad = finalize_classification(leg, tgt_bad, compare_nodes(leg, tgt_bad))
    assert r_bad["oracle_classification"] == "REAL_PARITY_FAILURE"

    with tempfile.TemporaryDirectory() as td:
        har = Path(td) / "legacy.har"
        har.write_text(json.dumps({
            "log": {
                "entries": [{
                    "request": {"url": "https://example/cloud_gateway_api/v3/stats/homes/H1/nodes"},
                    "response": {
                        "headers": [{"name": "Server-Timing", "value": 'tier;desc="L2_TMP_MMAP"'}],
                        "content": {"mimeType": "application/json", "text": '{"items":[{"id":"1"}]}'},
                    },
                }]
            }
        }), encoding="utf-8")
        payload, loop = load_payload_with_meta(str(har))
        rep = compare_nodes(payload, payload)
        rep["self_comparison_trap"] = loop
        rep = finalize_classification(payload, payload, rep)
        assert rep["oracle_classification"] == "SELF_COMPARISON_TRAP_INVALID"

    print("parity_gate v2.0 self-test: OK")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="OnDemand L1-L4 Parity & POPULATED_MATCH Oracle v2.0")
    ap.add_argument("--legacy", help="Legacy JSON or HAR path")
    ap.add_argument("--target", help="Target JSON or HAR path")
    ap.add_argument("--json", action="store_true", help="Emit JSON report")
    ap.add_argument("--self-test", action="store_true", help="Run built-in self-test")
    args = ap.parse_args(argv)

    if args.self_test:
        return run_self_test()
    if not args.legacy or not args.target:
        ap.error("--legacy and --target are required unless --self-test is passed.")

    leg_obj, leg_loop = load_payload_with_meta(args.legacy)
    tgt_obj, _ = load_payload_with_meta(args.target)
    rep = compare_nodes(leg_obj, tgt_obj)
    rep["self_comparison_trap"] = leg_loop
    rep = finalize_classification(leg_obj, tgt_obj, rep)

    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(f"[Parity-Gate v2.0] Verdict={rep['oracle_classification']} | Populated={rep['populated_matches']} | EmptyBoth={rep['empty_both_200']}")
    return 0 if rep["oracle_classification"] in ("POPULATED_MATCH", "EMPTY_BOTH_200") else 1


if __name__ == "__main__":
    sys.exit(main())
