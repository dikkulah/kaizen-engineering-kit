---
name: apidoc-sync
description: >
  Investigate endpoint changes across serverless/backend repositories and synchronize
  OpenAPI/Swagger documentation without requiring legacy Java controllers or models.
  Orchestrates 0-token endpoint diffing and hybrid Claude/Antigravity SDK doc updates.
  Use when an endpoint is added, modified, or migrated out of CloudGatewayApi while keeping
  OpenAPI documentation intact and up to date.
---

# API Documentation Synchronizer (`apidoc-sync`)

## Workflow

1. **Endpoint & Contract Change Detection (`0-Token` Scan)**:
   - Extract route definitions, query parameters, and response payload fields from the active endpoint handlers and compare them against the published OpenAPI specification shards.
2. **Decoupled OpenAPI Preservation (Zero Java Model Dependency)**:
   - Maintain static OpenAPI path and schema definitions so deleted legacy controllers and models remain documented in Swagger/Portal outputs without keeping dead Java classes in the codebase.
3. **Hybrid Antigravity SDK + Claude Documentation Update**:
   - Using the `antigravity-claude-bridge` (`agy-flow`) pattern from conversation `previous-architecture-session`, inspect any modified endpoint's request/response contract, update the corresponding OpenAPI YAML description/schema definitions, and verify consumer TypeScript types.
4. **Deterministic Proof Gate**:
   - Execute the OpenAPI specification test suite to confirm all paths, tags, descriptions, and schemas generate cleanly with zero missing references.

## CLI Helper (v2.0 — Supports Single-Crate `VectorCube` Router Directories & Multi-Crate Files)

```bash
# Single-Crate VectorCube (scans src/rest/router/*.rs + src/http_router.rs automatically):
kaizen-kit apidoc-sync \
  --http-api c:/work-space/kaizen-serverless-lambda/ondemand-telemetry-engine/src \
  --openapi-spec c:/work-space/kaizen-core-backend/cloud_gateway_api/src/main/resources/openapi/shared/ondemand-paths-and-schemas.yaml

# Self-test:
kaizen-kit apidoc-sync --self-test
```


