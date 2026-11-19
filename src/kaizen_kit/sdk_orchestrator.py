#!/usr/bin/env python3
"""
Multi-Agent SDK & Direct AWS Bedrock Orchestrator (`sdk_orchestrator.py`).

Supports TWO execution modes with Company AWS Bedrock Claude (`sonnet`, `opus`, `haiku`)
and ZERO Gemini API keys:
  1. `kaizen-kit flow ask --agent <agent> --model <sonnet|opus|haiku> "<prompt>"`:
     Works out-of-the-box with ONLY `boto3` (via `bedrock_bridge.invoke_bedrock`) OR `claude -p` CLI fallback!
     Teammates do NOT even need `google-antigravity` installed to query any of the 15 specialists.
  2. `kaizen-kit flow chat|repl --agent <agent> --model <sonnet|opus|haiku>`:
     Uses `google.antigravity` (`LocalOpenAIAgentConfig`) pointed at `http://127.0.0.1:18889/v1`
     (`bedrock_bridge.py`) with all 15 subagents, 48 skills, and Storybook `Stop` hooks wired automatically.
"""

import argparse
import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_KIT_SRC = Path(__file__).resolve().parent.parent
if str(_KIT_SRC) not in sys.path:
    sys.path.insert(0, str(_KIT_SRC))

from kaizen_kit import agency_router, bedrock_bridge, model_router
from kaizen_kit.gates import story_guard
from kaizen_kit.paths import resolve_assets_dir

AGENT_DOMAIN_PACK_MAP: Dict[str, str] = {
    "e2e-systems-visionary": "rust-ondemand.md",
    "systems-architect": "rust-ondemand.md",
    "data-sql-architect": "rust-ondemand.md",
    "jvm-service-architect": "java-spring-loom.md",
    "incident-rca-falsifier": "uc-triage-cloudwatch.md",
    "release-hygiene-guardian": "mr-ci-deploy.md",
    "bedrock-architect": "rust-ondemand.md",
    "bedrock-judge": "uc-triage-cloudwatch.md",
    "reality-checker": "uc-triage-cloudwatch.md",
    "autonomous-optimization-architect": "rust-ondemand.md",
    "ai-security-auditor": "mr-ci-deploy.md",
    "docs-spec-analyst": "rust-ondemand.md",
    "refactoring-specialist": "rust-ondemand.md",
}


def parse_agent_markdown(md_path: Path) -> Dict[str, Any]:
    raw = md_path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n(.*)$", raw, re.DOTALL)
    if not m:
        return {"name": md_path.stem, "description": "", "model": "sonnet", "readonly": False, "body": raw.strip()}
    fm_text, body = m.group(1), m.group(2).strip()
    meta: Dict[str, Any] = {"name": md_path.stem, "description": "", "model": "sonnet", "readonly": False, "body": body}
    for line in fm_text.splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip().strip("\"'")
            if k in ("name", "description", "model"):
                meta[k] = v
            elif k == "readonly":
                meta["readonly"] = v.lower() == "true"
    return meta


def load_all_agents() -> Dict[str, Dict[str, Any]]:
    agents_dir = resolve_assets_dir() / "agents"
    out: Dict[str, Dict[str, Any]] = {}
    if not agents_dir.exists():
        return out
    for md in sorted(agents_dir.glob("*.md")):
        meta = parse_agent_markdown(md)
        out[meta["name"]] = meta
    return out


def compose_system_instructions(agent_name: str) -> str:
    assets = resolve_assets_dir()
    agents = load_all_agents()
    sections: List[str] = []

    if agent_name in agents and agents[agent_name]["body"]:
        sections.append(agents[agent_name]["body"])
    else:
        agency_entry = agency_router.load_agent(agent_name, compact=True)
        if agency_entry and agency_entry.get("compact_prompt"):
            sections.append(agency_entry["compact_prompt"])

    pillars_dir = assets / "rules" / "pillars"
    if pillars_dir.exists():
        for p in sorted(pillars_dir.glob("*.md")):
            sections.append(p.read_text(encoding="utf-8", errors="replace").strip())

    domain_name = AGENT_DOMAIN_PACK_MAP.get(agent_name)
    if domain_name:
        dp = assets / "rules" / "domains" / domain_name
        if dp.exists():
            sections.append(dp.read_text(encoding="utf-8", errors="replace").strip())

    return "\n\n---\n\n".join(sections)


def _run_claude_cli_fallback(prompt: str, sys_prompt: str) -> Optional[Tuple[int, str]]:
    if model_router.is_local_bedrock_disabled():
        return None
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return None
    proc = subprocess.run(
        [claude_bin, "-p", prompt, "--append-system-prompt", sys_prompt],
        text=True,
        capture_output=True,
    )
    if proc.returncode == 0:
        return 0, (proc.stdout or "").strip()
    err_text = proc.stderr if proc.stderr else proc.stdout
    return proc.returncode, (err_text or "").strip()


def _parse_json_schema_arg(raw_arg: str) -> Optional[Dict[str, Any]]:
    text = (raw_arg or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        text = Path(text[1:]).expanduser().read_text(encoding="utf-8")
    elif Path(text).is_file():
        text = Path(text).read_text(encoding="utf-8")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("--json-schema must decode to a JSON object.")
    return parsed


def _invoke_specialist_text(
    agent_name: str,
    model: str,
    prompt: str,
    max_tokens: int = 4096,
    *,
    json_schema: Optional[Dict[str, Any]] = None,
    effort: Optional[str] = None,
) -> Tuple[int, str]:
    """Invoke a canonical or lazy Agency specialist via ModelRouter (Bedrock 5.5 or EC2 OSS)."""
    sys_prompt = compose_system_instructions(agent_name)
    selected_model = model or "sonnet"
    router = model_router.get_default_router()
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": prompt},
    ]
    invoke_kwargs: Dict[str, Any] = {"messages": messages, "model": selected_model, "max_tokens": max_tokens}
    if json_schema is not None:
        resp_fmt = {
            "type": "json_schema",
            "json_schema": {"name": "flow_output", "schema": json_schema, "strict": True},
        }
        sig = inspect.signature(router.invoke)
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if accepts_var_kw or "response_format" in sig.parameters:
            invoke_kwargs["response_format"] = resp_fmt
        if accepts_var_kw or "json_schema" in sig.parameters:
            invoke_kwargs["json_schema"] = json_schema
    if effort:
        sig = inspect.signature(router.invoke)
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if accepts_var_kw or "effort" in sig.parameters:
            invoke_kwargs["effort"] = effort
    try:
        resp = router.invoke(**invoke_kwargs)
    except Exception as invoke_err:
        if not model_router.is_bedrock_model(selected_model):
            return 1, f"[kaizen-kit flow] ModelRouter invocation failed ({selected_model}): {invoke_err}"
        fallback = _run_claude_cli_fallback(prompt, sys_prompt)
        if fallback is not None:
            return fallback
        return 1, f"[kaizen-kit flow] ModelRouter invocation failed ({selected_model}): {invoke_err}"
    choices = resp.get("choices") or [{}]
    content = choices[0].get("message", {}).get("content", "")
    return 0, str(content or "")


def run_ask(
    agent_name: str,
    model: str,
    prompt: str,
    *,
    json_schema: Optional[Dict[str, Any]] = None,
    effort: Optional[str] = None,
) -> int:
    """
    Zero-SDK-Lock-In Specialist Query:
    Tries AWS Bedrock Converse directly (`boto3`); if `boto3` isn't configured, falls back cleanly to `claude -p`.
    """
    rc, output = _invoke_specialist_text(agent_name, model, prompt, json_schema=json_schema, effort=effort)
    if rc == 0:
        print(output)
        return 0
    sys.stderr.write(f"{output}\n")
    return rc


_QA_PASS_RE = re.compile(r"\b(?:VERDICT|STATUS|QA_GATE)\s*:\s*PASS\b", re.IGNORECASE)
_QA_FAIL_RE = re.compile(r"\bNEEDS WORK\b|\bFAIL\b", re.IGNORECASE)


def _is_qa_pass(text: str) -> bool:
    if _QA_PASS_RE.search(text or ""):
        return True
    first_line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return "PASS" in first_line.upper() and not _QA_FAIL_RE.search(first_line)


def run_nexus_pipeline(
    prompt: str,
    dev_agent: str = "systems-architect",
    qa_agent: str = "reality-checker",
    model: str = "sonnet",
    max_retries: int = 2,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    NEXUS Dev -> Handoff -> Reality-Checker QA Gate loop.
    Default QA stance is NEEDS WORK unless empirical evidence earns `VERDICT: PASS`.
    """
    handoff_preview = agency_router.render_handoff(
        from_agent=dev_agent,
        to_agent=qa_agent,
        phase="Phase 3 - Build & Verify",
        task_ref="NEXUS-1",
        summary=prompt,
        evidence="Pending Reality-Checker QA gate verification.",
        mode="standard",
    )
    if dry_run:
        return {
            "status": "DRY_RUN",
            "dev_agent": dev_agent,
            "qa_agent": qa_agent,
            "model": model,
            "max_retries": max_retries,
            "steps": ["1.Dev", "2.NEXUS-Handoff", "3.Reality-Checker-QA"],
            "handoff_template": handoff_preview,
        }

    feedback = ""
    dev_output = ""
    qa_output = ""
    attempts_limit = max(1, max_retries)
    for attempt in range(1, attempts_limit + 1):
        dev_prompt = (
            prompt
            if not feedback
            else f"{prompt}\n\n[NEXUS QA Rejection Feedback - Attempt {attempt}/{attempts_limit}]\n{feedback}"
        )
        rc_dev, dev_output = _invoke_specialist_text(dev_agent, model, dev_prompt)
        if rc_dev != 0:
            return {
                "status": "DEV_ERROR",
                "attempt": attempt,
                "dev_agent": dev_agent,
                "qa_agent": qa_agent,
                "dev_output": dev_output,
            }

        handoff_doc = agency_router.render_handoff(
            from_agent=dev_agent,
            to_agent=qa_agent,
            phase=f"Phase 3 - Attempt {attempt}",
            task_ref=f"NEXUS-{attempt}",
            summary=dev_output[:2400],
            evidence="Submitted for adversarial QA verification.",
            mode="standard",
        )
        qa_prompt = (
            "Default posture is NEEDS WORK. Reject unverified claims or missing tests.\n"
            "Reply with exact marker `VERDICT: PASS` only if verified, else `VERDICT: NEEDS WORK` with defects.\n\n"
            f"Original Task:\n{prompt}\n\n{handoff_doc}"
        )
        rc_qa, qa_output = _invoke_specialist_text(qa_agent, model, qa_prompt)
        if rc_qa != 0:
            return {
                "status": "QA_ERROR",
                "attempt": attempt,
                "dev_agent": dev_agent,
                "qa_agent": qa_agent,
                "dev_output": dev_output,
                "qa_output": qa_output,
            }
        if _is_qa_pass(qa_output):
            return {
                "status": "PASS",
                "attempts": attempt,
                "dev_agent": dev_agent,
                "qa_agent": qa_agent,
                "dev_output": dev_output,
                "qa_output": qa_output,
                "handoff": agency_router.render_handoff(
                    from_agent=qa_agent,
                    to_agent="coordinator",
                    task_ref=f"NEXUS-{attempt}",
                    summary=qa_output[:1500],
                    mode="qa-pass",
                ),
            }
        feedback = qa_output

    return {
        "status": "NEEDS_WORK_ESCALATED",
        "attempts": attempts_limit,
        "dev_agent": dev_agent,
        "qa_agent": qa_agent,
        "dev_output": dev_output,
        "qa_output": qa_output,
        "handoff": agency_router.render_handoff(
            from_agent=qa_agent,
            to_agent=dev_agent,
            task_ref=f"NEXUS-{attempts_limit}",
            summary=qa_output[:1500],
            mode="escalation",
        ),
    }


async def _run_claude_agent_sdk_session(
    agent_name: str, model: str, workspace: str, prompt: str, interactive: bool
) -> int:
    """
    Native Anthropic `claude-agent-sdk` execution engine:
    - Loads user/project/local settings (`~/.claude/settings.json` hooks + proxy env, `CLAUDE.md`)
    - Connects all configured MCP servers from `~/.claude.json` (`serena`, `kaizen-atlassian`,
      `kaizen-gitlab`, `internal-git-gitlab`, `kaizen-devops`, `awsknowledge`, `context7`)
    - Exposes an in-process `kaizen-kaizen` SDK MCP server (`symbol_rag`, `ast_audit`, `arch_audit`)
    - Registers all 15 specialist subagents (`AgentDefinition`) and all skills (`skills='all'`)
    """
    from claude_agent_sdk import (  # type: ignore
        AgentDefinition,
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
        create_sdk_mcp_server,
        query,
        tool,
    )
    from kaizen_kit.gates import arch_audit, oss_ast_gate, symbol_rag_indexer
    from kaizen_kit.installer import READONLY_DISALLOWED_TOOLS

    ws_abs = str(Path(workspace).resolve())

    # Load user-scoped MCP servers from ~/.claude.json
    mcp_servers: Dict[str, Any] = {}
    claude_json = Path.home() / ".claude.json"
    if claude_json.is_file():
        try:
            raw_mcps = (json.loads(claude_json.read_text(encoding="utf-8")) or {}).get("mcpServers") or {}
            for m_name, m_spec in raw_mcps.items():
                if isinstance(m_spec, dict):
                    mcp_servers[m_name] = m_spec
        except Exception:
            pass

    # Build in-process Kaizen v3.4 SDK MCP tools
    @tool(
        "kaizen_symbol_rag",
        "Zero-full-file 1-bit + FTS5 conceptual symbol search across C:/work-space",
        {"query": str, "repo": str},
    )
    async def kaizen_symbol_rag_tool(args: Dict[str, Any]) -> Dict[str, Any]:
        q = str(args.get("query") or "")
        repo_filter = str(args.get("repo") or "")
        hits = symbol_rag_indexer.search_concepts(q, repo=repo_filter or None, limit=12, workspace_root=Path(ws_abs))
        return {"content": [{"type": "text", "text": json.dumps(hits, ensure_ascii=False, indent=2)}]}

    @tool(
        "kaizen_ast_audit",
        "Run offline OSS AST, clone, dependency & 12m Git hotspot bridge pack on a repo/path",
        {"path": str},
    )
    async def kaizen_ast_audit_tool(args: Dict[str, Any]) -> Dict[str, Any]:
        target = Path(args.get("path") or ws_abs).resolve()
        _, sg_findings = oss_ast_gate.run_ast_grep_audit(target)
        _, og_findings = oss_ast_gate.run_opengrep_audit(target)
        behavioral = oss_ast_gate.compute_behavioral_hotspots(target)
        pack = {
            "target": str(target),
            "bridge_pack": oss_ast_gate.build_compact_bridge_pack(target, sg_findings, og_findings, behavioral),
            "hotspots_top5": (behavioral.get("hotspots") or [])[:5],
            "coupling_top5": (behavioral.get("temporal_coupling") or [])[:5],
        }
        return {"content": [{"type": "text", "text": json.dumps(pack, ensure_ascii=False, indent=2)}]}

    @tool(
        "kaizen_arch_audit",
        "Compute Clean Architecture component DAG, cycles, and Main Sequence metrics",
        {"path": str},
    )
    async def kaizen_arch_audit_tool(args: Dict[str, Any]) -> Dict[str, Any]:
        target = Path(args.get("path") or ws_abs).resolve()
        rep = arch_audit.analyze_architecture(target)
        return {"content": [{"type": "text", "text": json.dumps(rep, ensure_ascii=False, indent=2)}]}

    mcp_servers["kaizen-kaizen"] = create_sdk_mcp_server(
        name="kaizen-kaizen",
        version="3.4.0",
        tools=[kaizen_symbol_rag_tool, kaizen_ast_audit_tool, kaizen_arch_audit_tool],
    )

    agents_map = load_all_agents()
    sdk_agents: Dict[str, AgentDefinition] = {}
    for name, meta in agents_map.items():
        sdk_agents[name] = AgentDefinition(
            description=meta.get("description") or f"Specialist {name}",
            prompt=compose_system_instructions(name),
            disallowedTools=list(READONLY_DISALLOWED_TOOLS) if meta.get("readonly") else None,
            model=meta.get("model") or "sonnet",
            maxTurns=40,
        )

    cli_bin = shutil.which("claude.exe") or shutil.which("claude")
    opts = ClaudeAgentOptions(
        system_prompt=compose_system_instructions(agent_name or "e2e-systems-visionary"),
        mcp_servers=mcp_servers,
        agents=sdk_agents,
        skills="all",
        setting_sources=["user", "project", "local"],
        model=model or "sonnet",
        cwd=ws_abs,
        cli_path=cli_bin,
    )

    def _print_msg(msg: Any) -> None:
        if isinstance(msg, AssistantMessage):
            for blk in getattr(msg, "content", []) or []:
                if isinstance(blk, TextBlock) and blk.text:
                    sys.stdout.write(blk.text)
                    if not blk.text.endswith("\n"):
                        sys.stdout.write("\n")
                    sys.stdout.flush()
        elif isinstance(msg, ResultMessage) and getattr(msg, "is_error", False):
            sys.stderr.write(f"[claude-agent-sdk error] {getattr(msg, 'result', '')}\n")

    if interactive:
        async with ClaudeSDKClient(options=opts) as client:
            print(f"[kaizen-kit claude-sdk REPL] agent={agent_name} model={model} mcps={list(mcp_servers.keys())}")
            while True:
                try:
                    user_in = input("\nclaude-sdk> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not user_in or user_in.lower() in ("exit", "quit", "/quit"):
                    break
                await client.query(user_in)
                async for m in client.receive_response():
                    _print_msg(m)
        return 0

    async for m in query(prompt=prompt, options=opts):
        _print_msg(m)
    return 0


async def _run_sdk_session(
    agent_name: str, model: str, workspace: str, prompt: str, interactive: bool, engine: str = "auto"
) -> int:
    if engine in ("auto", "claude-sdk"):
        try:
            import claude_agent_sdk  # noqa: F401
            return await _run_claude_agent_sdk_session(agent_name, model, workspace, prompt, interactive)
        except ImportError:
            if engine == "claude-sdk":
                sys.stderr.write(
                    "[kaizen-kit flow] `claude-agent-sdk` is not installed. Run: `python -m pip install --user claude-agent-sdk`\n"
                )
                return 1

    try:
        from google.antigravity import Agent, LocalOpenAIAgentConfig, types  # type: ignore
        from google.antigravity.hooks import hooks, policy  # type: ignore
    except ImportError:
        if not interactive and prompt:
            return run_ask(agent_name, model, prompt)
        sys.stderr.write(
            "[kaizen-kit flow] Neither `claude-agent-sdk` nor `google-antigravity` SDK is installed. "
            "Install via `python -m pip install --user claude-agent-sdk`.\n"
        )
        return 1

    base_url = bedrock_bridge.ensure_bridge_running()
    agents_map = load_all_agents()
    subagents = []
    for name, meta in agents_map.items():
        enabled_tools = (
            list(types.BuiltinTools.read_only())
            if meta.get("readonly")
            else [
                types.BuiltinTools.VIEW_FILE,
                types.BuiltinTools.EDIT_FILE,
                types.BuiltinTools.CREATE_FILE,
                types.BuiltinTools.RUN_COMMAND,
                types.BuiltinTools.SEARCH_DIR,
                types.BuiltinTools.FIND_FILE,
                types.BuiltinTools.LIST_DIR,
            ]
        )
        subagents.append(
            types.SubagentConfig(
                name=name,
                description=meta.get("description") or f"Specialist {name}",
                system_instructions=compose_system_instructions(name),
                capabilities=types.SubagentCapabilities(
                    enabled_tools=enabled_tools,
                    agent_behavior=types.AgentBehavior.AUTONOMOUS,
                ),
            )
        )

    @hooks.stop
    async def verify_story_guard_on_stop(data: types.StopArgs) -> types.StopHookResult:
        if data.continuation_count >= 1:
            return types.StopHookResult(decision=types.StopDecision.ALLOW_STOP)
        ws_path = Path(workspace)
        if (ws_path / "Cargo.toml").exists():
            rep = story_guard.run_all_gates(ws_path)
            if not rep["passed"]:
                return types.StopHookResult(
                    decision=types.StopDecision.CONTINUE,
                    reason=f"Storybook Clean Architecture Guard failed: {rep['gates']}",
                )
        return types.StopHookResult(decision=types.StopDecision.ALLOW_STOP)

    skills_dir = str(resolve_assets_dir() / "skills")
    cfg = LocalOpenAIAgentConfig(
        model=model or "sonnet",
        base_url=base_url,
        system_instructions=compose_system_instructions(agent_name or "e2e-systems-visionary"),
        capabilities=types.CapabilitiesConfig(
            file_reads=True,
            file_writes=True,
            command_execution=True,
            enable_subagents=True,
            max_subagent_depth=3,
            agent_behavior=types.AgentBehavior.INTERACTIVE if interactive else types.AgentBehavior.AUTONOMOUS,
        ),
        subagents=subagents,
        skills_paths=[skills_dir],
        workspaces=[os.path.abspath(workspace)],
        hooks=[verify_story_guard_on_stop],
        policies=[policy.allow_all()],
    )

    async with Agent(config=cfg) as ag:
        if interactive:
            from google.antigravity.utils.interactive import run_interactive_loop  # type: ignore
            await run_interactive_loop(ag)
        else:
            stream = await ag.chat(prompt)
            async for tok in stream:
                sys.stdout.write(tok)
                sys.stdout.flush()
            print()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kaizen Multi-Agent Bedrock & SDK Orchestrator")
    sub = ap.add_subparsers(dest="mode", required=True)

    p_ask = sub.add_parser("ask", help="One-shot specialist query via ModelRouter (AWS Bedrock 5.5 or EC2 OSS)")
    p_ask.add_argument("prompt", help="Task or architectural question")
    p_ask.add_argument("--agent", default="e2e-systems-visionary", help="Specialist agent persona")
    p_ask.add_argument(
        "--model",
        default="sonnet",
        help=(
            "Model alias (sonnet, opus, haiku, fable, claude-*-5-5, or EC2 OSS MoE: "
            "qwen3.5-4b, qwen3.5-35b-a3b, qwen3-coder-30b-a3b, gemma-4-26b-a4b, ling-mini-2.0, mini-pool)"
        ),
    )
    p_ask.add_argument("--json-schema", default="", help="Optional inline JSON schema or @path/to/schema.json")
    p_ask.add_argument("--effort", default="", choices=["", "minimal", "low", "medium", "high"])
    p_ask.add_argument("--tier", default="", help="Optional specialist tier hint (compat with skill templates)")

    p_chat = sub.add_parser("chat", help="Autonomous tool & MCP session via Claude Agent SDK (or Antigravity SDK)")
    p_chat.add_argument("prompt", help="Task prompt")
    p_chat.add_argument("--agent", default="e2e-systems-visionary")
    p_chat.add_argument("--model", default="sonnet")
    p_chat.add_argument("--workspace", default=".")
    p_chat.add_argument("--engine", default="auto", choices=["auto", "claude-sdk", "antigravity"])

    p_repl = sub.add_parser("repl", help="Interactive multi-agent & MCP REPL via Claude Agent SDK (or Antigravity SDK)")
    p_repl.add_argument("--agent", default="e2e-systems-visionary")
    p_repl.add_argument("--model", default="sonnet")
    p_repl.add_argument("--workspace", default=".")
    p_repl.add_argument("--engine", default="auto", choices=["auto", "claude-sdk", "antigravity"])

    sub.add_parser("list-agents", help="List all 15 canonical specialist agents + 283 lazy agency agents")

    p_nexus = sub.add_parser(
        "nexus",
        help="Run NEXUS Dev -> Handoff -> Reality-Checker QA Gate loop",
    )
    p_nexus.add_argument("prompt", help="Task prompt for the NEXUS Dev -> QA loop")
    p_nexus.add_argument("--dev-agent", default="systems-architect", help="Implementation specialist agent")
    p_nexus.add_argument("--qa-agent", default="reality-checker", help="Adversarial QA gate agent")
    p_nexus.add_argument(
        "--model",
        default="sonnet",
        help="Model alias (sonnet, opus, haiku, fable, claude-*-5-5, or EC2 OSS pool)",
    )
    p_nexus.add_argument("--max-retries", type=int, default=2, help="Maximum Dev -> QA retry cycles")
    p_nexus.add_argument(
        "--dry-run",
        action="store_true",
        help="Output the planned NEXUS handoff & QA pipeline without calling Bedrock",
    )

    args = ap.parse_args(argv)
    if args.mode == "list-agents":
        for name, meta in load_all_agents().items():
            ro = "read-only" if meta.get("readonly") else "read-write"
            print(f"  - {name:<34} [{meta.get('model', 'sonnet'):<6} | {ro:<10}] {meta.get('description', '')}")
        cat = agency_router.load_catalog()
        print(f"\n  Lazy Agency Catalog: {len(cat.get('agents') or [])} agents across {len(cat.get('divisions') or {})} divisions (`kaizen-kit agency search <query>`)")
        return 0
    if args.mode == "nexus":
        result = run_nexus_pipeline(
            prompt=args.prompt,
            dev_agent=args.dev_agent,
            qa_agent=args.qa_agent,
            model=args.model,
            max_retries=args.max_retries,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("status") in ("DRY_RUN", "PASS") else 2
    if args.mode == "ask":
        schema = _parse_json_schema_arg(args.json_schema)
        return run_ask(
            args.agent,
            args.model,
            args.prompt,
            json_schema=schema,
            effort=args.effort or None,
        )
    if args.mode == "chat":
        return asyncio.run(
            _run_sdk_session(args.agent, args.model, args.workspace, args.prompt, interactive=False, engine=args.engine)
        )
    return asyncio.run(
        _run_sdk_session(args.agent, args.model, args.workspace, "", interactive=True, engine=args.engine)
    )


if __name__ == "__main__":
    sys.exit(main())
