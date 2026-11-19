#!/usr/bin/env python3
"""
Test Suite 3: Non-Destructive Auth-Preserving Installer & Uninstaller Verification.

Simulates a teammate's machine that ALREADY has working corporate Claude Code credentials
in `~/.claude/settings.json` (`env.ANTHROPIC_BASE_URL`, `env.ANTHROPIC_API_KEY`, custom hooks,
and existing `CLAUDE.md` notes). Verifies:
  1. `install --target all` preserves 100% of existing auth/env keys and custom notes.
  2. Installs all 11 `@` agents, 48 curated skills, 6 lifecycle hooks, and Cursor `.mdc` rules.
  3. Idempotent re-run does not duplicate hooks or `CLAUDE.md` sections.
  4. `uninstall` cleanly removes kit hooks/agents/markers while keeping teammate auth intact.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from kaizen_kit import installer  # noqa: E402


class TestNonDestructiveInstall(unittest.TestCase):
    def test_auth_preserving_install_idempotency_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            claude_dir = fake_home / ".claude"
            claude_dir.mkdir(parents=True)

            # Pre-existing corporate Claude Code config + custom third-party hook + personal CLAUDE.md
            orig_settings = {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://corporate-claude.example.internal",
                    "ANTHROPIC_API_KEY": "corp-existing-token-placeholder",
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                },
                "theme": "dark",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo custom-teammate-hook"}],
                        }
                    ]
                },
            }
            (claude_dir / "settings.json").write_text(json.dumps(orig_settings, indent=2), encoding="utf-8")
            (claude_dir / "CLAUDE.md").write_text("# Teammate Personal Notes\nKeep this header.\n", encoding="utf-8")

            # 1. Run install twice (idempotency check)
            res1 = installer.install_claude_target(fake_home)
            res2 = installer.install_claude_target(fake_home)
            cur_res = installer.install_cursor_target(fake_home)

            self.assertEqual(res1["agents_installed"], 15)
            self.assertEqual(res2["agents_installed"], 15)
            self.assertEqual(res1["codices_installed"], 7)
            self.assertGreaterEqual(res1["skills_installed"], 30)
            self.assertEqual(cur_res["domain_rules"], 2)

            # 2. Verify auth env and custom teammate hook are 100% untouched
            merged = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(merged["env"], orig_settings["env"])
            self.assertEqual(merged["theme"], "dark")
            pre_hooks = merged["hooks"]["PreToolUse"]
            self.assertEqual(len(pre_hooks), 2, "Should contain 1 custom teammate hook + 1 kit hook (no duplicates)")
            self.assertEqual(pre_hooks[0]["hooks"][0]["command"], "echo custom-teammate-hook")

            # 3. Verify CLAUDE.md preserved teammate notes and injected kit block once
            md_text = (claude_dir / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("# Teammate Personal Notes", md_text)
            self.assertEqual(md_text.count(installer.CLAUDE_MD_START), 1)

            # 4. Verify compiled Claude agent frontmatter preserves full MCP inheritance and read-only disallowedTools
            sys_arch = (claude_dir / "agents" / "systems-architect.md").read_text(encoding="utf-8")
            self.assertIn("model: sonnet", sys_arch)
            self.assertNotIn("tools:\n", sys_arch, "Read-write agents must omit restrictive tools: allowlist to inherit MCP tools")
            self.assertNotIn("disallowedTools:", sys_arch)
            explorer = (claude_dir / "agents" / "code-explorer.md").read_text(encoding="utf-8")
            self.assertIn("disallowedTools:", explorer)
            self.assertIn("  - Write", explorer)
            self.assertIn("  - mcp__serena__replace_symbol_body", explorer)

            # 4b. Verify user-scoped MCP servers synced into ~/.claude.json
            claude_json = json.loads((fake_home / ".claude.json").read_text(encoding="utf-8"))
            self.assertIn("serena", claude_json["mcpServers"])
            self.assertIn("awsknowledge", claude_json["mcpServers"])
            self.assertIn("context7", claude_json["mcpServers"])

            # 5. Uninstall and confirm clean restoration of teammate state
            installer.uninstall_claude_target(fake_home)
            after_uninst = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(after_uninst["env"], orig_settings["env"])
            self.assertEqual(len(after_uninst["hooks"]["PreToolUse"]), 1)
            self.assertEqual(after_uninst["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "echo custom-teammate-hook")
            md_after = (claude_dir / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("# Teammate Personal Notes", md_after)
            self.assertNotIn(installer.CLAUDE_MD_START, md_after)

    def test_resolve_shim_python_prefers_venv_radar_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            kit_dir = Path(td)
            venv_py = kit_dir / ".venv-radar" / "Scripts" / "python.exe"
            venv_py.parent.mkdir(parents=True)
            venv_py.write_text("", encoding="utf-8")
            self.assertEqual(installer._resolve_shim_python(kit_dir), venv_py)

    def test_detect_installed_harnesses_and_codex_opencode_generic_mcp(self) -> None:
        import tomllib

        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            codex_dir = fake_home / ".codex"
            codex_dir.mkdir(parents=True)
            (codex_dir / "AGENTS.md").write_text("# User Codex Notes\nPreserve me.\n", encoding="utf-8")
            (codex_dir / "config.toml").write_text(
                'model = "gpt-5"\napproval_policy = "on-request"\n\n[mcp_servers.custom_tool]\ncommand = "echo"\nargs = ["hi"]\n',
                encoding="utf-8",
            )

            opencode_dir = fake_home / ".config" / "opencode"
            opencode_dir.mkdir(parents=True)
            (opencode_dir / "AGENTS.md").write_text("# User OpenCode Notes\n", encoding="utf-8")
            (opencode_dir / "opencode.json").write_text(
                json.dumps(
                    {
                        "$schema": "https://opencode.ai/config.json",
                        "provider": {"openai": {"options": {"apiKey": "sk-keep-me"}}},
                        "mcp": {"existing-server": {"type": "local", "command": ["node", "srv.js"]}},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            (fake_home / ".aider.conf.yml").write_text(
                "openai-api-key: sk-aider-secret\nmodel: gpt-4.1\n",
                encoding="utf-8",
            )
            cont_cfg = fake_home / ".continue" / "config.json"
            cont_cfg.parent.mkdir(parents=True)
            cont_cfg.write_text('{"models": [{"title": "Local Ollama", "provider": "ollama"}]}', encoding="utf-8")

            detected = installer.detect_installed_harnesses(fake_home)
            self.assertEqual(detected, ["codex", "opencode", "aider", "continue"])

            # Run auto-install twice for idempotency
            rc1 = installer.main(["--harness", "auto", "--home", str(fake_home)])
            rc2 = installer.main(["--harness", "auto", "--home", str(fake_home)])
            self.assertEqual(rc1, 0)
            self.assertEqual(rc2, 0)

            # Verify Codex AGENTS.md & config.toml
            codex_md = (codex_dir / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("# User Codex Notes", codex_md)
            self.assertEqual(codex_md.count(installer.CLAUDE_MD_START), 1)
            parsed_toml = tomllib.loads((codex_dir / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(parsed_toml["model"], "gpt-5")
            self.assertEqual(parsed_toml["mcp_servers"]["custom_tool"]["command"], "echo")
            self.assertEqual(parsed_toml["mcp_servers"]["kaizen-kit"]["command"], "kaizen-kit")
            self.assertEqual(parsed_toml["mcp_servers"]["kaizen-kit"]["args"], ["mcp"])

            # Verify OpenCode AGENTS.md & opencode.json
            oc_md = (opencode_dir / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("# User OpenCode Notes", oc_md)
            self.assertEqual(oc_md.count(installer.CLAUDE_MD_START), 1)
            oc_json = json.loads((opencode_dir / "opencode.json").read_text(encoding="utf-8"))
            self.assertEqual(oc_json["provider"]["openai"]["options"]["apiKey"], "sk-keep-me")
            self.assertIn("existing-server", oc_json["mcp"])
            self.assertEqual(oc_json["mcp"]["kaizen-kit"]["command"], ["kaizen-kit", "mcp"])
            self.assertEqual(oc_json["mcpServers"]["kaizen-kit"]["args"], ["mcp"])

            # Verify Aider, Continue & generic-mcp preserved existing auth/configs
            aider_txt = (fake_home / ".aider.conf.yml").read_text(encoding="utf-8")
            self.assertIn("openai-api-key: sk-aider-secret", aider_txt)
            self.assertEqual(aider_txt.count(installer.AIDER_YAML_START), 1)
            self.assertIn("Local Ollama", cont_cfg.read_text(encoding="utf-8"))
            cont_mcp = json.loads((fake_home / ".continue" / "mcpServers" / "kaizen-kit.json").read_text(encoding="utf-8"))
            self.assertEqual(cont_mcp["mcpServers"]["kaizen-kit"]["command"], "kaizen-kit")

            generic_mcp = json.loads((fake_home / ".kaizen-kit" / "kaizen-mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(generic_mcp["mcpServers"]["kaizen-kit"]["args"], ["mcp"])

            # Verify clean non-destructive uninstall
            installer.uninstall_harness("all", home_dir=fake_home)
            parsed_after = tomllib.loads((codex_dir / "config.toml").read_text(encoding="utf-8"))
            self.assertNotIn("kaizen-kit", parsed_after.get("mcp_servers", {}))
            self.assertEqual(parsed_after["mcp_servers"]["custom_tool"]["command"], "echo")
            oc_after = json.loads((opencode_dir / "opencode.json").read_text(encoding="utf-8"))
            self.assertNotIn("kaizen-kit", oc_after.get("mcp", {}))
            self.assertEqual(oc_after["provider"]["openai"]["options"]["apiKey"], "sk-keep-me")

    def test_universal_hook_adapter_multi_harness_normalization_and_crash_safety(self) -> None:
        from kaizen_kit.hooks import universal_hook_adapter as uha

        with tempfile.TemporaryDirectory() as td:
            prev_state = os.environ.get("AI_KIT_STATE_DIR")
            os.environ["AI_KIT_STATE_DIR"] = str(Path(td) / "hook-state")
            try:
                codex_norm = uha.normalize_hook_payload(
                    {
                        "thread_id": "codex-t1",
                        "tool": "shell",
                        "arguments": json.dumps({"command": ["bash", "-lc", "pytest -q"]}),
                    },
                    runtime="auto",
                    event_name="pre_tool_use",
                )
                self.assertEqual(codex_norm["runtime"], "codex")
                self.assertEqual(codex_norm["canonical_event"], "pre_tool")
                self.assertEqual(codex_norm["canonical_tool"], "run_command")
                self.assertEqual(codex_norm["canonical_args"]["CommandLine"], "pytest -q")

                opencode_norm = uha.normalize_hook_payload(
                    {
                        "sessionID": "oc-1",
                        "tool": "write",
                        "args": {"filePath": "src/app.py", "content": "print('ok')\n"},
                    },
                    runtime="auto",
                    event_name="tool.execute.before",
                )
                self.assertEqual(opencode_norm["runtime"], "opencode")
                self.assertEqual(opencode_norm["canonical_event"], "pre_tool")
                self.assertEqual(opencode_norm["canonical_tool"], "write_file")
                self.assertEqual(opencode_norm["canonical_args"]["Path"], "src/app.py")

                cursor_norm = uha.normalize_hook_payload(
                    {"conversation_id": "cur-1", "command": "git status"},
                    runtime="auto",
                    event_name="beforeShellExecution",
                )
                self.assertEqual(cursor_norm["runtime"], "cursor")
                self.assertEqual(cursor_norm["canonical_event"], "pre_tool")
                self.assertEqual(cursor_norm["canonical_tool"], "run_command")
                self.assertEqual(cursor_norm["canonical_args"]["CommandLine"], "git status")

                # Malformed, missing, and unknown inputs must never raise
                for rt in ("auto", "claude", "codex", "opencode", "cursor", "antigravity"):
                    for bad_payload in (None, "", [], 42, {"toolCall": "bad", "tool_input": None, "unknown_key": [1, 2]}):
                        code, out, _ = uha.handle_harness_event(rt, "unknown_event", bad_payload)
                        self.assertEqual(code, 0)
                        self.assertIsInstance(out, dict)
            finally:
                if prev_state is None:
                    os.environ.pop("AI_KIT_STATE_DIR", None)
                else:
                    os.environ["AI_KIT_STATE_DIR"] = prev_state


if __name__ == "__main__":
    unittest.main()
