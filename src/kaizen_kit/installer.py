#!/usr/bin/env python3
"""
Non-Destructive Plug-and-Play Multi-Harness Installer (`installer.py`).

Installs the Kaizen v4.0 AI Engineering Kit non-destructively across:
  - `claude`      — Claude Code CLI (`~/.claude/settings.json`, `CLAUDE.md`, `agents/`, `skills/`)
  - `codex`       — OpenAI Codex CLI (`~/.codex/AGENTS.md` + `~/.codex/config.toml` MCP entry)
  - `opencode`    — OpenCode CLI (`~/.config/opencode/AGENTS.md` + `opencode.json` MCP config)
  - `cursor`      — Cursor IDE (`~/.cursor/rules/kaizen-core.mdc` + glob `domain-*.mdc`)
  - `aider`       — Aider CLI (`~/.aider.conf.yml` + `~/.kaizen-kit/CONVENTIONS.md`)
  - `continue`    — Continue IDE (`~/.continue/mcpServers/kaizen-kit.json` + `rules/kaizen-core.md`)
  - `generic-mcp` — Universal MCP snippet (`~/.kaizen-kit/kaizen-mcp.json`)
  - `antigravity` — Antigravity IDE (`~/.gemini/config/plugins`, optional when present)
  - `git-hooks`   — Optional git `pre-push` pre-flight gate
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import tomllib  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

from kaizen_kit.paths import resolve_assets_dir, resolve_kit_root
from kaizen_kit.sdk_orchestrator import load_all_agents

KIT_HOOK_MARKER = "# [kaizen-kit]"
CLAUDE_MD_START = "<!-- KAIZEN_KIT_START -->"
CLAUDE_MD_END = "<!-- KAIZEN_KIT_END -->"
KAIZEN_MD_START = CLAUDE_MD_START
KAIZEN_MD_END = CLAUDE_MD_END

CODEX_TOML_START = "# BEGIN KAIZEN_KIT_MCP"
CODEX_TOML_END = "# END KAIZEN_KIT_MCP"
AIDER_YAML_START = "# BEGIN KAIZEN_KIT"
AIDER_YAML_END = "# END KAIZEN_KIT"

HARNESS_CHOICES: Tuple[str, ...] = (
    "auto",
    "all",
    "claude",
    "codex",
    "opencode",
    "cursor",
    "aider",
    "continue",
    "generic-mcp",
    "antigravity",
)

CURSOR_DOMAIN_GLOBS: Dict[str, tuple[str, str]] = {
    "java-spring-loom.md": ("**/*.java, **/pom.xml", "Java 21 Loom Virtual Threads, Spring Boot 3.4/4 & OpenAPI Parity"),
    "mr-ci-deploy.md": ("**/Jenkinsfile*, **/.gitlab-ci.yml, **/docker-compose*.yml", "CI/CD 4-Gate Pre-Flight & Compose Parity"),
}

CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact", "SubagentStop", "Stop")


def build_hook_command(event_name: str, runtime: str = "claude") -> str:
    """
    Build a cross-platform Python invocation referencing the kit's `src` path directly,
    requiring zero global PATH mutation or pipx installation to work immediately after `git clone`.
    """
    py_exe = Path(sys.executable).as_posix()
    kit_root = resolve_kit_root()
    src_dir = (kit_root / "src") if (kit_root / "src").is_dir() else kit_root.parent
    adapter_script = (src_dir / "kaizen_kit" / "hooks" / "universal_hook_adapter.py").as_posix()
    return f'"{py_exe}" -X utf8 "{adapter_script}" --runtime {runtime} {event_name}'


def _is_kit_hook_entry(entry: Dict[str, Any]) -> bool:
    for h in entry.get("hooks") or []:
        cmd = str(h.get("command") or "")
        if "universal_hook_adapter.py" in cmd or "kaizen-kit" in cmd or KIT_HOOK_MARKER in cmd:
            return True
    return False


def merge_claude_settings(settings_path: Path, remove: bool = False) -> Dict[str, Any]:
    """
    Surgically merge (or remove) `[kaizen-kit]` hooks in `settings.json`
    while leaving `env`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `model`, `theme`,
    and third-party hooks 100% untouched.
    """
    existing: Dict[str, Any] = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Refusing to overwrite malformed JSON at {settings_path}: {exc}") from exc

    hooks_obj: Dict[str, List[Dict[str, Any]]] = dict(existing.get("hooks") or {})

    for ev in CLAUDE_EVENTS:
        kept = [item for item in (hooks_obj.get(ev) or []) if isinstance(item, dict) and not _is_kit_hook_entry(item)]
        if not remove:
            entry: Dict[str, Any] = {
                "hooks": [
                    {
                        "type": "command",
                        "command": build_hook_command(ev, runtime="claude"),
                    }
                ]
            }
            if ev == "PreToolUse":
                entry["matcher"] = "Bash|PowerShell|Edit|MultiEdit|Write|Read|Task|Agent"
            elif ev == "PostToolUse":
                entry["matcher"] = "Bash|PowerShell|Edit|MultiEdit|Write|Read|Task|Agent|mcp__serena__.*"
            kept.append(entry)
        if kept:
            hooks_obj[ev] = kept
        else:
            hooks_obj.pop(ev, None)

    if hooks_obj:
        existing["hooks"] = hooks_obj
    else:
        existing.pop("hooks", None)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return existing


def sync_claude_mcp_servers(base_home: Path) -> List[str]:
    """
    Non-destructively sync MCP servers from `~/.gemini/config/mcp_config.json` (if present)
    and standard HTTP public documentation MCPs (`awsknowledge`, `context7`, local `serena`)
    into user-scoped `~/.claude.json` (`mcpServers`).
    """
    claude_json_path = base_home / ".claude.json"
    existing: Dict[str, Any] = {}
    if claude_json_path.exists():
        try:
            existing = json.loads(claude_json_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    mcp_servers: Dict[str, Any] = dict(existing.get("mcpServers") or {})

    # Baseline zero-secret HTTP MCPs
    defaults = {
        "serena": {"type": "http", "url": "http://127.0.0.1:24280/mcp"},
        "awsknowledge": {"type": "http", "url": "https://knowledge-mcp.global.api.aws"},
        "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
    }
    for k, v in defaults.items():
        if k not in mcp_servers:
            mcp_servers[k] = v

    # Bridge existing developer MCP definitions from Antigravity config if present on same machine
    agy_mcp_cfg = base_home / ".gemini" / "config" / "mcp_config.json"
    if agy_mcp_cfg.is_file():
        try:
            src_servers = (json.loads(agy_mcp_cfg.read_text(encoding="utf-8")) or {}).get("mcpServers") or {}
            for name, spec in src_servers.items():
                if not isinstance(spec, dict) or name == "graphify":
                    continue
                entry = dict(spec)
                if entry.get("url") and not entry.get("type"):
                    entry["type"] = "http"
                elif entry.get("command") and not entry.get("type"):
                    entry["type"] = "stdio"
                mcp_servers[name] = entry
        except Exception:
            pass

    existing["mcpServers"] = mcp_servers
    claude_json_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return sorted(mcp_servers.keys())


READONLY_DISALLOWED_TOOLS = [
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "mcp__serena__replace_symbol_body",
    "mcp__serena__insert_after_symbol",
    "mcp__serena__insert_before_symbol",
    "mcp__serena__rename_symbol",
    "mcp__kaizen-atlassian__jira_create_issue",
    "mcp__kaizen-atlassian__jira_batch_create_issues",
    "mcp__kaizen-atlassian__jira_update_issue",
    "mcp__kaizen-atlassian__jira_assign_issue",
    "mcp__kaizen-atlassian__jira_delete_issue",
    "mcp__kaizen-atlassian__jira_move_issue",
    "mcp__kaizen-atlassian__confluence_create_page",
    "mcp__kaizen-atlassian__confluence_update_page",
    "mcp__kaizen-atlassian__confluence_update_page_section",
    "mcp__kaizen-atlassian__confluence_delete_page",
    "mcp__kaizen-atlassian__confluence_move_page",
    "mcp__kaizen-atlassian__confluence_set_page_restrictions",
    "mcp__kaizen-atlassian__confluence_copy_page",
    "mcp__kaizen-gitlab__create_issue",
    "mcp__kaizen-gitlab__create_merge_request",
    "mcp__kaizen-gitlab__update_merge_request",
    "mcp__kaizen-gitlab__create_merge_request_thread",
    "mcp__kaizen-gitlab__resolve_merge_request_thread",
    "mcp__kaizen-gitlab__create_issue_note",
    "mcp__kaizen-gitlab__update_issue",
    "mcp__kaizen-gitlab__update_issue_description_patch",
    "mcp__internal-git-gitlab__create_issue",
    "mcp__internal-git-gitlab__create_merge_request",
    "mcp__internal-git-gitlab__update_merge_request",
    "mcp__kaizen-devops__gerrit_review",
    "mcp__kaizen-devops__jenkins_trigger_build",
]


def _collect_claude_skill_sources(assets: Path, base_home: Path) -> List[Path]:
    sources: List[Path] = []
    agy_plugins = base_home / ".gemini" / "config" / "plugins"
    if agy_plugins.is_dir():
        sources.extend(
            p for p in sorted(agy_plugins.glob("*/skills/*")) if p.is_dir() and (p / "SKILL.md").exists()
        )
    skills_src = assets / "skills"
    if skills_src.exists():
        sources.extend(p for p in sorted(skills_src.iterdir()) if p.is_dir() and (p / "SKILL.md").exists())
    return sources


def _remove_path(target: Path) -> None:
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)


def _sync_claude_skills(assets: Path, base_home: Path, skills_dst: Path) -> List[str]:
    skills_dst.mkdir(parents=True, exist_ok=True)
    _remove_path(skills_dst / "wsl-env-memory-watchdog")

    installed: set[str] = set()
    for sdir in _collect_claude_skill_sources(assets, base_home):
        target_sdir = skills_dst / sdir.name
        _remove_path(target_sdir)
        shutil.copytree(sdir, target_sdir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        installed.add(sdir.name)
    return sorted(installed)


def install_claude_target(home_dir: Optional[Path] = None) -> Dict[str, Any]:
    base_home = home_dir or Path.home()
    claude_dir = base_home / ".claude"
    assets = resolve_assets_dir()

    # 1. Non-destructive settings.json hook merge + user-scope MCP sync (~/.claude.json)
    settings_path = claude_dir / "settings.json"
    merge_claude_settings(settings_path, remove=False)
    synced_mcps = sync_claude_mcp_servers(base_home)

    # 2. Compile 12 specialist agents into ~/.claude/agents/*.md
    #    IMPORTANT: Omit restrictive `tools:` allowlist so every subagent inherits ALL
    #    configured MCP tools (`serena`, `kaizen-atlassian`, `kaizen-gitlab`, `kaizen-devops`,
    #    `awsknowledge`, `context7`). Enforce read-only safety via `disallowedTools`.
    agents_dst = claude_dir / "agents"
    agents_dst.mkdir(parents=True, exist_ok=True)
    agents_map = load_all_agents()
    installed_agents: List[str] = []
    for name, meta in sorted(agents_map.items()):
        desc = meta.get("description", f"Kaizen specialist agent {name}").replace("\n", " ").strip()
        model = meta.get("model", "sonnet")
        fm_lines = [
            "---",
            f"name: {name}",
            f"description: {desc}",
            f"model: {model}",
        ]
        if meta.get("readonly"):
            fm_lines.append("disallowedTools:")
            for dt in READONLY_DISALLOWED_TOOLS:
                fm_lines.append(f"  - {dt}")
        fm_lines.append("---")
        content = "\n".join(fm_lines) + f"\n\n{meta['body']}\n"
        (agents_dst / f"{name}.md").write_text(content, encoding="utf-8")
        installed_agents.append(name)

    # 3. Copy local Antigravity plugin skills first, then curated kit skills (authoritative)
    installed_skills = _sync_claude_skills(assets, base_home, claude_dir / "skills")

    # 4. Merge 4-pillar rules into ~/.claude/CLAUDE.md
    pillars_dir = assets / "rules" / "pillars"
    pillar_texts = [p.read_text(encoding="utf-8").strip() for p in sorted(pillars_dir.glob("*.md"))] if pillars_dir.exists() else []
    merged_block = (
        f"{CLAUDE_MD_START}\n\n"
        + "\n\n---\n\n".join(pillar_texts)
        + f"\n\n{CLAUDE_MD_END}"
    )
    claude_md = claude_dir / "CLAUDE.md"
    existing_md = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    if CLAUDE_MD_START in existing_md and CLAUDE_MD_END in existing_md:
        rx = re.compile(re.escape(CLAUDE_MD_START) + r".*?" + re.escape(CLAUDE_MD_END), re.DOTALL)
        updated_md = rx.sub(lambda _: merged_block, existing_md)
    elif existing_md.strip():
        updated_md = existing_md.rstrip() + "\n\n---\n\n" + merged_block + "\n"
    else:
        updated_md = merged_block + "\n"
    claude_md.write_text(updated_md, encoding="utf-8")

    # 5. Copy domain packs and 7 Canonical Engineering Codices for on-demand reference
    domains_src = assets / "rules" / "domains"
    domains_dst = claude_dir / "rules" / "domains"
    if domains_src.exists():
        domains_dst.mkdir(parents=True, exist_ok=True)
        for dp in sorted(domains_src.glob("*.md")):
            shutil.copy2(dp, domains_dst / dp.name)

    codices_src = assets / "references" / "codices"
    codices_count = 0
    if codices_src.exists():
        claude_codices = claude_dir / "references" / "codices"
        state_codices = (base_home / ".kaizen-kit" / "codices")
        claude_codices.mkdir(parents=True, exist_ok=True)
        state_codices.mkdir(parents=True, exist_ok=True)
        for cf in sorted(codices_src.glob("*.md")):
            shutil.copy2(cf, claude_codices / cf.name)
            shutil.copy2(cf, state_codices / cf.name)
            codices_count += 1

    # 6. Install zero-setuptools `kaizen-kit` CLI launcher shims into user bin directories
    shims_written = _install_cli_shims(base_home)

    return {
        "target": "claude",
        "settings_path": str(settings_path),
        "mcp_servers_synced": synced_mcps,
        "agents_installed": len(installed_agents),
        "skills_installed": len(installed_skills),
        "codices_installed": codices_count,
        "cli_shims": shims_written,
        "claude_md": str(claude_md),
    }


def _resolve_shim_python(kit_root: Path) -> Path:
    candidates = (
        kit_root / ".venv-radar" / "Scripts" / "python.exe",
        kit_root / ".venv-radar" / "bin" / "python",
    )
    for cand in candidates:
        if cand.is_file():
            return cand
    return Path(sys.executable)


def _install_cli_shims(base_home: Path) -> List[str]:
    """Write zero-dependency `kaizen-kit` launcher shims so `pip`/`setuptools` is never required."""
    import site

    kit_root = resolve_kit_root()
    py_exe = _resolve_shim_python(kit_root)
    src_dir = (kit_root / "src") if (kit_root / "src").is_dir() else kit_root.parent
    cli_script = src_dir / "kaizen_kit" / "cli.py"

    sh_body = (
        "#!/usr/bin/env sh\n"
        f'exec "{py_exe.as_posix()}" "{cli_script.as_posix()}" "$@"\n'
    )
    cmd_body = (
        "@echo off\r\n"
        f'"{py_exe}" "{cli_script}" %*\r\n'
    )

    bin_dirs: List[Path] = [base_home / ".local" / "bin"]
    if home_dir_is_real := (base_home.resolve() == Path.home().resolve()):
        try:
            user_base = Path(site.getuserbase())
            win_scripts = user_base / f"Python{sys.version_info.major}{sys.version_info.minor}" / "Scripts"
            unix_bin = user_base / "bin"
            bin_dirs.append(win_scripts if os.name == "nt" else unix_bin)
        except Exception:
            pass
        # Also drop into ~/.claude/bin if on PATH
        bin_dirs.append(base_home / ".kaizen-kit" / "bin")

    written: List[str] = []
    for bdir in bin_dirs:
        try:
            bdir.mkdir(parents=True, exist_ok=True)
            sh_path = bdir / "kaizen-kit"
            sh_path.write_text(sh_body, encoding="utf-8")
            try:
                os.chmod(sh_path, 0o755)
            except OSError:
                pass
            written.append(str(sh_path))
            if os.name == "nt":
                cmd_path = bdir / "kaizen-kit.cmd"
                cmd_path.write_text(cmd_body, encoding="utf-8")
                written.append(str(cmd_path))
        except OSError:
            continue
    return written


def uninstall_claude_target(home_dir: Optional[Path] = None) -> Dict[str, Any]:
    base_home = home_dir or Path.home()
    claude_dir = base_home / ".claude"
    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        merge_claude_settings(settings_path, remove=True)

    claude_md = claude_dir / "CLAUDE.md"
    if claude_md.exists():
        txt = claude_md.read_text(encoding="utf-8")
        rx = re.compile(r"(?:\n---\n\n)?" + re.escape(CLAUDE_MD_START) + r".*?" + re.escape(CLAUDE_MD_END) + r"\n?", re.DOTALL)
        claude_md.write_text(rx.sub("", txt), encoding="utf-8")

    removed_agents = 0
    for name in load_all_agents():
        af = claude_dir / "agents" / f"{name}.md"
        if af.exists():
            af.unlink()
            removed_agents += 1

    return {"target": "claude", "uninstalled": True, "removed_agents": removed_agents}


def install_cursor_target(home_dir: Optional[Path] = None) -> Dict[str, Any]:
    base_home = home_dir or Path.home()
    cursor_rules = base_home / ".cursor" / "rules"
    cursor_rules.mkdir(parents=True, exist_ok=True)
    assets = resolve_assets_dir()

    pillars_dir = assets / "rules" / "pillars"
    pillar_texts = [p.read_text(encoding="utf-8").strip() for p in sorted(pillars_dir.glob("*.md"))] if pillars_dir.exists() else []
    core_mdc = (
        "---\n"
        "description: Kaizen Kaizen v4.0 Closed-Loop Architecture, AWS Tenancy & Senior Review Standards\n"
        "alwaysApply: true\n"
        "---\n\n"
        + "\n\n---\n\n".join(pillar_texts)
        + "\n"
    )
    (cursor_rules / "kaizen-core.mdc").write_text(core_mdc, encoding="utf-8")

    domain_count = 0
    domains_src = assets / "rules" / "domains"
    if domains_src.exists():
        for fname, (globs_str, desc_str) in CURSOR_DOMAIN_GLOBS.items():
            dp = domains_src / fname
            if dp.exists():
                mdc = (
                    f"---\n"
                    f"description: {desc_str}\n"
                    f"globs: {globs_str}\n"
                    f"alwaysApply: false\n"
                    f"---\n\n"
                    f"{dp.read_text(encoding='utf-8').strip()}\n"
                )
                (cursor_rules / f"domain-{dp.stem}.mdc").write_text(mdc, encoding="utf-8")
                domain_count += 1

    return {"target": "cursor", "rules_dir": str(cursor_rules), "domain_rules": domain_count}


def install_git_prepush_hook(repo_dir: Path) -> Dict[str, Any]:
    git_hooks_dir = repo_dir / ".git" / "hooks"
    if not git_hooks_dir.is_dir():
        raise RuntimeError(f"Not a git repository (missing .git/hooks): {repo_dir}")
    py_exe = Path(sys.executable).as_posix()
    kit_root = resolve_kit_root()
    src_dir = (kit_root / "src") if (kit_root / "src").is_dir() else kit_root.parent
    cli_script = (src_dir / "kaizen_kit" / "cli.py").as_posix()

    hook_body = (
        "#!/usr/bin/env sh\n"
        f"{KIT_HOOK_MARKER}\n"
        f'"{py_exe}" "{cli_script}" check --pre-flight .\n'
    )
    hook_path = git_hooks_dir / "pre-push"
    hook_path.write_text(hook_body, encoding="utf-8")
    try:
        os.chmod(hook_path, 0o755)
    except OSError:
        pass
    return {"target": "git-hooks", "hook_path": str(hook_path)}


def _sync_antigravity_skills_and_domains(assets: Path, agy_cfg: Path, plugins_dir: Path) -> int:
    synced = 0
    domains_src = assets / "rules" / "domains"
    domains_dst = plugins_dir / "agent-orchestrator" / "rules" / "domains"
    if domains_src.is_dir():
        domains_dst.mkdir(parents=True, exist_ok=True)
        for dp in sorted(domains_src.glob("*.md")):
            shutil.copy2(dp, domains_dst / dp.name)
            synced += 1

    watchdog_src = assets / "skills" / "ec2-cloud-brain-watchdog"
    watchdog_dst = agy_cfg / "skills" / "ec2-cloud-brain-watchdog"
    if watchdog_src.is_dir():
        if watchdog_dst.exists():
            shutil.rmtree(watchdog_dst)
        shutil.copytree(watchdog_src, watchdog_dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        synced += 1

    legacy_wsl = agy_cfg / "skills" / "wsl-env-memory-watchdog"
    if legacy_wsl.is_dir():
        shutil.rmtree(legacy_wsl)

    bridge_skill_src = assets / "skills" / "kaizen-bedrock-bridge" / "SKILL.md"
    bridge_skill_dst = plugins_dir / "kaizen-bedrock-bridge" / "skills" / "kaizen-bedrock-bridge" / "SKILL.md"
    if bridge_skill_src.is_file() and bridge_skill_dst.parent.is_dir():
        shutil.copy2(bridge_skill_src, bridge_skill_dst)
        synced += 1

    kaizen_skill_src = assets / "skills" / "kaizen-optimizer" / "SKILL.md"
    kaizen_skill_dst = plugins_dir / "kaizen-optimizer" / "skills" / "kaizen-optimizer" / "SKILL.md"
    if kaizen_skill_src.is_file() and kaizen_skill_dst.parent.is_dir():
        shutil.copy2(kaizen_skill_src, kaizen_skill_dst)
        synced += 1

    return synced


def _sync_antigravity_codices(assets: Path, agy_cfg: Path, plugins_dir: Path) -> int:
    codices_src = assets / "references" / "codices"
    if not codices_src.is_dir():
        return 0
    codices_dst = agy_cfg / "knowledge" / "codices"
    rg_refs_dst = plugins_dir / "refactor-guru" / "skills" / "refactor-guru" / "references"
    codices_dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for cf in sorted(codices_src.glob("*.md")):
        shutil.copy2(cf, codices_dst / cf.name)
        count += 1
        if rg_refs_dst.is_dir() and cf.name.startswith(("01-", "02-", "03-", "04-")):
            shutil.copy2(cf, rg_refs_dst / cf.name)
    return count


def _antigravity_agent_path(plugins_dir: Path, name: str) -> Path:
    if name.startswith("bedrock-"):
        return plugins_dir / "kaizen-bedrock-bridge" / "agents" / f"{name}.md"
    if name == "refactoring-specialist":
        return plugins_dir / "refactor-guru" / "agents" / "refactoring-specialist.md"
    return plugins_dir / "agent-orchestrator" / "agents" / name / "agent.md"


def _sync_antigravity_agents(plugins_dir: Path) -> int:
    agents_map = load_all_agents()
    count = 0
    for name, meta in sorted(agents_map.items()):
        agy_tools = (
            ["read_file", "run_command", "search_web", "call_mcp_tool"]
            if meta.get("readonly")
            else ["read_file", "write_file", "edit_file", "run_command", "manage_task", "search_web", "call_mcp_tool"]
        )
        tools_yaml = "\n".join(f"  - {t}" for t in agy_tools)
        desc = meta.get("description", f"Kaizen specialist agent {name}").replace("\n", " ").strip()
        agy_doc = f"---\nname: {name}\ndescription: {desc}\ntools:\n{tools_yaml}\n---\n\n{meta['body']}\n"
        dst = _antigravity_agent_path(plugins_dir, name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(agy_doc, encoding="utf-8")
        count += 1
    return count


def install_antigravity_target(home_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Optional 1:1 synchronizer for developers who ALSO have Antigravity IDE (`~/.gemini/config/plugins`).
    No-op if `~/.gemini/config/plugins` does not exist on the machine (teammates without Antigravity).
    """
    base_home = home_dir or Path.home()
    agy_cfg = base_home / ".gemini" / "config"
    plugins_dir = agy_cfg / "plugins"
    if not plugins_dir.is_dir():
        return {"target": "antigravity", "skipped": True, "reason": "No ~/.gemini/config/plugins directory"}

    assets = resolve_assets_dir()
    codices_count = _sync_antigravity_codices(assets, agy_cfg, plugins_dir)
    agents_count = _sync_antigravity_agents(plugins_dir)
    skills_domains_synced = _sync_antigravity_skills_and_domains(assets, agy_cfg, plugins_dir)

    return {
        "target": "antigravity",
        "agents_synced": agents_count,
        "codices_synced": codices_count,
        "skills_domains_synced": skills_domains_synced,
    }


def _render_pillars_markdown(assets: Optional[Path] = None) -> str:
    resolved_assets = assets or resolve_assets_dir()
    pillars_dir = resolved_assets / "rules" / "pillars"
    if not pillars_dir.is_dir():
        return "# Kaizen Engineering Kit Rules"
    texts = [p.read_text(encoding="utf-8").strip() for p in sorted(pillars_dir.glob("*.md"))]
    return "\n\n---\n\n".join(t for t in texts if t)


def merge_marked_markdown(md_path: Path, body: str, *, remove: bool = False) -> str:
    """Idempotently merge or remove `<!-- KAIZEN_KIT_START --> ... <!-- KAIZEN_KIT_END -->` in markdown."""
    existing = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    if remove:
        if CLAUDE_MD_START not in existing:
            return existing
        rx_rm = re.compile(
            r"(?:\r?\n---\r?\n\r?\n)?" + re.escape(CLAUDE_MD_START) + r".*?" + re.escape(CLAUDE_MD_END) + r"\r?\n?",
            re.DOTALL,
        )
        cleaned = rx_rm.sub("", existing)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(cleaned, encoding="utf-8")
        return cleaned

    block = f"{CLAUDE_MD_START}\n\n{body.strip()}\n\n{CLAUDE_MD_END}"
    if CLAUDE_MD_START in existing and CLAUDE_MD_END in existing:
        rx = re.compile(re.escape(CLAUDE_MD_START) + r".*?" + re.escape(CLAUDE_MD_END), re.DOTALL)
        updated = rx.sub(lambda _: block, existing)
    elif existing.strip():
        updated = existing.rstrip() + "\n\n---\n\n" + block + "\n"
    else:
        updated = block + "\n"

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(updated, encoding="utf-8")
    return updated


def detect_installed_harnesses(
    home_dir: Optional[Path] = None,
    *,
    check_path: Optional[bool] = None,
) -> List[str]:
    """Detect installed coding harnesses under `home_dir` (and `PATH` when running on real user home)."""
    base_home = home_dir or Path.home()
    use_path = (home_dir is None) if check_path is None else bool(check_path)
    detected: List[str] = []

    if (base_home / ".claude").exists() or (base_home / ".claude.json").exists() or (use_path and shutil.which("claude")):
        detected.append("claude")
    if (base_home / ".codex").exists() or (use_path and shutil.which("codex")):
        detected.append("codex")
    if (
        (base_home / ".config" / "opencode").exists()
        or (base_home / ".opencode").exists()
        or (use_path and shutil.which("opencode"))
    ):
        detected.append("opencode")
    if (base_home / ".cursor").exists() or (use_path and shutil.which("cursor")):
        detected.append("cursor")
    if (base_home / ".aider.conf.yml").exists() or (base_home / ".aider").exists() or (use_path and shutil.which("aider")):
        detected.append("aider")
    if (base_home / ".continue").exists():
        detected.append("continue")
    if (base_home / ".gemini" / "config" / "plugins").is_dir():
        detected.append("antigravity")

    return detected


def merge_codex_config_toml(config_path: Path, *, remove: bool = False) -> str:
    """Non-destructively merge or remove `[mcp_servers.kaizen-kit]` (`kaizen-kit mcp`) in `~/.codex/config.toml`."""
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if existing.strip() and tomllib is not None:
        try:
            tomllib.loads(existing)
        except Exception as exc:
            raise RuntimeError(f"Refusing to overwrite malformed TOML at {config_path}: {exc}") from exc

    rx_block = re.compile(
        r"\r?\n?" + re.escape(CODEX_TOML_START) + r".*?" + re.escape(CODEX_TOML_END) + r"\r?\n?",
        re.DOTALL,
    )
    if remove:
        updated = rx_block.sub("\n", existing).strip()
        final_txt = (updated + "\n") if updated else ""
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(final_txt, encoding="utf-8")
        return final_txt

    mcp_block = (
        f"{CODEX_TOML_START}\n"
        '[mcp_servers.kaizen-kit]\n'
        'command = "kaizen-kit"\n'
        'args = ["mcp"]\n'
        f"{CODEX_TOML_END}"
    )
    if CODEX_TOML_START in existing and CODEX_TOML_END in existing:
        updated = rx_block.sub(f"\n{mcp_block}\n", existing).lstrip("\n")
    elif re.search(r"^\s*\[mcp_servers\.(?:\"kaizen-kit\"|'kaizen-kit'|kaizen-kit)\]", existing, re.MULTILINE):
        updated = existing if existing.endswith("\n") else (existing + "\n")
    elif existing.strip():
        updated = existing.rstrip() + "\n\n" + mcp_block + "\n"
    else:
        updated = mcp_block + "\n"

    if tomllib is not None:
        tomllib.loads(updated)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(updated, encoding="utf-8")
    return updated


def install_codex_target(home_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Install Kaizen rules & MCP server non-destructively into OpenAI Codex CLI (`~/.codex/`)."""
    base_home = home_dir or Path.home()
    codex_dir = base_home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)

    agents_md = codex_dir / "AGENTS.md"
    merge_marked_markdown(agents_md, _render_pillars_markdown(), remove=False)

    config_toml = codex_dir / "config.toml"
    merge_codex_config_toml(config_toml, remove=False)
    shims = _install_cli_shims(base_home)

    return {
        "target": "codex",
        "harness": "codex",
        "agents_md": str(agents_md),
        "config_toml": str(config_toml),
        "mcp_server": "kaizen-kit",
        "cli_shims": shims,
    }


def uninstall_codex_target(home_dir: Optional[Path] = None) -> Dict[str, Any]:
    base_home = home_dir or Path.home()
    codex_dir = base_home / ".codex"
    if (codex_dir / "AGENTS.md").exists():
        merge_marked_markdown(codex_dir / "AGENTS.md", "", remove=True)
    if (codex_dir / "config.toml").exists():
        merge_codex_config_toml(codex_dir / "config.toml", remove=True)
    return {"target": "codex", "uninstalled": True}


def merge_opencode_config(config_path: Path, *, remove: bool = False) -> Dict[str, Any]:
    """Non-destructively merge or remove `kaizen-kit` MCP entry in `~/.config/opencode/opencode.json`."""
    existing: Dict[str, Any] = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Refusing to overwrite malformed JSON at {config_path}: {exc}") from exc

    mcp_obj = dict(existing.get("mcp") or {})
    mcp_servers_obj = dict(existing.get("mcpServers") or {})

    if remove:
        mcp_obj.pop("kaizen-kit", None)
        mcp_servers_obj.pop("kaizen-kit", None)
    else:
        mcp_obj["kaizen-kit"] = {
            "type": "local",
            "command": ["kaizen-kit", "mcp"],
            "enabled": True,
        }
        mcp_servers_obj["kaizen-kit"] = {
            "command": "kaizen-kit",
            "args": ["mcp"],
        }

    if mcp_obj:
        existing["mcp"] = mcp_obj
    else:
        existing.pop("mcp", None)
    if mcp_servers_obj:
        existing["mcpServers"] = mcp_servers_obj
    else:
        existing.pop("mcpServers", None)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return existing


def install_opencode_target(home_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Install Kaizen rules & MCP config non-destructively into OpenCode (`~/.config/opencode/`)."""
    base_home = home_dir or Path.home()
    opencode_dir = base_home / ".config" / "opencode"
    opencode_dir.mkdir(parents=True, exist_ok=True)

    agents_md = opencode_dir / "AGENTS.md"
    merge_marked_markdown(agents_md, _render_pillars_markdown(), remove=False)

    config_json = opencode_dir / "opencode.json"
    merge_opencode_config(config_json, remove=False)
    shims = _install_cli_shims(base_home)

    return {
        "target": "opencode",
        "harness": "opencode",
        "agents_md": str(agents_md),
        "config_json": str(config_json),
        "mcp_server": "kaizen-kit",
        "cli_shims": shims,
    }


def uninstall_opencode_target(home_dir: Optional[Path] = None) -> Dict[str, Any]:
    base_home = home_dir or Path.home()
    opencode_dir = base_home / ".config" / "opencode"
    if (opencode_dir / "AGENTS.md").exists():
        merge_marked_markdown(opencode_dir / "AGENTS.md", "", remove=True)
    if (opencode_dir / "opencode.json").exists():
        merge_opencode_config(opencode_dir / "opencode.json", remove=True)
    return {"target": "opencode", "uninstalled": True}


def install_generic_mcp_target(
    home_dir: Optional[Path] = None,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Emit/merge clean zero-secret `kaizen-mcp.json` without touching existing user MCP keys."""
    base_home = home_dir or Path.home()
    mcp_path = output_path or (base_home / ".kaizen-kit" / "kaizen-mcp.json")
    existing: Dict[str, Any] = {}
    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Refusing to overwrite malformed JSON at {mcp_path}: {exc}") from exc

    servers = dict(existing.get("mcpServers") or {})
    servers["kaizen-kit"] = {"command": "kaizen-kit", "args": ["mcp"]}
    servers.setdefault("serena", {"type": "http", "url": "http://127.0.0.1:24280/mcp"})
    servers.setdefault("context7", {"type": "http", "url": "https://mcp.context7.com/mcp"})
    existing["mcpServers"] = servers

    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "target": "generic-mcp",
        "harness": "generic-mcp",
        "mcp_json": str(mcp_path),
        "mcp_servers": sorted(servers.keys()),
    }


def install_aider_target(home_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Emit Kaizen conventions & non-destructively merge snippet into `~/.aider.conf.yml`."""
    base_home = home_dir or Path.home()
    conv_md = base_home / ".kaizen-kit" / "CONVENTIONS.md"
    conv_md.parent.mkdir(parents=True, exist_ok=True)
    conv_md.write_text(_render_pillars_markdown() + "\n", encoding="utf-8")

    aider_conf = base_home / ".aider.conf.yml"
    existing = aider_conf.read_text(encoding="utf-8") if aider_conf.exists() else ""
    rx_block = re.compile(
        r"\r?\n?" + re.escape(AIDER_YAML_START) + r".*?" + re.escape(AIDER_YAML_END) + r"\r?\n?",
        re.DOTALL,
    )
    stripped_outside = rx_block.sub("", existing)
    has_existing_read_key = bool(re.search(r"^\s*read\s*:", stripped_outside, re.MULTILINE))
    conv_posix = conv_md.as_posix()
    if has_existing_read_key:
        snippet_body = f"# Kaizen conventions path: {conv_posix}"
    else:
        snippet_body = f'read:\n  - "{conv_posix}"'
    snippet = f"{AIDER_YAML_START}\n{snippet_body}\n{AIDER_YAML_END}"

    if AIDER_YAML_START in existing and AIDER_YAML_END in existing:
        updated = rx_block.sub(f"\n{snippet}\n", existing).lstrip("\n")
    elif existing.strip():
        updated = existing.rstrip() + "\n\n" + snippet + "\n"
    else:
        updated = snippet + "\n"

    aider_conf.write_text(updated, encoding="utf-8")
    mcp_res = install_generic_mcp_target(base_home)
    return {
        "target": "aider",
        "harness": "aider",
        "aider_conf": str(aider_conf),
        "conventions_md": str(conv_md),
        "mcp_json": mcp_res["mcp_json"],
    }


def uninstall_aider_target(home_dir: Optional[Path] = None) -> Dict[str, Any]:
    base_home = home_dir or Path.home()
    aider_conf = base_home / ".aider.conf.yml"
    if aider_conf.exists():
        existing = aider_conf.read_text(encoding="utf-8")
        rx_block = re.compile(
            r"\r?\n?" + re.escape(AIDER_YAML_START) + r".*?" + re.escape(AIDER_YAML_END) + r"\r?\n?",
            re.DOTALL,
        )
        cleaned = rx_block.sub("\n", existing).strip()
        aider_conf.write_text((cleaned + "\n") if cleaned else "", encoding="utf-8")
    return {"target": "aider", "uninstalled": True}


def install_continue_target(home_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Emit drop-in Continue MCP & rules snippets without modifying user `config.json` auth/models."""
    base_home = home_dir or Path.home()
    cont_dir = base_home / ".continue"
    mcp_dropin = cont_dir / "mcpServers" / "kaizen-kit.json"
    rules_dropin = cont_dir / "rules" / "kaizen-core.md"

    mcp_dropin.parent.mkdir(parents=True, exist_ok=True)
    rules_dropin.parent.mkdir(parents=True, exist_ok=True)

    mcp_payload = {
        "mcpServers": {
            "kaizen-kit": {
                "command": "kaizen-kit",
                "args": ["mcp"],
            }
        }
    }
    mcp_dropin.write_text(json.dumps(mcp_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    rules_dropin.write_text(_render_pillars_markdown() + "\n", encoding="utf-8")
    return {
        "target": "continue",
        "harness": "continue",
        "mcp_config": str(mcp_dropin),
        "rules_md": str(rules_dropin),
    }


def _dispatch_single_harness(
    harness_name: str,
    base_home: Path,
    repo_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    dispatch = {
        "claude": install_claude_target,
        "codex": install_codex_target,
        "opencode": install_opencode_target,
        "cursor": install_cursor_target,
        "aider": install_aider_target,
        "continue": install_continue_target,
        "generic-mcp": install_generic_mcp_target,
        "antigravity": install_antigravity_target,
    }
    if harness_name == "git-hooks":
        return install_git_prepush_hook((repo_dir or Path.cwd()).resolve())
    if harness_name not in dispatch:
        raise ValueError(f"Unsupported harness {harness_name!r}; expected one of {HARNESS_CHOICES}")
    return dispatch[harness_name](base_home)


def install_harness(
    harness: str = "auto",
    home_dir: Optional[Path] = None,
    repo_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Install Kaizen across one, `auto`-detected, or `all` coding harnesses non-destructively."""
    base_home = home_dir or Path.home()
    choice = (harness or "auto").strip().lower()

    if choice == "auto":
        targets = detect_installed_harnesses(home_dir)
        if "generic-mcp" not in targets:
            targets = [*targets, "generic-mcp"]
    elif choice == "all":
        targets = ["claude", "codex", "opencode", "cursor", "aider", "continue", "generic-mcp", "antigravity"]
    else:
        targets = [choice]

    installed = [_dispatch_single_harness(t, base_home, repo_dir=repo_dir) for t in targets]
    return {"harness": choice, "targets": targets, "installed": installed}


def uninstall_harness(harness: str = "all", home_dir: Optional[Path] = None) -> Dict[str, Any]:
    base_home = home_dir or Path.home()
    choice = (harness or "all").strip().lower()
    removed: List[Dict[str, Any]] = []
    if choice in ("all", "claude"):
        removed.append(uninstall_claude_target(base_home))
    if choice in ("all", "codex"):
        removed.append(uninstall_codex_target(base_home))
    if choice in ("all", "opencode"):
        removed.append(uninstall_opencode_target(base_home))
    if choice in ("all", "aider"):
        removed.append(uninstall_aider_target(base_home))
    return {"harness": choice, "uninstalled": removed}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kaizen Plug-and-Play Multi-Harness Installer")
    ap.add_argument(
        "--harness",
        choices=list(HARNESS_CHOICES),
        default="",
        help="Coding harness to configure (`auto`, `all`, `claude`, `codex`, `opencode`, `cursor`, `aider`, `continue`, `generic-mcp`, `antigravity`)",
    )
    ap.add_argument(
        "--target",
        choices=[*HARNESS_CHOICES, "git-hooks"],
        default="",
        help="Alias for --harness (plus `git-hooks`)",
    )
    ap.add_argument("--home", default="", help="Optional custom HOME directory")
    ap.add_argument("--repo", default=".", help="Target git repo when `--target git-hooks` is used")
    ap.add_argument("--uninstall", action="store_true", help="Non-destructively remove kit hooks and markers")
    args = ap.parse_args(argv)

    selected = args.harness or args.target or "auto"
    home_override = Path(args.home).expanduser().resolve() if args.home else None
    if args.uninstall:
        res = uninstall_harness(selected if selected != "auto" else "all", home_dir=home_override)
    else:
        res = install_harness(selected, home_dir=home_override, repo_dir=Path(args.repo))
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
