# Canonical Codex 06 — Effective Java 21, Project Loom, Spring Boot 3.4/4 & Vue 3 State Laws (`[EJ-*]`, `[SPRING-*]`, `[UI-*]`)

Authoritative synthesis of **Joshua Bloch's *Effective Java (3rd Ed.)*** modernized for **Java 21 LTS (`JEP 431, 440, 441, 444`)**, **Spring Boot 3.4 / 4.x**, **RFC 9457 (`ProblemDetail`)**, and **Vue 3 / TypeScript Reactive State Discipline**.

---

## 1. Joshua Bloch *Effective Java* — Modernized for Java 21 (`[EJ-01 .. EJ-15]`)

1. **`[EJ-01]` Static Factory Methods & Staged Builders (`Items 1 & 2`)**:
   - Prefer named static factories (`SliceWindow.ofHours(24)`, `AuthResult.denied(reason)`) over ambiguous overloaded constructors.
   - For value types with >4 parameters, provide a clean `Builder` on the `record` validating invariants inside the compact constructor (`public CustomerView { Objects.requireNonNull(homeId); }`).
2. **`[EJ-02]` Enforce Immutability & Java 21 `record`s (`Items 16, 17 & JEP 440`)**:
   - Every DTO, cache entry, event payload, and value object MUST be an immutable Java 21 `record`.
   - **Deep Immutability**: Inside a `record` compact constructor, defensively copy collections (`items = items == null ? List.of() : List.copyOf(items);`) so callers can never mutate cached state (`[SEC-CACHE-IMMUTABILITY]`)!
3. **`[EJ-03]` Favor Composition Over Inheritance & `sealed` Hierarchies (`Items 18, 19 & JEP 409/441`)**:
   - Never extend concrete service/controller base classes to reuse code—inject collaborators via constructor composition.
   - Model closed domain outcomes with **`sealed interface` + `record`s** (`public sealed interface FetchOutcome permits Hit, Miss, UpstreamFailure`) + exhaustive `switch` pattern matching (no `default` branch hiding unhandled new variants!).
4. **`[EJ-04]` Generics & Bounded Wildcards (`PECS` — `Items 26–31`)**:
   - Zero raw types (`List` without `<T>`). Apply **Producer-`extends`, Consumer-`super`** (`Collection<? extends Station>`) on reusable library/shared-utils APIs.
5. **`[EJ-05]` Check Parameters for Validity & Defensive Copies (`Items 49 & 50`)**:
   - Validate inputs at the public boundary (`StringUtils.isNotBlank`, `Objects.requireNonNull`, numeric bounds) before any downstream I/O or computation.
6. **`[EJ-06]` Return Empty Collections or `Optional`, Never `null` (`Items 54 & 55`)**:
   - Methods returning collections/maps MUST return `List.of()` / `Map.of()` / `Collections.emptyList()` (never `null`).
   - Use `Optional<T>` strictly as a **return type** for single-element queries—never as a `record` field, method parameter, or wrapper around a `List` (`Optional<List<T>>` is an anti-pattern!).
7. **`[EJ-07]` Java 21 Sequenced Collections (`JEP 431`) & Commons Idioms**:
   - Use `list.getFirst()` and `list.getLast()` instead of `list.get(0)` or `list.get(list.size() - 1)` (which throws `IndexOutOfBoundsException: -1` on empty lists vs clean `NoSuchElementException`, always guarded by `CollectionUtils.isNotEmpty(list)`!).
   - Prefer `CollectionUtils.emptyIfNull`, `MapUtils.emptyIfNull`, `StringUtils.defaultString`.
8. **`[EJ-08]` Judicious Streams & Formatting (`Items 45–48`)**:
   - Stream pipelines use pure side-effect-free lambdas (`Collectors.toUnmodifiableList()` / `.toList()`, `Collectors.groupingBy`). Never mutate external `ArrayList`s inside `.forEach(...)` when `.map(...).toList()` expresses the transformation purely!
9. **`[EJ-09]` Exception Translation & Failure Atomicity (`Items 73, 75, 76`)**:
   - Catch low-level HTTP/SDK/JDBC exceptions (`RestClientException`, `SdkException`) at the adapter boundary and translate into domain exceptions carrying endpoint/status context.
   - Always include a fallback `catch (Exception ex)` where specific catches would otherwise let unexpected `RuntimeException`s masquerade as `403 Forbidden` or raw `500` stack leaks!
10. **`[EJ-10]` Zero Ignored Exceptions & Zero Log-and-Throw (`Item 77`)**:
    - Never write empty `catch` blocks, and never `LOG.error(...)` + `throw` the same exception in the same `catch` block.

---

## 2. Project Loom Virtual Threads (`JEP 444`) & Spring Boot 3.4 / 4 Laws (`[SPRING-01 .. SPRING-08]`)

1. **`[SPRING-01]` Zero Carrier-Thread Pinning (`JEP 444` Critical Law)**:
   - On Java 21 (`spring.threads.virtual.enabled=true`), holding a `synchronized` monitor while executing blocking socket/HTTP/DB I/O **pins** the underlying OS carrier thread (`ForkJoinPool` starvation under burst load!).
   - **Resolution**: Replace `synchronized` blocks guarding I/O or cache-miss loaders with **`java.util.concurrent.locks.ReentrantLock`** (`lock.lock(); try { ... } finally { lock.unlock(); }`)!
2. **`[SPRING-02]` Principal-Isolated Singleflight Coalescing**:
   - TelemetryDashboard bursts 14+ concurrent slice requests for the same home. Coalesce concurrent cache misses using `ConcurrentHashMap<PrincipalScopedKey, CompletableFuture<Snapshot>>` (removing failed futures immediately via `whenComplete` so transient errors are never poisoned in the map!).
   - **Security Law**: `PrincipalScopedKey` MUST include `(principalSubjectOrRoleHash, tenantId, homeId, window)`—never coalesce across distinct users/tenants!
3. **`[SPRING-03]` Pooled `RestClient` + `Apache5HttpClient` & Single E2E Deadline**:
   - Never instantiate `new RestTemplate()` or `HttpClient.newHttpClient()` per request! Inject a shared `RestClient` backed by `HttpComponentsClientHttpRequestFactory` (`PoolingHttpClientConnectionManager` with explicit `maxConnTotal`, `maxConnPerRoute`, `connectTimeout`, `responseTimeout`).
   - Enforce a single end-to-end request deadline budget; **never** auto-retry non-idempotent `POST`/`PUT`/`PATCH` requests on read timeout!
4. **`[SPRING-04]` Pure Constructor Injection & Zero Static Business Logic**:
   - Zero `@Autowired` field/setter injection. Every `@Service` / `@RestController` uses `private final` fields + single constructor.
   - Zero `static` business logic helpers preventing mock/stub substitution.
5. **`[SPRING-05]` Core Java Review Hygiene (Zero AI Footprint)**:
   - **Zero inline full package paths** (`java.util.List`, `com.kaizen.cloud.*`) in method bodies, Javadocs, or signatures—import cleanly at top of file.
   - **Single-line SLF4J `LOG.*` statements** (even if >160 chars) using `{}` placeholders (zero `+` concatenation) so CloudWatch/Kibana log lines never split.
   - **Declare variables immediately above first use** (`[CC-G10]`), never above early-return `if` guards.
   - Cache repeated `Map.get(k)` or `SecurityContextHolder` lookups in a single `final` local variable.
6. **`[SPRING-06]` RFC 9457 `ProblemDetail` Error Contracts**:
   - Centralize HTTP exception mapping in `@RestControllerAdvice` returning standardized RFC 9457 `ProblemDetail` (`type`, `title`, `status`, `detail`, `instance` + trace correlation ID), never leaking internal class names or SQL/S3 paths to clients.
7. **`[SPRING-07]` OpenAPI 1:1 Slice Contract Parity**:
   - OpenAPI `@Schema` annotations, enum `allowableValues`, and examples MUST reference domain constants/enums directly. Verify spec diffs via `GenerateOpenApiSpecTest` -> `/v3/api-docs` and `kaizen-kit apidoc`.
8. **`[SPRING-08]` ArchUnit Automated Layer Guard**:
   - Enforce `controller -> service -> domain <- adapter/repository` inward dependency rules automatically in `mvn test` via ArchUnit (`layeredArchitecture().consideringAllDependencies()`).

---

## 3. Vue 3, TypeScript & TelemetryDashboard Frontend State Laws (`[UI-01 .. UI-07]`)

1. **`[UI-01]` Stable Table & Chart State on Network Errors (Zero UI Blanking)**:
   - NEVER wipe `records.value = []` before an `await api.fetch(...)` completes! Keep previous valid records displayed with a loading indicator until the new response succeeds; surface an error toast/banner on failure.
2. **`[UI-02]` Dynamic Prop Synchronization**:
   - Whenever a composable, form, or rich editor initializes state from `props` (`props.readonly`, `props.homeId`, `props.duration`), attach an explicit `watch(() => props.readonly, ...)` so dynamic prop updates never desynchronize UI state.
3. **`[UI-03]` Pagination Cursor & Filter Coherence**:
   - Call `resetPagination()` on every search, refresh, create, and delete action; preserve active status/date/band filters when executing text search.
4. **`[UI-04]` Read-Only Action Guard**:
   - Explicitly hide or disable mutating controls (`Submit`, `Save`, `Discard`, `Delete`, `Reboot`) via `v-if="!isReadOnly"` / `:disabled="isReadOnly"`.
5. **`[UI-05]` Zero Auto-Import Symbol Collisions (`vue-tsc`)**:
   - Never shadow auto-imported Lodash/VueUse helpers (`isEmpty`, `debounce`, `get`) with local `computed` ref names—causes template type-check regressions in `npm run typecheck`.
6. **`[UI-06]` W3C Credentialed CORS Contract**:
   - Credentialed browser requests (`credentials: 'include'` / `Authorization` header) FORBID wildcard `Access-Control-Allow-Origin: *` AND forbid duplicate comma-joined headers (`*, *` when both proxy/ALB and app inject CORS!). Emit **exactly one** validated origin + `Vary: Origin` at **one** authoritative layer.
7. **`[UI-07]` Gerrit Review Discipline (`kaizen-ui/unified-frontend`)**:
   - Source of truth is remote **`gerrit`**, target branch **`telemetry_dashboard-frontend_dev`** (`git push gerrit HEAD:refs/for/telemetry_dashboard-frontend_dev` with `Change-Id` trailer). Never push `unified-frontend` to GitLab mirror `origin` unless explicitly instructed.

