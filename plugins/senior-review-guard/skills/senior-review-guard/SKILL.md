---
name: senior-review-guard
description: Universal Senior Code Reviewer Simulator & Pre-Flight CI Gate. Audits Git diffs, PRs, and commits against production engineering invariants.
---

# Senior Review Guard Skill (`senior-review-guard`)

Audits code changes against classical production invariants across 6 engineering dimensions:
1. **Clean Architecture & Backend Hygiene**: Single-line logs, no log & throw, zero full package paths, guard clauses.
2. **Concurrency & Security**: Fail-closed auth, principal-isolated singleflight coalescing, deep immutability.
3. **Frontend & Reactive State**: Dynamic prop watchers, stable table state on error, pagination cursor reset.
4. **Observability & Boundaries**: Clean microservice models, HTTP 40x/50x error logs, datastore FinOps.
5. **Resilient Fallback**: Typed domain result objects, generic exception catch fallbacks, zero dead code.
6. **Pre-Flight CI Shield**: Fast-forward git rebase verification and 0-error local compilation check.

Run pre-flight check:
```bash
kaizen-kit check --pre-flight .
```
