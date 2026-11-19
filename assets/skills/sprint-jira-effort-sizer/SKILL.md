---
name: sprint-jira-effort-sizer
description: >
  Empirical task effort sizer (Story Points & person-days) and sprint capacity planner
  powered by local Git history, module blast-radius, and Jira (`UC-*`, `RDM-*`) complexity heuristics.
---

# Empirical Sprint & Jira Effort Sizer (`sprint-jira-effort-sizer`)

## 1. Purpose
Estimates **Person-Days (`adam-gÃ¼n`)**, **Fibonacci Story Points (`1, 2, 3, 5, 8, 13`)**, **Target Repositories**, and **Canonical Subagent Owner** for any feature/bug description or backlog list by combining:
1. Cross-layer blast radius (Single-repo vs. Multi-repo Rust + Java + SQL + Vue E2E).
2. Historical Git commit density and test/verification requirements (`proof-gate`).

## 2. CLI Usage
```bash
python $AI_KIT_HOME/assets/skills/sprint-jira-effort-sizer/scripts/effort_sizer.py \
  --task "OnDemand v4 expert-bundle endpoint migration + CloudGatewayApi MySQL cleanup + TelemetryDashboard UI parity"
```


