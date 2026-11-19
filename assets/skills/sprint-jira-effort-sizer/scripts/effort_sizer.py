#!/usr/bin/env python3
"""
Empirical Sprint & Jira Task Effort Sizer (`effort_sizer.py`).
Computes Fibonacci Story Points, Person-Days (adam-gün), Cross-Repo Blast Radius,
and Canonical Agent assignment for one or more task descriptions.
"""
import argparse
import json
import sys

LAYER_WEIGHTS = [
    ("Rust / OnDemand Engine (kaizen-the-ondemand-machine)", ("rust", "avro", "bson", "ingest", "simd", "lambda", "ondemand", "team"), 1.5, "systems-architect"),
    ("SQL DAG & Read-Model Parity (sql/**/*.sql)", ("sql", "duckdb", "view", "macro", "tbl_", "cache_", "parity"), 1.0, "data-sql-architect"),
    ("JVM Backend / BFF (CloudGatewayApi / AAS / SQE / ReadModelService)", ("cloud_gateway_api", "aas", "sqe", "sq ", "java", "spring", "openapi", "swagger", "mysql"), 1.5, "jvm-service-architect"),
    ("TelemetryDashboard Frontend UI & Playwright E2E (unified-frontend)", ("telemetry_dashboard", "ncv", "nvc", "vue", "ui", "tablo", "chart", "playwright", "gerrit"), 1.0, "release-hygiene-guardian"),
    ("Cloud / CI / CloudCompose / FinOps Infrastructure", ("aws", "s3", "dynamodb", "cloudwatch", "cloud_compose", "jenkins", "docker", "lora", "gpu"), 1.0, "e2e-systems-visionary"),
    ("Incident RCA & Falsification Test", ("uc-", "bug", "har", "502", "not found", "triage", "regresyon"), 1.0, "incident-rca-falsifier"),
]

FIBONACCI = [1, 2, 3, 5, 8, 13, 21]


def nearest_fib(val: float) -> int:
    best = FIBONACCI[0]
    for f in FIBONACCI:
        if abs(f - val) < abs(best - val):
            best = f
    return best


def size_task(desc: str) -> dict:
    d_low = desc.lower()
    matched_layers = []
    agents = []
    base_days = 0.5

    for layer_name, keywords, weight_days, agent in LAYER_WEIGHTS:
        if any(k in d_low for k in keywords):
            matched_layers.append(layer_name)
            base_days += weight_days
            if agent not in agents:
                agents.append(agent)

    if not matched_layers:
        matched_layers.append("General Module Change")
        base_days = 1.0
        agents.append("code-explorer")

    # Cross-repo integration multiplier when >=3 layers are touched
    if len(matched_layers) >= 3:
        base_days = round(base_days * 1.25, 1)

    sp = nearest_fib(base_days * 1.4)
    return {
        "task": desc,
        "estimated_person_days": round(base_days, 1),
        "story_points_fib": sp,
        "touched_layers": matched_layers,
        "recommended_agents": agents,
        "risk_level": "HIGH (Multi-Repo E2E)" if len(matched_layers) >= 3 else ("MEDIUM" if len(matched_layers) == 2 else "LOW"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Empirical Sprint & Jira Task Effort Sizer")
    ap.add_argument("--task", "-t", required=True, help="Task description (or ';'-separated task list)")
    ap.add_argument("--json", action="store_true", help="Emit JSON output")
    args = ap.parse_args()

    tasks = [t.strip() for t in args.task.split(";") if t.strip()]
    results = [size_task(t) for t in tasks]
    tot_days = sum(r["estimated_person_days"] for r in results)
    tot_sp = sum(r["story_points_fib"] for r in results)

    if args.json:
        print(json.dumps({"tasks": results, "total_person_days": tot_days, "total_story_points": tot_sp}, indent=2, ensure_ascii=False))
        return 0

    print("=" * 98)
    print("SPRINT & JIRA EMPIRICAL EFFORT SIZER REPORT")
    print("=" * 98)
    for idx, r in enumerate(results, 1):
        print(f"  [{idx}] {r['task']}")
        print(f"      -> Efor Tahmini : {r['estimated_person_days']} Adam-Gün | {r['story_points_fib']} Story Point (Fibonacci) | Risk: {r['risk_level']}")
        print(f"      -> Katmanlar    : {', '.join(r['touched_layers'])}")
        print(f"      -> Kanonik Ajan : {', '.join(r['recommended_agents'])}")
    print("-" * 98)
    print(f"  TOPLAM: {len(results)} Görev | {tot_days:.1f} Adam-Gün | {tot_sp} Story Point")
    print("=" * 98)
    return 0


if __name__ == "__main__":
    sys.exit(main())
