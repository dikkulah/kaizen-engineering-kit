# Kaizen Autonomous Engineering Operating System (`kaizen-optimizer`)

## 1. Classical & Lean Engineering Invariants (Toyota Production System)
1. **Poka-Yoke (Mistake-Proofing)**:
   - Make illegal states unrepresentable at compile time (Rust `Typestate`, `NewType`, Java 21 `sealed interface` / `record`, TypeScript discriminated unions).
   - Hook guards block hardcoded secrets, test deletions, and cross-branch collisions synchronously in `<20ms`.
2. **Jidoka & Andon Cord (Autonomation)**:
   - Scan every edited file in real time for provenance, Clean Code invariants, and AST complexity.
   - When an invariant fails, stop the line immediately—never push synthetic workarounds downstream.
3. **Genchi Genbutsu (Go and See)**:
   - Never guess wire formats or upstream behavior from memory.
   - Inspect raw network payloads, logs, and server timing headers first.
4. **Heijunka & Lean 3M (Zero Muda, Muri, Mura)**:
   - *Zero Muda (Waste)*: Read targeted line ranges (`<= 120` lines) or AST skeletons; filter unused payload branches at the earliest decode boundary; never truncate test command outputs with pipe filters.
   - *Zero Muri (Overburden)*: Cap local concurrency; offload heavy Linux/ARM64 and GPU workloads to dedicated cloud-brain worker instances.
   - *Zero Mura (Unevenness)*: Dispatch at most **1 remediation worker** on build/test failure (never spawn multi-agent explorer committees); max **1 survey turn** before executing a concrete edit or test.
5. **Hansei & Yokoten (Institutional Memory & Continual Learning)**:
   - Whenever a subtle bug, dead-end, or user preference is established, persist it immediately via `DEAD_ENDS.md` and `kaizen_ledger.json`.
   - Inject this memory into every new session start to prevent recurring mistakes.

---

## 2. Karl Popper Falsification & 3-Attempt Fail-Forward
1. **Karl Popper Falsification**:
   - A hypothesis or fix is valid only if it survives an attempt to falsify it: write a failing test first, verify populated parity separately from empty pass, and satisfy verification gates before claiming completion.
2. **Goldratt Theory of Constraints (TOC) & 3-Attempt Fail-Forward**:
   - Optimize only the active system bottleneck.
   - After 3 failed attempts on an isolated milestone, park the milestone, reset cleanly to checkpoint, and advance to the next independent workstream.

---

## 3. Context Budget & Privacy Laws
1. **Atomic Workstream Decomposition (`WS-1..WS-N`)**:
   - Decompose compound prompts into dependency-ordered workstreams (`Architecture -> Domain -> Contracts -> Verification`).
   - Verify each workstream before concluding the turn.
2. **Context & Subagent Budgets**:
   - Compaction gate at `>= 80` planner steps.
   - Subagents are hard-capped at `<= 40` steps.
   - Zero polling loops (`sleep` and busy-wait loops are strictly blocked).
3. **Deterministic Pseudonymization & Zero Secrets**:
   - Never commit `.env`, private keys, or raw credentials.
   - Preserve relational integrity when pseudonymizing sensitive identifiers.
