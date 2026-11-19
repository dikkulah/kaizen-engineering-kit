---
name: effort-autosense
description: System 1 Thinking Effort AutoSense calibrating reasoning levels (minimal..xhigh) before frontier spend.
---

# Effort AutoSense Skill (`effort-autosense`)

Uses fast heuristics and ultra-light sub-1B/4B System 1 models to automatically determine thinking effort:
- **`minimal` (0 tokens)**: Typos, comments, docstrings, formatting, 1-line syntax tweaks.
- **`low` (1,024 tokens)**: Localized function bugfixes, mechanical guard-clause flattening.
- **`medium` (4,096 tokens)**: Standard feature development, multi-function refactoring, unit tests.
- **`high` (16,384 tokens)**: Concurrency race conditions, Clean Architecture layer boundary fixes.
- **`xhigh` (32,768 tokens)**: Distributed deadlocks, formal protocol verification, multi-repo blast radius.

Run via CLI: `kaizen-kit autosense "<prompt_or_code>"`
Run via MCP: `kaizen_effort_autosense`
