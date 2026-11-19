"""End-to-end tests for Refactor Radar (Rust/Java/Go Phase 1, Phase 2 Federation, Boundaries & HTML Dashboard)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import duckdb
import pytest

from kaizen_kit.radar.boundary_extractor import extract_boundaries, scan_smells
from kaizen_kit.radar.federate import federate_capsules
from kaizen_kit.radar.single_service import analyze_service


def _git_init_and_commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "radar@test.local"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Radar Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def polyglot_repos(tmp_path: Path) -> tuple[Path, Path, Path]:
    # 1. Rust service
    rust_repo = tmp_path / "order-rust"
    (rust_repo / "src").mkdir(parents=True)
    (rust_repo / "src" / "main.rs").write_text(
        """
use axum::{routing::get, Router};

pub fn calculate_discount(amount: i64, tier: i32, promo: bool) -> i64 {
    let mut base = amount;
    if tier > 2 {
        base = base - (base / 10);
    }
    if promo {
        if base > 100 {
            base = base - 15;
        }
    }
    let val = Some(base).unwrap();
    val
}

pub fn duplicate_discount_logic(amount: i64, tier: i32, promo: bool) -> i64 {
    let mut base = amount;
    if tier > 2 {
        base = base - (base / 10);
    }
    if promo {
        if base > 100 {
            base = base - 15;
        }
    }
    let val = Some(base).unwrap();
    val
}

pub async fn routes() -> Router {
    let _q = sqlx::query("SELECT id FROM orders_table WHERE status = 'OPEN'");
    Router::new().route("/api/v1/orders", get(calculate_discount))
}
""",
        encoding="utf-8",
    )
    _git_init_and_commit(rust_repo, "UC-4101 add rust order discount and orders_table query")

    # 2. Java service (shares orders_table + HTTP contract + ticket UC-4101)
    java_repo = tmp_path / "billing-java"
    (java_repo / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (java_repo / "src" / "main" / "java" / "com" / "acme" / "BillingController.java").write_text(
        """
package com.acme;

@Table(name = "orders_table")
@RestController
public class BillingController {
    @GetMapping("/api/v1/invoices")
    @Transactional
    public String processInvoice(int amount, boolean vip) {
        if (amount <= 0) {
            return "invalid";
        }
        if (vip) {
            for (int i = 0; i < 3; i++) {
                if (i == 2) {
                    return restClient.getForEntity("http://order-rust/api/v1/orders", String.class);
                }
            }
        }
        return "ok";
    }
}
""",
        encoding="utf-8",
    )
    _git_init_and_commit(java_repo, "UC-4101 add billing controller touching orders_table")

    # 3. Go service (shares /api/v1/orders + orders_table + Go smells)
    go_repo = tmp_path / "fulfillment-go"
    (go_repo / "cmd").mkdir(parents=True)
    (go_repo / "cmd" / "server.go").write_text(
        """
package main

import (
    "context"
    "database/sql"
    "net/http"
)

func ServeOrders(db *sql.DB, ctx context.Context, ids []string) {
    http.HandleFunc("/api/v1/fulfillment", func(w http.ResponseWriter, r *http.Request) {
        w.WriteHeader(200)
    })
    for _, id := range ids {
        defer func(x string) { _ = x }(id)
    }
    go func() {
        _ = len(ids)
    }()
    _, _ = db.QueryContext(ctx, "SELECT id FROM orders_table WHERE id = $1", "1")
    _, _ = http.NewRequest("GET", "/api/v1/orders", nil)
}
""",
        encoding="utf-8",
    )
    _git_init_and_commit(go_repo, "UC-4102 fulfillment service")

    return rust_repo, java_repo, go_repo


def test_polyglot_smells_and_boundaries(polyglot_repos: tuple[Path, Path, Path]) -> None:
    rust_repo, java_repo, go_repo = polyglot_repos

    r_smells = scan_smells(rust_repo)
    assert any(s.rule_id == "rust-unwrap-in-prod" for s in r_smells)
    r_bounds = extract_boundaries(rust_repo, "order-rust")
    assert any(b.protocol == "HTTP" and b.direction == "INBOUND" and "/api/v1/orders" in b.identifier for b in r_bounds)
    assert any(b.protocol == "SQL" and "table:orders_table" == b.identifier for b in r_bounds)

    j_smells = scan_smells(java_repo)
    assert any(s.rule_id == "java-transactional-external-io" for s in j_smells)
    j_bounds = extract_boundaries(java_repo, "billing-java")
    assert any(b.protocol == "SQL" and "table:orders_table" == b.identifier for b in j_bounds)

    g_smells = scan_smells(go_repo)
    assert any(s.rule_id == "go-defer-in-loop" for s in g_smells)
    assert any(s.rule_id == "go-unbounded-goroutine" for s in g_smells)
    g_bounds = extract_boundaries(go_repo, "fulfillment-go")
    assert any(b.protocol == "HTTP" and b.direction == "OUTBOUND" and "/api/v1/orders" in b.identifier for b in g_bounds)


def test_phase1_and_phase2_federation_end_to_end(
    polyglot_repos: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    rust_repo, java_repo, go_repo = polyglot_repos
    capsules_root = tmp_path / "capsules"

    m_rust = analyze_service(rust_repo, "order-rust", capsules_root / "order-rust", embed_backend="local-hash")
    m_java = analyze_service(java_repo, "billing-java", capsules_root / "billing-java", embed_backend="local-hash")
    m_go = analyze_service(go_repo, "fulfillment-go", capsules_root / "fulfillment-go", embed_backend="local-hash")

    assert m_rust.node_count >= 2
    assert m_rust.clone_count >= 1
    assert (capsules_root / "order-rust" / "dashboard.html").is_file()
    assert (capsules_root / "order-rust" / "refactor_report.md").is_file()
    assert (capsules_root / "order-rust" / "vectors.lance").is_dir()

    # Verify Parquet readable via DuckDB
    con = duckdb.connect()
    cnt = con.execute(
        "SELECT count(*) FROM read_parquet(?)",
        [str(capsules_root / "order-rust" / "nodes.parquet")],
    ).fetchone()[0]
    assert cnt == m_rust.node_count
    assert m_java.smell_count >= 1
    assert m_go.smell_count >= 2

    # Phase 2 Federation
    fed_dir = capsules_root / "_federated"
    summary = federate_capsules(capsules_root, fed_dir, clone_threshold=0.50)

    assert sorted(summary["analyzed_services"]) == ["billing-java", "fulfillment-go", "order-rust"]
    assert "SQL:table:orders_table" in summary["shared_contracts_or_tables"]
    assert len(summary["shared_contracts_or_tables"]["SQL:table:orders_table"]) == 3
    assert any(
        link["consumer"] == "fulfillment-go" and link["provider"] == "order-rust" and "/api/v1/orders" in link["identifier"]
        for link in summary["service_contract_links"]
    )
    assert any(t["ticket"] == "UC-4101" and set(t["services"]) == {"order-rust", "billing-java"} for t in summary["distributed_monolith_tickets"])
    assert (fed_dir / "ecosystem_dashboard.html").is_file()
    assert (fed_dir / "ecosystem_refactor_report.md").is_file()
    dash_html = (fed_dir / "ecosystem_dashboard.html").read_text(encoding="utf-8")
    assert "Refactor Radar" in dash_html
    assert "orders_table" in dash_html


def test_spot_batch_enforces_one_time_spot_and_zero_leftover_cleanup() -> None:
    from kaizen_kit.radar.ec2_spot_batch import (
        launch_one_time_spot_instance,
        terminate_and_verify_zero_leftovers,
    )

    captured: dict = {}

    class FakeWaiter:
        def wait(self, **kwargs):
            captured["waiter_kwargs"] = kwargs

    class FakeEc2:
        def run_instances(self, **kwargs):
            captured["run_kwargs"] = kwargs
            return {"Instances": [{"InstanceId": "i-0fake123456"}]}

        def terminate_instances(self, **kwargs):
            captured["term_kwargs"] = kwargs

        def get_waiter(self, name):
            captured["waiter_name"] = name
            return FakeWaiter()

        def describe_instances(self, **kwargs):
            return {"Reservations": []}

    fake = FakeEc2()
    iid, itype, subnet = launch_one_time_spot_instance(
        ec2_client=fake,
        ami_id="ami-test",
        subnet_ids=("subnet-a",),
        sg_id="sg-test",
        iam_profile_arn="arn:aws:iam::123:instance-profile/test",
        instance_types=("g5.2xlarge",),
        spot_max_price="0.75",
        auto_shutdown_minutes=45,
        run_id="run-1",
    )
    assert iid == "i-0fake123456"
    assert itype == "g5.2xlarge"
    assert subnet == "subnet-a"
    rk = captured["run_kwargs"]
    assert rk["InstanceInitiatedShutdownBehavior"] == "terminate"
    assert rk["InstanceMarketOptions"]["MarketType"] == "spot"
    assert rk["InstanceMarketOptions"]["SpotOptions"]["SpotInstanceType"] == "one-time"
    assert rk["InstanceMarketOptions"]["SpotOptions"]["InstanceInterruptionBehavior"] == "terminate"

    leftovers = terminate_and_verify_zero_leftovers(fake, iid)
    assert leftovers == []
    assert captured["term_kwargs"]["InstanceIds"] == [iid]
    assert captured["waiter_name"] == "instance_terminated"

