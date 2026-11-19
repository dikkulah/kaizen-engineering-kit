# Canonical Codex 04 — 22 GoF Design Patterns: Idiomatic Rust, Java 21, Vue 3 & Anti-Pattern Traps (`[GOF-*]`)

Authoritative synthesis of **Refactoring.Guru's 22 Gang-of-Four Design Patterns**, modernized so agents never write 1994 Java/C++ boilerplate where **Rust Algebraic Types / Typestate**, **Java 21 Sealed Records / Virtual Threads**, or **Vue 3 Composables** express the pattern in 1/5th the code—and never fall into *Speculative Generality* (`[RG-SM18]`).

---

## 1. Creational Patterns (`[GOF-C1 .. GOF-C5]`)

| ID & Pattern | Core Problem Solved | Idiomatic Rust (2021/2024) | Idiomatic Java 21 / Spring | Anti-Pattern Trap (When NOT to Use) |
| :--- | :--- | :--- | :--- | :--- |
| **`[GOF-C1]` Factory Method** | Decouple construction validation/dispatch from callers | Named associated functions (`Window::from_hours(24)?`, `TryFrom<&str>`) | Static factories (`DeviceDto.fromDomain(d)`, Bloch Item 1) | Creating `FooFactoryImpl` classes for 2-field structs that need no polymorphism |
| **`[GOF-C2]` Abstract Factory** | Create cohesive families of transport/storage adapters (e.g. Live AWS vs In-Memory Test Kit) | Trait with associated types (`trait TelemetryBackend { type Blob: BlobPort; type Index: IndexPort; }`) | `@Configuration` profile beans wiring cohesive port implementations | Multi-layer factory-of-factories when constructor DI suffices |
| **`[GOF-C3]` Builder** | Construct immutable complex objects (>4 optional/validated fields) step-by-step | **Typestate Builder** (`Builder<MissingHomeId> -> Builder<Ready>`) or `#[derive(bon::Builder)]` | Immutable `record` + staged static `Builder` validating in `build()` (Bloch Item 2) | Adding builders to 2–3 field structs where named fields/records are already crystal clear |
| **`[GOF-C4]` Prototype** | Fast cloning of pre-parsed templates/schemas | `#[derive(Clone)]` on compiled `Arc<Schema>` or warm base buffers | Copy methods / immutable `record` `with*` transformations | Deep-cloning mutable graphs with hidden shared references |
| **`[GOF-C5]` Singleton** | Reuse expensive warm runtime resources (TLS pools, compiled schemas, L1 RAM cache) | **`std::sync::OnceLock<T>`** / `LazyLock<T>` holding immutable/concurrent state (`Arc<HomeOrchestrator>`) | Spring constructor-injected `@Service` / `@Bean` (never manual `getInstance()` statics!) | Global **mutable** singletons (`static mut`, hidden global state leaking across unit tests!) |

---

## 2. Structural Patterns (`[GOF-S1 .. GOF-S7]`)

| ID & Pattern | Core Problem Solved | Idiomatic Rust | Idiomatic Java 21 / Vue 3 | Anti-Pattern Trap |
| :--- | :--- | :--- | :--- | :--- |
| **`[GOF-S1]` Adapter (ACL)** | Translate hostile external wire schemas (Avro, BSON, S3, Legacy v3) into pure domain types | `codecs/` Anticorruption Layer (`TryFrom<RawBriefcase> for DomainSample`) | Dedicated `Adapter` implementing domain `Port` interface, translating SDK exceptions (`[EJ-Item73]`) | Letting external SDK types leak past the adapter into `domain/` |
| **`[GOF-S2]` Bridge** | Orthogonal evolution of M projections x N storage tiers | Generic composition `Orchestrator<S: SnapshotStore, L: LineageSource>` | Constructor-injected narrow `Port` interfaces (`StoragePort` + `CodecPort`) | Inheriting `S3WifiService extends WifiService` combinatorial class explosions |
| **`[GOF-S3]` Composite** | Uniform traversal over tree topologies (Gateway -> Extenders -> MLO Links -> Stations) | Recursive algebraic `enum` or arena-indexed node table (`Vec<TopologyNode>` + `ParentIdx`) | `sealed interface MeshElement permits NodeGroup, LeafStation` | Pointer-chasing `Rc<RefCell<Node>>` parent cycles causing memory leaks |
| **`[GOF-S4]` Decorator** | Wrap cross-cutting metrics, `Server-Timing`, retries, or gzip framing around a core port | Zero-cost Newtype wrapper `TimedStore<S>(S)` implementing the same trait | HTTP `ClientHttpRequestInterceptor` / Decorator bean wrapping `Port` | Deep 8-layer invisible proxy stacks obscuring timeout/retry multiplication! |
| **`[GOF-S5]` Facade** | Expose a **Deep Module** (`[APOSD-Ch4]`): 1 narrow entry point hiding 6 internal subsystems | Top-level `HomeOrchestratorService::execute_slice(req)` (`<= 120`L storybook facade) | Package-private internal helpers behind 1 public `@Service` facade; Vue `useHomeDiagnostics()` composable | "Pass-through Facade" that exposes 30 shallow 1-line methods (`Middle Man` smell) |
| **`[GOF-S6]` Flyweight** | Dramatic RAM reduction across 100k telemetry events sharing repeated MACs/SSIDs/OUI strings | Fixed-size `[u8; 6]` `MacAddress` + string interning table (`SymbolId(u16)`) inside `VectorCube` | Interned immutable enums / compact primitive arrays | Premature interning on tiny (<100 item) payloads |
| **`[GOF-S7]` Proxy** | Control access, coalesce burst traffic, or lazy-load expensive S3 blobs | **Principal-Isolated Singleflight Proxy** (`(principal_hash, tenant, home, window) -> SharedFuture`) + 2-Tier Lazy Loader | Singleflight `ConcurrentHashMap<PrincipalScopedKey, CompletableFuture<Snapshot>>` | Coalescing across different user security principals (`SEC-PRINCIPAL-ISOLATION` breach!) |

---

## 3. Behavioral Patterns (`[GOF-B1 .. GOF-B10]`)

| ID & Pattern | Core Problem Solved | Idiomatic Rust | Idiomatic Java 21 / Vue 3 | Anti-Pattern Trap |
| :--- | :--- | :--- | :--- | :--- |
| **`[GOF-B1]` Chain of Responsibility** | Ordered pipeline of independent guards (Auth -> CORS -> Cache L1 -> L2 -> L3 -> Compute) | Tower `Layer` middleware or early-return `?` pipeline functions | Spring Security filter chain / `HandlerInterceptor` pipeline | Implicit mutable context maps passed through 10 handlers where order dependencies are undocumented |
| **`[GOF-B2]` Command** | Reify an operation + parameters + audit metadata as an immutable value | Algebraic `enum AdminCommand { RebootNode { ... }, SteerClient { ... } }` | Immutable `record SteerCommand(...)` | Creating 20 `ICommand` classes when a plain function/closure suffices |
| **`[GOF-B3]` Iterator** | Lazy, zero-allocation streaming pipelines over time buckets & events | `impl Iterator<Item = &Bucket>` + `.filter().map()` combinators | `Stream<T>` (single-line chained stages) or `SequencedCollection` | Collecting intermediate `.collect::<Vec<_>>()` 4 times in a single hot loop! |
| **`[GOF-B4]` Mediator** | Prevent $N \times N$ spaghetti coupling between sibling UI panels or subsystems | Central `VectorCube` coordinator owning cross-feature correlation (`Mesh` + `Station` + `WAN`) | Pinia store / parent coordinator composable owning cross-widget filter & pagination state | Letting the Mediator bloat into a 2,000-line God Object (`[RG-SM02]`) |
| **`[GOF-B5]` Memento** | Snapshot & atomic rollback of state (`L2 /tmp` & `L3 S3 .zst` serialized cube state; UI table state on network error) | Versioned binary/zstd immutable snapshot (`MaterializedHomeState` checksummed header) | Retaining prior `records` snapshot in Vue state until async fetch succeeds (Zero Table Flicker!) | Mutating live state before validation succeeds |
| **`[GOF-B6]` Observer** | Decouple state producers from subscribers | `tokio::sync::watch` / `broadcast` bounded channels | Spring `ApplicationEvent` / Vue `watch(() => props.readonly)` explicit prop synchronization | Unbounded channels causing OOM on slow consumers, or forgetting dynamic prop watchers |
| **`[GOF-B7]` State (`Typestate`)** | Eliminate illegal lifecycle transitions (`Unauthenticated -> Authorized -> Decoded -> Folded`) | **Zero-Cost Compile-Time Typestate** (`PhantomData<State>`) or closed `enum ConnectionState` | `sealed interface WorkflowState permits Pending, Validated, Completed` | Runtime boolean soup (`isInitialized && !isClosed && hasSchema && !isError`)! |
| **`[GOF-B8]` Strategy** | Swap rollup/scoring/compression algorithms cleanly | Static generics (`fn fold<P: RollupPolicy>`) or closed enum dispatch (zero vtable overhead!) | Functional `@FunctionalInterface` lambda or injected `ScoringStrategy` map | Dynamic `Box<dyn Trait>` heap allocation inside tight inner per-packet loops |
| **`[GOF-B9]` Template Method** | Standardize envelope framing, error mapping, and `Server-Timing` headers across 26+ endpoints | Higher-order wrapper `execute_slice_endpoint(req, |cube, params| project_nodes(cube, params))` | Higher-order template helper `executeWithAudit(req, ctx -> ...)` (prefer composition over `abstract class` inheritance!) | Deep 4-level `AbstractBaseController` inheritance trees (`[EJ-Item18]` violation!) |
| **`[GOF-B10]` Visitor** | Add new projections/analyses over a fixed set of telemetry AST/event variants | **Native Exhaustive `match`** on Rust `enum` (compiler fails build if a variant is missed!) | **Java 21 `switch` Pattern Matching over `sealed interface` + `record` (`JEP 441`)** — completely replaces classic double-dispatch Visitor boilerplate! | Writing classic `accept(Visitor v)` double-dispatch boilerplate in Rust or Java 21! |

