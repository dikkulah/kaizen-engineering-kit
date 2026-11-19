# Universal Agent Matrix & Two-Phase Efficiency Law (`agent-orchestrator`)

## 1. Authoritative 5-Tier Routing Matrix

Delegate tasks across specialized subagents based on cognitive tier and required context:

| Subagent | Tier & Model | Scope & Mandate |
| :--- | :--- | :--- |
| `e2e-systems-visionary` | Tier 3 (`opus` / `pro`) | Cross-repo E2E latency (p95), Kleppmann DDIA, Goldratt TOC, Singleflight coalescing, multi-tier caching. |
| `incident-rca-falsifier` | Tier 3 (`opus` / `pro`) | Log trace correlation, David Agans 9 debugging rules, git log pickaxe forensics, Karl Popper falsification. |
| `reality-checker` | Tier 3 (`opus` / `pro`) | Skeptical, evidence-obsessed QA and integration verifier. Defaults to NEEDS WORK until proven by live tests. |
| `systems-architect` | Tier 2 (`sonnet` / `flash`) | Deep Modules, Evans DDD, Typestate pattern, compile-time serialization codecs. |
| `jvm-service-architect` | Tier 2 (`sonnet` / `flash`) | Java 21 Loom Virtual Threads, Joshua Bloch Effective Java, records, sealed types, OpenAPI parity. |
| `data-sql-architect` | Tier 2 (`sonnet` / `flash`) | Time-series bucket rollups, wire-to-DTO lineage, and set-theoretic parity verification. |
| `refactoring-specialist` | Tier 2 (`sonnet` / `flash`) | Clean Code (G1-G36), Clean Architecture, Refactoring.Guru (22 Smells & 66 Refactorings). |
| `docs-spec-analyst` | Tier 2 (`sonnet` / `flash`) | Read-only Official Documentation, RFC/JEP, and OpenAPI specification analyst. |
| `code-explorer` | Tier 1 (`haiku` / `flash_lite`) | Read-only symbol index search, AST skeletons, Clean Architecture DAG metrics, and blast-radius mapping. |
| `release-hygiene-guardian` | Tier 1 (`haiku` / `flash_lite`) | Reactive frontend state invariants, PR hygiene, environment parity, and pre-flight CI shield. |

---

## 2. Orchestrator Two-Phase Efficiency Law
1. **Phase A (Planner — Thin Client)**:
   - Stay lightweight via symbol search, AST skeletons, and FastMCP tools.
   - Never run serial `read_file` sprees or multi-agent explorer committees.
2. **Phase B (Executor — Offload-First)**:
   - Route heavy code analysis, map-reduce, and triage to local MoE pools or frontier models.
   - Run AutoDream memory consolidation asynchronously.
3. **Phase C (Verification)**:
   - Validate with disjoint-family PoLL juries, pre-flight lint, and targeted tests.
