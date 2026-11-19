#!/usr/bin/env python3
"""
Kaizen v4.0 Unified CLI (`kaizen-kit`).

Subcommands:
  - `kaizen-kit install --target {all,claude,cursor,antigravity,git-hooks}`
  - `kaizen-kit uninstall`
  - `kaizen-kit doctor [--self-test]`
  - `kaizen-kit check [--pre-flight] [path]`
  - `kaizen-kit arch-audit [path]`
  - `kaizen-kit ast-audit [path] [--bridge-pack|--hotspots|--diff-only]`
  - `kaizen-kit symbol-rag <query> [--workspace <dir>] [--repo <r>] [--json]`
  - `kaizen-kit docs {list,codex,fetch,symbol,library} ...`
  - `kaizen-kit hook --runtime {auto,claude,antigravity} <event>`
  - `kaizen-kit ledger ...`
  - `kaizen-kit parity ...`
  - `kaizen-kit story-guard ...`
  - `kaizen-kit skeleton <path>`
  - `kaizen-kit bridge {start,stop,status,serve}`
  - `kaizen-kit flow {ask,chat,repl,nexus,list-agents}`
  - `kaizen-kit agency {build,search,inspect,load,runbook,handoff,delegate,self-test}`
  - `kaizen-kit qlora {self-test,emit-scripts}`
  - `kaizen-kit mcp-render [--output <path>]`
  - `kaizen-kit mini-pool {up,down,tunnel,status,scale,ask,map-reduce,systemone}`
  - `kaizen-kit cloud-brain {sync,status,search,rag}`
  - `kaizen-kit adk {list,inspect,mcp-matrix,map-reduce,run,rag-ocr,voice-to-action}`
  - `kaizen-kit gpu-burst {tiers,plan,fleet,spot-check,self-test}`
  - `kaizen-kit carbon {catalog,route,jury,validate-fit,litellm-config,provision-plan,neon-plan,status,self-test}`
  - `kaizen-kit neon {neon-plan,night-shift,status,catalog,route,jury,validate-fit,litellm-config,provision-plan,self-test}`
  - `kaizen-kit night-shift [--tasks-file <json>] [--morning] [--execute] [--json]`
  - `kaizen-kit gemini {status,generate,decide,cache-plan,batch-plan,self-test}`
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

KIT_SRC_DIR = Path(__file__).resolve().parent.parent
if str(KIT_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(KIT_SRC_DIR))

from kaizen_kit import agency_router, bedrock_bridge, docs_reader, installer, sdk_orchestrator
from kaizen_kit.gates import (
    apidoc_sync,
    arch_audit,
    ast_skeleton,
    check_clean_code,
    hansei_ledger,
    oss_ast_gate,
    parity_gate,
    senior_review_lint,
    story_guard,
    symbol_rag_indexer,
    verify_provenance,
)
from kaizen_kit.hooks import (
    compact_state_guard,
    lossless_test_distiller,
    universal_hook_adapter,
)
from kaizen_kit.paths import resolve_assets_dir, resolve_kit_root, resolve_state_dir

EXPECTED_SPECIALIST_AGENTS = 15


def validate_kit_skills() -> tuple[int, List[str]]:
    skills_root = resolve_assets_dir() / "skills"
    errors: List[str] = []
    count = 0
    if not skills_root.exists():
        return 0, ["Missing assets/skills directory"]
    for sdir in sorted(skills_root.iterdir()):
        if not sdir.is_dir():
            continue
        sf = sdir / "SKILL.md"
        if not sf.exists():
            errors.append(f"{sdir.name}: missing SKILL.md")
            continue
        txt = sf.read_text(encoding="utf-8")
        if txt.startswith("\ufeff"):
            errors.append(f"{sdir.name}: UTF-8 BOM detected")
            continue
        m = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", txt, re.DOTALL)
        if not m:
            errors.append(f"{sdir.name}: invalid YAML frontmatter delimiters")
            continue
        fm = m.group(1)
        if "name:" not in fm or "description:" not in fm:
            errors.append(f"{sdir.name}: missing name or description in frontmatter")
            continue
        count += 1
    return count, errors


def run_doctor(self_test: bool = False) -> int:
    kit_root = resolve_kit_root()
    state_dir = resolve_state_dir()
    agents = sdk_orchestrator.load_all_agents()
    codices = list((resolve_assets_dir() / "references" / "codices").glob("*.md"))
    skill_count, skill_errors = validate_kit_skills()
    claude_settings = Path.home() / ".claude" / "settings.json"
    hooks_installed = False
    if claude_settings.exists():
        try:
            sdata = json.loads(claude_settings.read_text(encoding="utf-8"))
            hooks_installed = bool(sdata.get("hooks", {}).get("PreToolUse"))
        except Exception:
            pass

    print("=" * 72)
    print("KAIZEN KAIZEN v4.0 AI ENGINEERING KIT — DOCTOR")
    print("=" * 72)
    print(f"  Kit Root               : {kit_root}")
    print(f"  State & Ledger Dir     : {state_dir}")
    print(f"  Specialist Subagents   : {len(agents)} (expected {EXPECTED_SPECIALIST_AGENTS})")
    print(f"  Canonical Codices      : {len(codices)} (expected >= 7)")
    print(f"  Validated Skills       : {skill_count} (errors={len(skill_errors)})")
    print(f"  Claude Code Hooks      : {'ACTIVE' if hooks_installed else 'NOT INSTALLED (run `python install.py`)'}")
    from kaizen_kit import localmodels
    for k, v in localmodels.status().items():
        print(f"  LocalModel ({k:12s}): {v}")

    if skill_errors:
        for err in skill_errors:
            print(f"  [ERROR] {err}")
        return 1

    if self_test:
        print("-" * 72)
        print("Running Deterministic Gate & Codex Self-Tests...")
        rc = 0
        rc |= hansei_ledger.run_self_test()
        rc |= verify_provenance.run_self_test()
        rc |= check_clean_code.run_self_test()
        rc |= arch_audit.run_self_test()
        rc |= docs_reader.run_self_test()
        rc |= senior_review_lint.run_self_test()
        rc |= story_guard.run_self_test()
        rc |= parity_gate.run_self_test()
        rc |= apidoc_sync.run_self_test()
        rc |= oss_ast_gate.run_self_test()
        rc |= compact_state_guard.run_self_test()
        rc |= lossless_test_distiller.run_self_test()
        rc |= symbol_rag_indexer.run_self_test()
        rc |= agency_router.run_self_test()
        from kaizen_kit import mirix_memory
        rc |= mirix_memory.run_self_test()
        from kaizen_kit import qlora_pipeline as _qlora
        rc |= _qlora.run_self_test()
        from kaizen_kit import guardrails as _guardrails
        rc |= _guardrails.run_self_test()
        from kaizen_kit import gpu_burst_orchestrator as _gpu_burst
        rc |= _gpu_burst.run_self_test()
        from kaizen_kit import carbon_orchestrator as _carbon
        rc |= _carbon.run_self_test()
        from kaizen_kit import gemini_cloud_client as _gemini
        rc |= _gemini.run_self_test()
        from kaizen_kit import neon_night_shift as _night
        rc |= _night.run_self_test()
        print("-" * 72)
        print("ALL SELF-TESTS PASSED" if rc == 0 else "SELF-TEST FAILURE DETECTED")
        return rc

    return 0 if len(agents) == EXPECTED_SPECIALIST_AGENTS and len(codices) >= 7 and skill_count >= 35 else 1


def run_unified_check(target_path: str, pre_flight: bool = False) -> int:
    target = Path(target_path).resolve()
    rc = 0
    print(f"[kaizen-kit check] Target: {target}")

    # 1. Senior Review & Jenkins Fast-Forward Gate
    s_args = [str(target)] + (["--pre-flight"] if pre_flight else [])
    rc |= senior_review_lint.main(s_args)

    # 2. Clean Architecture DAG & Layer Isolation Gate
    has_code = (
        target.suffix in (".rs", ".java", ".ts", ".vue")
        or (target.is_dir() and any(target.rglob("*.rs")) or any(target.rglob("*.java")) or any(target.rglob("*.ts")))
    )
    if has_code:
        rc |= arch_audit.main([str(target)])

    # 3. Clean Code & Cognitive Complexity Gate (if Rust/Java present)
    if target.suffix in (".rs", ".java") or (target.is_dir() and (any(target.rglob("*.rs")) or any(target.rglob("*.java")))):
        rc |= check_clean_code.main([str(target)])

    # 4. Rust VectorCube / Provenance Gates (if Cargo.toml or .rs present)
    if (target / "Cargo.toml").exists() or target.suffix == ".rs":
        rc |= verify_provenance.main(["--workspace", str(target)])
        if (target / "Cargo.toml").exists():
            rc |= story_guard.main(["check", "--workspace", str(target)])

    # 5. Kaizen v3.3 Offline OSS AST, Dataflow, Clone & Behavioral Gate
    if has_code:
        oss_args = [str(target)] + (["--diff-only", "--hotspots"] if pre_flight else [])
        rc |= oss_ast_gate.main(oss_args)

    return 0 if rc == 0 else 1


def run_mcp_render(output_path: Optional[str] = None) -> int:
    tpl_path = resolve_assets_dir() / "mcp" / "mcp_templates.json"
    if not tpl_path.exists():
        sys.stderr.write("Missing assets/mcp/mcp_templates.json\n")
        return 1
    raw = tpl_path.read_text(encoding="utf-8")
    ws_root = os.environ.get("WORKSPACE_ROOT", str(Path.cwd().as_posix()))
    rendered = raw.replace("${WORKSPACE_ROOT}", ws_root)
    if output_path:
        out_p = Path(output_path).expanduser().resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(rendered, encoding="utf-8")
        print(f"Rendered sanitized MCP configuration to {out_p}")
    else:
        print(rendered)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if not argv_list:
        return run_doctor(self_test=False)

    top = argv_list[0]
    rest = argv_list[1:]

    if top == "hook":
        return universal_hook_adapter.main(rest)
    if top == "ledger":
        return hansei_ledger.main(rest)
    if top == "parity":
        return parity_gate.main(rest)
    if top == "story-guard":
        return story_guard.main(rest)
    if top == "provenance":
        return verify_provenance.main(rest)
    if top == "clean-code":
        return check_clean_code.main(rest)
    if top == "arch-audit":
        return arch_audit.main(rest)
    if top == "ast-audit":
        return oss_ast_gate.main(rest)
    if top == "symbol-rag":
        return symbol_rag_indexer.main(rest)
    if top == "docs":
        return docs_reader.main(rest)
    if top == "senior-lint":
        return senior_review_lint.main(rest)
    if top == "apidoc-sync":
        return apidoc_sync.main(rest)
    if top == "skeleton":
        return ast_skeleton.main(rest)
    if top == "bridge":
        return bedrock_bridge.main(rest)
    if top == "flow":
        return sdk_orchestrator.main(rest)
    if top == "agency":
        return agency_router.main(rest)
    if top == "radar":
        from kaizen_kit.radar import cli as radar_cli

        return radar_cli.main(rest)
    if top in ("qlora", "qlora-pipeline"):
        from kaizen_kit import qlora_pipeline

        return qlora_pipeline.main(rest)
    if top == "mini-pool":
        from kaizen_kit import mini_pool

        return mini_pool.main(rest)
    if top == "cloud-brain":
        from kaizen_kit import cloud_brain

        return cloud_brain.main(rest)
    if top == "adk":
        from kaizen_kit import adk_agents

        return adk_agents.main(rest)
    if top == "gpu-burst":
        from kaizen_kit import gpu_burst_orchestrator

        return gpu_burst_orchestrator.main(rest)
    if top == "carbon":
        from kaizen_kit import carbon_orchestrator

        return carbon_orchestrator.main(rest)
    if top == "neon":
        from kaizen_kit import carbon_orchestrator

        return carbon_orchestrator.main(rest)
    if top == "night-shift":
        from kaizen_kit import carbon_orchestrator

        return carbon_orchestrator.main(["night-shift"] + rest)
    if top == "gemini":
        from kaizen_kit import gemini_cloud_client

        return gemini_cloud_client.main(rest)

    ap = argparse.ArgumentParser(prog="kaizen-kit", description="Kaizen Kaizen v4.0 AI Engineering Kit CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_inst = sub.add_parser("install", help="Install agents, codices, skills, rules & hooks non-destructively")
    p_inst.add_argument("--target", choices=["all", "claude", "cursor", "antigravity", "git-hooks"], default="all")
    p_inst.add_argument("--home", default="", help="Optional custom HOME directory (for isolated testing)")
    p_inst.add_argument("--repo", default=".", help="Target git repo when --target git-hooks is used")

    p_uninst = sub.add_parser("uninstall", help="Remove kit hooks and markers cleanly while preserving auth")
    p_uninst.add_argument("--home", default="", help="Optional custom HOME directory")

    p_doc = sub.add_parser("doctor", help="Validate kit health and run deterministic gate self-tests")
    p_doc.add_argument("--self-test", action="store_true", help="Run all 13 gate & codex self-test suites")

    p_chk = sub.add_parser("check", help="Run unified pre-flight lint & architecture gates on a repo/file")
    p_chk.add_argument("path", nargs="?", default=".")
    p_chk.add_argument("--pre-flight", action="store_true", help="Include Jenkins git fast-forward check")

    p_mcp = sub.add_parser("mcp-render", help="Render zero-secret MCP server template")
    p_mcp.add_argument("--output", default="", help="Optional file path to write rendered JSON")
    p_auto = sub.add_parser("autosense", help="Effort AutoSense: System 1 calibrated reasoning effort determination")
    p_auto.add_argument("prompt", nargs="*", help="Task prompt, code snippet or diff to calibrate effort for")
    p_auto.add_argument("--json", action="store_true", help="Output result as JSON")
    sub.add_parser("mini-pool", help="Manage EC2 32-core parallel mini-AI pool & SSM loopback tunnels")
    sub.add_parser("cloud-brain", help="EC2 Cloud-Brain workspace sync, Zoekt code search, Code-RAG & Serena LSP")
    sub.add_parser("adk", help="Google ADK Kaizen domain workflows, 7-MCP matrix & LlmArmyMapReduce")
    sub.add_parser("gpu-burst", help="Unified AWS kaizen-u2 GPU Burst & 2..10-node Tier-2 Spot Fleet orchestrator")
    sub.add_parser("carbon", help="Carbon 6-Layer Open-Source LLM Army catalog, CascadeRouter & PoLL jury")
    sub.add_parser("neon", help="Neon Two-Phase Native Orchestrator (Planner, Offload Executor & PoLL Verifier)")
    sub.add_parser("night-shift", help="Neon Night-Shift overnight pipeline (AutoDream, Best-of-N, QLoRA & Gemini)")
    sub.add_parser("gemini", help="Fail-closed Gemini Cloud API (Decision-1, Flex, Batch, Context Caching) client")

    args = ap.parse_args(argv_list)
    home_override = Path(args.home).expanduser().resolve() if getattr(args, "home", "") else None

    if args.cmd == "install":
        results = []
        if args.target in ("all", "claude"):
            results.append(installer.install_claude_target(home_override))
        if args.target in ("all", "cursor"):
            results.append(installer.install_cursor_target(home_override))
        if args.target in ("all", "antigravity"):
            results.append(installer.install_antigravity_target(home_override))
        if args.target == "git-hooks":
            results.append(installer.install_git_prepush_hook(Path(args.repo).resolve()))
        print(json.dumps({"installed": results}, indent=2))
        return 0

    if args.cmd == "uninstall":
        res = installer.uninstall_claude_target(home_override)
        print(json.dumps(res, indent=2))
        return 0

    if args.cmd == "doctor":
        return run_doctor(self_test=args.self_test)

    if args.cmd == "check":
        return run_unified_check(args.path, pre_flight=args.pre_flight)

    if args.cmd == "autosense":
        from kaizen_kit import effort_autosense
        prompt_text = " ".join(args.prompt) if args.prompt else ""
        res = effort_autosense.autosense_effort(prompt_text)
        if args.json:
            print(json.dumps(res.to_dict(), indent=2))
        else:
            print(f"Effort Level    : {res.level} ({res.effort})")
            print(f"Thinking Budget : {res.thinking_budget} tokens")
            print(f"Confidence      : {res.confidence:.2f}")
            print(f"System 1 Model  : {res.system_1_model}")
            print(f"Rationale       : {res.rationale}")
        return 0

    if args.cmd == "mcp-render":
        return run_mcp_render(args.output or None)

    return 0


if __name__ == "__main__":
    sys.exit(main())
