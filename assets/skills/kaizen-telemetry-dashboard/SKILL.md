---
name: kaizen-telemetry-dashboard
description: Daily AI Kaizen Telemetry Dashboard, 0-Token AST Skeleton Pruner (Rust & Java 21), Cross-Chat Decision Ledger (`kaizen_ledger.json`) manager, and Web-Research-Driven Skill/Plugin Recommender. Use when auditing daily AI bottlenecks, compressing context via AST skeletons, or syncing architectural decisions across chats.
---

# Kaizen Telemetry Dashboard & Context Engineering Plugin

## 1. 0-Token AST Skeleton Pruner (`scripts/ast_skeleton_pruner.py`)

Based on 2025–2026 Context Engineering best practices, dumping entire `>250`-line source files into an agent's context wastes 78–85% of input tokens on method bodies and causes "lost-in-the-middle" attention degradation.
Before reading any large `.rs` or `.java` file, extract its **AST signature skeleton** (structs, enums, traits, function/method signatures, and line numbers) with zero LLM tokens:

```bash
kaizen-kit skeleton <file_or_dir>
```

Then call `read_file` **only** on the exact `StartLine`–`EndLine` of the function you need to modify.

## 2. Daily AI Kaizen & Web-Backed Capability Analyzer

Run the daily Kaizen analyzer combined with live web research (`search_web` + `search_marketplace`):

```bash
python $AI_KIT_HOME/assets/plugins/token-optimizer/scripts/daily_ai_kaizen_analyzer.py --hours 24 --write-report
```

## 3. Cross-Chat Architectural Decision Ledger

Read and update [`kaizen_ledger.json`](file:///$AI_KIT_STATE_DIR/kaizen_ledger.json) whenever an architectural decision is locked or an endpoint reaches `POPULATED_MATCH` parity so no decision is ever forgotten across chat transitions.
