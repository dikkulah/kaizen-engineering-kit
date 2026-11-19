---
name: clean-code-enforcer
description: >-
  Enforces production-grade Clean Code, Cognitive Complexity <= 10, Guard Clauses,
  Zero Nested Pyramids, Deep Modules, Refactoring.Guru 22 Smells & 66 Refactorings,
  and Canonical Classics (Uncle Bob, Ousterhout APOSD, Evans DDD, Bloch Effective Java,
  Rust API Guidelines) across Rust and Java 21 codebases.
---

# Clean Code, Cognitive Complexity & Canonical Refactoring Enforcer

This skill establishes the universal engineering baseline across Rust and Java 21 codebases, authoritatively grounded in the 6 canonical software engineering classics:
1. **Refactoring.Guru & Martin Fowler**: 22 Code Smells & 66 Refactoring catalog.
2. **John Ousterhout (*A Philosophy of Software Design*)**: Deep Modules, Information Hiding, Anti-Classitis, and "Define Errors Out of Existence".
3. **Robert C. Martin (*Clean Code* & *Clean Architecture*)**: Functions $\le 35$L, Cognitive Complexity $\le 10$, SLAP, and Inward Dependency Rule.
4. **Eric Evans (*Domain-Driven Design*)**: Ubiquitous Language, Value Objects over Primitive Obsession, Aggregate Roots, and Anticorruption Layers.
5. **Joshua Bloch (*Effective Java*, 3rd Edition / Java 21)**: Immutability, Records, Sealed Hierarchies, Static Factories, and Exception Translation.
6. **Official Rust API Guidelines (RFC 430 & Rust API Subteam)**: `C-NEWTYPE`, `C-COMMON-TRAITS`, `C-CONV`, `C-GETTER`, `C-CUSTOM-TYPE`.

---

## Pillar 1: Refactoring.Guru Catalog (22 Code Smells & 66 Refactorings)

All production code must remain free of the 22 canonical code smells across the 5 canonical smell families:

### 1. Bloaters (Code, methods, and classes that have increased to gargantuan proportions)
| Code Smell | Architectural Risk & Violation | Authoritative Refactoring Moves |
| :--- | :--- | :--- |
| **Long Method** | Method exceeds 35 lines or spans multiple abstraction levels | `Extract Method`, `Replace Temp with Query`, `Decompose Conditional`, `Introduce Parameter Object` |
| **Large / God Class** | Class/module has $>1$ responsibility, exceeds 300 lines | `Extract Class`, `Extract Subclass`, `Replace Data Value with Object`, `Extract Interface` |
| **Primitive Obsession** | Raw `String`, `u64`, `int` used for rich domain concepts (MAC, timestamp, bandwidth) | `Replace Data Value with Object`, `Introduce Parameter Object`, Rust `NewType`, Java 21 `record` |
| **Long Parameter List** | Method takes $>4$ parameters, causing call-site brittleness | `Introduce Parameter Object`, `Preserve Whole Object`, `Builder Pattern`, `#[bon]` |
| **Data Clumps** | Identical groups of 3+ fields/parameters appearing in multiple places | `Extract Class`, `Introduce Parameter Object`, `Preserve Whole Object` |

### 2. Object-Orientation Abusers (Incorrect or incomplete application of OO principles)
| Code Smell | Architectural Risk & Violation | Authoritative Refactoring Moves |
| :--- | :--- | :--- |
| **Switch Statements** | Complex `switch` or `match` blocks repeated across callers for type codes | `Replace Conditional with Polymorphism`, `Replace Type Code with Strategy/State`, `Typestate Pattern` |
| **Temporary Field** | Fields populated only in certain workflows, remaining null/uninitialized otherwise | `Extract Class`, `Introduce Null Object`, compile-time `Typestate` |
| **Refused Bequest** | Subclass inherits methods/fields it does not want or support (violates LSP) | `Replace Inheritance with Delegation` (Bloch Item 18), `Extract Superclass` |
| **Alternative Classes with Different Interfaces** | Two classes perform identical operations under different method signatures | `Rename Method`, `Move Method`, `Extract Superclass`, `Unified Adapter Trait` |

### 3. Change Preventers (Changes in one place trigger cascades of modifications elsewhere)
| Code Smell | Architectural Risk & Violation | Authoritative Refactoring Moves |
| :--- | :--- | :--- |
| **Divergent Change** | One class is commonly changed for different reasons (violates SRP) | `Extract Class` along axis of change, split into distinct UseCases |
| **Shotgun Surgery** | A single requirement change forces minor edits across 10+ distinct files | `Move Method`, `Move Field`, `Combine Functions into Transform`, consolidate shared abstraction |
| **Parallel Inheritance Hierarchies** | Creating a subclass in one tree requires creating a subclass in another | `Move Method`, `Move Field`, `Collapse Hierarchy` |

### 4. Dispensables (Pointless, redundant, or dead code that increases cognitive load)
| Code Smell | Architectural Risk & Violation | Authoritative Refactoring Moves |
| :--- | :--- | :--- |
| **Comments** | Apologetic comments written to explain convoluted, poorly named code | `Extract Variable`, `Extract Method`, `Rename Method` (make code self-documenting) |
| **Duplicate Code** | Copy-pasted logic across branches or files | `Extract Method`, `Pull Up Method`, `Form Template Method`, `Substitute Algorithm` |
| **Lazy Class** | Class that costs more to maintain than it delivers in capability | `Inline Class`, `Collapse Hierarchy` |
| **Data Class** | Dumb struct with only public fields or getters/setters and zero business logic | `Encapsulate Field`, `Move Method` (elevate to rich domain entity) |
| **Dead Code** | Unused variables, parameters, fields, or dead functions | `Remove Dead Code`, enforce compiler flags (`-D dead_code`) |
| **Speculative Generality** | Premature abstractions, unneeded generics, and unused parameters for hypothetical futures | `Collapse Hierarchy`, `Inline Class`, `Remove Parameter` (YAGNI principle) |

### 5. Couplers (Excessive coupling between classes or delegators)
| Code Smell | Architectural Risk & Violation | Authoritative Refactoring Moves |
| :--- | :--- | :--- |
| **Feature Envy** | Method accesses data of another object more than its own data | `Move Method` into the data owner, `Extract Method` |
| **Inappropriate Intimacy** | Classes delving directly into each other's private fields or implementation details | `Move Method`, `Move Field`, `Encapsulate Field`, `Hide Delegate` |
| **Message Chains** | Caller navigates through object graphs: `a.getB().getC().getD().doSomething()` | `Hide Delegate`, `Extract Method`, enforce Law of Demeter |
| **Middle Man** | Class that merely delegates all its work to another class without adding value | `Remove Middle Man` (talk directly to the delegate) |

---

## Pillar 2: Ousterhout's Philosophy of Software Design (APOSD)

1. **Deep Modules vs Shallow Modules**:
   - The depth of a module is its functionality divided by its interface complexity:
     $$\text{Module Depth} = \frac{\text{Internal Capability \& Complexity Concealed}}{\text{Interface Surface Area}}$$
   - **Deep Modules (Required)**: Small, simple, narrow interfaces that hide immense internal power (e.g. `VectorCube`, `BoundedCubeCache`, `OnceLock<Orchestrator>`).
   - **Shallow Modules / Classitis (Forbidden)**: Prohibit shallow passthrough classes where interface size equals or exceeds implementation logic. If a class merely redirects calls to another service without transformation or coordination, eliminate it (`Remove Middle Man` / `Inline Class`).
2. **Information Hiding vs Information Leakage**:
   - Internal data structures (raw Avro schemas, BSON binary frames, database connection pools, memory layouts) must never escape module boundaries.
   - Modules must possess single-place knowledge ownership: changing an internal serialization scheme must never impact callers outside that codec module.
3. **"Define Errors Out of Existence"**:
   - Design APIs so abnormal situations are handled naturally or cannot happen:
     - Return empty slices or collections (`Collections.emptyList()`, `&[]`) instead of throwing exceptions or panicking when a query matches nothing.
     - Use compile-time `Typestate` or Java 21 `sealed interface` so illegal execution states fail compilation rather than throwing runtime errors.

---

## Pillar 3: Robert C. Martin's Clean Code & Clean Architecture

1. **Single Level of Abstraction Principle (SLAP)**:
   - Every function must operate at exactly one level of abstraction. High-level orchestrators do not manipulate raw byte arrays, bitmasks, or HTTP headers directly.
2. **The Inward Dependency Rule**:
   - `Frameworks & Drivers` $\rightarrow$ `Interface Adapters` $\rightarrow$ `Application Use Cases` $\rightarrow$ `Pure Domain Entities`.
   - Source code dependencies point strictly inward. Domain entities must never import Spring Boot, AWS SDK, Tokio, or database libraries.
3. **Cognitive Complexity $\le 10$ & Maximum Nesting Depth $= 1$**:
   - Invert conditional checks and exit early using **Guard Clauses**:
     - **Rust**: `let Some(value) = opt else { return Ok(None); };` or `?` operator.
     - **Java 21**: Early `if (!valid) { return ...; }`.
   - Maximum function body length is **35 lines** (excluding doc comments).
   - Maximum controller/facade length is **120 lines**.

---

## Pillar 4: Eric Evans' Domain-Driven Design (DDD)

1. **Ubiquitous Language (Zero Cryptic Abbreviations)**:
   - Code must reflect real domain language without cryptic shortening.
   - **Forbidden**: `sta`, `thpt`, `trh`, `rad`, `ev`, `dur`, `gw`, `cnt`, `cap`, `tmp`, `val`, `res2`.
   - **Required**: `station`, `throughput`, `radio`, `event`, `duration`, `gateway`, `count`, `capability`.
2. **Value Objects over Primitive Obsession**:
   - Domain primitives must be wrapped into immutable, self-validating Value Objects:
     - **Rust**: `struct MacAddress([u8; 6]);`, `struct TimeSlot(u16);`
     - **Java 21**: `public record MacAddress(String value) { ... }`
3. **Aggregate Roots & Bounded Contexts**:
   - External callers interact only with Aggregate Roots (`MaterializedHomeState`), preserving domain invariants across internal entities.
   - Bounded Context boundaries must be strictly defended via Anticorruption Layers (`codecs` $\rightarrow$ `domain`).

---

## Pillar 5: Canonical Language Idioms (Effective Java & Rust API Guidelines)

### Modern Java 21 (Joshua Bloch *Effective Java* Standards)
- **Item 1 & 2 (Static Factories & Builders)**: Use static factory methods (`DeviceSummaryDto.from(entity)`) or builders for objects with $>3$ parameters.
- **Item 16 & 17 (Immutability & Records)**: Use Java 21 `record` for all DTOs, value objects, and commands. Make nested collections immutable (`List.copyOf`).
- **Item 18 & 19 (Composition over Inheritance & Sealed Hierarchies)**: Favor composition over inheritance. Use `sealed interface` for closed domain hierarchies.
- **Item 54 (Empty Collections over Null)**: Return `Collections.emptyList()` or `Optional.empty()`, never return `null`.
- **Item 73 (Exception Translation)**: Catch low-level adapter exceptions (AWS SDK, HTTP, SQL) at the boundary and translate them to strongly typed domain exceptions.
- **Modern Concurrency**: Project Loom virtual threads (`spring.threads.virtual.enabled=true`), zero carrier-thread pinning (`ReentrantLock` over `synchronized`), and `StructuredTaskScope`.

### Official Rust API Guidelines (`C-*`)
- **`C-NEWTYPE`**: Use tuple structs (`struct NodeIndex(u8);`) for zero-cost type safety and semantic clarity.
- **`C-COMMON-TRAITS`**: Implement `Debug, Clone, PartialEq, Eq, Hash, Default` for value types. Implement `Copy` when size $\le 16$ bytes.
- **`C-CONV`**: Adhere to standard conversion naming conventions (`as_` for zero-cost view, `to_` for expensive copy, `into_` for ownership consumption).
- **`C-GETTER`**: Field getters omit `get_` prefix (`pub fn mac(&self) -> &MacAddress`).
- **`C-CUSTOM-TYPE`**: Model domain and adapter errors using `thiserror` enums with specific variants. Ban `.unwrap()` and `.expect()` in production code.

---

## Verification & Automated Audit

Run the deterministic Clean Code & Cognitive Complexity scanner on any target directory or file:

```bash
kaizen-kit clean-code <path>
```
