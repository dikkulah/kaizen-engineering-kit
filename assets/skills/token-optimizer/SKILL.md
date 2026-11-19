---
name: token-optimizer
description: >-
  Analyze historical and real-time token and AI credit consumption across all
  Antigravity conversations, subagent trees (/teamwork, /boost), models, tools,
  zombie crons, and user turns. Use when the user asks about token usage, AI
  credit depletion, expensive chats, runaway agent loops, model cost breakdown,
  or configuring the token-optimizer v2.0 guard hooks.
---

# Token Optimizer & Credit Analyzer Skill (v2.0)

This skill provides full visibility into local Antigravity token/credit consumption (including `/teamwork` and `/boost` subagent tree roll-ups and snapshot delta tracking) and manages the real-time loop-breaker hooks (`PreInvocation`, `PreToolUse`, and `PostInvocation`).

## 1. Run Full Historical, Delta & Subagent-Tree Audit

Always run the analyzer script first to inspect deduplicated protobuf telemetry from `~/.gemini/antigravity/conversations/*.db`, hierarchy relationships from `~/.gemini/antigravity/conversation_summaries.db`, and step-level logs from `~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl`:

```powershell
python "$HOME\.gemini\config\plugins\kaizen-optimizer\scripts\token_analyzer.py"
```

Optional flags:
- `--hours 24`: Filter transcript-level analysis (`[4]`, `[5]`, `[6]`) to the last N hours.
- `--top 15`: Number of top root trees, conversations, and turns to display.
- `--json`: Emit machine-readable JSON summary.
- `--save-snapshot`: Save the current usage baseline to `~/.gemini/antigravity/token_optimizer_history.json` to track delta consumption (`[1B]`) between credit top-ups or optimization milestones.

## 2. Real-Time Circuit-Breaker Guard (`scripts/hook_guard.py` v2.0)

The `token-optimizer` plugin runs an active 3-stage circuit breaker (`PreInvocation`, `PreToolUse`, and `PostInvocation`) that protects against:
1. **Zombie Cron Wakeups (`PreInvocation`)**: Detects recurring `schedule(CronExpression=...)` wakeups (`(iteration N)`) that continue firing after `VICTORY CONFIRMED` or `>=2` idle ticks, injects a 1-time `manage_task(Action="kill")` directive, and then terminates subsequent wakeups **BEFORE the model is called** (0 LLM tokens billed).
2. **Unbounded Cron Schedules (`PreToolUse` overwrite)**: Automatically rewrites any `schedule(CronExpression=...)` call lacking `MaxIterations` (or `>15`) to `MaxIterations: 15, IsDaemon: false`.
3. **Subagent Turn Reset (`read_turn_state`)**: Accurately resets per-turn counters on non-cron `SYSTEM_MESSAGE` steps (parent/peer subagent messages) across a 2 MB transcript tail window, while ALWAYS allowing cleanup tools (`manage_task(Action="kill")`).
4. **Redundant File Reads (`PreToolUse`)**: Blocks `>=3` identical `read_file` / `view_file` calls on the same unmodified file range within a single turn.
5. **Polling & Forced-Continue Loops**: Blocks repeated `manage_task(status|list)` polls (`>2`), corporate VPN/GitLab `ping` loops (`>2`), shell `sleep` polling, forced-continue no-tool retry loops (`>=3`), and runaway single turns (`>80` invocations).

To view blocked loop events:
```bash
tail -n 40 ~/.gemini/antigravity/token_guard_audit.log 2>/dev/null || echo "No blocked loops recorded yet."
```

## 3. Delegating to the `token-optimizer` Subagent

If the user wants a background audit or continuous monitoring without adding audit output to the main conversation context, invoke the `token-optimizer` agent:
- Use `invoke_subagent(TypeName="token-optimizer", Role="Token & Credit Auditor", Model="flash_lite", Prompt="...")`.
