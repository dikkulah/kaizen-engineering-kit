# Canonical Codex 01 — Clean Code Complete Heuristics & Smell Taxonomy (`[CC-*]`)

Authoritative engineering synthesis of **Robert C. Martin's *Clean Code: A Handbook of Agile Software Craftsmanship*** (Chapters 1–17), adapted to **Rust (2021/2024)**, **Java 21 (LTS)**, and **TypeScript / Vue 3**. Every rule carries a stable citation ID (`[CC-*]`) for deterministic code review and refactoring diagnostics.

---

## 1. Meaningful Names (`[CC-N1 .. CC-N7]`)

| ID | Canonical Principle | Smell / Violation Pattern | Idiomatic Rust / Java 21 / TS Resolution |
| :--- | :--- | :--- | :--- |
| **`[CC-N1]`** | **Choose Descriptive & Intention-Revealing Names** | `let d = 7;`, `int list1;`, `const res2 = fetch()` | `let window_days = 7;`, `List<StationMetrics> activeStations;`, `const topologySnapshot = await fetchTopology()` |
| **`[CC-N2]`** | **Choose Names at the Appropriate Level of Abstraction** | Exposing wire/storage implementation in domain APIs (`fetchAvroByteArray`, `redisHashKeyMap`) | Name by domain contract (`load_home_telemetry`, `SessionTokenStore`), hiding transport inside the adapter |
| **`[CC-N3]`** | **Use Standard Nomenclature Where Possible** | Inventing bespoke words (`MakerThing`, `WrapperBox`) when ubiquitous patterns exist | Use exact Ubiquitous Language + pattern roles (`HomeStateBuilder`, `CodecAdapter`, `TopologyProjection`) |
| **`[CC-N4]`** | **Unambiguous Names & Meaningful Distinctions** | Number-series (`a1`, `a2`) or noise suffixes (`DeviceData`, `DeviceInfo`, `DeviceObject`) | Distinguish by lifecycle/ownership (`RawDeviceFrame`, `ValidatedDeviceRecord`, `DeviceSummaryDto`) |
| **`[CC-N5]`** | **Long Names for Long Scopes, Short Names for Tiny Scopes** | 1-letter variables (`s`, `m`, `c`) spanning 40-line functions, or 45-char names inside a 1-line closure | Loop/closure of 1–3 lines may use `i` or `item`; module/class/struct fields and multi-line locals must use full domain nouns (`station_mac`, `channel_utilization`) |
| **`[CC-N6]`** | **Avoid Encodings & Cryptic Prefixes** | Hungarian notation (`strName`, `m_count`, `IServiceImpl`) or vowel-stripped abbreviations (`sta`, `thpt`, `trh`, `gw`, `cnt`, `dur`, `val`) | Rely on static type systems (`MacAddress`, `ThroughputMbps`, `Duration`); name interfaces as pure domain roles (`TelemetryRepository`) |
| **`[CC-N7]`** | **Names Should Describe Side Effects Honestly** | `get_or_init_connection()` called `get_connection()`, or `check_token()` silently mutating session cache | Separate or name explicitly: `ensure_warm_cache_populated()`, `validate_and_record_audit()` |

### Part-of-Speech Law
- **Classes, Structs, Records, Traits, Interfaces, Types**: Nouns or noun phrases (`MaterializedHomeState`, `PrincipalCacheKey`, `RssiWindowBucket`). Avoid vague verbs/managers (`Processor`, `Manager`, `Data`).
- **Functions & Methods**: Verbs or verb phrases (`fold_briefcase_event`, `compute_airtime_score`). Rust getters omit `get_` (`pub fn mac(&self) -> &MacAddress`); boolean predicates read naturally (`is_expired`, `has_mlo_link`, `can_steer`).

---

## 2. Functions & Control Flow (`[CC-F1 .. CC-F8]`)

| ID | Canonical Principle | Violation / Anti-Pattern | Deterministic Refactoring Move |
| :--- | :--- | :--- | :--- |
| **`[CC-F1]`** | **Too Many Arguments (`Arity <= 3`, Hard Cap `4`)** | `fn build_link(src: &str, dst: &str, band: u8, rssi: i32, phy: u32, chan: u8)` | Group cohesive arguments into a domain Value Object / Parameter Struct (`LinkObservation`) -> `[RG-RF40: Introduce Parameter Object]` |
| **`[CC-F2]`** | **Output Arguments** | Mutating a caller-supplied collection/struct solely as an output channel when a return value suffices (`fn append_errors(req, &mut errs)`) | Return owned/immutable result (`Result<ValidatedRequest, Vec<ValidationError>>`) or make the receiver `self` (`report.record_issue(...)`) |
| **`[CC-F3]`** | **Flag Arguments (`bool` / `boolean` Parameters)** | `pub fn render_topology(include_offline: bool, compact: bool)` — loudly proclaims the function does two things | Split into explicit entry points (`render_full_topology()`, `render_online_topology()`) or pass a domain enum (`TopologyFilter::OnlineOnly`) -> `[RG-RF43: Remove Flag Argument]` |
| **`[CC-F4]`** | **Dead Functions** | Unreachable private helpers, commented-out legacy handlers, unused feature branches | Delete immediately; Git preserves history (`[RG-SM16: Dead Code]`) |
| **`[CC-F5]`** | **Small & Single-Thing Discipline (`<= 35` Lines)** | 120-line God methods mixing parsing, validation, sorting, and serialization | Extract cohesive sub-steps until nothing further can be meaningfully extracted (`[RG-RF01: Extract Method]`) |
| **`[CC-F6]`** | **Single Level of Abstraction per Function (`SLAP`)** | High-level orchestration (`orchestrate_home_view`) inline-manipulating bitmasks (`flags & 0x04 != 0`) or HTTP header strings | Push low-level bit/string details into named predicates (`frame.is_steering_capable()`) |
| **`[CC-F7]`** | **The Stepdown Rule (Top-to-Bottom Newspaper Order)** | Reader must jump erratically up and down a 400-line file to trace execution | Place public entry points at the top, followed immediately by the level-1 helpers they call, then level-2 helpers |
| **`[CC-F8]`** | **Command-Query Separation (`CQS`)** | `if set_attribute("mode", "auto") { ... }` — mutates state AND returns a query boolean | Split state mutation (`command: set_mode(...) -> Result<(), Err>`) from state inspection (`query: mode(&self) -> Mode`) |

---

## 3. Comments (`[CC-C1 .. CC-C5]`)

1. **`[CC-C1]` Inappropriate Information**: Never put changelogs, author tags, Jira history narratives, or sprint trivia inside source comments—that belongs in Git history and commit messages.
2. **`[CC-C2]` Obsolete Comments**: A comment contradicting the code is worse than no comment. Update or delete comments whenever signatures or invariants change.
3. **`[CC-C3]` Redundant Comments**: Never write `// increments i` above `i += 1;` or `/** Gets the id */` above `getId()`.
4. **`[CC-C4]` Poorly Written / Apologetic Comments**: If you feel compelled to write a comment explaining *what* a convoluted block does, refactor the block (`Extract Variable` / `Extract Method`) until the code speaks for itself.
5. **`[CC-C5]` Commented-Out Code**: Strictly forbidden in commits and MRs. Delete commented-out code blocks unconditionally.
- **Legitimate Comments**: Module/file architectural headers (`//!`), public API docstrings explaining invariants/units/complexity, `SAFETY:` justifications on audited `unsafe` blocks, and explanations of non-obvious RFC/hardware protocol quirks.

---

## 4. Objects vs. Data Structures & The Law of Demeter (`[CC-OD1 .. CC-OD3]`)

1. **`[CC-OD1]` Antisymmetry of Objects and Data Structures**:
   - **Data Structures (DTOs / Records / Algebraic Enums)**: Expose data transparently (immutable fields) and have minimal behavior. Easy to add new functions without changing existing data structures (`match` exhaustiveness in Rust / Java 21 `sealed interface` + `record` pattern matching).
   - **Objects (Domain Services / Repositories / Aggregates)**: Hide internal data behind private state and expose behavior (`Trait` / `Interface`). Easy to add new implementations without changing existing callers.
2. **`[CC-OD2]` Forbidden Hybrid Structures**:
   - Half-object, half-data-structure monsters with public mutable fields, getters/setters for private state, AND domain methods mixed together. Pick one cleanly: immutable Value Record/DTO OR encapsulated Aggregate/Module.
3. **`[CC-OD3]` The Law of Demeter (Principle of Least Knowledge — Zero Train Wrecks)**:
   - A method $f$ of object $O$ may invoke methods only on: (1) $O$ itself, (2) parameters passed to $f$, (3) objects instantiated inside $f$, (4) direct components/fields of $O$.
   - **Violation (Train Wreck)**: `let zip = ctxt.get_options().get_scratch_dir().get_absolute_path();`
   - **Exception**: Pure immutable data transformations / fluent builders / iterators (`items.iter().filter(...).map(...).collect()`) do not violate encapsulation because they transform values rather than navigating mutable object internals.

---

## 5. Error Handling & Boundaries (`[CC-EH1 .. CC-EH6]`)

1. **`[CC-EH1]` Separate Error Handling from Happy-Path Logic**: Use Rust `?` + early `let ... else` guard clauses, or Java top-level `@RestControllerAdvice` (`RFC 9457 ProblemDetail`), so business logic is not obscured by nested `try/catch` blocks.
2. **`[CC-EH2]` Provide Rich Failure Context**: Every error must carry the operation, domain identifier (tenant/home/endpoint), and upstream status—never bare `"failed"` or swallowed `catch (Exception ignored) {}`.
3. **`[CC-EH3]` Define Exception/Error Classes in Terms of a Caller's Needs (Wrapper / ACL Pattern)**: Wrap third-party SDKs (AWS S3, DynamoDB, HTTP clients) behind a narrow port so 5 different vendor exceptions translate into 1 domain `StorageError` (`[EJ-Item73]`).
4. **`[CC-EH4]` Define the Normal Flow (Special Case / Empty Collection Pattern)**: If "no records in window" is a normal analytical outcome, return an empty slice/list (`Vec::new()`, `List.of()`) rather than throwing `NotFoundException` (`[APOSD-Ch10]`).
5. **`[CC-EH5]` Never Return `null` & Never Pass `null`**: Use `Option<T>` / `Optional<T>`, empty collections, or non-null validated constructors. Passing `null` into methods is an automatic defect.
6. **`[CC-EH6]` Zero Log-and-Throw Duplication**: Either handle and log at the boundary OR enrich and propagate—never log `ERROR` and rethrow the same exception up the stack (causes duplicate CloudWatch alarms).

---

## 6. Unit Tests (`[CC-UT1 .. CC-UT5]` — `F.I.R.S.T.`)

- **Fast (`F`)**: Millisecond execution; zero live network/sleep in unit tests.
- **Independent (`I`)**: Zero shared mutable static state or execution-order coupling between tests.
- **Repeatable (`R`)**: Deterministic clocks/seeds (`FixedClock`, injected timestamps) — runs identically offline, in WSL, and on Jenkins.
- **Self-Validating (`S`)**: Strict boolean/equality assertions (`POPULATED_MATCH` on concrete values, never `println!` eyeball inspection or vacuous `status == 200` on empty arrays).
- **Timely (`T`)**: Written *before* or alongside production code (Karl Popper Red-Green-Refactor falsification).
- **Clean Test Readability**: `Build-Operate-Check` (`Given-When-Then`) structure with domain-specific test builders so test intent is readable in `<10` lines.

---

## 7. Concurrency Cleanliness (`[CC-CON1 .. CC-CON5]`)

1. **`[CC-CON1]` Concurrency Defense Principle**: Separate concurrency coordination (`Singleflight`, `JoinSet`, `Semaphore`, `StructuredTaskScope`) from pure data transformation (`fold`, `project`). Pure folding functions must be 100% stateless/synchronous over owned or borrowed slices!
2. **`[CC-CON2]` Severely Limit the Scope of Shared Data**: Encapsulate shared caches inside `OnceLock<BoundedCache>` or thread-safe concurrent maps storing **deeply immutable** snapshots (`Arc<MaterializedHomeState>`, Java `record`).
3. **`[CC-CON3]` Use Copies / Immutable Snapshots Over Locks**: Read immutable `Arc<T>` snapshots lock-free; never mutate cached objects in place.
4. **`[CC-CON4]` Zero Blocking Inside Async / Virtual-Thread Pinning Traps**:
   - **Rust Tokio**: Never run synchronous file/network I/O or >1ms CPU loops directly on an async worker without `spawn_blocking` or bounded concurrency; never hold a `std::sync::MutexGuard` across `.await`.
   - **Java 21 Loom**: Never hold `synchronized` monitors across blocking socket/DB calls (pins the carrier OS thread); use `ReentrantLock`.
5. **`[CC-CON5]` Bounded Queues, Semaphores & Single E2E Deadlines**: Never spawn unbounded tasks (`tokio::spawn` inside an unbounded loop); enforce bounded `Semaphore` fan-out and propagate a single end-to-end deadline (`CancellationToken` / request budget).

---

## 8. Uncle Bob's Complete General Smell Checklist (`[CC-G1 .. CC-G36]`)

- **`[CC-G1]` Multiple Languages in One Source File**: Keep SQL/HTML/Bash/Regex templates cleanly modularized rather than tangled inline strings.
- **`[CC-G2]` Obvious Behavior Is Unimplemented**: Principle of Least Astonishment—if `parse_duration("24h")` works, `"1d"` or case-sensitivity behavior must follow documented contract consistently.
- **`[CC-G3]` Incorrect Behavior at the Boundaries**: Always test `0`, `1`, empty slice, max window, leap/epoch boundary, `NaN`/`0`-divisor, and `1 MB` payload edge cases!
- **`[CC-G4]` Overridden Safeties**: Never bypass `clippy`, `@SuppressWarnings`, `--no-verify`, or TLS/auth checks to "make it green".
- **`[CC-G5]` Duplication (`DRY` — Every Piece of Knowledge Must Have a Single Authoritative Representation)**: Eliminate both verbatim copy-paste AND structural switch/if-else chains repeated across modules.
- **`[CC-G6]` Code at Wrong Level of Abstraction**: Constants, defaults, or transport concerns placed in base traits/domain entities instead of concrete adapters.
- **`[CC-G7]` Base Classes Depending on Their Derivatives**: Parent types must have zero knowledge of child variants.
- **`[CC-G8]` Too Much Information (Wide Interfaces)**: Keep `pub` visibility minimal (`pub(crate)` / package-private by default; narrow module facades).
- **`[CC-G9]` Dead Code**: Unreachable `match`/`if` arms, unused DTO fields, orphan helpers.
- **`[CC-G10]` Vertical Separation**: Declare local variables immediately above their first usage—never hoist 10 declarations to the top of a function before early-return guards!
- **`[CC-G11]` Inconsistency**: If one controller uses `resolve_tenant_scope()`, do not write `extract_org_context()` in the next file for the identical concept.
- **`[CC-G12]` Clutter**: Default constructors doing nothing, unused imports, redundant temp variables.
- **`[CC-G13]` Artificial Coupling**: Putting unrelated `enum`s, constants, or utility functions inside a domain class just because it was "convenient".
- **`[CC-G14]` Feature Envy**: Method calculating values almost exclusively from another struct's getters -> move the method onto that struct (`[RG-RF15: Move Method]`).
- **`[CC-G15]` Selector Arguments**: Boolean, integer, or enum arguments passed solely to choose internal behavior paths -> split the function.
- **`[CC-G16]` Obscured Intent**: Clever one-liners, nested ternaries, magic bit shifts, and cryptic lambdas.
- **`[CC-G17]` Misplaced Responsibility**: Placing formatting in a repository or HTTP status mapping inside a domain calculator (Principle of Least Surprise).
- **`[CC-G18]` Inappropriate Static**: Static business methods in Spring/DI services that cannot be polymorphically substituted or unit-tested cleanly (pure mathematical functions without I/O are the sole exception).
- **`[CC-G19]` Use Explanatory Variables**: Break complex boolean conditions or math formulas into self-documenting intermediate immutable bindings (`let is_band_steering_eligible = ...;`).
- **`[CC-G20]` Function Names Should Say What They Do**: `window.add(5)` (5 milliseconds? days? items?) -> `window.extend_by_days(5)`.
- **`[CC-G21]` Understand the Algorithm**: Never stop at "the test happened to pass" via trial-and-error nudging; know *why* every branch is mathematically sound.
- **`[CC-G22]` Make Logical Dependencies Physical**: If Module B assumes Module A writes timestamps in milliseconds, export a typed `EpochMillis` NewType/constant from Module A rather than silently assuming `1000` in Module B!
- **`[CC-G23]` Prefer Polymorphism / Exhaustive Algebraic Match to Repeated `If/Else` or `Switch/Case`**: Dispatch once at the type boundary.
- **`[CC-G24]` Follow Standard Conventions**: Rustfmt + Clippy pedantic, Java 21 + Checkstyle, ESLint + Prettier + `vue-tsc`.
- **`[CC-G25]` Replace Magic Numbers with Named Constants**: Zero raw `86400`, `1_048_576`, `-75`, or `"24h"` literals floating in logic (`SECONDS_PER_DAY`, `ALB_MAX_PAYLOAD_BYTES`, `WEAK_RSSI_DBM_THRESHOLD`).
- **`[CC-G26]` Be Precise**: Use exact types (`Decimal` / integer cents for money, `u64` / `Instant` for monotonic time, explicit timezone `UTC` timestamps, deterministic sorting when order matters).
- **`[CC-G27]` Structure Over Convention**: Enforce architectural rules via compiler types, `sealed` hierarchies, visibility modifiers, and `arch-audit` AST checks rather than prose comments.
- **`[CC-G28]` Encapsulate Conditionals**: Extract `if timer.has_expired() && ! timer.is_recurrent()` into `if should_be_deleted(&timer)`.
- **`[CC-G29]` Avoid Negative Conditionals**: Prefer `if buffer.should_compact()` over `if !buffer.should_not_compact()`.
- **`[CC-G30]` Functions Should Do One Thing**: If a function has sections (parse -> validate -> enrich -> serialize), each section is a separate helper function.
- **`[CC-G31]` Hidden Temporal Couplings**: If `open()`, `parse()`, `close()` must be called in exact sequence, enforce it via **Typestate** (`Unopened -> Opened -> Parsed`) or a scoped closure/RAII guard so out-of-order calls fail to compile!
- **`[CC-G32]` Don't Be Arbitrary**: Package/module structure must follow domain architecture, never ad-hoc `misc/` or `helpers2/` buckets.
- **`[CC-G33]` Encapsulate Boundary Conditions**: Put `+ 1` / `- 1` slice/window boundary arithmetic in one named variable (`let next_level = level + 1;`) rather than scattering `level + 1` five times.
- **`[CC-G34]` Functions Should Descend Only One Level of Abstraction (`SLAP`)**: Mixing domain policy with wire delimiter parsing is an immediate refactor trigger.
- **`[CC-G35]` Keep Configurable Data at High Levels**: Timeouts, S3 bucket names, TTLs, and thresholds belong in top-level typed `Config` structs injected downwards—never buried `env::var(...)` calls deep inside domain functions!
- **`[CC-G36]` Avoid Transitive Navigation (Law of Demeter)**: Talk only to immediate collaborators; never reach across `A -> B -> C -> D`.

