# Domain Pack: Java 21, Spring Boot 4, Loom VT & OpenAPI Slices (`java-spring-loom`)

Loaded on-demand for `*.java`, `pom.xml`, and `jvm-service-architect` across CloudGatewayApi, AAS, PortalEdgeApi, SQE, ReadModelService, and `kaizen-api-client-library`.

## 1. Kaizen Java Code Formatting & Hygiene Invariants
- **Never `== false` or `== true`**: Use `!` for booleans or Apache Commons / JDK helpers (`StringUtils.isBlank`, `StringUtils.isNotBlank`, `ObjectUtils.isEmpty`, `ObjectUtils.isNotEmpty`, `ObjectUtils.allNotNull`, `ObjectUtils.notEqual`, `!StringUtils.equalsIgnoreCase`).
- **Single-Line SLF4J `LOGGER` Calls (Mandatory)**: Never split `LOGGER.debug`, `LOGGER.info`, `LOGGER.warn`, or `LOGGER.error` across multiple lines. Keep the message and all arguments on a single line.
- **Blank Line After Early-Return `if` (Mandatory)**: Insert **1 blank line** after an `if` block whose body is only `return` or `throw`.
- **Blank Line After Method `{` Only When Body `> 5` Lines**:
  - Method/constructor body `<= 5` lines: **no** blank line after `{`.
  - Method/constructor body `> 5` lines: **1** blank line right after `{`.
- **Declare Locals Immediately Above First Use**: Never hoist local variables above early-return guard clauses that don't use them.
- **No Unsolicited Javadoc**: Never add Javadoc or block comments unless explicitly requested or required by a public OpenAPI contract.

## 2. Java 21 Virtual Threads (Loom) & AWS SDK v2 (`SQE` / Boot 3.4 & 4.x)
- **Virtual Threads Over WebFlux/R2DBC**: Prefer `spring.threads.virtual.enabled=true` with blocking-style code parked on virtual threads. Avoid `synchronized` blocks around I/O (use `ReentrantLock` to avoid carrier pinning).
- **Inject `RestClient.Builder`**: Never call `RestClient.create()` or `RestClient.builder()` directly. Always inject Spring Boot's VT-backed `RestClient.Builder`.
- **Shared Sync AWS SDK v2 (`>= 2.41.0`) + `Apache5HttpClient`**: Use a single shared `Apache5HttpClient.builder().maxConnections(httpCap).build()` across sync `SqsClient`, `S3Client`, `IotDataPlaneClient`, and `StsClient`.
- **Memory & Pool Bounded Backpressure**: Bound concurrent inflight work via Semaphore: `inflight = min(memBound, jdbcPoolCap, httpCap, maxConcurrency)`.

## 3. OpenAPI Client Slice Architecture (`kaizen-api-client-library` / `readmodel_service` / `aas`)
- **1:1 Slice Artifacts (No Wrapper/Facade Jars)**: Each OpenAPI tag/slice maps to a dedicated Maven artifact (`readmodel_service-direct-home-client`, `aas-client` URL segment packages).
- **Meaningful Method Signatures**: Never leave generated numeric suffixes (`create_2`, `create_3`). Align method names (`camelCase` verb + resource) and typed request/response bodies with the controller contract.

## 4. CloudGatewayApi & Ondemand Service Boundary Rules
- **Clean Service & Contract Boundaries**: Keep Lambda-internal storage/folding details inside the serverless telemetry engine; expose 1:1 OpenAPI-compliant JSON payloads with W3C CORS, gzip framing, and fail-closed Keycloak JWT validation.
- **No Hardcoded Production Tenant IDs**: Never hardcode test tenant UUIDs in production Java classes, and remove dead migration flags cleanly.
