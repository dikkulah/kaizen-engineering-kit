# Canonical Codex 03 — Refactoring.Guru Complete 22 Smells & 66 Refactorings (`[RG-*]`)

Authoritative reference catalog of **Refactoring.Guru** and **Martin Fowler's *Refactoring (2nd Edition)***: complete symptom diagnosis for all **22 Code Smells (`[RG-SM01 .. RG-SM22]`)** and mechanical transformation recipes for all **66 Refactorings (`[RG-RF01 .. RG-RF66]`)** across **Rust**, **Java 21**, and **TypeScript / Vue 3**.

---

## Part I — Complete 22 Code Smells Diagnostic Matrix (`[RG-SM01 .. RG-SM22]`)

### Family 1: Bloaters (Code swollen until impossible to reason about)
1. **`[RG-SM01]` Long Method**: Function > 35 lines or Cognitive Complexity > 10.
   - *Cure*: `RG-RF01` (Extract Method), `RG-RF03` (Replace Temp with Query), `RG-RF39` (Introduce Parameter Object), `RG-RF40` (Preserve Whole Object), `RG-RF08` (Replace Method with Method Object), `RG-RF29` (Decompose Conditional).
2. **`[RG-SM02]` Large Class / God Module**: Struct/Class > 300 lines, > 10 fields, or serving > 1 actor (`[CA-SRP]`).
   - *Cure*: `RG-RF12` (Extract Class), `RG-RF55` (Extract Subclass), `RG-RF57` (Extract Interface / Trait), `RG-RF18` (Duplicate Observed Data / Separate Presenter).
3. **`[RG-SM03]` Primitive Obsession**: Raw `String`, `u64`, `i32`, `Map<String, Object>` representing MACs, RSSI dBm, tenant IDs, bandwidth, or state codes.
   - *Cure*: `RG-RF20` (Replace Data Value with Object / Rust `NewType` / Java `record`), `RG-RF26` (Replace Type Code with Class/Enum), `RG-RF27` (Replace Type Code with Subclasses/Sealed), `RG-RF28` (Replace Type Code with State/Strategy/Typestate), `RG-RF21` (Replace Array/Tuple with Object).
4. **`[RG-SM04]` Long Parameter List**: Arity > 4 parameters.
   - *Cure*: `RG-RF39` (Introduce Parameter Object), `RG-RF40` (Preserve Whole Object), `RG-RF38` (Replace Parameter with Method Call).
5. **`[RG-SM05]` Data Clumps**: Identical 3+ fields or parameter groups (`tenant_id, home_id, window_days`) traveling together across multiple signatures.
   - *Cure*: `RG-RF12` (Extract Class -> `HomeQueryScope`), `RG-RF39` (Introduce Parameter Object).

### Family 2: Object-Orientation & Type-System Abusers
6. **`[RG-SM06]` Switch / Repeated Match Statements**: Identical `switch`/`match`/`if-else` type-code dispatch copy-pasted across multiple functions.
   - *Cure*: `RG-RF34` (Replace Conditional with Polymorphism / Exhaustive Trait or Enum Method), `RG-RF37` (Replace Parameter with Explicit Methods), `RG-RF35` (Introduce Null/Special-Case Object).
7. **`[RG-SM07]` Temporary Field**: Struct/Class fields that are `Option<T>` / `null` 80% of the time and populated only during one specific phase/algorithm.
   - *Cure*: `RG-RF12` (Extract Class / Phase-Specific Typestate `UnfoldedHome -> MaterializedHome`), `RG-RF08` (Replace Method with Method Object), `RG-RF35` (Introduce Null Object).
8. **`[RG-SM08]` Refused Bequest**: Subclass/impl inherits methods or state it doesn't support (throwing `UnsupportedOperationException` or `unimplemented!()`).
   - *Cure*: `RG-RF65` (Replace Inheritance with Delegation — Bloch Item 18), `RG-RF56` (Extract Superclass / Narrower Trait `ISP`).
9. **`[RG-SM09]` Alternative Classes with Different Interfaces**: Two modules perform equivalent operations under mismatched names (`DeviceFetcher::grab_macs` vs `NodeLoader::read_identifiers`).
   - *Cure*: `RG-RF37`/`RG-RF36` (Rename Method + Add/Parametrize), `RG-RF10` (Move Method), `RG-RF56` (Extract Shared Trait/Interface).

### Family 3: Change Preventers (High friction & blast-radius amplification)
10. **`[RG-SM10]` Divergent Change**: One file/class gets edited every time S3 storage changes, every time Avro schema changes, AND every time JSON DTO formatting changes (`[CA-SRP]` violation).
    - *Cure*: `RG-RF12` (Extract Class along each axis of change: `codec.rs` | `fold.rs` | `projection.rs`).
11. **`[RG-SM11]` Shotgun Surgery**: Adding 1 field or changing 1 business rule requires micro-edits across 12 scattered files (`[CA-CCP]` violation).
    - *Cure*: `RG-RF10` (Move Method), `RG-RF11` (Move Field), `RG-RF13` (Inline Class) to consolidate scattered responsibilities into one Deep Module (`[APOSD-Ch4]`).
12. **`[RG-SM12]` Parallel Inheritance Hierarchies**: Adding a new `TelemetryEvent` variant forces adding a matching `TelemetryParser` subclass in a separate hierarchy.
    - *Cure*: `RG-RF10` (Move Method) & `RG-RF11` (Move Field) — collapse one hierarchy into the other (in Rust/Java 21, co-locate decoding/dispatch on a single `sealed` hierarchy or macro-registered codec table).

### Family 4: Dispensables (Zero-value noise that obscures signal — Lean *Muda*)
13. **`[RG-SM13]` Comments (Deodorant Comments)**: Multi-line comments explaining tangled conditionals.
    - *Cure*: `RG-RF05` (Extract Explaining Variable), `RG-RF01` (Extract Method), `RG-RF36` (Rename Method), `RG-RF36b` (Introduce Assertion).
14. **`[RG-SM14]` Duplicate Code**: Identical or structurally isomorphic logic blocks in 2+ places.
    - *Cure*: `RG-RF01` (Extract Method), `RG-RF51` (Pull Up Method), `RG-RF62` (Form Template Method / Higher-Order Closure), `RG-RF09` (Substitute Algorithm).
15. **`[RG-SM15]` Lazy Class / Shallow Wrapper**: Pass-through class/module whose interface is as complex as its body (`[APOSD]` *Classitis*).
    - *Cure*: `RG-RF13` (Inline Class), `RG-RF59` (Collapse Hierarchy).
16. **`[RG-SM16]` Data Class (Anemic Domain Model)**: Struct/class with dumb getters/setters while external `ServiceUtils` manipulate its fields everywhere.
    - *Cure*: `RG-RF24` (Encapsulate Field), `RG-RF25` (Encapsulate Collection), `RG-RF10` (Move Method — move domain invariants onto the type itself!), `RG-RF42` (Remove Setting Method -> Immutable `record`/struct).
17. **`[RG-SM17]` Dead Code**: Unused parameters, unreachable `else` branches, unreferenced structs/methods, stale feature flags.
    - *Cure*: Delete unconditionally (`RG-RF37b` Remove Parameter, `RG-RF59` Collapse Hierarchy).
18. **`[RG-SM18]` Speculative Generality**: Generic `<T, U, V>` traits with only 1 concrete type, hooks "for future plugins", unused config knobs.
    - *Cure*: `RG-RF59` (Collapse Hierarchy), `RG-RF13` (Inline Class), `RG-RF02` (Inline Method), `RG-RF37b` (Remove Parameter).

### Family 5: Couplers (Information leakage & tight entanglement)
19. **`[RG-SM19]` Feature Envy**: Function in Module A touches 6 fields/getters of Struct B and 0–1 of its own.
    - *Cure*: `RG-RF10` (Move Method onto B), or `RG-RF01` (Extract envious slice) + `RG-RF10`.
20. **`[RG-SM20]` Inappropriate Intimacy**: Two modules reaching directly into `pub` internal maps/buffers of each other or bidirectional cyclic references.
    - *Cure*: `RG-RF10`/`RG-RF11` (Move Method/Field), `RG-RF19` (Change Bidirectional Association to Unidirectional), `RG-RF12` (Extract shared Value Object), `RG-RF14` (Hide Delegate).
21. **`[RG-SM21]` Message Chains (Train Wrecks)**: `a.b().c().d().value()` coupling caller to the entire navigation chain.
    - *Cure*: `RG-RF14` (Hide Delegate), or `RG-RF01` + `RG-RF10` to push computation to the owner.
22. **`[RG-SM22]` Middle Man**: Class where >50% of methods are 1-line delegations to an inner field without adding policy, caching, or boundary translation.
    - *Cure*: `RG-RF15` (Remove Middle Man) or `RG-RF13` (Inline Class).

---

## Part II — Complete 66 Refactoring Mechanics (`[RG-RF01 .. RG-RF66]`)

### Category A: Composing Methods (`RG-RF01 .. RG-RF09`)
1. **`[RG-RF01]` Extract Method**: Isolate a cohesive fragment into a verb-named helper; pass read-only inputs by `&T` reference and return computed values cleanly.
2. **`[RG-RF02]` Inline Method**: When a helper's body is clearer than its name (or needless indirection), inline it and delete the wrapper.
3. **`[RG-RF03]` Replace Temp with Query**: Replace mutable/cluttered intermediate temps with pure idempotent helper calls (`self.base_price()`) when cost is $O(1)$.
4. **`[RG-RF04]` Split Temporary Variable**: Never reuse one mutable `let mut temp` for two distinct meanings in the same function; bind separate immutable `let` variables!
5. **`[RG-RF05]` Extract Variable (Introduce Explaining Variable)**: Break `if (platform.contains("MAC") && browser.contains("IE") && resize > 0)` into named booleans.
6. **`[RG-RF06]` Inline Temp**: Eliminate `let base = order.base_price(); return base > 1000;` -> `order.base_price() > 1000`.
7. **`[RG-RF07]` Remove Assignments to Parameters**: Never mutate incoming function parameters (`mut param`); bind a fresh local variable if transformation is needed.
8. **`[RG-RF08]` Replace Method with Method Object**: When a 150-line algorithm has 8 intertwined local variables blocking `Extract Method`, turn the algorithm into a dedicated short-lived struct (`TopologyFolder { ... }`) where locals become struct fields and steps become small private methods!
9. **`[RG-RF09]` Substitute Algorithm**: Replace a convoluted multi-pass $O(N^2)$ loop with a single deterministic `HashMap`/`BTreeMap` index + single-pass fold ($O(N)$).

### Category B: Moving Features Between Objects (`RG-RF10 .. RG-RF17`)
10. **`[RG-RF10]` Move Method**: Relocate a method onto the struct/class whose data it uses most heavily.
11. **`[RG-RF11]` Move Field**: Relocate a field to the struct that reads/updates it most frequently.
12. **`[RG-RF12]` Extract Class**: Split a multi-actor struct into two focused structs with single responsibilities.
13. **`[RG-RF13]` Inline Class**: Fold a 15-line anemic helper class back into its sole consumer.
14. **`[RG-RF14]` Hide Delegate**: Provide a delegating method on the immediate collaborator to sever Law of Demeter train wrecks (`home.gateway_firmware()` instead of `home.gateway().device_info().firmware()`).
15. **`[RG-RF15]` Remove Middle Man**: Delete trivial pass-through methods when delegation bloat outweighs encapsulation value.
16. **`[RG-RF16]` Introduce Foreign Method**: When an external crate/JDK type lacks one small pure helper (`Instant`, `HeaderMap`), create a clean pure helper taking `&ExternalType` as its first parameter.
17. **`[RG-RF17]` Introduce Local Extension (Rust Extension Trait / Newtype Wrapper)**: Group multiple methods on an external type into a domain `trait HeaderMapExt` or NewType wrapper (`TenantClaims(JwtClaims)`).

### Category C: Organizing Data (`RG-RF18 .. RG-RF32`)
18. **`[RG-RF18]` Duplicate Observed Data (Separate Domain State from UI/DTO State)**: Keep pure domain models decoupled from presentation/Vue reactive refs via explicit unidirectional projection.
19. **`[RG-RF19]` Change Bidirectional Association to Unidirectional**: Eliminate parent<->child cycles (`Rc<RefCell<...>>` / cyclic JPA refs); store typed IDs (`NodeId`) for back-references!
20. **`[RG-RF20]` Replace Data Value with Object (`NewType` / `record`)**: Turn `String` -> `MacAddress([u8; 6])`, `i32` -> `RssiDbm(i16)` with construction-time validation.
21. **`[RG-RF21]` Replace Array / Anonymous Tuple with Object**: Replace `(String, u64, i32, bool)` tuples with named structs (`StationLinkSample { mac, rx_bytes, rssi_dbm, is_mlo }`).
22. **`[RG-RF22]` Change Value to Reference**: Deduplicate repeated heavy immutable metadata (e.g. shared vendor OUI table or cached `Arc<MaterializedHomeState>`) via `Arc<T>` or interned IDs.
23. **`[RG-RF23]` Change Reference to Value**: Convert needlessly shared mutable entities into small `Copy`/`Clone` immutable Value Objects (`record` / `#[derive(Clone, Copy, PartialEq, Eq)]`).
24. **`[RG-RF24]` Encapsulate Field**: Make `pub` mutable struct/class fields private; enforce invariants through constructors and domain methods.
25. **`[RG-RF25]` Encapsulate Collection**: Never return `&mut Vec<T>` or mutable `java.util.List<T>` from a getter! Return `&[T]` (Rust slice) or `List.copyOf(...)` / `Collections.unmodifiableList(...)` (Java).
26. **`[RG-RF26]` Replace Magic Number with Symbolic Constant**: `const ALB_GZIP_CEILING_BYTES: usize = 1_000_000;`.
27. **`[RG-RF27]` Replace Type Code with Class / Algebraic Enum**: Replace `const RADIO_2G: u8 = 0; const RADIO_5G: u8 = 1;` with `pub enum RadioBand { Band2Ghz, Band5Ghz, Band6Ghz }`.
28. **`[RG-RF28]` Replace Type Code with Subclasses / Java 21 `sealed interface`**: Model closed domain variants (`sealed interface LinkTopology permits WifiBackhaul, EthernetBackhaul, MloBackhaul`).
29. **`[RG-RF29b]` Replace Type Code with State / Strategy / Rust Typestate**: Encode lifecycle transitions at compile time (`Builder<Unvalidated> -> Builder<Validated>`).
30. **`[RG-RF30]` Replace Subclass with Fields**: If subclasses differ ONLY by constant return values (`getBandCode() -> "5G"`), collapse them into a single struct/enum with fields!
31. **`[RG-RF31]` Self Encapsulate Field**: Route complex lazy-computed or unit-scaled field access through a single internal accessor.
32. **`[RG-RF32]` Change Unidirectional Association to Bidirectional**: Apply *only* when proven $O(1)$ reverse lookup is required—prefer an external index map inside the Aggregate Root first!

### Category D: Simplifying Conditional Expressions (`RG-RF33 .. RG-RF40b`)
33. **`[RG-RF33]` Decompose Conditional**: Turn `if is_outside_window(ts, start, end)` condition, `then` branch, and `else` branch each into 1-line intention-revealing helper calls!
34. **`[RG-RF34]` Consolidate Conditional Expression**: Combine consecutive guards returning the exact same outcome (`if a.is_empty() || b.is_expired() || !c.is_authorized() { return Err(...); }`) into a single named predicate (`if !is_request_eligible(...)`).
35. **`[RG-RF35]` Consolidate Duplicate Conditional Fragments**: Hoist identical statements appearing at the end (or start) of both `if` and `else` branches outside the conditional.
36. **`[RG-RF36c]` Remove Control Flag**: Eliminate `let mut found = false; for x in items { if !found { ... found = true; } }` — use `return`, `break`, or `.iter().find(...)`!
37. **`[RG-RF37c]` Replace Nested Conditional with Guard Clauses (THE GOLDEN PYRAMID FLATTENER)**:
    - Invert every precondition check to exit immediately (`let Some(x) = opt else { return ...; };`, `if !valid { continue; }`).
    - Keep the happy path completely unindented (`depth <= 1`)!
38. **`[RG-RF38c]` Replace Conditional with Polymorphism / Exhaustive Match**: Move variant-specific math onto the `enum` / `sealed interface` implementation so callers never branch on type tags.
39. **`[RG-RF39c]` Introduce Null Object / Special Case**: Represent `Guest / UnknownDevice / EmptyTelemetryWindow` as a first-class domain value implementing neutral defaults (`0` throughput, empty slices) rather than `null` checks everywhere.
40. **`[RG-RF40b]` Introduce Assertion (`debug_assert!` / Type Invariant)**: Document mathematical invariants explicitly where preconditions are guaranteed by prior construction.

### Category E: Simplifying Method Calls (`RG-RF41 .. RG-RF54`)
41. **`[RG-RF41]` Rename Method**: Rename immediately when a method name forces callers to read its body to know what it does.
42. **`[RG-RF42b]` Add Parameter**: Add *only* when the object genuinely lacks context and Parameter Object (`RG-RF47`) is not yet warranted.
43. **`[RG-RF43b]` Remove Parameter**: Strip unused parameters across trait/impl/call sites immediately.
44. **`[RG-RF44]` Separate Query from Modifier (`CQS`)**: Split `pop_and_validate_next()` or `get_and_increment()` (unless atomic lock-free primitive) into distinct query + command functions.
45. **`[RG-RF45]` Parameterize Method**: Collapse `five_percent_raise()` and `ten_percent_raise()` into `apply_raise(Percent)`.
46. **`[RG-RF46]` Replace Parameter with Explicit Methods (Remove Flag Argument)**: Split `set_dimension(name: &str, val: u32)` or `fetch(use_cache: bool)` into `set_width(u32)` / `set_height(u32)` and `fetch_cached()` / `fetch_live()`.
47. **`[RG-RF47]` Preserve Whole Object**: Pass `&StationRecord` instead of extracting 5 separate fields (`s.mac, s.rssi, s.band, s.tx, s.rx`) at the call site.
48. **`[RG-RF48]` Replace Parameter with Method Call**: If callee can already compute `b` cheaply from `a`, pass only `a` instead of `(a, compute_b(&a))`.
49. **`[RG-RF49]` Introduce Parameter Object**: Bundle cohesive parameters `(tenant_id, home_id, window_days, trace_id)` into `RequestContext`.
50. **`[RG-RF50]` Remove Setting Method**: Delete `set_*` methods on DTOs, cache entries, and Value Objects so fields are initialized once at construction!
51. **`[RG-RF51b]` Hide Method**: Downgrade `pub` to `pub(crate)` or `private` whenever no external module calls it.
52. **`[RG-RF52]` Replace Constructor with Factory Method**: Use named constructors (`WindowRange::last_hours(24)`, `WindowRange::last_days(7)`, `ProblemDetail.forStatusAndDetail(...)`) instead of ambiguous overloaded constructors.
53. **`[RG-RF53]` Replace Error Code with Exception / Typed `Result<T, E>`**: Never return `-1`, `0`, or `"ERROR"` magic sentinel values inside success types!
54. **`[RG-RF54]` Replace Exception with Test (Precheck)**: Never use `try { Integer.parseInt(s) } catch` for normal control flow on expected user input—validate or return `Option<u32>` (`s.parse::<u32>().ok()`).

### Category F: Dealing with Generalization (`RG-RF55 .. RG-RF66`)
55. **`[RG-RF55]` Pull Up Field**: Consolidate duplicated state into shared composite struct.
56. **`[RG-RF56]` Pull Up Method**: Consolidate identical implementations into default `trait`/`interface` method or shared component.
57. **`[RG-RF57]` Pull Up Constructor Body**: Share base initialization via delegation.
58. **`[RG-RF58]` Push Down Method**: Move behavior implemented for only 1 subtype down into that specific variant.
59. **`[RG-RF59b]` Push Down Field**: Move fields used by only 1 subtype into that specific variant (`enum` variant payload in Rust / specific `record` in Java 21 `sealed interface` — eliminates `Temporary Field` at compile time!).
60. **`[RG-RF60]` Extract Subclass / Variant**: Split optional specialized state into a distinct `enum` variant or `sealed` record.
61. **`[RG-RF61]` Extract Superclass / Shared Component**: Factor shared data + behavior across sibling types.
62. **`[RG-RF62]` Extract Interface / Narrow Role Trait (`ISP`)**: Extract a minimal 1–3 method `trait` (`Clock`, `BlobReader`, `NodeDirectory`) for testability and `DIP` cycle breaking.
63. **`[RG-RF63]` Collapse Hierarchy**: Merge needless base/derived splits when they carry no behavioral divergence.
64. **`[RG-RF64]` Form Template Method (or Functional Skeleton + Closure)**: Capture invariant workflow steps once (e.g. `with_telemetry_span_and_gzip(req, |ctx| ...)`), passing only the varying projection closure!
65. **`[RG-RF65]` Replace Inheritance with Delegation (Bloch Item 18)**: Favor `struct CachedRepository<R> { inner: R, cache: Cache }` composition over fragile `extends` inheritance hierarchies!
66. **`[RG-RF66]` Replace Delegation with Trait/Interface Implementation (Only When True `IS-A` Holds)**: Expose trait directly when wrapper genuinely satisfies 100% of the Liskov contract (`[CA-LSP]`).

---

## Part III — Safe Refactoring Execution Protocol (Zero-Regression Law)

1. **Precondition Gate**: Never combine behavioral feature changes and structural refactoring in the same blind edit. Verify baseline unit tests + `kaizen-kit arch-audit` first.
2. **Atomic Mechanics**: Apply one catalog move (`[RG-RF*]`) at a time, keeping the project compilable (`cargo check` / `mvn test-compile` / `vue-tsc --noEmit`) at every checkpoint.
3. **Wire-Contract Invariance**: Public HTTP paths, JSON field names/casings, OpenAPI schemas, and Avro/BSON field mappings MUST remain 100% unchanged unless explicitly migrating contracts.
4. **Postcondition Proof**: Re-run `kaizen-kit check` (`arch-audit` + `clean-code` + `provenance` + `senior-lint`) + unit tests + `POPULATED_MATCH` parity when touching endpoint paths.

