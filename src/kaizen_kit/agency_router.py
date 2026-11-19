#!/usr/bin/env python3
"""
Zero-Token-Bloat Lazy Router & NEXUS Workflow Engine for `msitarzewski/agency-agents`.

Indexes 220+ specialist personas across 18 divisions, 4 NEXUS scenario runbooks, and
standardized NEXUS handoff templates into `$AI_KIT_STATE_DIR/agency_catalog.json`,
keeping system-prompt token overhead at 0 until a specialist is dynamically loaded.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_KIT_SRC = Path(__file__).resolve().parent.parent
if str(_KIT_SRC) not in sys.path:
    sys.path.insert(0, str(_KIT_SRC))

from kaizen_kit import bedrock_bridge
from kaizen_kit.paths import resolve_assets_dir, resolve_kit_root, resolve_state_dir

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+.#_-]*", re.IGNORECASE)
_HEADING_RE = re.compile(r"^##+\s+(.+?)\s*$", re.MULTILINE)

DIVISION_DEFAULT_MODELS: Dict[str, str] = {
    "security": "opus",
    "specialized": "sonnet",
    "engineering": "sonnet",
    "testing": "sonnet",
    "product": "sonnet",
    "project-management": "sonnet",
    "design": "sonnet",
    "research": "sonnet",
    "support": "haiku",
    "sales": "haiku",
    "marketing": "haiku",
    "paid-media": "haiku",
    "finance": "haiku",
    "academic": "sonnet",
    "game-development": "sonnet",
    "gis": "sonnet",
    "healthcare": "sonnet",
    "spatial-computing": "sonnet",
}

OPUS_TIER_SLUGS = {
    "agents-orchestrator",
    "testing-reality-checker",
    "engineering-software-architect",
    "engineering-backend-architect",
    "engineering-multi-agent-systems-architect",
    "security-architect",
    "security-cloud-security-architect",
    "specialized-master-plan-architect",
    "specialized-workflow-architect",
}

COMPACT_SECTION_HINTS = (
    "identity",
    "mission",
    "critical rule",
    "core belief",
    "workflow",
    "deliverable",
    "success metric",
)


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_agency_repo() -> Optional[Path]:
    """Locate local clone of `msitarzewski/agency-agents` cleanly."""
    env_root = os.environ.get("AGENCY_AGENTS_ROOT", "").strip()
    candidates = []
    if env_root:
        candidates.append(Path(env_root).expanduser().resolve())
    kit_root = resolve_kit_root()
    candidates.extend([
        kit_root.parent / "agency-agents",
        kit_root.parent.parent / "agency-agents",
        Path.cwd() / "agency-agents",
    ])
    for cand in candidates:
        if (cand / "divisions.json").is_file():
            return cand
    return None


def resolve_catalog_path() -> Path:
    return resolve_state_dir() / "agency_catalog.json"


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower())
    return cleaned.strip("-")


def _parse_frontmatter(raw: str) -> tuple[Dict[str, str], str]:
    normalized = raw.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized.strip()
    parts = normalized.split("---\n", 2)
    if len(parts) < 3:
        return {}, normalized.strip()
    fm_block, body = parts[1], parts[2].lstrip("\n")
    fields: Dict[str, str] = {}
    current_key: Optional[str] = None
    for line in fm_block.splitlines():
        if line.startswith((" ", "\t")):
            if current_key and line.strip():
                fields[current_key] = f"{fields[current_key]} {line.strip()}".strip()
            continue
        current_key = None
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        current_key = key.strip()
        fields[current_key] = val.strip()
    for key, val in list(fields.items()):
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            fields[key] = val[1:-1]
    return fields, body


def _extract_sections(body: str) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(body))
    if not matches:
        return {"Overview": body.strip()}
    for idx, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        if content:
            sections[title] = content
    return sections


def _recommend_model(slug: str, division: str) -> str:
    if slug in OPUS_TIER_SLUGS:
        return "opus"
    return DIVISION_DEFAULT_MODELS.get(division, "sonnet")


def _compact_persona(agent: Dict[str, Any], max_chars: int = 1600) -> str:
    header = (
        f"# {agent['name']} (`{agent['slug']}` | {agent['division']})\n"
        f"- Vibe: {agent.get('vibe') or agent.get('description')}\n"
        f"- Recommended Bedrock Tier: `{agent.get('model', 'sonnet')}`\n"
    )
    sections = agent.get("sections") or {}
    picked: List[str] = []
    for title, text in sections.items():
        t_low = title.lower()
        if any(hint in t_low for hint in COMPACT_SECTION_HINTS):
            snippet = text if len(text) <= 450 else text[:447].rstrip() + "..."
            picked.append(f"## {title}\n{snippet}")
        if sum(len(p) for p in picked) + len(header) >= max_chars:
            break
    if not picked:
        body_head = (agent.get("body") or "")[: max(200, max_chars - len(header))].strip()
        return f"{header}\n{body_head}"[:max_chars]
    combined = header + "\n" + "\n\n".join(picked)
    return combined[:max_chars].rstrip()


def build_catalog(repo_root: Optional[Path] = None, out_path: Optional[Path] = None) -> Dict[str, Any]:
    root = repo_root or resolve_agency_repo()
    if root is None or not (root / "divisions.json").is_file():
        raise FileNotFoundError(
            "Local `agency-agents` repository not found. Set $AGENCY_AGENTS_ROOT or clone into parent workspace."
        )

    divisions_meta = json.loads((root / "divisions.json").read_text(encoding="utf-8")).get("divisions", {})
    agents: List[Dict[str, Any]] = []
    seen_slugs: set[str] = set()

    for division in sorted(divisions_meta.keys()):
        div_dir = root / division
        if not div_dir.is_dir():
            continue
        for md_path in sorted(div_dir.rglob("*.md")):
            raw = md_path.read_text(encoding="utf-8", errors="replace")
            fields, body = _parse_frontmatter(raw)
            name = fields.get("name", "").strip()
            if not name:
                continue
            slug = md_path.stem
            if slug in seen_slugs:
                slug = f"{division}-{_slugify(md_path.stem)}"
            seen_slugs.add(slug)
            rel_path = md_path.relative_to(root).as_posix()
            sections = _extract_sections(body)
            agents.append({
                "slug": slug,
                "name": name,
                "division": division,
                "description": fields.get("description", "").strip(),
                "color": fields.get("color", "").strip(),
                "emoji": fields.get("emoji", "").strip(),
                "vibe": fields.get("vibe", "").strip(),
                "model": _recommend_model(slug, division),
                "source_path": rel_path,
                "sections": sections,
                "body": body,
            })

    runbooks: List[Dict[str, Any]] = []
    rb_file = root / "strategy" / "runbooks.json"
    if rb_file.is_file():
        runbooks = json.loads(rb_file.read_text(encoding="utf-8")).get("runbooks", [])

    catalog = {
        "schema_version": "1.0",
        "built_at": utc_now_iso(),
        "repo_root": str(root),
        "division_count": len(divisions_meta),
        "agent_count": len(agents),
        "divisions": divisions_meta,
        "runbooks": runbooks,
        "agents": agents,
    }
    target = out_path or resolve_catalog_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return catalog


def load_catalog(auto_build: bool = True, catalog_path: Optional[Path] = None) -> Dict[str, Any]:
    target = catalog_path or resolve_catalog_path()
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("agents"):
                return data
        except Exception:
            pass
    if auto_build:
        return build_catalog(out_path=target)
    return {"divisions": {}, "runbooks": [], "agents": []}


def search_agents(
    query: str, division: str = "", limit: int = 8, catalog: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    cat = catalog or load_catalog()
    agents = cat.get("agents") or []
    div_filter = division.strip().lower()
    pool = [a for a in agents if not div_filter or a.get("division", "").lower() == div_filter]
    q_clean = query.strip().lower()
    if not q_clean:
        return [
            {
                "slug": a["slug"],
                "name": a["name"],
                "division": a["division"],
                "model": a.get("model", "sonnet"),
                "vibe": a.get("vibe", ""),
                "description": a.get("description", ""),
                "score": 1.0,
            }
            for a in pool[:limit]
        ]

    q_tokens = [t for t in _WORD_RE.findall(q_clean) if len(t) > 1]
    n_docs = max(len(pool), 1)
    doc_freq: Dict[str, int] = {}
    doc_tokens: List[set[str]] = []
    for a in pool:
        hay = " ".join([a["slug"], a["name"], a["division"], a.get("vibe", ""), a.get("description", ""), a.get("body", "")[:1200]]).lower()
        toks = set(_WORD_RE.findall(hay))
        doc_tokens.append(toks)
        for t in q_tokens:
            if t in toks:
                doc_freq[t] = doc_freq.get(t, 0) + 1

    scored: List[tuple[float, Dict[str, Any]]] = []
    for idx, a in enumerate(pool):
        toks = doc_tokens[idx]
        score = 0.0
        slug_low = a["slug"].lower()
        name_low = a["name"].lower()
        desc_low = a.get("description", "").lower()
        if q_clean == slug_low or q_clean == name_low:
            score += 20.0
        elif q_clean in slug_low or q_clean in name_low:
            score += 8.0
        elif q_clean in desc_low:
            score += 4.0
        for t in q_tokens:
            if t in toks:
                idf = math.log(1.0 + n_docs / (1.0 + doc_freq.get(t, 0)))
                boost = 2.5 if (t in slug_low or t in name_low) else 1.5 if t in desc_low else 1.0
                score += idf * boost
        if score > 0.0:
            scored.append((round(score, 3), a))

    scored.sort(key=lambda item: (-item[0], item[1]["slug"]))
    return [
        {
            "slug": a["slug"],
            "name": a["name"],
            "division": a["division"],
            "model": a.get("model", "sonnet"),
            "vibe": a.get("vibe", ""),
            "description": a.get("description", ""),
            "score": sc,
        }
        for sc, a in scored[:limit]
    ]


def load_agent(
    slug_or_name: str, compact: bool = False, catalog: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    cat = catalog or load_catalog()
    agents = cat.get("agents") or []
    key = slug_or_name.strip().lower()
    if not key:
        return None

    matched: Optional[Dict[str, Any]] = None
    for a in agents:
        if a["slug"].lower() == key or _slugify(a["name"]) == key:
            matched = dict(a)
            break
    if matched is None:
        for a in agents:
            div_prefix = f"{a['division'].lower()}-"
            short_slug = a["slug"].lower()[len(div_prefix):] if a["slug"].lower().startswith(div_prefix) else a["slug"].lower()
            if short_slug == key or key in a["slug"].lower():
                matched = dict(a)
                break
    if matched is None:
        hits = search_agents(slug_or_name, limit=1, catalog=cat)
        if hits:
            return load_agent(hits[0]["slug"], compact=compact, catalog=cat)
        return None

    if compact:
        matched["compact_prompt"] = _compact_persona(matched)
    return matched


def list_runbooks(catalog: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    cat = catalog or load_catalog()
    return list(cat.get("runbooks") or [])


def get_runbook(slug: str, catalog: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    key = slug.strip().lower()
    for rb in list_runbooks(catalog=catalog):
        if rb.get("slug", "").lower() == key:
            return rb
    return None


def render_handoff(
    from_agent: str,
    to_agent: str,
    phase: str = "Phase 3 - Build & Verify",
    task_ref: str = "TASK-1",
    summary: str = "",
    files: Optional[List[str]] = None,
    evidence: str = "",
    mode: str = "standard",
) -> str:
    ts = utc_now_iso()
    file_bullets = "\n".join(f"- `{f}`" for f in (files or ["(none)"]))
    if mode == "qa-pass":
        return (
            "# NEXUS QA Gate — PASS\n\n"
            f"- **Reviewer**: `{from_agent}` -> **Next**: `{to_agent}`\n"
            f"- **Task**: `{task_ref}` | **Timestamp**: `{ts}`\n"
            f"- **Summary**: {summary or 'Verified against acceptance criteria.'}\n"
            f"- **Files**:\n{file_bullets}\n"
            f"- **Empirical Evidence**: {evidence or 'Tests and provenance checks passed.'}\n"
        )
    if mode == "qa-fail":
        return (
            "# NEXUS QA Gate — NEEDS WORK (FAIL)\n\n"
            f"- **Reviewer**: `{from_agent}` -> **Return To**: `{to_agent}`\n"
            f"- **Task**: `{task_ref}` | **Timestamp**: `{ts}`\n"
            f"- **Defects / Blockers**: {summary or 'Acceptance criteria or empirical evidence missing.'}\n"
            f"- **Target Files**:\n{file_bullets}\n"
            f"- **Required Evidence for Re-Review**: {evidence or 'Failing-then-passing test output + parity proof.'}\n"
        )
    if mode == "escalation":
        return (
            "# NEXUS Escalation Notice (3-Attempt Limit Reached)\n\n"
            f"- **From**: `{from_agent}` -> **Escalated To**: `{to_agent}`\n"
            f"- **Task**: `{task_ref}` | **Timestamp**: `{ts}`\n"
            f"- **Failure Context**: {summary or 'Task exceeded retry budget; requires architectural decision.'}\n"
            f"- **Touched Files**:\n{file_bullets}\n"
            f"- **Evidence Collected**: {evidence or 'See verification logs.'}\n"
        )
    return (
        "# NEXUS Handoff Document\n\n"
        "| Field | Value |\n"
        "| :--- | :--- |\n"
        f"| **From** | `{from_agent}` |\n"
        f"| **To** | `{to_agent}` |\n"
        f"| **Phase** | `{phase}` |\n"
        f"| **Task Reference** | `{task_ref}` |\n"
        f"| **Timestamp** | `{ts}` |\n\n"
        f"## Context & Completed State\n{summary or 'Pending summary.'}\n\n"
        f"## Relevant Files\n{file_bullets}\n\n"
        f"## Evidence / Quality Gate\n{evidence or 'Default status: NEEDS WORK until verified.'}\n"
    )


def delegate_to_agency(
    slug: str, prompt: str, model: str = "", max_tokens: int = 4096
) -> str:
    agent = load_agent(slug, compact=False)
    if not agent:
        raise ValueError(f"Unknown agency agent slug: {slug}")
    pillars_dir = resolve_assets_dir() / "rules" / "pillars"
    pillar_blocks: List[str] = []
    if pillars_dir.is_dir():
        for p in sorted(pillars_dir.glob("*.md")):
            pillar_blocks.append(p.read_text(encoding="utf-8", errors="replace").strip())
    sys_prompt = "\n\n---\n\n".join([agent["body"], *pillar_blocks])
    chosen_model = model or agent.get("model") or "sonnet"
    resp = bedrock_bridge.invoke_bedrock(
        {
            "model": chosen_model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
        },
        model_alias=chosen_model,
    )
    return (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")


def run_self_test() -> int:
    import tempfile

    repo = resolve_agency_repo()
    if repo is None:
        sys.stderr.write("agency_router self-test: FAILED (missing agency-agents clone)\n")
        return 1
    with tempfile.TemporaryDirectory() as td:
        cat_path = Path(td) / "agency_catalog.json"
        cat = build_catalog(repo_root=repo, out_path=cat_path)
        assert cat["agent_count"] >= 150, f"Expected >=150 agents, got {cat['agent_count']}"
        assert cat["division_count"] >= 15, f"Expected >=15 divisions, got {cat['division_count']}"
        hits = search_agents("reality checker", catalog=cat)
        assert hits and hits[0]["slug"] == "testing-reality-checker", f"Unexpected search hits: {hits[:2]}"
        loaded = load_agent("reality-checker", compact=True, catalog=cat)
        assert loaded and len(loaded.get("compact_prompt", "")) <= 1600
        rb = get_runbook("incident-response", catalog=cat)
        assert rb and rb["slug"] == "incident-response"
        hd = render_handoff("systems-architect", "testing-reality-checker", summary="Ready for QA", mode="standard")
        assert "NEXUS Handoff Document" in hd and "testing-reality-checker" in hd
    print(f"agency_router self-test: OK ({cat['agent_count']} agents, {cat['division_count']} divisions)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="kaizen-kit agency", description="Zero-bloat lazy router for agency-agents & NEXUS")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="Rebuild agency_catalog.json from local agency-agents repo")
    p_build.add_argument("--repo", default="", help="Optional path to agency-agents clone")

    p_search = sub.add_parser("search", help="Search agency agents by keyword or capability")
    p_search.add_argument("query")
    p_search.add_argument("--division", default="")
    p_search.add_argument("--limit", type=int, default=8)
    p_search.add_argument("--json", action="store_true")

    p_load = sub.add_parser("load", help="Load full or compact persona prompt for an agency agent")
    p_load.add_argument("slug")
    p_load.add_argument("--compact", action="store_true")
    p_load.add_argument("--json", action="store_true")

    p_insp = sub.add_parser("inspect", help="Inspect agent metadata and section headings")
    p_insp.add_argument("slug")

    p_rb = sub.add_parser("runbook", help="Show NEXUS scenario runbooks or inspect one by slug")
    p_rb.add_argument("slug", nargs="?", default="")

    p_ho = sub.add_parser("handoff", help="Generate a standardized NEXUS handoff document")
    p_ho.add_argument("--from-agent", required=True)
    p_ho.add_argument("--to-agent", required=True)
    p_ho.add_argument("--phase", default="Phase 3 - Build & Verify")
    p_ho.add_argument("--task-ref", default="TASK-1")
    p_ho.add_argument("--summary", default="")
    p_ho.add_argument("--files", nargs="*", default=[])
    p_ho.add_argument("--evidence", default="")
    p_ho.add_argument("--mode", choices=["standard", "qa-pass", "qa-fail", "escalation"], default="standard")

    p_del = sub.add_parser("delegate", help="Execute a prompt on Bedrock Claude using any agency agent persona")
    p_del.add_argument("slug")
    p_del.add_argument("prompt")
    p_del.add_argument("--model", default="")

    sub.add_parser("self-test", help="Run isolated catalog/router/NEXUS self-tests")

    args = ap.parse_args(argv)
    if args.cmd == "self-test":
        return run_self_test()
    if args.cmd == "build":
        repo = Path(args.repo).expanduser().resolve() if args.repo else None
        cat = build_catalog(repo_root=repo)
        print(json.dumps({"built": True, "path": str(resolve_catalog_path()), "agents": cat["agent_count"], "divisions": cat["division_count"]}, indent=2))
        return 0
    if args.cmd == "search":
        hits = search_agents(args.query, division=args.division, limit=args.limit)
        if args.json:
            print(json.dumps(hits, indent=2, ensure_ascii=False))
            return 0
        for h in hits:
            print(f"- {h['slug']:<46} [{h['division']:<18} | {h['model']:<6} | score={h['score']}] {h['description'][:100]}")
        return 0
    if args.cmd == "inspect":
        ag = load_agent(args.slug, compact=False)
        if not ag:
            sys.stderr.write(f"Agent not found: {args.slug}\n")
            return 1
        print(json.dumps({
            "slug": ag["slug"],
            "name": ag["name"],
            "division": ag["division"],
            "model": ag["model"],
            "vibe": ag["vibe"],
            "description": ag["description"],
            "source_path": ag["source_path"],
            "sections": list((ag.get("sections") or {}).keys()),
        }, indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "load":
        ag = load_agent(args.slug, compact=args.compact)
        if not ag:
            sys.stderr.write(f"Agent not found: {args.slug}\n")
            return 1
        if args.json:
            print(json.dumps(ag, indent=2, ensure_ascii=False))
        else:
            print(ag["compact_prompt"] if args.compact else ag["body"])
        return 0
    if args.cmd == "runbook":
        if not args.slug:
            for rb in list_runbooks():
                print(f"- {rb['slug']:<20} [{rb['mode']}] {rb['title']} — {rb['summary']}")
            return 0
        rb = get_runbook(args.slug)
        if not rb:
            sys.stderr.write(f"Runbook not found: {args.slug}\n")
            return 1
        print(json.dumps(rb, indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "handoff":
        print(render_handoff(
            from_agent=args.from_agent,
            to_agent=args.to_agent,
            phase=args.phase,
            task_ref=args.task_ref,
            summary=args.summary,
            files=args.files,
            evidence=args.evidence,
            mode=args.mode,
        ))
        return 0
    if args.cmd == "delegate":
        print(delegate_to_agency(args.slug, args.prompt, model=args.model))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
