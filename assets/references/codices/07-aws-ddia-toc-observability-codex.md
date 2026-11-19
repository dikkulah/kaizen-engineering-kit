# Canonical Codex 07 — AWS Operational Hard-Limits, *DDIA*, Goldratt *TOC* & Popperian RCA (`[AWS-*]`, `[DDIA-*]`, `[TOC-*]`, `[RCA-*]`)

Offline-first authoritative reference engineered specifically so agents **never** rely on shallow marketing summaries or missing MCP servers when reasoning about AWS production quotas, wire framing, distributed systems (*Designing Data-Intensive Applications* — Martin Kleppmann), *Theory of Constraints* (Eliyahu Goldratt), FinOps, and Root-Cause Analysis (Karl Popper & David Agans).

---

## 1. AWS Hard Operational Limits & Wire Contracts (`[AWS-01 .. AWS-09]`)

### 1.1 `[AWS-01]` AWS Lambda + Application Load Balancer (ALB) vs. Function URL Matrix

| Operational Dimension | ALB Target Group -> Lambda (`Synchronous`) | Lambda Function URL / API Gateway HTTP API | Architectural Consequence |
| :--- | :--- | :--- | :--- |
| **Max Request + Response Payload** | **1 MB (`1,048,576` bytes) HARD CEILING** (including status line, headers, and Base64 inflation!) | **6 MB (`6,291,456` bytes)** buffered (`20 MB` only in `RESPONSE_STREAM` mode) | **CRITICAL**: Base64 encoding inflates binary gzip size by **$\times 4/3$ (`+33.3%`)**! To stay safely under ALB's `1 MB` total limit with headers, raw gzipped bytes MUST stay **`<= 720 KB`** (`flate2` gzip + `Content-Encoding: gzip` + `"isBase64Encoded": true`). |
| **Multi-Value Headers** | Depends on Target Group `lambda.multi_value_headers.enabled` (`headers` vs `multiValueHeaders` JSON shape!) | Payload Format `2.0` joins cookies/headers differently from `1.0` | Universal parser in Lambda router MUST accept **both** v1 (`headers` / `multiValueHeaders`) and v2 (`rawPath`, `requestContext.http`) event envelopes! |
| **Streaming (`SSE` / Chunked)** | **NOT supported** by ALB -> Lambda (`Invoke` buffered only) | Supported via `InvokeWithResponseStream` (`RESPONSE_STREAM`) on Function URL | ALB endpoints must return complete buffered JSON/gzip payloads within timeout; SSE requires Function URL or ECS/ALB target. |
| **Init Phase Timeout** | **10 seconds hard cap** on standard on-demand cold init | **10 seconds hard cap** | Never run heavy network fetches during `OnceLock` cold init—embed Avro schemas at compile time (`0ms`)! |
| **Ephemeral `/tmp` Storage** | Default **`512 MB`** (configurable up to `10,240 MB`); shared across warm invocations in same sandbox | Same (`512 MB` default) | Enforce bounded eviction in `/tmp` L2 cache (`rm` oldest `.ondemand` snapshots before `ENOSPC` / `No space left on device`!) + atomic write-temp-then-rename (`tmp -> final`)! |
| **vCPU Scaling Ratio** | **`1,769 MB` RAM = 1 full Graviton3 vCPU** (`3,538 MB` = 2 vCPUs) | Same | Below `1,769 MB`, CPU time is fractionally throttled—parallel `JoinSet` decompression or crypto on `256 MB` starves CPU and inflates `GB-s` latency! Sweet spot for bursty VectorCube compute: **`1,769 MB – 3,008 MB` ARM64**. |

### 1.2 `[AWS-02]` Amazon S3 Performance, Consistency & Cost Traps
- **Consistency**: Strong read-after-write consistency for `GET`, `PUT`, `LIST` since Dec 2020.
- **Throughput Quotas**: **`5,500 GET/HEAD/s`** and **`3,500 PUT/COPY/POST/DELETE/s` per partitioned prefix** (`HTTP 503 Slow Down` if exceeded without exponential jittered backoff!).
- **Request Cost Reality**: S3 `LIST` (`$0.005 / 1,000`) costs **12.5x more** per call than S3 `GET` (`$0.0004 / 1,000`)! Never run unbounded `ListObjectsV2` scans on every UI poll—index object keys via DynamoDB time-range partition keys or cache L2/L3 consolidated `.zst` snapshots!
- **Small-Object Tax**: Reading 400 tiny 8 KB Avro files per home costs `400x` `GET` TTFB (`~35–80ms` p95 tail) + `400x` request charges. Coalesce into a single zstd-compressed L3 snapshot (`1 GET`) with `LastModified` TTL validation.

### 1.3 `[AWS-03]` Amazon DynamoDB Partition & Query Laws
- **Hard Item Size Limit**: **`400 KB`** (attribute names + values). Never store unbounded home/station arrays in a single DynamoDB item!
- **`Query` Page Ceiling**: Single `Query` / `Scan` response reads at most **`1 MB` of data before filter expressions** (`FilterExpression` saves network bytes, **NOT** RCU consumption!). Always paginate `LastEvaluatedKey` and push range bounds into `KeyConditionExpression` (`PK = :home AND SK BETWEEN :t0 AND :t1`).
- **Partition Throughput**: **`3,000 RCU` / `1,000 WCU` per physical partition** (`Adaptive Capacity` helps, but single hot partition keys still throttle with `ProvisionedThroughputExceededException`!).
- **Consistency**: Eventually consistent read = `0.5 RCU` per `4 KB`; Strongly consistent read = `1.0 RCU` per `4 KB`.

### 1.4 `[AWS-04]` CloudWatch Logs, EMF & X-Ray Trace Grammar
- **Trace Header Grammar**: `X-Amzn-Trace-Id: Root=1-<8-hex-epoch-sec>-<24-hex-unique-id>;Parent=<16-hex>;Sampled=<0|1>`.
  - First 8 hex digits after `Root=1-` encode the exact **Unix epoch timestamp in seconds** (`int("6730c1a0", 16)`)—use this to bound CloudWatch `start-time` / `end-time` within `±5 minutes` instead of scanning 24 hours of TB-scale logs (`FinOps` $0.005/GB scanned)!
- **Ingestion Cost Trap**: CloudWatch Logs standard ingestion is ~`$0.50–$0.57/GB` (`eu-west-1` list baseline before EDP) + storage. Logging a 4 KB JSON payload inside a 100-item station loop at `INFO` burns megabytes per home request! Keep per-request summary to **1 structured single-line log** (or EMF metric) and guard payload dumps behind `DEBUG`.
- **Kaizen Multi-Account Tenancy Law (`SEC-AWS-TENANCY-ISOLATION`)**:
  - **41 AWS accounts are strictly READ-ONLY** (`kaizen-prod`, `kaizen-staging`, `production`, `testing`, `airdi-*`, all operator accounts).
  - **UCA Staging (`*-uca*` log groups)** lives inside **`AWS_PROFILE=kaizen-prod`** (NOT `kaizen-staging`!).
  - **Only `--profile kaizen-u2`** is in operational scope, and **every mutating command in `u2` requires explicit prior user confirmation**.

### 1.5 `[AWS-05]` AWS Bedrock (`eu-west-1`) Data-Residency & Converse API Laws
- Corporate source code and customer telemetry stay strictly inside Kaizen AWS tenant (`eu-west-1` cross-region inference profiles `eu.anthropic.claude-*`).
- Handle `ThrottlingException` (`429`) and `ModelStreamErrorException` with full-jitter exponential backoff; cap `maxTokens` and prompt caching (`cachePoint`) on static system/codex blocks when supported.

---

## 2. Kleppmann *DDIA* & Goldratt *Theory of Constraints* (`[DDIA-*]`, `[TOC-*]`)

1. **`[DDIA-01]` Tail Latency Amplification (Fan-Out Math)**:
   - If 1 request fans out to $k = 50$ parallel S3 `GET` calls and single-call $p99 = 120\text{ ms}$, the probability that **at least one** call hits $p99$ is $1 - (0.99)^{50} = \mathbf{39.5\%}$! Meaning your $p99$ single-object tail becomes your **$p60$ median-ish user experience**!
   - *Mitigation*: Two-tier caching (`L1 RAM -> L2 /tmp -> L3 consolidated S3 .zst`), bounded concurrency (`Semaphore`), and hedging/timeouts.
2. **`[DDIA-02]` Thundering Herd / Cache Stampede Prevention (`Singleflight 14 -> 1`)**:
   - When TelemetryDashboard opens a home dashboard, the browser fires 14–26 slice endpoints simultaneously (`stations`, `nodes`, `wifiLinks`, `ethLinks`, `ethMeshLinks`, `speedtest`, etc.).
   - Without principal-isolated Singleflight + warm L1/L2 `VectorCube` sharing, 14 parallel requests execute $14 \times$ identical DynamoDB + S3 + Avro decode work! With Singleflight + L1/L2, 1 leader folds the cube and 13 followers read the materialized snapshot in `<2ms`.
3. **`[DDIA-03]` Deterministic Fold Idempotency & Ordering**:
   - S3/Kafka/Kinesis telemetry can arrive out-of-order or contain duplicate deliveries. Event folding into `VectorCube` MUST sort deterministically by `(event_timestamp_ms, sequence_or_object_key)` and deduplicate idempotently so re-folding the exact same window produces byte-identical JSON (`POPULATED_MATCH`).
4. **`[TOC-01]` Eliyahu Goldratt's 5 Focusing Steps**:
   - *Step 1 Identify Constraint*: Measure real `Server-Timing` (`ddb`, `s3`, `decode`, `fold`, `gzip`, `total`) before optimizing anything!
   - *Step 2 Exploit Constraint*: Eliminate wasted work on the bottleneck (`Source-Level Elimination` of unused Avro branches during `decode`).
   - *Step 3 Subordinate Everything Else*: Bound upstream concurrency to protect the bottleneck.
   - *Step 4 Elevate Constraint*: Add L1/L2/L3 caching or Graviton3 SIMD/memory tuning.
   - *Step 5 Prevent Inertia*: Re-measure `Server-Timing`—once `s3` drops from `400ms -> 0ms` (warm L2), the constraint shifts!
   - **Rule of Zero Illusion**: Optimizing a `0.4ms` projection loop when S3 cold fetch takes `650ms` is pure engineering waste (*Muda*).
5. **`[TOC-02]` Realistic EDP FinOps Calculation Discipline**:
   - Never claim `$0.00` for serverless! Always compute explicit monthly formula:
     $$\text{Cost}_{\text{monthly}} = N_{\text{req}} \times \left( C_{\text{invoke}} + T_{\text{sec}} \times M_{\text{GB}} \times C_{\text{GB-s, ARM64}} + N_{\text{S3 GET}} \times C_{\text{GET}} + B_{\text{CW GB}} \times C_{\text{CW}} \right) \times (1 - d_{\text{EDP}})$$
     labeled clearly as an *"AWS Pricing rough estimate (tahmini kaba hesap)"*.

---

## 3. Karl Popper Falsification & David Agans' 9 Debugging Rules (`[RCA-01 .. RCA-09]`)

1. **`[RCA-01]` Understand the System (Read the Actual Spec & Code, Never Guess)**: Read the exact Avro `.avsc`, OpenAPI contract, and upstream Java/Rust mapper before forming any theory.
2. **`[RCA-02]` Make It Fail Deterministically**: Capture the exact failing input (HAR request URL/headers, raw S3 Avro/BSON fixture, `Root=1-<hex>` trace) and encode it into a reproducible automated unit/integration test that **fails (`RED`)** right now!
3. **`[RCA-03]` Quit Thinking and Look (`Genchi Genbutsu`)**: Inspect raw wire headers (`Access-Control-Allow-Origin: *, *` duplicate header proof!), raw CloudWatch stack traces, and raw Avro field values—stop speculating from memory!
4. **`[RCA-04]` Divide and Conquer (Binary Search Across the 5-Stage Pipeline)**: Bisect `UI -> Proxy/ALB -> BFF/CloudGatewayApi -> Lambda Router -> Codec/Cube -> S3/DDB` (and `git log -S` / `git bisect` across commit history) to isolate the exact faulty boundary in $O(\log N)$ steps.
5. **`[RCA-05]` Change One Thing at a Time**: Never bundle 4 speculative tweaks into one blind hotpatch; isolate the exact variable responsible.
6. **`[RCA-06]` Keep an Audit Trail (`kaizen_ledger.json` & `DEAD_ENDS.md`)**: Record every falsified hypothesis and dead-end so neither you nor a teammate repeats it.
7. **`[RCA-07]` Check the Plug (Question Base Assumptions)**: Are you hitting the right environment (`u2` vs `uca` vs `k2.stg`)? Is `Lambda == Lambda` comparing the service against itself? Did a cached `/tmp` file mask your code change? Are credentials/CORS headers mutated by an upstream proxy (`Express` / `ALB`)?
8. **`[RCA-08]` Get a Fresh View (Adversarial Falsification)**: Ask: *"What input would still break my proposed fix?"* (`EMPTY_BOTH_200` vacuous pass check, `null`/missing optional fields, multi-node MLO, 0-station homes).
9. **`[RCA-09]` If You Didn't Fix It, It Ain't Fixed (Shared Root-Cause Sweep)**: After turning the reproduction test **`GREEN`**, grep all sibling endpoints/mappers for the identical pattern and verify zero regressions across the entire suite!

