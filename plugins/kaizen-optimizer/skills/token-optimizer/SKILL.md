---
name: token-optimizer
description: Token Reduction Governor, Dynamic TALE Budgeting, and AI Credit Consumption Auditor.
---

# Token Optimizer Skill (`token-optimizer`)

Analyzes and enforces token efficiency across developer workflows:
- **Observation Masking**: Truncates large test/diff outputs (`>80L`) to 15-line head + 15-line tail while preserving alerts and stack traces.
- **AST Skeleton Pruning**: Replaces full-file reads with structural AST skeletons.
- **Dynamic Thinking Budgeting**: Calibrates reasoning tokens via `effort-autosense`.
