"""Phase 2: Multi-Microservice Federation Reducer (Reduce Stage)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import lancedb
from openai import OpenAI

from kaizen_kit.radar.dashboard import generate_federation_dashboard
from kaizen_kit.radar.embeddings import find_cosine_clones


def _synthesize_ecosystem_plan(summary: dict[str, Any]) -> tuple[str, str]:
    url = os.getenv("LLM_API_URL", "http://127.0.0.1:8000/v1")
    model = os.getenv("LLM_MODEL", "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ")
    try:
        r = httpx.get(url.rstrip("/") + "/models", timeout=3.0)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        client = OpenAI(base_url=url, api_key=os.getenv("LLM_API_KEY", "local-only"), timeout=120.0)
        payload = {
            "analyzed_services": summary["analyzed_services"],
            "cross_service_polyglot_clones": summary["cross_service_polyglot_clones"][:15],
            "shared_contracts_or_tables": summary["shared_contracts_or_tables"],
            "service_contract_links": summary["service_contract_links"][:20],
            "distributed_monolith_tickets": summary["distributed_monolith_tickets"][:15],
        }
        resp = client.chat.completions.create(
            model=model,
            temperature=0.1,
            max_tokens=1800,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a distributed systems architect reviewing federated Rust, Java, and Go microservice capsules. "
                        "Diagnose distributed monoliths, shared database tight coupling, and polyglot duplicate domain logic. "
                        "Provide a concrete cross-service refactoring roadmap in Markdown."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        txt = (resp.choices[0].message.content or "").strip()
        if txt:
            return txt, f"vllm:{model}"
    except Exception:
        pass

    lines = [
        "# Ecosystem Federation Refactor Plan",
        "",
        f"- **Services Analyzed**: {', '.join(summary['analyzed_services'])}",
        f"- **Cross-Service Polyglot Clones**: {len(summary['cross_service_polyglot_clones'])}",
        f"- **Shared Contracts / Tables**: {len(summary['shared_contracts_or_tables'])}",
        f"- **Distributed Monolith Tickets**: {len(summary['distributed_monolith_tickets'])}",
        "",
        "## Cross-Service Polyglot Clones",
    ]
    for c in summary["cross_service_polyglot_clones"][:10]:
        lines.append(
            f"- `{c['service_a']}` (`{c['lang_a']}` `{c['fn_a']}`) <-> "
            f"`{c['service_b']}` (`{c['lang_b']}` `{c['fn_b']}`) — cosine `{c['similarity']}`"
        )
    lines.extend(["", "## Shared Boundaries / Database Coupling"])
    for ident, svcs in summary["shared_contracts_or_tables"].items():
        lines.append(f"- `{ident}` shared across: {', '.join(svcs)}")
    return "\n".join(lines), "deterministic:offline"


def federate_capsules(
    capsules_root: Path,
    out_dir: Path,
    clone_threshold: float = 0.87,
) -> dict[str, Any]:
    capsules_root = capsules_root.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    capsule_dirs = sorted(p.parent for p in capsules_root.glob("*/manifest.json"))
    global_db = lancedb.connect(str(out_dir / "global_vectors.lance"))
    merged_records: list[dict[str, Any]] = []

    boundary_index: dict[str, set[str]] = {}
    inbound_map: dict[tuple[str, str], set[str]] = {}
    outbound_map: dict[tuple[str, str], set[str]] = {}
    ticket_services: dict[str, set[str]] = {}

    total_nodes = 0
    total_edges = 0
    services: list[str] = []

    for cap in capsule_dirs:
        manifest = json.loads((cap / "manifest.json").read_text(encoding="utf-8"))
        svc = manifest["service_name"]
        services.append(svc)
        total_nodes += int(manifest.get("node_count", 0))
        total_edges += int(manifest.get("edge_count", 0))

        vpath = cap / "vectors.lance"
        if vpath.is_dir():
            local_db = lancedb.connect(str(vpath))
            tbl_resp = local_db.list_tables() if hasattr(local_db, "list_tables") else local_db.table_names()
            tbl_names = getattr(tbl_resp, "tables", tbl_resp)
            if "functions" in tbl_names:
                tbl = local_db.open_table("functions")
                merged_records.extend(tbl.to_arrow().to_pylist())

        b_file = cap / "boundaries.json"
        if b_file.is_file():
            for item in json.loads(b_file.read_text(encoding="utf-8")):
                ident = str(item.get("identifier", "")).strip()
                proto = str(item.get("protocol", "HTTP")).strip()
                direction = str(item.get("direction", "OUTBOUND")).strip()
                if not ident:
                    continue
                boundary_index.setdefault(f"{proto}:{ident}", set()).add(svc)
                if direction == "INBOUND":
                    inbound_map.setdefault((proto, ident), set()).add(svc)
                else:
                    outbound_map.setdefault((proto, ident), set()).add(svc)

        t_file = cap / "git_tickets.json"
        if t_file.is_file():
            tdata = json.loads(t_file.read_text(encoding="utf-8"))
            for ticket in (tdata.get("recent_tickets") or {}).keys():
                ticket_services.setdefault(ticket, set()).add(svc)

    cross_service_clones: list[dict[str, Any]] = []
    if merged_records:
        global_db.create_table("global_functions", data=merged_records, mode="overwrite")
        pairs = find_cosine_clones(
            merged_records, threshold=clone_threshold, cross_service_only=True
        )
        cross_service_clones = [
            {
                "a_id": p.a_id,
                "b_id": p.b_id,
                "service_a": p.a_service,
                "lang_a": p.a_lang,
                "fn_a": p.a_name,
                "service_b": p.b_service,
                "lang_b": p.b_lang,
                "fn_b": p.b_name,
                "similarity": p.similarity,
            }
            for p in pairs
        ]

    shared_boundaries = {
        k: sorted(v) for k, v in boundary_index.items() if len(v) > 1
    }

    service_contract_links: list[dict[str, Any]] = []
    for (proto, ident), providers in inbound_map.items():
        consumers = outbound_map.get((proto, ident), set())
        for p_svc in sorted(providers):
            for c_svc in sorted(consumers):
                if p_svc != c_svc:
                    service_contract_links.append(
                        {
                            "consumer": c_svc,
                            "provider": p_svc,
                            "protocol": proto,
                            "identifier": ident,
                        }
                    )

    distributed_tickets = [
        {"ticket": t, "services": sorted(svcs)}
        for t, svcs in sorted(ticket_services.items())
        if len(svcs) > 1
    ]

    summary = {
        "analyzed_services": services,
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "cross_service_polyglot_clones": cross_service_clones,
        "shared_contracts_or_tables": shared_boundaries,
        "service_contract_links": service_contract_links,
        "distributed_monolith_tickets": distributed_tickets,
    }
    report_md, llm_used = _synthesize_ecosystem_plan(summary)
    summary["llm_backend"] = llm_used

    (out_dir / "ecosystem_federation_report.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    (out_dir / "ecosystem_refactor_report.md").write_text(report_md, encoding="utf-8")
    generate_federation_dashboard(capsules_root, out_dir, summary, report_md)
    return summary

