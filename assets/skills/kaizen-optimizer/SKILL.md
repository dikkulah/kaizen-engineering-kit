---
name: kaizen-optimizer
description: Unified Kaizen v4.0 & Lean developer optimization engine fusing Prompt Optimization (WS-1..WS-N), Token & Runaway Loop Guard, Smart Compaction State Recovery, Selective Lossless Test Distillation, Symbol-Summary Conceptual RAG, Offline OSS AST/Dataflow/Clone/Hotspot Bridge, 1h Corporate Bedrock Prompt Cache Shield, and the 6 Lean Engineering Invariants. Use when auditing AI bottlenecks, managing context budgets, refactoring via AST/hotspot patterns, analyzing token consumption, or running daily developer maintenance.
---

# Kaizen v4.0 Closed-Loop Lifecycle, EC2 Cloud-Brain & Token Engineering Engine

The `kaizen-optimizer` v4.0 engine implements **Toyota Production System (TPS)**, **Deming PDCA**, **Karl Popper Falsification**, **Goldratt Theory of Constraints**, **Tornhill Behavioral Code Analysis**, **EC2 Cloud-Brain FastMCP (`:8770`) + Google ADK (`kaizen-kit adk`)**, and **Serena RAM LSP (`:24280`) + Corporate AWS Bedrock `Claude 5.5` (`0.1x` Prompt Cache)** across both Antigravity (`hooks.json` -> `hook_guard.py`) and multi-runtime adapters (`universal_hook_adapter.py` -> `engine.py`).

## 1. 4-Tier Code Intelligence & Refactoring Router
1. **Known Symbol / Type / Reference Graph (`<100ms` Warm RAM)**:
   - `serena` MCP (`127.0.0.1:24280/mcp`) + `Serena ProjectServer` (`:24225`) or `lsp-query find <Symbol> --project <repo>` / `lsp-query search <term>`.
2. **Unknown Symbol / Conceptual Discovery (`~110ms` Zero-Full-File Symbol-Summary RAG)**:
   - `kaizen-kit symbol-rag "<conceptual query>" [--workspace <dir>] [--repo <r>]` (`symbol_rag_indexer.py`): queries camelCase/snake_case-split SQLite FTS5 BM25 + 64-bit 1-bit sign-quantized semantic sketches over workspace symbol summaries & architectural `.md` headings, then hands exact symbol names back to `Serena MCP`.
3. **Function-Body AST, Dataflow, Clone & 12-Month Git Hotspot Bridge (100% Offline CPU)**:
   - `kaizen-kit ast-audit <repo> --bridge-pack` (`oss_ast_gate.py`): fuses `ast-grep` (with `#[cfg(test)]` isolation & `--codemod-rule <id> --apply`), `opengrep --disable-version-check`, `jscpd`, `dependency-cruiser`, and Tornhill 12-month `Churn x LOC` Git Hotspots + live-file Temporal Coupling + enclosing Serena symbol names.
4. **Multi-File Architectural Synthesis (`Claude-RAM :24302 -> Corporate Bedrock :18889`, `1h` Prompt Cache)**:
   - `claude-ram ask --model sonnet|opus|haiku --repo <repo> --files "src/ingest/*.rs" "<question>"`: keeps static system prompt + sorted RAM file blocks before AWS Bedrock `cachePoint` (`ttl: "1h"`, `90%` `cacheReadInputTokens` discount at `0.1x`) and appends dynamic questions last.

---

## 2. 6-Event Closed-Loop Lifecycle (`hook_guard.py` / `engine.py`)
1. **`SessionStart` (Yokoten Briefing + Auto-Warm RAM Daemons & Compaction Recovery)**: Injects `<240`-token institutional memory (`ADR-*`, `DE-*`), warms `Serena LSP (:24280)` + `Claude-RAM (:24302)` asynchronously, and restores compacted session state when `source == "compact"`.
2. **`PreInvocation` / `PreCompact` (Smart Compaction Recovery + Zombie Cron Breaker)**:
   - **`compact_state_guard.py`**: Detects auto-compaction (`initialNumSteps` drop `>= 4` from `max_seen_steps >= 8`) or explicit `/compact` and injects a one-shot verified `serena` / `lsp-query` symbol + modified-file checkpoint via `systemMessage` so the agent never re-reads files from scratch.
3. **`PreToolUse` (Poka-Yoke Mistake-Proofing + Selective Lossless Test Distiller)**:
   - **`lossless_test_distiller.py`**: Selectively wraps test/compiler/linter commands (`cargo test|check|clippy`, `pytest`, `mvn test`, `go test`, `npm test|typecheck|lint`, `opengrep`) — distilling passing runs into a 2-line `[Test Distiller - PASS]` summary while preserving 100% of failure traces and original exit codes.
   - Enforces Cross-Chat Worktree Mutex, `SEC-AWS-TENANCY-ISOLATION` (`--profile kaizen-u2` only for AWS mutations), git hook-bypass denial (`--no-verify`, `core.hooksPath`), Zero Synthetic Telemetry, `>300L` AST Skeleton Gate, Anti-Chunk-Scraping, Serial Read-Spree Breaker, and Blind Recursive Grep Redirect.
4. **`PostToolUse` (Jidoka Real-Time Sensor `<40ms` + Continuous Checkpointing)**:
   - Runs `verify_provenance.py`, `check_clean_code.py`, `senior_review_lint.py`, and `ast-grep` on edited production files; checkpoints verified symbols and edits into `compact_state_guard`.
5. **`PostInvocation` (Immediate Andon Feedback & Step Budget Gate)**:
   - Surfaces Jidoka findings immediately and enforces Subagent `<= 40` / Root `>= 80` step budgets.
6. **`Stop` / `SubagentStop` (Popperian Verification-Before-Completion Gate + Auto Lock Release)**:
   - Blocks premature turn completion if production edits lack a passing verification run or carry unresolved `INV-*` violations.

---

## 3. Quick Commands & Deterministic Self-Tests
```powershell
# Unified Kaizen Doctor & All Gate Self-Tests (AST, Compaction, Distiller, Symbol-RAG, Provenance, Parity)
kaizen-kit doctor --self-test

# Compact AST + Enclosing Serena Symbol + 12m Git Hotspot & Live Coupling Bridge Pack
kaizen-kit ast-audit . --bridge-pack

# Portable Symbol-Summary Conceptual RAG
kaizen-kit symbol-rag "station score telemetry fold" --workspace . --json

# Hansei & Yokoten Ledger + 0-Token AST Skeleton Pruner
kaizen-kit ledger status
kaizen-kit skeleton <file>

# Plugin Hook Guard & Bridge Self-Tests (Antigravity plugin scripts)
python "$env:USERPROFILE\.gemini\config\plugins\kaizen-optimizer\scripts\test_hook_guard.py"
python "$env:USERPROFILE\.gemini\config\plugins\kaizen-bedrock-bridge\scripts\test_bridge.py"
```
