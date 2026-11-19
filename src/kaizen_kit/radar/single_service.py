"""Phase 1: Single Microservice Refactor Analysis Pipeline (Map Stage)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import duckdb
import httpx
from openai import OpenAI

from kaizen_kit.radar.boundary_extractor import (
    default_rules_dir,
    extract_boundaries,
    find_binary,
    scan_smells,
)
from kaizen_kit.radar.capsule_schema import CapsuleManifest, SemanticClonePair
from kaizen_kit.radar.dashboard import generate_service_dashboard
from kaizen_kit.radar.embeddings import embed_and_store_lancedb


def resolve_radar_core_bin() -> str:
    env_bin = os.getenv("RADAR_CORE_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin
    found = find_binary(["radar-core"])
    if found:
        return found
    kit_root = Path(__file__).resolve().parents[3]
    exe = "radar-core.exe" if os.name == "nt" else "radar-core"
    for profile in ("release", "debug"):
        cand = kit_root / "crates" / "radar-core" / "target" / profile / exe
        if cand.is_file():
            return str(cand)
    raise FileNotFoundError(
        "radar-core binary not found. Build it with `cargo build --release --manifest-path crates/radar-core/Cargo.toml`."
    )


def run_scip_indexers(repo: Path, workdir: Path, timeout_sec: int = 300) -> Path | None:
    scip_out = workdir / "index.scip"
    try:
        if (repo / "Cargo.toml").is_file() and find_binary(["rust-analyzer"]):
            subprocess.run(
                ["rust-analyzer", "scip", str(repo), "--output", str(scip_out)],
                cwd=repo,
                check=False,
                timeout=timeout_sec,
            )
        elif (repo / "go.mod").is_file() and find_binary(["scip-go"]):
            subprocess.run(
                ["scip-go", "--repository-root", str(repo), "-o", str(scip_out)],
                cwd=repo,
                check=False,
                timeout=timeout_sec,
            )
        elif (
            (repo / "pom.xml").is_file()
            or (repo / "build.gradle").is_file()
            or (repo / "build.gradle.kts").is_file()
        ) and find_binary(["scip-java"]):
            subprocess.run(
                ["scip-java", "index", "--output", str(scip_out)],
                cwd=repo,
                check=False,
                timeout=timeout_sec,
            )
    except subprocess.TimeoutExpired:
        return None
    return scip_out if scip_out.is_file() else None


def rank_refactor_targets(
    capsule_dir: Path,
    service_name: str,
    smells: list[dict[str, Any]],
    clones: list[SemanticClonePair],
    limit: int = 50,
) -> list[dict[str, Any]]:
    smell_per_file: dict[str, int] = {}
    for s in smells:
        fp = s.get("file_path", "")
        smell_per_file[fp] = smell_per_file.get(fp, 0) + 1

    clone_counts: dict[str, int] = {}
    for c in clones:
        clone_counts[c.a_id] = clone_counts.get(c.a_id, 0) + 1
        clone_counts[c.b_id] = clone_counts.get(c.b_id, 0) + 1

    con = duckdb.connect()
    nodes = con.execute(
        """
        SELECT symbol_id, language, name, file_path, start_line, end_line,
               complexity, fan_in, fan_out, churn_90d, hotspot_score, clean_body
        FROM read_parquet(?)
        """,
        [str(capsule_dir / "nodes.parquet")],
    ).fetchall()

    ranked: list[dict[str, Any]] = []
    for n in nodes:
        sym_id, lang, name, fp, s_line, e_line, comp, fan_in, fan_out, churn, hotspot, body = n
        s_cnt = smell_per_file.get(fp, 0)
        c_cnt = clone_counts.get(sym_id, 0)
        smell_w = 1.0 + (0.35 * s_cnt)
        clone_w = 1.0 + (0.50 * c_cnt)
        blast_penalty = 1.0 + (0.4 * (float(fan_in) ** 0.5))
        roi_score = ((float(hotspot) + 1.0) * smell_w * clone_w) / blast_penalty
        ranked.append(
            {
                "symbol_id": sym_id,
                "service_name": service_name,
                "language": lang,
                "name": name,
                "file_path": fp,
                "start_line": int(s_line),
                "end_line": int(e_line),
                "complexity": int(comp),
                "fan_in": int(fan_in),
                "fan_out": int(fan_out),
                "churn_90d": int(churn),
                "hotspot_score": round(float(hotspot), 3),
                "smell_count": s_cnt,
                "clone_count": c_cnt,
                "roi_score": round(roi_score, 3),
                "clean_body": str(body)[:3500],
            }
        )
    ranked.sort(key=lambda x: x["roi_score"], reverse=True)
    return ranked[:limit]


def _deterministic_report(
    service_name: str,
    top_targets: list[dict[str, Any]],
    clones: list[SemanticClonePair],
    smells: list[dict[str, Any]],
    boundaries: list[dict[str, Any]],
) -> str:
    lines = [
        f"# Pre-Refactor Analysis Report: `{service_name}`",
        "",
        "## Top High-ROI Refactor Candidates",
    ]
    for i, t in enumerate(top_targets[:10], 1):
        lines.append(
            f"{i}. **`{t['name']}`** (`{t['file_path']}:{t['start_line']}-{t['end_line']}`, `{t['language']}`) — "
            f"ROI `{t['roi_score']}`, Complexity `{t['complexity']}`, Churn90d `{t['churn_90d']}`, "
            f"Fan-In `{t['fan_in']}`, Smells `{t['smell_count']}`, Clones `{t['clone_count']}`"
        )
    lines.extend(["", "## Detected Semantic Clones"])
    if clones:
        for c in clones[:10]:
            lines.append(f"- `{c.a_name}` <-> `{c.b_name}` (cosine `{c.similarity}`)")
    else:
        lines.append("- None above similarity threshold.")
    lines.extend(["", "## Structural Smells & Boundary Surface"])
    lines.append(f"- Structural smells detected: **{len(smells)}**")
    lines.append(f"- Extracted service boundary contracts: **{len(boundaries)}**")
    lines.extend(
        [
            "",
            "## Recommended Execution Checklist",
            "1. Add characterization/contract tests around top Fan-In symbols before splitting complex methods.",
            "2. Extract shared helpers for semantic clone clusters above 0.88 cosine similarity.",
            "3. Remediate error-severity smells (e.g. `@Transactional` external I/O, loop `defer`, production `.unwrap()`).",
        ]
    )
    return "\n".join(lines)


def synthesize_refactor_plan(
    service_name: str,
    top_targets: list[dict[str, Any]],
    clones: list[SemanticClonePair],
    smells: list[dict[str, Any]],
    boundaries: list[dict[str, Any]],
    llm_url: str | None = None,
    llm_model: str | None = None,
) -> tuple[str, str]:
    url = llm_url or os.getenv("LLM_API_URL", "http://127.0.0.1:8000/v1")
    model = llm_model or os.getenv("LLM_MODEL", "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ")

    try:
        # Quick reachability probe before OpenAI client call
        probe_url = url.rstrip("/") + "/models"
        r = httpx.get(probe_url, timeout=3.0)
        if r.status_code >= 400:
            raise RuntimeError(f"LLM probe HTTP {r.status_code}")

        client = OpenAI(base_url=url, api_key=os.getenv("LLM_API_KEY", "local-only"), timeout=120.0)
        payload = {
            "service": service_name,
            "top_refactor_candidates": [
                {k: v for k, v in t.items() if k != "clean_body"} | {"body_preview": t["clean_body"][:1600]}
                for t in top_targets[:8]
            ],
            "top_semantic_clones": [c.model_dump() for c in clones[:10]],
            "top_smells": smells[:20],
            "boundaries": boundaries[:20],
        }
        resp = client.chat.completions.create(
            model=model,
            temperature=0.1,
            max_tokens=1800,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a principal software architect specializing in Rust, Java, and Go. "
                        "Using the provided AST cognitive complexity, SCIP fan-in/fan-out blast radius, "
                        "90-day Git churn, structural smells, service boundaries, and semantic clones, "
                        "produce a concrete, step-by-step Pre-Refactor Action Plan in Markdown. "
                        "Name exact symbols, files, extraction boundaries, and characterization tests to write first."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        content = (resp.choices[0].message.content or "").strip()
        if content:
            return content, f"vllm:{model}"
    except Exception:
        pass

    return (
        _deterministic_report(service_name, top_targets, clones, smells, boundaries),
        "deterministic:offline",
    )


def analyze_service(
    repo: Path,
    service_name: str,
    capsule_out: Path,
    rules_dir: Path | None = None,
    with_scip: bool = False,
    since_days: int = 90,
    embed_backend: str = "auto",
) -> CapsuleManifest:
    repo = repo.resolve()
    capsule_out = capsule_out.resolve()
    capsule_out.mkdir(parents=True, exist_ok=True)
    rdir = rules_dir or default_rules_dir()

    scip_path = run_scip_indexers(repo, capsule_out) if with_scip else None
    radar_bin = resolve_radar_core_bin()
    cmd = [
        radar_bin,
        "extract",
        "--repo",
        str(repo),
        "--service-name",
        service_name,
        "--capsule-out",
        str(capsule_out),
        "--since-days",
        str(since_days),
    ]
    if scip_path:
        cmd.extend(["--scip-index", str(scip_path)])
    subprocess.run(cmd, check=True)

    smell_objs = scan_smells(repo, rules_dir=rdir)
    boundary_objs = extract_boundaries(repo, service_name=service_name, rules_dir=rdir)
    smells = [s.model_dump() for s in smell_objs]
    boundaries = [b.model_dump() for b in boundary_objs]
    (capsule_out / "smells.json").write_text(json.dumps(smells, indent=2), encoding="utf-8")
    (capsule_out / "boundaries.json").write_text(json.dumps(boundaries, indent=2), encoding="utf-8")

    clones, embed_used = embed_and_store_lancedb(capsule_out, backend=embed_backend)
    top_targets = rank_refactor_targets(capsule_out, service_name, smells, clones)
    (capsule_out / "ranked_targets.json").write_text(json.dumps(top_targets, indent=2), encoding="utf-8")

    report_md, llm_used = synthesize_refactor_plan(service_name, top_targets, clones, smells, boundaries)
    (capsule_out / "refactor_report.md").write_text(report_md, encoding="utf-8")

    con = duckdb.connect()
    n_cnt = int(con.execute("SELECT count(*) FROM read_parquet(?)", [str(capsule_out / "nodes.parquet")]).fetchone()[0])
    e_cnt = int(con.execute("SELECT count(*) FROM read_parquet(?)", [str(capsule_out / "edges.parquet")]).fetchone()[0])
    langs = [
        r[0]
        for r in con.execute("SELECT DISTINCT language FROM read_parquet(?)", [str(capsule_out / "nodes.parquet")]).fetchall()
    ]

    sha_proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else "no-git-head"

    manifest = CapsuleManifest(
        service_name=service_name,
        commit_sha=sha,
        languages=sorted(langs),
        node_count=n_cnt,
        edge_count=e_cnt,
        smell_count=len(smells),
        boundary_count=len(boundaries),
        clone_count=len(clones),
        embedding_backend=embed_used,
        llm_backend=llm_used,
    )
    (capsule_out / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    generate_service_dashboard(capsule_out)
    return manifest

