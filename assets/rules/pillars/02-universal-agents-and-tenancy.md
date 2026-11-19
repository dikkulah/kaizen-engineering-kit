# Pillar 2: Universal 12-Agent Matrix, Canonical Codices, 5-Stage E2E & AWS Tenancy Law

## 1. Authoritative 12-Agent Specialist Roster
Delegate focused tasks to `@<agent-name>` in Claude Code or via `kaizen-kit flow`:

| Specialist Agent | Default Model | Scope & Philosophical Mandate |
| :--- | :--- | :--- |
| `e2e-systems-visionary` | `opus` | Cross-repo 5-Stage E2E latency (p95), Kleppmann DDIA, Goldratt TOC, Singleflight (`14->1`), Multi-Tier Cache, EDP FinOps (`$`). |
| `code-explorer` | `haiku` | Read-only `lsp-query search/concept` + RAM Serena LSP (`:24280`), 0-token AST `skeleton`, `arch-audit` DAG metrics, cross-repo `blast-radius` (`<= 40` steps). |
| `docs-spec-analyst` | `sonnet` | Read-only 4-Channel Official Doc & Specification Analyst (7 Offline Codices -> `kaizen-kit docs fetch` -> MCP -> Local SDK Symbol Grounding). |
| `systems-architect` | `sonnet` | Single-Crate Rust `VectorCube` (`src/*`), Ousterhout Deep Modules, Evans DDD, Typestate, compile-time Avro/BSON, Graviton3 ARM64, ALB `gzip`. |
| `jvm-service-architect` | `sonnet` | Java 21 Loom Virtual Threads (`ReentrantLock`, `RestClient` + `Apache5HttpClient`), Bloch Records/Sealed types, RFC 9457, OpenAPI 1:1 parity. |
| `data-sql-architect` | `sonnet` | `VectorCube` time-bucket rollups, Avro/BSON telemetry lineage, set-theoretic `POPULATED_MATCH` vs `EMPTY_BOTH_200` parity (`kaizen-kit parity`). |
| `incident-rca-falsifier` | `opus` | HAR `Root=1-<hex>` epoch/trace CloudWatch correlation, Agans 9 Rules, `git log -S` forensics, Karl Popper mandatory Red-Green TDD falsification. |
| `release-hygiene-guardian` | `sonnet` | Vue 3 / TS state invariants, GitLab MR / Gerrit (`refs/for/telemetry_dashboard-frontend_dev`), Jenkins 4-Gate Pre-Flight & CloudCompose env parity. |
| `refactoring-specialist` | `sonnet` | Clean Code (`G1-G36`), Clean Architecture (`SOLID`, `REP/CCP/CRP`, `ADP/SDP/SAP`), Refactoring.Guru (`22 Smells` & `66 Refactorings`), `22 GoF` (`kaizen-kit arch-audit`). |
| `bedrock-worker` | `haiku` | Tier 1 AWS Bedrock (`eu-west-1`) mechanical guard-clause flattening, DTO/record boilerplate, and deterministic HAR/Avro scrubbing. |
| `bedrock-architect` | `sonnet` | Tier 2 AWS Bedrock (`eu-west-1`) Clean Architecture, Rust `VectorCube`, Java 21 Loom, Spring Boot, and BFF/OpenAPI contracts. |
| `bedrock-judge` | `opus` | Tier 3 AWS Bedrock (`eu-west-1`) Incident RCA, Popperian Red-Green TDD verification, Clean Architecture metrics & FinOps judge. |

- **7 Offline Canonical Codices (`<5ms`) & Live Docs**: Query via `kaizen-kit docs codex <ID-or-topic>` (`[CC-*]`, `[CA-*]`, `[RG-*]`, `[GOF-*]`, `[APOSD-*]`, `[EJ-*]`, `[AWS-*]`) or fetch official docs directly via `kaizen-kit docs fetch <shortcut-or-url>`.
- **Google ADK Domain Workflows & Unified EC2 Cloud-Brain (`kaizen-kit adk {list,inspect,mcp-matrix,map-reduce,run}`)**: `KaizenOndemandParityLoop`, `KaizenUcRcaPipeline`, `KaizenSeniorReviewSwarm`, `KaizenCloudComposeOpsParityAgent`, `KaizenOpenApiContractSyncAgent`, and `LlmArmyMapReduce` backed by non-secret 7-MCP `McpToolset` discovery and EC2 Cloud-Brain (`:8765` Gateway, `:8770` FastMCP, `:4000` LiteLLM, `:6070` Zoekt, `:18808` Code-RAG, `:24280` Serena).

## 2. 5-Stage Ondemand E2E & Zero-Synthetic-Telemetry Law
1. **5-Stage Value Chain**: `UI/Proxy -> Principal-Isolated Singleflight -> Bounded Parallel Fan-Out -> Single-Crate VectorCube Folding -> Multi-Tier Cache (OnceLock L1 RAM -> /tmp/{home}_{days}d.ondemand L2 -> S3 .zst L3 with mtime/LastModified TTL) + Gzip Base64 Framing (<1MB ALB)`.
2. **Zero Synthetic Telemetry (`ZERO-SYNTHETIC-TELEMETRY-HARDCODING`)**:
   - Never hardcode fixture SSIDs/MACs, static capability heuristics (`"80MHz"`, `unwrap_or(true)`), fake status history (`"in_sync"`, `"ETHERNET"`), or HAR byte constants. Compute every field dynamically from ingested Avro/BSON telemetry; emit `None`/`0`/`"UNKNOWN"` when absent.
   - Embed all Avro/BSON schemas at compile time in `src/codecs` (never fetch from S3 at runtime).

## 3. AWS Multi-Account Isolation (`SEC-AWS-TENANCY-ISOLATION`)
1. **41 Accounts 100% Read-Only (Mechanically Enforced)**: `kaizen-staging`, `kaizen-prod`, `production`, `testing`, `airdi-*`, and all operator profiles (`att`, `telia`, `dtag`, `bytel`, `telstra`, `nam`, `frontier`, `sky`, `vdf`, `telenor`, `vznsbx`) are strictly **READ-ONLY**. Only `--profile kaizen-u2` is in operational scope, and **every mutation in `u2` requires explicit prior user approval**.
2. **CloudWatch Account Mapping**:
   - **UCA (`staging.console` / `*-uca*` log groups)**: Stored in **`AWS_PROFILE=kaizen-prod`** (NOT `kaizen-staging`!), log groups `unified-cloud_gateway_api-uca*`, `aas-uca*`, `portal_edge_api-uca*`.
   - **UC Prod (`*-uc-prd*`)**: `AWS_PROFILE=kaizen-prod`. **U2 (`*-u2*`)**: `AWS_PROFILE=kaizen-u2`.
