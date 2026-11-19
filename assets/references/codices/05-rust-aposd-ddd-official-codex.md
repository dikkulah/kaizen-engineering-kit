# Canonical Codex 05 — Rust Systems, Ousterhout *APOSD*, Evans *DDD* & Tokio Async Laws (`[APOSD-*]`, `[DDD-*]`, `[RUST-*]`)

Authoritative synthesis of **John Ousterhout's *A Philosophy of Software Design (2nd Ed.)***, **Eric Evans' *Domain-Driven Design***, **Official Rust API Guidelines (`RFC 430` / `C-*`)**, **The Rust Reference / Rustonomicon**, and **Tokio Production Concurrency**.

---

## 1. John Ousterhout — *A Philosophy of Software Design* (`[APOSD-01 .. APOSD-10]`)

1. **`[APOSD-01]` Tactical vs. Strategic Programming (Zero "Tactical Tornado")**:
   - Working code is NOT enough. Never ship quick copy-paste workarounds or synthetic fallbacks "to fix it later"—complexity compounds multiplicatively. Invest 10–20% of every task in clean module boundaries.
2. **`[APOSD-02]` Deep Modules Over Shallow Modules (`Depth = Functionality / Interface`)**:
   - **Deep Module**: Exposes 1–3 simple, obvious signatures while concealing immense internal mechanics (`OnceLock` lifecycle, S3 `.zst` decompression, mmap L2 verification, Avro schema resolution, Singleflight coalescing).
   - **Shallow Module (*Classitis* — Forbidden)**: Small classes/functions whose signature + boilerplate takes as much cognitive effort to read as their implementation (`Pass-Through Methods` passing identical parameters down 4 layers).
3. **`[APOSD-03]` Information Hiding & Zero Information Leakage**:
   - Leakage occurs whenever a design decision (wire format, time-bucket resolution, cache filename grammar, Avro fingerprint layout) is reflected in 2+ modules!
   - **Back-Door Leakage**: Even if two modules don't call each other directly, if `Writer` and `Reader` both hardcode `/tmp/{home}_{days}d.ondemand` independently instead of sharing one private codec/store owner, that is fatal Information Leakage!
4. **`[APOSD-04]` Against Temporal Decomposition**:
   - Do NOT split modules strictly by execution order (*Step 1 Read File -> Step 2 Parse Header -> Step 3 Close File*) when Steps 1 and 3 share the exact same format knowledge! Group by **knowledge ownership**, not chronological order.
5. **`[APOSD-05]` Different Layer, Different Abstraction**:
   - Adjacent layers MUST speak genuinely different vocabularies (`S3 Object Keys & Byte Frames` -> `Decoded Telemetry Events` -> `MaterializedHomeState` -> `OpenAPI Slice DTOs`). If two adjacent layers have nearly identical structs/methods, merge them!
6. **`[APOSD-06]` Pull Complexity Downwards**:
   - It is more important for a module to have a **simple interface** than a simple implementation. Module authors should absorb edge cases internally rather than exporting 12 configuration parameters to callers.
7. **`[APOSD-07]` Define Errors Out of Existence**:
   - Eliminate unnecessary error branches by refining semantics:
     - Deleting a cache key that is already absent -> idempotent `Ok(())`, not `KeyNotFoundError`.
     - Querying stations for a valid home with zero connected Wi-Fi clients -> `Ok(Vec::new())` (`200 OK []`), not `500` or panic.
     - Illegal state combinations -> eliminated at compile time via Rust `enum` / `Typestate`.
8. **`[APOSD-08]` Mask & Aggregate Exceptions at Boundaries**:
   - Handle transient S3/network retries inside the transport adapter (`Masking`), and translate all remaining low-level errors into a single domain `ServiceError` hierarchy (`Exception Aggregation`) so upper layers handle 1 error type, not 14.
9. **`[APOSD-09]` Design It Twice**:
   - For every non-trivial module (`VectorCube` memory layout, cache invalidation key, cross-repo API slice), explicitly compare at least **2 radically different designs** and trade off RAM, latency, and blast radius before coding.
10. **`[APOSD-10]` Obvious Code & High-Signal Documentation**:
    - Interface doc comments (`///` and `//!`) must state **what is NOT obvious from the signature**: units (`ms` vs `s`, `bps` vs `Mbps`, `dBm`), boundary inclusivity `[start, end)`, thread-safety, and invariants—never restating the function name in prose!

---

## 2. Eric Evans — *Domain-Driven Design* (`[DDD-01 .. DDD-06]`)

1. **`[DDD-01]` Ubiquitous Language & Authenticity (`ZERO-SYNTHETIC-TELEMETRY`)**:
   - Every identifier in `domain/` and `cube/` uses exact Wi-Fi / Mesh / Cloud domain terminology (`MacAddress`, `Bssid`, `RssiDbm`, `AirtimeUtilization`, `SteeringAttempt`, `MloLink`, `BackhaulType`).
   - **Absolute Authenticity**: Never fabricate synthetic values (`"80MHz"`, `"ETHERNET"`, `"in_sync"`, `unwrap_or(true)`) when telemetry is absent; represent absence honestly (`Option::None` / `"UNKNOWN"` / `0` per documented OpenAPI contract).
2. **`[DDD-02]` Bounded Contexts & External Ownership**:
   - Respect upstream system-of-record boundaries: live XMPP/Presence (`GetNodeIds` / `GetNodePeerIds`) belongs to `GreeterTeamLookupService`; identity/JWT claims belong to `Keycloak`; historical Avro belongs to S3 Data Lake; near-real-time topology belongs to Hermes BSON.
3. **`[DDD-03]` Anticorruption Layer (`ACL` in `src/codecs/`)**:
   - External Avro/BSON schemas evolve independently and contain legacy quirks (nullable unions, inconsistent casing, stringified numbers). Isolate 100% of wire quirk normalization inside `src/codecs/` (`INV-ISOLATION` + `INV-PROVENANCE`) so `src/domain/` and `src/cube/` remain pristine.
4. **`[DDD-04]` Value Objects Over Primitive Obsession**:
   - Defined strictly by structural equality (`PartialEq, Eq, Hash`), zero mutable identity, self-validating on construction (`MacAddress::parse(raw)?`).
5. **`[DDD-05]` Entities & Aggregate Roots (`MaterializedHomeState`)**:
   - All mutations during event folding flow through the Aggregate Root (`VectorCubeBuilder -> MaterializedHomeState`), guaranteeing cross-index consistency (every station link references a valid indexed node/radio).
6. **`[DDD-06]` Read-Model Projections (`CQRS` Slice Projections)**:
   - Fold raw events **once** into `Arc<MaterializedHomeState>`, then project 26+ lightweight read-only slice DTOs (`O(V)` over pre-folded nodes/stations) with zero re-scanning of raw S3 bytes!

---

## 3. Official Rust API Guidelines (`RFC 430`) & Systems Safety (`[RUST-01 .. RUST-12]`)

1. **`[RUST-01]` `C-NEWTYPE` & `C-CUSTOM-TYPE`**: Wrap domain primitives (`struct WindowDays(u16);`, `struct TenantId(Arc<str>);`) and replace boolean parameters with explicit 2-variant enums (`enum CachePolicy { AllowWarmCache, BypassCache }`).
2. **`[RUST-02]` `C-COMMON-TRAITS`**: Eagerly derive `Debug, Clone, PartialEq, Eq` (plus `Copy, Hash, PartialOrd, Ord, Default` where semantically valid). Never implement `Default` if no meaningful valid default exists!
3. **`[RUST-03]` `C-CONV` Cost-Honest Naming**:
   - `as_*(&self) -> Borrowed` (**Free**, $O(1)$, zero heap allocation)
   - `to_*(&self) -> Owned` (**Expensive**, borrows self, allocates/formats new value)
   - `into_*(self) -> Owned` (**Variable/Zero-copy move**, consumes ownership)
4. **`[RUST-04]` `C-GETTER` & `C-ITER`**: Omit `get_` prefix on Rust accessors (`fn stations(&self) -> &[Station]`); follow `iter()` / `iter_mut()` / `into_iter()` conventions.
5. **`[RUST-05]` `C-GOOD-ERR` Zero Panic Law**:
   - Zero `.unwrap()`, `.expect()`, `panic!()`, `todo!()`, `unimplemented!()`, or unchecked `slice[idx]` indexing in production `src/`!
   - Use `.get(idx)`, checked/saturating integer math (`saturating_sub`, `checked_div`), `f64::is_finite()` guards before casting/serializing floats (since JSON has no `NaN`/`Infinity`!), and `thiserror` typed error enums.
6. **`[RUST-06]` Borrowed Slices Over Owned Containers in Signatures**:
   - Accept `&str` instead of `&String`, `&[T]` instead of `&Vec<T>`, `impl AsRef<Path>` instead of `&PathBuf`.
7. **`[RUST-07]` Zero-Copy & Allocation Discipline on Hot Paths**:
   - Pre-size collections (`Vec::with_capacity(n)`, `HashMap::with_capacity(n)`) when upper bounds are known.
   - Filter unused Avro/BSON branches **before** heap-allocating `String`s (`Source-Level Elimination`).
   - Prefer `Cow<'a, str>` or `Arc<str>` over repeated `.to_string()` cloning across 50k telemetry rows.
8. **`[RUST-08]` Tokio Async & Concurrency Laws**:
   - **Never hold `std::sync::MutexGuard` or `RwLockGuard` across an `.await` point!** Acquire, copy/clone cheap `Arc`/handle, and drop the guard in an inner block before `.await`.
   - **Bounded Fan-Out**: Always bound S3/HTTP concurrent requests with `Arc<tokio::sync::Semaphore>` + `tokio::task::JoinSet` (e.g. `16..=32` permits tuned to Graviton3 vCPU & S3 prefix limits).
   - **CPU-Bound Offload / Yield**: Heavy zstd compression or multi-MB Avro folding (>2ms synchronous CPU) runs either in `spawn_blocking` or outside shared async locks.
   - **Cancellation Safety**: Know whether futures inside `tokio::select!` are cancellation-safe; prefer `JoinSet` + `tokio::time::timeout(deadline, ...)` with explicit deadline budgets.
9. **`[RUST-09]` Warm Container Lifecycle (`OnceLock`)**:
   - AWS Lambda freezes and thaws execution environments across invocations. Store SDK clients (`aws_sdk_s3::Client`, `aws_sdk_dynamodb::Client`), compiled Avro schemas, and bounded LRU `VectorCube` RAM caches inside `static ORCHESTRATOR: OnceLock<HomeOrchestrator>` so warm invocations pay `0ms` init cost!
10. **`[RUST-10]` Cross-Tenant & TTL Cache Safety**:
    - Every L1 (`OnceLock`), L2 (`/tmp`), and L3 (`S3 .zst`) cache key MUST partition by `(tenant_id, home_id, window_days, schema_version)` and verify `mtime` / `LastModified` TTL before serving!
11. **`[RUST-11]` Graviton3 ARM64 Reproducible Builds**:
    - Pin `aarch64-unknown-linux-gnu` (AL2023 glibc parity via `cargo zigbuild` or Graviton3 EC2 builder), `codegen-units = 1`, `lto = "thin"` (or `"fat"`), `strip = "symbols"` for minimal cold-start binary footprint.
12. **`[RUST-12]` Rustonomicon `unsafe` Prohibition**:
    - Default to `#![forbid(unsafe_code)]` (or zero `unsafe` blocks unless proven 10x bottleneck via criterion flamegraph AND accompanied by a formal `// SAFETY:` invariant proof + Miri verification).

