---
name: java-springboot-aws-craft
description: >-
  Modern Java 21 + Spring Boot 3.4/4 + AWS Cloud & Serverless engineering: Joshua Bloch
  Effective Java (Items 1, 2, 16, 17, 18, 19, 54, 73), ArchUnit Clean Architecture layer
  boundaries, Project Loom Virtual Threads & Structured Concurrency (zero carrier pinning),
  layered REST API design, RestClient + Apache5HttpClient pooling, SnapStart (CRaC),
  and RFC 9457 ProblemDetail error handling. Use when building, reviewing, or optimizing
  Java/Spring Boot microservices.
---

# Java 21 + Spring Boot 3.4/4 + AWS Cloud Engineering Craft

Comprehensive architecture, API patterns, and concurrency best practices for scalable, production-grade Spring Boot microservices on AWS, authoritatively grounded in:
- **Joshua Bloch (*Effective Java*, Modern Java 21 Edition)**
- **Robert C. Martin (*Clean Architecture*) & ArchUnit Layer Enforcement**
- **Project Loom Virtual Threads & JEP 453 Structured Concurrency**
- **Eric Evans (*Domain-Driven Design*) Tactical Java Records**
- **Kaizen Senior Reviewer Guardrails (Core Java & Clean Architecture Invariants)**

---

## 1. Joshua Bloch's *Effective Java* (Modern Java 21 Standards)

Production Spring Boot code must strictly apply canonical *Effective Java* patterns adapted for Java 21:

| Effective Java Item | Core Principle | Modern Java 21 & Serverless Implementation |
| :--- | :--- | :--- |
| **Item 1: Static Factories** | Consider static factory methods instead of public constructors | Provide expressive factory methods on domain records: `public static DeviceSummaryDto from(MaterializedDevice device)`. Allows caching, encapsulation of creation invariants, and cleaner calling code. |
| **Item 2: Builders** | Consider a builder when constructor parameters exceed 3 | For complex configuration or query parameter objects, provide a builder or record factory to prevent parameter transposition bugs. |
| **Item 16: Accessors** | In public classes, use accessor methods, not public fields | Never expose public mutable fields. Use Java 21 `record` component accessors (`record.gatewayMac()`) or `private final` fields with accessors. |
| **Item 17: Immutability** | Minimize mutability | All DTOs, domain value objects, and commands must be Java 21 `record`s. Defensively copy mutable collections: `this.tags = List.copyOf(tags);`. Deeply immutable objects are inherently thread-safe for shared caches. |
| **Item 18: Composition** | Favor composition over inheritance | Ban open concrete class inheritance. Favor composing independent components and services via constructor injection. |
| **Item 19: Sealed Hierarchies** | Design and document for inheritance or else prohibit it | Prohibit open class hierarchies. Represent closed domain models using `sealed interface` permitted exclusively to specific records or final classes. |
| **Item 54: Empty Collections** | Return empty collections or optionals, not nulls | Methods returning collections must return `Collections.emptyList()` or `List.of()`, NEVER `null`. Methods returning nullable single results must return `Optional<T>`. |
| **Item 73: Exception Translation** | Throw exceptions appropriate to the abstraction | Catch low-level AWS SDK (`AwsServiceException`), HTTP client (`HttpClientErrorException`), or SQL exceptions at adapter boundaries and translate them to strongly typed domain exceptions. |

### Sealed Domain Hierarchy Pattern (Items 18 & 19)
```java
public sealed interface TelemetryEnvelope permits AvroEnvelope, BsonEnvelope {
    Instant timestamp();
    MacAddress gatewayMac();
}

public record AvroEnvelope(
    Instant timestamp,
    MacAddress gatewayMac,
    String eventSchema,
    byte[] payload
) implements TelemetryEnvelope {
    public AvroEnvelope {
        Objects.requireNonNull(timestamp, "timestamp must not be null");
        Objects.requireNonNull(gatewayMac, "gatewayMac must not be null");
        payload = payload != null ? payload.clone() : new byte[0];
    }
}

public record BsonEnvelope(
    Instant timestamp,
    MacAddress gatewayMac,
    String collectionName,
    Map<String, Object> document
) implements TelemetryEnvelope {
    public BsonEnvelope {
        Objects.requireNonNull(timestamp, "timestamp must not be null");
        Objects.requireNonNull(gatewayMac, "gatewayMac must not be null");
        document = document != null ? Map.copyOf(document) : Map.of();
    }
}
```

---

## 2. Project Loom Virtual Threads & Structured Concurrency

1. **Enable Virtual Threads (`application.yml`)**:
   ```yaml
   spring:
     threads:
       virtual:
         enabled: true
   ```
2. **Zero Carrier-Thread Pinning Rule**:
   - **Never** use `synchronized` blocks/methods around blocking I/O, HTTP calls, or JDBC queries—this pins the virtual thread to its underlying OS carrier thread.
   - Always replace `synchronized` with `java.util.concurrent.locks.ReentrantLock` or `StampedLock`.
3. **Structured Concurrency with `StructuredTaskScope` (JEP 453)**:
   Use structured task scopes for fan-out operations, ensuring child subtasks are cleanly joined or cancelled on failure:
   ```java
   public HomeTelemetrySummary aggregateHomeData(String homeId) throws InterruptedException, ExecutionException {
       try (var scope = new StructuredTaskScope.ShutdownOnFailure()) {
           Subtask<DeviceTopology> topologyTask = scope.fork(() -> fetchTopology(homeId));
           Subtask<WifiMetrics> wifiMetricsTask = scope.fork(() -> fetchWifiMetrics(homeId));

           scope.join();           // Join both concurrent subtasks
           scope.throwIfFailed();  // Propagate first failure if any subtask failed

           return new HomeTelemetrySummary(topologyTask.get(), wifiMetricsTask.get());
       }
   }
   ```
4. **Never Pool Virtual Threads**:
   - Virtual threads are cheap and ephemeral; create them on-demand per task.
   - Bound downstream resource consumption using a `Semaphore` or connection pool limits (HikariCP), never by capping thread pools.

---

## 3. Clean Architecture Enforced by `ArchUnit`

Dependencies must point strictly inward (`adapter -> usecase -> domain`):
- **`domain`**: Pure Java 21 `record`s, `sealed interface`s, value objects, and domain exceptions. Zero Spring/JPA/AWS dependencies.
- **`usecase`**: Application ports (Inbound/Outbound interfaces) and orchestration interactors.
- **`adapter.inbound`**: Thin `@RestController`s, SQS message listeners, or AWS Lambda handlers (`<= 100` lines).
- **`adapter.outbound`**: `RestClient` + shared `Apache5HttpClient`, AWS SDK v2 clients, JDBC/JPA repositories.

Enforce via `ArchitectureTest.java`:
```java
@ArchTest
static final ArchRule domain_must_be_pure =
    noClasses().that().resideInAPackage("..domain..")
        .should().dependOnClassesThat().resideInAnyPackage("..springframework..", "..software.amazon..", "..jakarta..");

@ArchTest
static final ArchRule controllers_must_not_access_repositories =
    noClasses().that().resideInAPackage("..adapter.inbound..")
        .should().dependOnClassesThat().resideInAPackage("..adapter.outbound..repository..");

@ArchTest
static final ArchRule inward_dependency_rule =
    classes().that().resideInAPackage("..usecase..")
        .should().onlyDependOnClassesThat().resideInAnyPackage("..domain..", "..usecase..", "java..");
```

---

## 4. Layered REST API Design & Controller Patterns

Controllers should be ultra-thin facades delegating immediately to use cases/services:

```java
@RestController
@RequestMapping("/api/v1/resources")
@Validated
public class ResourceController {
  private final ResourceService service;

  public ResourceController(ResourceService service) {
    this.service = service;
  }

  @GetMapping
  public ResponseEntity<Page<ResourceResponse>> list(
      @RequestParam(defaultValue = "0") int page,
      @RequestParam(defaultValue = "20") int size) {
    Page<Resource> results = service.list(PageRequest.of(page, size, Sort.by("createdAt").descending()));
    return ResponseEntity.ok(results.map(ResourceResponse::from));
  }

  @PostMapping
  public ResponseEntity<ResourceResponse> create(@Valid @RequestBody CreateResourceRequest request) {
    Resource created = service.create(request);
    return ResponseEntity.status(HttpStatus.CREATED).body(ResourceResponse.from(created));
  }
}
```

### DTOs & Validation
- Always use Java 21 `record`s for immutable request/response representations.
- Apply Jakarta Bean Validation annotations (`@NotBlank`, `@Size`, `@NotNull`, `@FutureOrPresent`).

```java
public record CreateResourceRequest(
    @NotBlank @Size(max = 200) String name,
    @NotBlank @Size(max = 2000) String description,
    @NotNull Instant effectiveDate,
    @NotEmpty List<@NotBlank String> tags) {
  public CreateResourceRequest {
    tags = tags != null ? List.copyOf(tags) : List.of();
  }
}

public record ResourceResponse(Long id, String name, String status) {
  public static ResourceResponse from(Resource entity) {
    return new ResourceResponse(entity.id(), entity.name(), entity.status().name());
  }
}
```

---

## 5. Service Layer, Transactions & Data Access

1. **Transactional Boundaries**:
   - Annotate read-only queries with `@Transactional(readOnly = true)` to avoid dirty-checking overhead in Hibernate.
   - Keep transactions as short as possible; never hold open transactions during external HTTP or SQS calls.
2. **Spring Data JPA Repositories**:
   - Write explicit JPQL queries or use Spring Data derived query methods.
   - Return `Optional<T>` for single entities; return empty collections (never `null`) for list queries.

```java
@Service
public class ResourceService {
  private final ResourceRepository repository;

  public ResourceService(ResourceRepository repository) {
    this.repository = repository;
  }

  @Transactional(readOnly = true)
  public Optional<Resource> findById(Long id) {
    return repository.findById(id);
  }

  @Transactional
  public Resource create(CreateResourceRequest request) {
    ResourceEntity entity = ResourceEntity.from(request);
    return repository.save(entity).toDomain();
  }
}
```

---

## 6. RFC 9457 `ProblemDetail` Exception Handling

Centralize exception mapping in `@RestControllerAdvice` returning standard `ProblemDetail` payloads:

```java
@RestControllerAdvice
public class GlobalExceptionHandler {
  private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

  @ExceptionHandler(MethodArgumentNotValidException.class)
  public ProblemDetail handleValidation(MethodArgumentNotValidException ex) {
    ProblemDetail problem = ProblemDetail.forStatusAndDetail(HttpStatus.BAD_REQUEST, "Validation failed");
    List<String> errors = ex.getBindingResult().getFieldErrors().stream()
        .map(e -> e.getField() + ": " + e.getDefaultMessage())
        .toList();
    problem.setProperty("validation_errors", errors);
    return problem;
  }

  @ExceptionHandler(ResourceNotFoundException.class)
  public ProblemDetail handleNotFound(ResourceNotFoundException ex) {
    return ProblemDetail.forStatusAndDetail(HttpStatus.NOT_FOUND, ex.getMessage());
  }

  @ExceptionHandler(Exception.class)
  public ProblemDetail handleGeneric(Exception ex) {
    log.error("Unhandled exception caught in global handler: {}", ex.getMessage(), ex);
    return ProblemDetail.forStatusAndDetail(HttpStatus.INTERNAL_SERVER_ERROR, "An internal error occurred");
  }
}
```

---

## 7. HTTP Client, Connection Pooling & SnapStart (CRaC)

1. **Shared `RestClient` + `Apache5HttpClient`**:
   - Never instantiate `new RestTemplate()` or `HttpClient.newHttpClient()` per request.
   - Configure a single pooled `PoolingHttpClientConnectionManager` with explicit `connectTimeout` and `responseTimeout`.
2. **AWS Lambda SnapStart (`org.crac.Resource`)**:
   - Implement `org.crac.Resource` (`beforeCheckpoint` / `afterRestore`) to cleanly close ephemeral socket connections before snapshot and re-seed random generators and pools upon restore.
3. **Caching & In-Memory Storage**:
   - Annotate read methods with `@Cacheable(value = "cacheName", key = "#id")`.
   - Ensure cached objects are deeply immutable (Java 21 `record` or unmodifiable collections).

---

## 8. Senior Reviewer Guardrails (Core Java & Clean Architecture Invariants)

1. **Zero AI Traces**: Never write inline full package paths in method bodies (e.g. `java.util.List`). Cleanly import all classes at the top of the file.
2. **Single-Line Logs**: All log statements must remain on a single line to preserve CloudWatch and Kibana log parsing. Use `{}` SLF4J placeholders; never concatenate strings with `+`.
3. **No "Log & Throw"**: Never both log an exception and re-throw it in the same catch block. Either handle and log, or propagate.
4. **Define Just Before Use**: Declare variables immediately prior to their first usage. Use guard clauses to exit early (`if (!valid) return;`).
5. **Constructor Injection**: Always use constructor injection for `@Service` and `@Component` dependencies; avoid field injection (`@Autowired`).
