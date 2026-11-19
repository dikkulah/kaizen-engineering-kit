# Pillar 3: Kaizen Role-Based Senior Review Standards & Jenkins 4-Gate CI Law

Enforced mechanically on every edit and pre-push check via `kaizen-kit check --pre-flight`:

## 1. Core Backend & Java 21 Clean Architecture Standard (`Core-Java-Guard`)
1. **Zero Inline Package Paths**: Never write inline full package paths (`java.util.List`, `com.kaizen.cloud.*`) in method signatures or bodies; import cleanly at the top of the file.
2. **Single-Line SLF4J Logs**: Keep all `LOG.info/warn/error` calls on a single line (even if `> 120` chars) for CloudWatch/grep parsing; use `{}` placeholders and never concatenate strings with `+`.
3. **No "Log & Throw" Anti-Pattern**: Never both log and re-throw an exception in the same `catch` block; either handle and log or propagate to the top-level handler.
4. **Define Just Before Use & Guard Clauses**: Declare variables immediately above their first usage (never above early-return guards). Maximum nesting depth `<= 1`.
5. **Spring DI & Java 21 Idioms**: Use constructor injection (`private final`, no `@Autowired` fields or static service utilities), `StringUtils` / `CollectionUtils` / `ObjectUtils`, and Java 21 `list.getFirst()` / `list.getLast()`.

## 2. Systems Concurrency, Singleflight & Fail-Closed Security Standard (`Concurrency-Security-Guard`)
1. **Principal-Isolated Singleflight**: Coalescing and cache keys MUST incorporate the caller's authorization principal when responses depend on user/tenant permissions.
2. **Fail-Closed Security Invariant**: Missing authorization headers, JWT claims, or tenant metadata MUST deny access (`401`/`403`)—never fall open to a default tenant ID on HTTP routes.
3. **Deep Cache Immutability & Deadlines**: Store only immutable objects (`record`, defensive collection copies) in shared caches; enforce end-to-end request deadlines and never retry non-idempotent `POST`/`PUT` calls on timeout.

## 3. Frontend Vue 3, TypeScript & Reactive State Standard (`Vue-State-Guard`)
1. **Stable Table State on Network Errors**: Never wipe table rows (`records.value = []`) before `await` completes; retain existing rows until the response succeeds.
2. **Dynamic Prop Watchers & Pagination Reset**: Add explicit `watch(() => props.readonly)` when initializing composables/editors from props; call `resetPagination()` on refresh/search/delete while preserving active status/date filters.
3. **Read-Only Action Guard**: Hide or disable Submit/Save/Discard buttons (`v-if="!isReadOnly"`) in read-only mode, and avoid naming local `computed` refs after auto-imported helpers (`isEmpty`).

## 4. Identity, Service Boundaries & FinOps Observability Standard (`FinOps-Observability-Guard`)
1. **Keycloak Source of Truth & Clean Boundaries**: Retrieve tokens strictly from Keycloak; keep worker/Lambda internals isolated from AAS/CloudGatewayApi domain models.
2. **Hot-Loop Log FinOps & Upstream Error Context**: Never emit `INFO` logs inside `for`/`while` hot loops (downgrade to `DEBUG` or aggregate); log upstream `4xx`/`5xx` status codes with explicit endpoint context.

## 5. Defensive Error Fallback & DTO Hygiene Standard (`Resilience-DTO-Guard`)
1. **No Swallowed Exceptions & Typed Results**: Never leave empty `catch` blocks; include a generic fallback catch where needed so runtime errors do not masquerade as `403 Forbidden`, return typed result objects over raw booleans, and remove dead DTO fields/methods.

## 6. Jenkins 4-Gate Pre-Flight CI Shield (`PRE-MR-JENKINS-SHIELD`)
1. **Gate 1 (Fast-Forward Rebase)**: Always run `git fetch origin master && git rebase origin/master` and verify `git merge-base --is-ancestor origin/master HEAD` (`gerrit/telemetry_dashboard-frontend_dev` for `unified-frontend`).
2. **Gate 2 (Cross-Repo Publish Order)**: Merge and publish shared libraries (`shared-utils`, `avro`, `security-api`) before bumping consumer `pom.xml` / `package.json`.
3. **Gate 3 (Local Compile & Lint)**: Pass `mvn test-compile`, `npm run typecheck && npm run lint`, or `cargo check && cargo clippy` with 0 errors.
4. **Gate 4 (CloudCompose Compose Parity)**: Keep `MASTER/docker-compose.yml`, `MASTER/conf/`, `env-*/`, and `build-*-zip.sh` synchronized before opening CloudCompose MRs.
