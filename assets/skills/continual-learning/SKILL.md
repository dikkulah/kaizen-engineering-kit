---
name: continual-learning
description: >-
  Extract architectural lessons, user corrections, bug post-mortems, and engineering invariants
  from the current conversation and persist them into permanent rules, Hansei ledger, and DEAD_ENDS.md.
  Triggered by /learn, "bunu kalıcı yap", "bunu öğren", "bunu kenara not et", or "bir daha böyle yapma".
---

# Continual Learning, Hansei & Yokoten Persistence (`continual-learning`)

Turns debugging insights, post-mortems, user corrections, and architectural decisions into **permanent, cross-conversation institutional memory** injected automatically at `SessionStart`.

## 1. Executable Hansei & Yokoten CLI (`hansei_ledger.py`)

Always persist structured decisions, dead-ends, and parked milestones using `hansei_ledger.py`:

```powershell
# 1. Record an Architectural Decision (ADR) into kaizen_ledger.json:
kaizen-kit ledger record-decision --id ADR-005 --scope ondemand-telemetry-engine --title "..." --summary "..."

# 2. Record a Failure / Dead-End & its Permanent Guard (auto-syncs DEAD_ENDS.md):
kaizen-kit ledger record-dead-end --id DE-009 --symptom "..." --root-cause "..." --guard "..."

# 3. Park a Blocked Milestone after 3 Attempts (Goldratt Fail-Forward):
kaizen-kit ledger park-milestone --id MS-12 --checkpoint HEAD --reason "..."

# 4. Inspect Current Ledger & Active Cross-Chat Worktree Locks:
kaizen-kit ledger status
```

## 2. Routing Matrix

| Knowledge Type | Authoritative Target |
| :--- | :--- |
| **Architectural Decision / Dead-End / Parked Milestone** | `hansei_ledger.py` -> `~/.gemini/antigravity/kaizen_ledger.json` & `DEAD_ENDS.md` |
| **User Preferences, Model Strategy & Long-Term Notes** | `~/.gemini/config/knowledge/PERSISTENT_NOTES.md` |
| **Language / Domain Craft Rules** | `~/.gemini/config/plugins/agent-orchestrator/rules/domains/<domain>.md` |
| **Always-On Mechanical Enforcement** | `~/.gemini/config/plugins/kaizen-optimizer/scripts/hook_guard.py` + `test_hook_guard.py` |
