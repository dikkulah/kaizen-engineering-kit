---
name: rust-aws-lambda-craft
description: >-
  High-performance Rust + AWS Lambda Graviton3 (ARM64) engineering: Rust API Guidelines
  compliance (C-NEWTYPE, C-COMMON-TRAITS, C-CONV, C-GETTER, C-CUSTOM-TYPE), GoF Typestate
  State Machines, Ousterhout Deep Modules, OnceLock warm-container state caching,
  Tokio async concurrency (JoinSet, bounded Semaphores, CancellationToken), Single-Crate
  Clean Architecture, zero-copy Avro/BSON decoding, and strict Clippy pedantic compliance.
---

# Rust + AWS Lambda Graviton3 & Tokio Async Systems Craft

Comprehensive engineering guide for high-throughput, low-latency Rust services on AWS Lambda and async runtimes, authoritatively grounded in:
- **Official Rust API Guidelines (RFC 430 & Rust API Subteam)**
- **John Ousterhout (*A Philosophy of Software Design*) Deep Modules**
- **GoF Design Patterns (Compile-Time Typestate State Machine)**
- **Robert C. Martin Clean Architecture Inward Dependencies**
- **AWS Graviton3 (ARM64) SIMD & Hardware Architecture**

---

## 1. Rust API Guidelines Compliance Matrix (`C-*`)

Production Rust Lambda code must strictly conform to the canonical Rust API Guidelines:

| Guideline ID | Core Principle | Serverless & Telemetry Enforcement |
| :--- | :--- | :--- |
| **`C-NEWTYPE`** | Newtype pattern provides type safety and semantic clarity at zero runtime cost | Wrap raw primitives into dedicated tuple structs: `struct MacAddress([u8; 6]);`, `struct NodeIndex(u8);`, `struct RadioSlot(u8);`, `struct TimeSlot(u16);`. Never pass naked `String` or `usize` across domain boundaries. |
| **`C-COMMON-TRAITS`** | Implement common traits eagerly for value types | Derive or implement `Debug`, `Clone`, `PartialEq`, `Eq`, `Hash`, and `Default` on domain structs. Implement `Copy` when `size_of::<T>() <= 16` bytes. |
| **`C-CONV`** | Standardize conversion method names (`as_`, `to_`, `into_`) | `as_` for zero-cost borrowing/views (`as_bytes() -> &[u8]`); `to_` for expensive conversions (`to_mac_string() -> String`); `into_` for consuming ownership (`into_inner() -> [u8; 6]`). Implement `From` / `TryFrom` over ad-hoc converters. |
| **`C-GETTER`** | Omit `get_` prefix from field accessors | Use `pub fn mac(&self) -> &MacAddress`, NOT `pub fn get_mac(&self)`. Reserve `get_` for fallible or key-based lookups (`get(&self, key: &Key) -> Option<&Value>`). |
| **`C-CUSTOM-TYPE`** | Model domain and adapter errors using `thiserror` enums | Never return stringly-typed errors (`Result<T, String>`). Define dedicated error enums with `#[derive(thiserror::Error)]`. Zero `.unwrap()` or `.expect()` in production code. |

---

## 2. Compile-Time Typestate State Machine Pattern (GoF State & APOSD)

Following John Ousterhout's *"Define Errors Out of Existence"* and GoF State pattern, lifecycle transitions must be enforced statically at compile time using zero-sized phantom markers:

```rust
use std::marker::PhantomData;

// Sealed lifecycle states
pub struct Uninitialized;
pub struct Hydrated {
    raw_payloads: Vec<Vec<u8>>,
}
pub struct Folded {
    state: MaterializedHomeState,
}

pub struct TelemetryCube<State = Uninitialized> {
    home_id: String,
    state: State,
    _marker: PhantomData<State>,
}

impl TelemetryCube<Uninitialized> {
    pub fn new(home_id: impl Into<String>) -> Self {
        Self {
            home_id: home_id.into(),
            state: Uninitialized,
            _marker: PhantomData,
        }
    }

    pub fn hydrate(self, raw_payloads: Vec<Vec<u8>>) -> TelemetryCube<Hydrated> {
        TelemetryCube {
            home_id: self.home_id,
            state: Hydrated { raw_payloads },
            _marker: PhantomData,
        }
    }
}

impl TelemetryCube<Hydrated> {
    pub fn fold(self) -> Result<TelemetryCube<Folded>, IngestError> {
        let materialized = fold_payloads(self.state.raw_payloads)?;
        Ok(TelemetryCube {
            home_id: self.home_id,
            state: Folded { state: materialized },
            _marker: PhantomData,
        })
    }
}

impl TelemetryCube<Folded> {
    pub fn project_view(&self) -> ItemsResponse {
        self.state.state.project_items()
    }
}
```

*Architectural Guarantee*: Calling `.project_view()` on an unhydrated or unfolded cube is a **compile-time error**, making invalid execution states impossible at zero runtime penalty.

---

## 3. Ousterhout Deep Module Pattern for Lambda State Facades

A module is "deep" when its interface is small and simple, but the implementation conceals immense capability:
$$\text{Module Depth} = \frac{\text{Internal Capability \& Logic Concealed}}{\text{Interface Surface Area}}$$

- **Anti-Pattern (Shallow / Classitis)**: Scattering S3 fetching, zstd decompression, channel coordination, and Avro/BSON routing across 10 shallow classes and calling each step individually from the Lambda router.
- **Deep Facade Pattern**:
  ```rust
  pub struct HomeOrchestratorService {
      s3_fetcher: Arc<SingleflightS3Fetcher>,
      cube_cache: Arc<BoundedCubeCache>,
      worker_pool: Arc<tokio::sync::Semaphore>,
  }

  impl HomeOrchestratorService {
      /// Deep Facade: Single entry point handling decompression, folding, caching,
      /// and projection internally without leaking wire representations.
      pub async fn materialize_and_query(
          &self,
          query: HomeTelemetryQuery,
      ) -> Result<ItemsResponse, OrchestratorError> {
          let cached = self.cube_cache.get(&query.home_id).await;
          if let Some(state) = cached {
              return Ok(state.project_for_query(&query));
          }

          let raw_frames = self.s3_fetcher.fetch_singleflight(&query.home_id, query.time_range).await?;
          let cube = TelemetryCube::new(&query.home_id)
              .hydrate(raw_frames)
              .fold()?;

          self.cube_cache.insert(&query.home_id, cube.state.clone()).await;
          Ok(cube.project_view())
      }
  }
  ```

---

## 4. Warm Container State & `OnceLock` Singleton Law

In AWS Lambda (`lambda_runtime`), the `main()` function executes **once per cold start**, while the handler processes **thousands of requests across warm invocations**.
- **Anti-Pattern (Forbidden)**: Initializing heavy services, database/S3 connection pools, or cache maps inside per-request handlers (causes 40–150ms cold-latency on every call and wipes L1 cache).
- **Required Pattern (`std::sync::OnceLock`)**:
  ```rust
  use std::sync::OnceLock;

  static ORCHESTRATOR: OnceLock<HomeOrchestratorService> = OnceLock::new();

  pub fn global_orchestrator() -> &'static HomeOrchestratorService {
      ORCHESTRATOR.get_or_init(HomeOrchestratorService::default)
  }
  ```

---

## 5. Graviton3 (ARM64) SIMD & Hardware-Aware Memory Hierarchy

1. **Target `aarch64-unknown-linux-gnu` (Graviton3)**:
   - Yields ~20% better price-performance and higher SIMD/vector throughput:
   ```bash
   cargo zigbuild --release --target aarch64-unknown-linux-gnu
   ```
2. **Release Binary Footprint & LTO (`Cargo.toml`)**:
   ```toml
   [profile.release]
   opt-level = 3
   lto = "thin"
   codegen-units = 1
   strip = "symbols"
   panic = "abort"
   ```
3. **Cache-Line Alignment (`#[repr(align(64))]`)**:
   - Align high-throughput ring buffers and telemetry atomics to 64-byte Graviton3 cache lines to prevent CPU false sharing under multithreaded Tokio execution:
   ```rust
   #[repr(align(64))]
   pub struct AlignedRingBuffer {
       slots: [AtomicU64; 64],
   }
   ```
4. **Memory-to-vCPU Scaling**:
   - AWS Lambda allocates 1 full vCPU at `1,769 MB` and 2 full vCPUs at `3,538 MB`. Size memory allocations to eliminate compute throttling during bursts.

---

## 6. Tokio Async Concurrency & Task Management

### 6.1 Concurrent Tasks with `JoinSet`
Manage dynamic sets of concurrent futures with structured lifecycle control:
```rust
use tokio::task::JoinSet;

pub async fn fetch_all_telemetry(keys: Vec<String>) -> Result<Vec<TelemetryRecord>, ServiceError> {
    let mut set = JoinSet::new();

    for key in keys {
        set.spawn(async move { fetch_single(&key).await });
    }

    let mut records = Vec::new();
    while let Some(res) = set.join_next().await {
        match res {
            Ok(Ok(item)) => records.push(item),
            Ok(Err(err)) => tracing::error!("Failed to fetch record: {err}"),
            Err(join_err) => tracing::error!("Task panicked: {join_err}"),
        }
    }
    Ok(records)
}
```

### 6.2 Bounded Concurrency with Semaphores
Never spawn unbounded async tasks against AWS or internal microservices. Use `tokio::sync::Semaphore`:
```rust
use std::sync::Arc;
use tokio::sync::Semaphore;

let semaphore = Arc::new(Semaphore::new(16)); // Max 16 concurrent requests
let permit = semaphore.clone().acquire_owned().await.unwrap();
tokio::spawn(async move {
    let _permit = permit; // Released when dropped
    perform_network_call().await;
});
```

### 6.3 Channels & Cancellation
- **Channels**: Use `tokio::sync::mpsc` (producer-consumer), `broadcast` (multi-subscriber events), `oneshot` (single return value), or `watch` (latest config updates).
- **Graceful Cancellation**: Use `tokio_util::sync::CancellationToken` and `tokio::select!` for clean shutdowns:
  ```rust
  tokio::select! {
      _ = token.cancelled() => {
          tracing::info!("Received shutdown signal, terminating gracefully");
      }
      res = process_stream() => {
          res?;
      }
  }
  ```

---

## 7. Single-Crate Clean Architecture (`src/`)

Consolidate single-deployable Lambda engines into clean, cohesive modules:
```text
src/
├── domain/      # Pure types, NewTypes (MacAddress([u8;6]), NodeId(u16)), enums (strum) — 0 I/O deps
├── codecs/      # Isolated compile-time schemas: codecs::avro vs codecs::bson (INV-ISOLATION)
├── cube/        # MaterializedHomeState, fold_avro.rs, fold_bson.rs, TimeSeries ring buffers
├── contract/    # Strictly typed CloudGatewayApi wire DTOs (serde_with, camelCase)
├── ingest/      # S3 singleflight fetcher, zstd/gzip decompression, zero runtime schema fetch
└── runner/      # Thin HTTP/Lambda router, controllers (<=120L), OnceLock orchestrator
```

---

## 8. Zero-Copy & Idiomatic Rust Guardrails

1. **No Modulo Wrapping on Monotonic IDs**:
   - Use `NodeId(u16)` with monotonic `entry().or_insert_with()` lookup; never use modulo tricks (`idx % 15`).
2. **No Blocking Operations in Async**:
   - Never call `std::thread::sleep` or blocking I/O inside async functions; use `tokio::time::sleep` or `tokio::task::spawn_blocking`.
3. **Clippy Quality Gate**:
   ```bash
   cargo clippy --all-targets -- -D warnings -W clippy::cognitive_complexity -W clippy::unwrap_used
   ```
