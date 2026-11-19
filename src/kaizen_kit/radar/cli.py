"""Typer CLI entrypoint for `kaizen-kit radar`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer

from kaizen_kit.radar.dashboard import generate_service_dashboard
from kaizen_kit.radar.ec2_spot_batch import (
    DEFAULT_BUCKET,
    DEFAULT_PROFILE,
    DEFAULT_REGION,
    run_spot_batch,
)
from kaizen_kit.radar.federate import federate_capsules
from kaizen_kit.radar.single_service import analyze_service

app = typer.Typer(help="Refactor Radar — Polyglot Pre-Refactor & Federation CLI")


@app.command("analyze")
def cmd_analyze(
    repo: Path = typer.Option(..., help="Microservice repository directory"),
    service_name: str = typer.Option(..., help="Service name"),
    capsule_out: Path = typer.Option(..., help="Output Service Capsule directory"),
    rules_dir: Optional[Path] = typer.Option(None, help="Custom rules directory"),
    with_scip: bool = typer.Option(False, "--with-scip/--no-with-scip", help="Run SCIP indexer"),
    since_days: int = typer.Option(90, help="Git history window in days"),
    embed_backend: str = typer.Option("auto", help="auto | tei | local-hash"),
) -> None:
    manifest = analyze_service(
        repo=repo,
        service_name=service_name,
        capsule_out=capsule_out,
        rules_dir=rules_dir,
        with_scip=with_scip,
        since_days=since_days,
        embed_backend=embed_backend,
    )
    typer.echo(manifest.model_dump_json(indent=2))


@app.command("scan")
def cmd_scan(
    repo: Path = typer.Argument(..., help="Repository directory to scan"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Optional service name"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Capsule output directory"),
    since_days: int = typer.Option(90, "--since", help="Git history window in days"),
) -> None:
    """Single-command scan: runs full AST, smell, git forensics, clone detection, and generates HTML dashboard."""
    repo = repo.resolve()
    service_name = name or repo.name
    capsule_out = (out or Path(f"C:/work-space/artifacts/refactor-radar/{service_name}")).resolve()

    typer.echo(f"[*] Scanning repository: {repo} (service={service_name})...")
    manifest = analyze_service(
        repo=repo,
        service_name=service_name,
        capsule_out=capsule_out,
        since_days=since_days,
    )

    dashboard_path = generate_service_dashboard(capsule_dir=capsule_out)
    typer.echo(f"\n[+] Scan completed successfully for '{service_name}'!")
    typer.echo(f"    - Nodes: {manifest.node_count}")
    typer.echo(f"    - Edges: {manifest.edge_count}")
    typer.echo(f"    - Smells: {manifest.smell_count}")
    typer.echo(f"    - Boundaries: {manifest.boundary_count}")
    typer.echo(f"    - Dashboard: file:///{dashboard_path.as_posix()}")

    ranked_path = capsule_out / "ranked_targets.json"
    if ranked_path.is_file():
        try:
            targets = json.loads(ranked_path.read_text(encoding="utf-8"))
            if targets:
                typer.echo("\n[Top Refactor Targets by ROI]:")
                for i, t in enumerate(targets[:5], 1):
                    typer.echo(
                        f"  {i}. {t.get('signature', t.get('name'))} "
                        f"(ROI: {t.get('roi_score'):.2f}, "
                        f"Complexity: {t.get('cognitive_complexity')}, "
                        f"Churn: {t.get('churn')})"
                    )
        except Exception:
            pass


@app.command("federate")
def cmd_federate(
    capsules_root: Path = typer.Option(..., help="Directory containing Service Capsules"),
    out_dir: Path = typer.Option(..., help="Output federation directory"),
    clone_threshold: float = typer.Option(0.87, help="Cosine similarity threshold"),
) -> None:
    summary = federate_capsules(
        capsules_root=capsules_root,
        out_dir=out_dir,
        clone_threshold=clone_threshold,
    )
    typer.echo(json.dumps(summary, indent=2))


@app.command("dashboard")
def cmd_dashboard(
    capsule: Path = typer.Option(..., help="Service Capsule directory"),
    out_html: Optional[Path] = typer.Option(None, help="Optional output HTML path"),
) -> None:
    dest = generate_service_dashboard(capsule_dir=capsule, out_html=out_html)
    typer.echo(f"Dashboard written to {dest}")


@app.command("spot-run")
def cmd_spot_run(
    repo: List[str] = typer.Option(..., help="Target repo spec as name=path (repeatable)"),
    out_dir: Path = typer.Option(Path("C:/work-space/artifacts/refactor-radar"), help="Local download dir"),
    profile: str = typer.Option(DEFAULT_PROFILE, help="AWS profile (kaizen-u2)"),
    region: str = typer.Option(DEFAULT_REGION, help="AWS region"),
    bucket: str = typer.Option(DEFAULT_BUCKET, help="S3 bucket for staging/capsules"),
    spot_max_price: str = typer.Option("0.75", help="Spot max hourly price USD"),
    with_scip: bool = typer.Option(False, "--with-scip/--no-with-scip", help="Run SCIP indexers"),
    dry_run: bool = typer.Option(False, "--dry-run/--no-dry-run", help="Validate Spot placement score and runai_streamer config without launching EC2"),
) -> None:
    parsed: list[tuple[str, Path]] = []
    for item in repo:
        if "=" not in item:
            raise typer.BadParameter(f"Expected name=path, got: {item}")
        name, path_str = item.split("=", 1)
        p = Path(path_str).resolve()
        if not p.is_dir():
            raise typer.BadParameter(f"Repository path not found: {p}")
        parsed.append((name.strip(), p))

    summary = run_spot_batch(
        repos=parsed,
        out_dir=out_dir.resolve(),
        profile=profile,
        region=region,
        bucket=bucket,
        spot_max_price=spot_max_price,
        with_scip=with_scip,
        dry_run=dry_run,
    )
    typer.echo(json.dumps(summary, indent=2))


def main(argv: Optional[list[str]] = None) -> int:
    app(args=argv, standalone_mode=False)
    return 0


if __name__ == "__main__":
    app()

