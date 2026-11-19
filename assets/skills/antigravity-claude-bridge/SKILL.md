---
name: antigravity-claude-bridge
description: Orchestrate Google Antigravity SDK (google.antigravity) across Company AWS Bedrock Claude (Sonnet 5.5, Opus 5.5, Haiku 5.5, Fable 5), Personal Gemini, Cursor Grunt-Workers (Composer-2, Grok), and delegating grunt work ('amelelik') to Composer/Grok/Haiku, consulting Claude Opus/Sonnet from Gemini, or running continuous background analysis in background CI.
---

# Antigravity Multi-Provider Fleet & SDK Orchestrator (`agy-flow`)

The `agy-flow` CLI (`$HOME/.local/bin/agy-flow` -> `$HOME/.gemini/config/plugins/agent-orchestrator/scripts/agy_flow.py`) unites the **Google Antigravity Python SDK (`google.antigravity`)** with all 4 model ecosystems:

| Provider (`--provider`) | Models (`--model`) | Best Role in Your Workflow |
| :--- | :--- | :--- |
| `claude` (AWS Bedrock) | `sonnet` (Sonnet 5.5), `opus` (Opus 5.5), `haiku` (Haiku 5.5), `fable` (Fable 5) | Deep Rust/SQL architecture, council reviews, zero-Gemini-credit execution via `LocalOpenAIAgentConfig` |
| `gemini` / `hybrid` | `gemini-3.8-flash` / default | Interactive Antigravity IDE / SDK orchestration with cross-provider tool delegation (`delegate_to_claude_specialist`, `delegate_to_fast_grunt_worker`, `delegate_to__local`) |
| `cursor` (Grunt-Worker / 'Amelelik') | `composer-2`, `grok`, `grok-code` | Fast bulk refactoring, boilerplate generation, test scaffolding, multi-file sweep |
| `` (16 GB Single-Slot 24/7 Runner) | `` (`qwen2.5-coder:7b` ~4.7GB), `` (`qwen2.5-coder:14b` ~9.0GB), `` (`deepseek-r1:14b` ~9.0GB), `` (`nomic-embed-text` ~0.6GB) | Zero-swap, single-slot sequential model rotation (`OLLAMA_MAX_LOADED_MODELS=1`, `KV_CACHE=q8_0`) for 24/7 free code, RCA, and triage loops |

---

## 1. Quick Commands

### A. Run Full Antigravity SDK Agent (With Tools, 7 Universal Subagents, Skills & Guard Hooks)
```bash
# Run Antigravity SDK Agent backed by AWS Bedrock Claude Sonnet 5.5:
agy-flow chat --provider claude --model sonnet \
  --workspace "$HOME/work-space/kaizen-serverless-lambda/kaizen-the-ondemand-machine" \
  "Audit crates/ingest/src/storage for C1-C15 and P13 compliance."

# Interactive multi-turn terminal REPL on Claude Opus 5.5:
agy-flow repl --provider claude --model opus
```

### B. Fast Specialist or Grunt-Worker ('Amelelik') Dispatch
```bash
# Ask Claude Opus 5.5 as the systems-architect (or rust-pipeline-architect) specialist:
agy-flow ask --provider claude --model opus --agent systems-architect \
  "Review duckdb_read_session.rs for zero-temp-table read-only compliance (P13)."

# Delegate fast grunt work ('amelelik') to Cursor Composer-2 / Haiku worker:
agy-flow ask --provider cursor --model composer-2 --agent code-explorer \
  "Generate unit test cases for TimeWindow and ViewRegistrationTier value objects."
```

### C. Runs continuously all day long, executing `story_guard.py check` (`C1â€“C15` & `P1â€“P15`), agent sync checks, and automatic AI root-cause diagnosis whenever a violation appears, writing live reports to `~/.gemini/antigravity/_cyclic_report.md`:
```bash
# Run in background CI (via SSH tunnel port 11435 or local Ollama / Bedrock Haiku):
agy-flow daemon-loop \
  --provider  --model auto \
  --workspace "$HOME/work-space/kaizen-serverless-lambda/kaizen-the-ondemand-machine" \
  --interval-seconds 900
```

---

## 2. Automatic 3-Way Synchronization (`agent_registry.py sync`)

Running `python "$HOME/.gemini/config/plugins/agent-orchestrator/scripts/agent_registry.py" sync` (also triggered automatically after every turn via `PostInvocation` hook) synchronizes:
1. **Antigravity Global Config**: `~/.gemini/config/agents/*/agent.md` & `~/.gemini/config/plugins/agent-orchestrator/rules/AGENTS.md`
2. **Claude Code (`~/.claude/`)**: `~/.claude/agents/*.md`, `~/.claude/CLAUDE.md`, and `~/.claude/skills/*`
3. **Cursor (`~/.cursor/`)**: `~/.cursor/rules/antigravity-flow.mdc` and `~/.cursor/skills/*`

