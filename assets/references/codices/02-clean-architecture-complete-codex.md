# Canonical Codex 02 — Clean Architecture, SOLID & Component Metrics (`[CA-*]`)

Authoritative engineering synthesis of **Robert C. Martin's *Clean Architecture: A Craftsman's Guide to Software Structure and Design*** (Parts I–VI), complete with mathematical component coupling/cohesion formulas enforced by `kaizen-kit arch-audit`.

---

## 1. The Three Programming Paradigms as Architectural Constraints (`[CA-PAR1 .. CA-PAR3]`)

Each paradigm removes dangerous capabilities from the programmer to enforce architectural discipline:
1. **`[CA-PAR1]` Structured Programming (Böhm-Jacopini / Dijkstra)**: Removes unrestricted `goto` -> imposes discipline on **direct transfer of control**, making modules falsifiable via functional decomposition (Karl Popper).
2. **`[CA-PAR2]` Object-Oriented / Trait Polymorphism**: Removes unsafe function-pointer discipline -> imposes discipline on **indirect transfer of control**, giving architects absolute **Dependency Inversion (`DIP`)** power over source-code dependency direction!
3. **`[CA-PAR3]` Functional Programming**: Removes unrestricted mutation -> imposes discipline on **variable assignment**. All race conditions, deadlocks, and concurrent update bugs are caused by mutable state (`[CA-IMMUTABILITY]`: push event-sourced / fold-based immutability as far as memory/CPU budgets allow).

---

## 2. Deep Architectural SOLID (`[CA-SRP .. CA-DIP]` — Module & Architecture Level)

> **Critical Correction**: SOLID in *Clean Architecture* is NOT merely micro class-level syntax—it governs **mid-level module structure and actor boundaries**.

| ID | Principle | True Architectural Definition | Common Misunderstanding / Trap |
| :--- | :--- | :--- | :--- |
| **`[CA-SRP]`** | **Single Responsibility Principle** | *"A module should be responsible to one, and only one, **actor** (stakeholder group)."* | Splitting a 40-line cohesive parser into 5 tiny 8-line files (*Classitis*) when only 1 actor owns it. Conversely, sharing 1 `DeviceReport` calculator between TelemetryDashboard UI and Billing actors causes accidental breakage! |
| **`[CA-OCP]`** | **Open-Closed Principle** | Software artifacts should be open for extension by adding new modules/variants, and closed for modification of core orchestrators. Protect high-level domain policies from low-level UI/transport churn. | Creating premature generic frameworks (*Speculative Generality*) when there is only 1 concrete implementation. |
| **`[CA-LSP]`** | **Liskov Substitution Principle** | Subtypes / trait implementations must preserve behavioral contracts (preconditions no stronger, postconditions no weaker, no unexpected panics/`UnsupportedOperationException`). At system scale, REST/Lambda v4 slice endpoints substituting Legacy v3 endpoints must honor L1–L4 `POPULATED_MATCH` contracts! | Implementing a `CacheStore` trait whose S3 adapter silently ignores TTL while RAM adapter enforces TTL. |
| **`[CA-ISP]`** | **Interface Segregation Principle** | Never force callers to depend on fat traits/interfaces/crates containing methods or transitive dependencies they do not use (prevents unnecessary recompilation & blast radius). | Passing `AppContext` or a 25-method `GodRepository` into a focused projection that only needs `NodeLookupPort`. |
| **`[CA-DIP]`** | **Dependency Inversion Principle** | High-level domain policies own the abstract port (`trait` / `interface`); volatile concrete details (S3, DynamoDB, HTTP, Avro, BSON, Keycloak) implement that port from the outside pointing **inward**. | Defining `S3Client` or `JdbcTemplate` directly inside `domain/` or `usecase/` signatures. |

---

## 3. Component Cohesion Principles (`[CA-REP]`, `[CA-CCP]`, `[CA-CRP]`)

Deciding **which classes/modules belong together inside the same crate, package, or microservice**:

```
              [CA-REP] Reuse/Release Equivalence
                     /\
                    /  \    <-- Too many splits = Dependency Hell
   Too much        /    \
   recompilation  /      \
                 /________\
        [CA-CCP]            [CA-CRP]
     Common Closure       Common Reuse
```

1. **`[CA-REP]` Reuse/Release Equivalence Principle**: The granule of reuse is the granule of versioned release. Classes/modules grouped into a shared library (`shared-utils`, `avro-codecs`) must form a cohesive, version-tracked theme—never a random junk-drawer.
2. **`[CA-CCP]` Common Closure Principle (Component-Level SRP)**: Gather into the same component all classes that **change for the same reasons and at the same times**. Separate into different components classes that change at different rates or for different reasons (eliminates cross-repo *Shotgun Surgery*).
3. **`[CA-CRP]` Common Reuse Principle (Component-Level ISP)**: Don't force users of a component to depend on things they don't need. If Consumer A only needs DTO types, do not bundle heavy AWS SDK + Netty transitive dependencies into that same artifact!

---

## 4. Component Coupling Principles & Mathematical Metrics (`[CA-ADP]`, `[CA-SDP]`, `[CA-SAP]`)

### 4.1 `[CA-ADP]` Acyclic Dependencies Principle ("No Cycles Allowed")
- The dependency graph of packages/modules/crates **MUST be a Directed Acyclic Graph (DAG)**.
- If `A -> B -> C -> A` exists, you have created a "morning-after syndrome" monolith where neither `A`, `B`, nor `C` can be tested, understood, or released independently.
- **Cycle-Breaking Moves**:
  1. **Apply `DIP`**: Create an interface/trait inside `B` (or `A`) so `C` implements it pointing *upward* instead of `B` importing `C`.
  2. **Extract New Shared Component `D`**: Move the shared Value Objects/types both `B` and `C` depend on into a zero-dependency leaf module `D`.

### 4.2 `[CA-SDP]` Stable Dependencies Principle ("Depend in the Direction of Stability")
For every module/package $M$, `kaizen-kit arch-audit` computes:
- **$C_a$ (Afferent Coupling / Fan-In)**: Number of classes/modules *outside* $M$ that depend *on* $M$ (reasons **not** to change $M$).
- **$C_e$ (Efferent Coupling / Fan-Out)**: Number of classes/modules *outside* $M$ that $M$ depends *on* (external causes that force $M$ to change).
- **Instability Metric $I \in [0, 1]$**:
  $$I = \frac{C_e}{C_a + C_e}$$
  - $I = 0$: **Maximally Stable** ($C_e = 0, C_a > 0$, e.g. `src/domain` — depends on nothing, everything depends on it).
  - $I = 1$: **Maximally Unstable / Volatile** ($C_a = 0, C_e > 0$, e.g. `src/http_router` or `main.rs` — nothing depends on it, free to change rapidly).
- **Law**: The $I$ metric of a component must be **larger** than the $I$ metrics of the components it depends on ($I_{\text{caller}} \ge I_{\text{callee}}$). A stable core module ($I = 0.05$) must NEVER import a volatile adapter ($I = 0.90$)!

### 4.3 `[CA-SAP]` Stable Abstractions Principle & Distance from the Main Sequence ($D$)
- **Abstractness Metric $A \in [0, 1]$**:
  $$A = \frac{N_a}{N_c}$$
  where $N_a$ is the count of abstract types (`trait`, `interface`, `abstract class`, `sealed interface`, pure domain algebraic `enum`/`record` contracts) and $N_c$ is total types in the module.
- **The Main Sequence Ideal**: Stable components ($I \to 0$) should be abstract/extensible ($A \to 1$) or pure zero-I/O value types; unstable adapters ($I \to 1$) should be concrete ($A \to 0$).
- **Normalized Distance from the Main Sequence $D \in [0, 1]$**:
  $$D = |A + I - 1|$$
  - **`Zone of Pain` ($I \approx 0, A \approx 0$ with volatile internals)**: Highly depended-upon ($C_a \gg 0$), rigid concrete implementation (e.g. concrete DB utility schema coupled everywhere). *Note: Pure immutable standard/domain value structs (`MacAddress`, `String`) have $I=0, A=0$ safely because they are **non-volatile**.*
  - **`Zone of Uselessness` ($I \approx 1, A \approx 1$)**: Abstract interfaces/traits that **nobody** depends on ($C_a = 0$) — dead *Speculative Generality*!

---

## 5. Concentric Boundaries & The Dependency Rule (`[CA-DEP-RULE]`)

```
[ Outer Circle: Frameworks, Drivers & Dirty Main ]
   AWS Lambda Runtime, ALB/HTTP, S3, DynamoDB, Spring Web, Vue DOM
        │  (Source dependencies point STRICTLY INWARD)
        ▼
[ Interface Adapters: Controllers, Presenters, Codecs, Gateways ]
   Avro/BSON Codecs (ACL), OpenAPI DTO Mappers, Singleflight Proxy
        │
        ▼
[ Application Business Rules: Use Cases & Orchestrators ]
   HomeOrchestratorService, Bounded Fan-Out, Multi-Tier Snapshot Policy
        │
        ▼
[ Innermost Circle: Enterprise Domain Entities & Pure State ]
   MaterializedHomeState (VectorCube), Value Objects (MacAddress, RssiDbm), Pure Folding Math
```

### Mandatory Boundary Laws
1. **`[CA-DEP-RULE]` Strict Inward Source Dependencies**: Inner circles (`domain`, `cube`) must NEVER name anything declared in an outer circle (`aws_sdk_*`, `lambda_http`, `org.springframework.web`, `jakarta.servlet`, `axios`).
2. **`[CA-BOUNDARY-DTO]` Zero Entity / Codec Leakage Across Boundaries**: Never pass raw `apache_avro::types::Value`, `bson::Document`, `ResultSet`, or JPA `@Entity` across use-case or HTTP boundaries. Decode immediately into domain Value Objects at the codec boundary (`Source-Level Elimination`), and project pure DTOs at the outer presenter boundary.
3. **`[CA-SCREAMING]` Screaming Architecture**: Top-level directory structure should scream the **business domain** (`features/mesh_topology`, `features/wan_gateway`, `features/station_mlo_steering`), not just framework boilerplate!
4. **`[CA-HUMBLE]` The Humble Object Pattern**: At every hard-to-test boundary (HTTP/ALB handler, Vue DOM component, S3/CloudWatch SDK client, Lambda `main`), split the logic into two:
   - **Humble Adapter (`<= 40` lines)**: Trivial glue doing zero branching—only unmarshalling wire bytes and calling the testable core.
   - **Testable Presenter / Core**: 100% pure, deterministic, millisecond unit-testable in isolation without spinning up AWS, Docker, or browsers!
5. **`[CA-PARTIAL]` Partial Boundaries (Pragmatic YAGNI)**: Full reciprocal interfaces + input/output boundary DTOs have high ceremony cost. Inside a **Single-Crate Clean Architecture** (`src/*`), enforce strict one-way module visibility (`pub(crate)`) and inward `use` imports first; introduce dynamic `dyn Trait` polymorphism ONLY where multiple runtime implementations (or external I/O test doubles) genuinely exist!
6. **`[CA-MAIN]` The `Main` Component Is the Ultimate Dirty Plugin**: `main.rs` / `@SpringBootApplication` configuration is the outermost circle: it instantiates concrete S3/DDB clients, binds `OnceLock` singletons, injects ports into orchestrators, and hands control inward.

