# Pillar 1: Kaizen v4.0 Closed-Loop Engineering Philosophy

## 1. Classical & Lean Engineering Laws (Executable Core)
1. **Toyota Production System (TPS / Kaizen)**:
   - **Poka-Yoke (Mistake-Proofing)**: Make illegal states unrepresentable at compile time (Rust `Typestate` / `NewType`, Java 21 `sealed interface` / `record`). Lifecycle hooks physically block raw secrets, non-`u2` AWS mutations, test/parity deletions, and cross-session worktree collisions synchronously in `<20ms`, while offloading heavy symbol/RAG checks to async/cached EC2 Cloud-Brain endpoints (`:8765`, `:8770`, `:4000`, `:6070`, `:18808`, `:24280`).
   - **Jidoka & Andon Cord (Autonomation)**: `PostToolUse` runs `<40ms` real-time sensors (`verify_provenance`, `check_clean_code`, `senior_review_lint`) on every edited `.rs`, `.java`, `.vue`, `.ts` file. When an invariant fails, stop the line immediately—never push synthetic workarounds downstream.
   - **Genchi Genbutsu (Go and See)**: Never guess wire formats or upstream behavior from memory. Inspect raw S3 Avro/BSON bytes, HAR `Root=1-<hex>` traces, and `Server-Timing` headers first.
   - **Heijunka & Lean 3M (Zero Muda, Muri, Mura)**:
     - *Zero Muda (Waste)*: Read targeted line ranges (`<= 300`L) or AST skeletons (`kaizen-kit skeleton`); filter unused payload branches at the earliest decode boundary; never pipe build/test commands through `| tail` or `| head`.
     - *Zero Muri (Overburden)*: Cap local build concurrency (`CARGO_BUILD_JOBS=4`); offload heavy Linux/ARM64/OSS workloads to EC2 Cloud-Brain (`ec2-cloud-brain-watchdog`, zero WSL dependencies).
     - *Zero Mura (Unevenness)*: Dispatch at most **1 remediation worker** on build/test failure; max **1 survey turn** before executing a concrete edit or test.
   - **Hansei & Yokoten (Institutional Memory)**: Persist every subtle bug, dead-end, or architectural rule via `kaizen-kit ledger` (`kaizen_ledger.json` + `DEAD_ENDS.md`) so `SessionStart` briefings protect all future sessions.
2. **Karl Popper Falsification & Deming PDCA**:
   - Write a failing test first, verify `POPULATED_MATCH` separately from `EMPTY_BOTH_200`, and run deterministic verification (`cargo check/test`, `mvn test-compile`, `npm run typecheck`, or `kaizen-kit check`) before claiming completion.
3. **Goldratt Theory of Constraints (TOC) & 3-Attempt Fail-Forward**:
   - Optimize only the active system bottleneck. After 3 failed attempts on an isolated milestone, park it cleanly (`kaizen-kit ledger park-milestone`) and advance to the next independent workstream.
4. **Nassim Taleb Antifragility & Ousterhout Deep Modules (*APOSD*)**:
   - Build **Deep Modules** (narrow, simple interfaces hiding rich internal logic like `OnceLock` singletons and `MaterializedHomeState`), eliminate shallow passthroughs (*Via Negativa*), and define edge-case errors out of existence.

## 2. Privacy, Secrets & MR Hygiene Laws
- **Deterministic Pseudonymization (`DETERMINISTIC-ANONYMIZATION-CORE`)**: Preserve 1:1 relational integrity (`00:50:C2:XX:XX:XX` / `198.51.100.X`) on customer data; `kaizen-u2` and `kaizen-staging` testbeds are exempt.
- **Zero Secrets & Zero Intermediate Leak**: Never read `.env`, `~/.aws/credentials`, private keys, or `settings.xml`. Keep MR descriptions in clean English with zero agent session trivia or local `docs/architecture/**` notes.
