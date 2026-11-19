# Universal Senior Code Reviewer Guard & Pre-Flight CI Law (`senior-review-guard`)

When generating, reviewing, or modifying code, enforce the strict review criteria of the Senior Engineering Roster and the Pre-Flight CI Gate:

---

## 1. Core Backend & Clean Architecture Guard
1. **Zero AI Trace (No Inline Full Package Paths)**:
   - NEVER write full package paths in method bodies or signatures (e.g. `java.util.List`, `com.example.model.Foo`).
   - All classes and types MUST be imported cleanly at the top of the file.
2. **Single-Line Logs (Even if > 160 Characters)**:
   - All log statements MUST be formatted as a single line to prevent breaking centralized log parsers and grep pipelines.
   - Use structured `{}` placeholders; NEVER use string concatenation (`+`).
   - Downgrade high-frequency logs to `DEBUG` in production hot-paths.
3. **No "Log & Throw" Anti-Pattern**:
   - NEVER both log an exception and re-throw it in the same catch block. Either handle and log, or propagate cleanly to top-level exception handlers.
4. **No Artificial Line Splitting under 120 Characters**:
   - Do NOT split simple method invocations, constructors, or parameter lists across multiple lines if the statement fits comfortably within standard line budgets.
5. **Uncle Bob "Define Just Before Use" & Guard Clauses**:
   - Declare and compute variables only immediately before their actual usage. Never compute expensive values before early-exit `if` guards.
   - Guard Clauses: Invert conditional logic to avoid nested pyramid indentation. Maximum nesting depth is 1.
6. **Anti-Static Obsession (Dependency Injection First)**:
   - Do NOT create static utility classes/methods for core business logic in framework applications. Use constructor dependency injection.
7. **Modern Standard Library Idioms**:
   - Use standard utility libraries and modern idioms (`StringUtils.isNotBlank`, `CollectionUtils.isEmpty`, Java 21 Sequenced Collections `list.getFirst()`).
   - Replace complex nested null checks with functional `Optional` pipelines.
8. **No Redundant Invocations**:
   - Assign repeated method evaluations to local variables instead of re-evaluating inside loops or branches.

---

## 2. Systems Architecture, Concurrency & Security Guard
1. **Singleflight Concurrency & Principal Isolation**:
   - Cache keys and single-flight coalescing keys MUST incorporate the caller's authorization principal when responses depend on user credentials or permissions. Never coalesce across distinct security contexts.
2. **Fail-Closed Security Invariant**:
   - Missing authorization metadata, tenant tags, or authentication headers MUST deny access (`fail-closed`). Never fall through to `true` or permit access on empty/null metadata.
3. **Deep Immutability for Shared Caches**:
   - Objects stored in shared in-memory caches MUST be deeply immutable (records, defensive copies). Never expose public setters on cached response objects.
4. **End-to-End Deadlines vs Socket Timeouts**:
   - Enforce a single overall request deadline budget rather than repeating independent timeouts.
   - NEVER retry non-idempotent HTTP methods (`POST`, `PUT`) upon timeout.
5. **Reproducible Packaging & Target Architecture**:
   - Commit packaging scripts in repository (`package.json`, `Makefile`, `Cargo.toml`).
   - When using native binary bindings, explicitly pin target OS and architecture (`--os=linux --cpu=arm64`).
6. **OpenAPI & Contract Parity**:
   - Generated API documentation and examples MUST strictly derive from actual constants and enums—never hardcode divergent examples.

---

## 3. Frontend, Reactive State & TypeScript Guard
1. **Dynamic Prop Watchers**:
   - If an editor or composable initializes from a prop (`useEditor({ editable: !props.readonly })`), add an explicit watcher so dynamic updates do not desynchronize.
2. **Stable Table State on Network Failures**:
   - NEVER wipe UI table state (`records = []`) before a network request completes. Retain existing data until the response is received successfully to avoid flickering.
3. **Pagination Cursor Reset**:
   - Always invoke `resetPagination()` on refresh, search, delete, and record creation so cursor pointers remain in sync with fetched data.
4. **Filter Preservation during Search**:
   - Retain existing active status and date filters when the user executes a text search.
5. **Template Typecheck Helper Collisions**:
   - Avoid naming local computed refs with the same name as auto-imported utility functions, preventing typecheck collisions.
6. **Read-Only Form Submission Guard**:
   - In read-only mode, ensure form action buttons (Submit, Discard, Save) are explicitly hidden or disabled (`v-if="!isReadOnly"`).

---

## 4. Observability, Boundary & Identity Guard
1. **Identity Provider as Source of Truth**:
   - Retrieve authentication tokens directly from core identity providers; never invent ad-hoc tokens or bypass identity standards.
2. **Clean Microservice Boundaries**:
   - Keep worker-internal logic internal to the service; do not pollute public API contracts with worker-specific details.
3. **HTTP 40x / 50x Observability**:
   - Always log upstream HTTP error status codes (`40x`, `50x`) as explicit errors with endpoint context.
4. **Cache & Datastore FinOps**:
   - Do not decompress payload caches if the requested field is already resolved from index. Move heavy object creation out of hot loops.

---

## 5. Resilient Fallback & Code Hygiene Guard
1. **Generic Exception Fallback Catch**:
   - Always include a generic `catch (Exception e)` alongside specific HTTP/network catches to prevent internal runtime errors from masquerading as `403 Forbidden`.
2. **Typed Result Objects**:
   - Return structured domain objects (e.g. `DeactivationResult`, `OperationStatus`) instead of primitive boolean flags.
3. **Zero Dead Code & Clean DTOs**:
   - Remove unused variables, dead lines, and orphan methods before opening an MR.

---

## 6. Pre-Flight CI/CD Shield
Before pushing any commit or opening an MR, satisfy the 4 pre-flight gates:
1. **Gate 1: Fast-Forward Rebase Invariant**:
   - Always fetch target branch and rebase before pushing (`git fetch origin && git rebase origin/main`).
2. **Gate 2: Cross-Repo Dependency Order**:
   - Never commit a dependency version that has not yet been merged and published to package registries.
3. **Gate 3: Zero-Tolerance Local Compilation & Test Check**:
   - Java: `mvn test-compile` passes cleanly.
   - TypeScript: `npm run typecheck` (`tsc --noEmit`) and `npm run lint` report 0 violations.
   - Rust: `cargo check` and `cargo clippy` pass with 0 errors.
4. **Gate 4: Pre-Flight Lint Verification**:
   - Run `kaizen-kit check --pre-flight .` before pushing.
