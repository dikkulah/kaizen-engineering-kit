---
name: abstract-ast-provenance-gate
description: >-
  Enforces the 4 Abstract Architectural Invariants grounded in canonical classics:
  INV-ISOLATION (APOSD Information Leakage & Evans Bounded Context), INV-PROVENANCE
  (Fowler Anti-Feature-Envy & Uncle Bob Law of Demeter), INV-NO-SYNTHETIC-LITERAL
  (Evans Ubiquitous Authenticity), and INV-COMPILE-TIME-SCHEMA (Bloch Static Immutable
  Assets) plus the Parity Oracle Gate (POPULATED_MATCH vs EMPTY_BOTH vs VACUOUS_PASS).
---

# Abstract AST & Telemetry Provenance Gate (`abstract-ast-provenance-gate`)

This skill verifies that the codebase adheres to our **4 Abstract Architectural Invariants** and the **Parity Oracle Gate** before any commit, deployment, or parity claim. Every invariant is authoritatively grounded in canonical software engineering classics.

---

## 1. The 4 Abstract Architectural Invariants Grounded in Classics

### 1. `INV-ISOLATION` (Strict Avro vs. BSON Pipeline Isolation)
- **Canonical Grounding**: **John Ousterhout (*APOSD*) Information Leakage Prohibition** & **Eric Evans (*DDD*) Bounded Context Isolation**.
- **Theoretical Rationale**: Avro and BSON telemetry streams represent two distinct historical and protocol bounded contexts. Allowing BSON types into Avro modules couples disparate schemas, leaks wire serialization details across subsystem boundaries, and creates divergent change (Shotgun Surgery).
- **Rule**:
  - Avro decoding/folding modules (`*avro*.rs`) **MUST NEVER** import, construct, or reference BSON pipeline types (`TopologyRecord`, `AutomeshRecord`, `BsonTopologyRow`, `HermesBsonEnvelope`, `HERMES_DELIM`).
  - BSON decoding/folding modules (`*bson*.rs`) **MUST NEVER** import, construct, or reference Avro pipeline types (`AvroWhitelistedEvent`, `BriefcaseRecord`, `apache_avro`, `EmbeddedSchemaRegistry`).
  - Each pipeline folds its own native wire telemetry directly into the pure domain aggregate (`MaterializedHomeState`).

### 2. `INV-PROVENANCE` (Controller Data Lineage & Zero Hollow Stubs)
- **Canonical Grounding**: **Martin Fowler (*Refactoring.Guru*) Anti-Feature-Envy** & **Robert C. Martin (*Clean Code*) Law of Demeter**.
- **Theoretical Rationale**: An endpoint handler that takes `_cube` and returns a hardcoded empty response (`ItemsResponse::new(Vec::new())`) is a hollow stub exhibiting feature envy of synthetic data while severing genuine data lineage from the aggregate root.
- **Rule**:
  - Every `pub fn handle_*` endpoint handler in `rest/controllers/` (except explicitly deprecated v1 404 handlers) **MUST** consume `cube: &MaterializedHomeState` and project genuine data from a populated vertical feature store.
  - Prefixing `_cube` with an underscore to silence compiler warnings and returning hardcoded empty responses is strictly forbidden.

### 3. `INV-NO-SYNTHETIC-LITERAL` (Dynamic Telemetry Purity)
- **Canonical Grounding**: **Eric Evans (*DDD*) Ubiquitous Language Authenticity** & **Martin Fowler Anti-Speculative Generality**.
- **Theoretical Rationale**: Injecting static string or numeric constants (`"ETHERNET"`, `"UP"`, `"FULL"`, `1000`, `"Kaizen Gateway"`, `"Kaizen Extender"`, `slice_idx % 15`) into dynamic telemetry models commits fraud against domain reality. Telemetry must be derived end-to-end from ingested wire envelopes.
- **Rule**:
  - Folding and projection layers **MUST NEVER** assign hardcoded fallback constants to dynamic telemetry metrics.
  - When telemetry data is absent or unmeasured, emit `None`, `0`, or `"UNKNOWN"` honestly—never invent synthetic fallback literals to satisfy assertions.

### 4. `INV-COMPILE-TIME-SCHEMA` (Zero Network Schema I/O)
- **Canonical Grounding**: **Joshua Bloch (*Effective Java*) Item 6 Static Immutable Assets** & **Rust Zero-Cost Compile-Time Guarantees**.
- **Theoretical Rationale**: Fetching schemas over the network (e.g. from S3 or HTTP registries) at runtime introduces latency, network fragility, and non-deterministic cold starts into serverless execution. Telemetry schemas are immutable compile-time assets.
- **Rule**:
  - Schema resolution in `codecs` and `ingest` **MUST** be 100% embedded locally at compile time (`EmbeddedSchemaRegistry`).
  - Runtime schema downloads (`ensure_briefcase_schema_loaded` or `schemas/fingerprints/` S3 fetches) are strictly prohibited.

---

## 2. The Parity Oracle Gate (`POPULATED_MATCH` vs `EMPTY_BOTH` vs `VACUOUS_PASS`)

When validating parity between Legacy (LegacyBatchWorker / CloudGatewayApi v3) and Target (OnDemand v4 / DuckDB / VectorCube) engines, test results are classified into three strict oracle tiers:

```
+----------------------------------------------------------------------------------------------------+
|                                    PARITY ORACLE CLASSIFICATION                                    |
+----------------------------------------------------------------------------------------------------+
| 1. POPULATED_MATCH (Genuine Parity - Required for Verification)                                    |
|    - HTTP Status matches (200 OK == 200 OK).                                                       |
|    - Legacy has N > 0 populated records/metrics, and Target produces N > 0 matching records.        |
|    - Recursive JSON field values match within certified engineering tolerances.                    |
+----------------------------------------------------------------------------------------------------+
| 2. EMPTY_BOTH (Valid Absence)                                                                      |
|    - Both Legacy and Target return empty sets ([] or {}).                                          |
|    - VALID ONLY IF the underlying ingested telemetry envelope genuinely contains 0 events for      |
|      that specific metric/time range.                                                              |
+----------------------------------------------------------------------------------------------------+
| 3. VACUOUS_PASS (Strictly Forbidden - Test Fraud)                                                  |
|    - Asserting HTTP 200 without recursively validating response body values.                       |
|    - A test passes because Target returned 0, null, "", [], or epoch zero (1970-01-01) when       |
|      Legacy had populated data (False Positive).                                                   |
|    - Tolerance masking that treats unpopulated target values as passing.                           |
+----------------------------------------------------------------------------------------------------+
```

---

## 3. Run the Provenance & Isolation Audit

Run the automated provenance verifier before every commit or pull request:

```bash
kaizen-kit provenance --workspace <path-to-engine>
```
