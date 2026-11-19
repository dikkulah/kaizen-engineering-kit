---
name: refactor-radar
description: Polyglot (Rust/Java/Go) Pre-Refactor Pattern Recognition, Service Capsule Extractor, Phase 2 Federation, Interactive HTML Dashboard, and Auto-Terminating AWS EC2 GPU Spot Batch Orchestrator.
---

# Refactor Radar

Use `kaizen-kit radar` to analyze single microservices (Phase 1) or federate multiple Service Capsules across Rust, Java, and Go repositories (Phase 2).

## Commands

- Single service (local CPU/offline or remote TEI/vLLM):
  `kaizen-kit radar analyze --repo <path> --service-name <name> --capsule-out <dir>`
- Multi-service federation (cross-repo polyglot semantic clones, shared boundaries, distributed monolith coupling):
  `kaizen-kit radar federate --capsules-root <dir> --out-dir <dir>`
- Interactive HTML dashboard regeneration:
  `kaizen-kit radar dashboard --capsule <dir>`
- Ephemeral AWS `kaizen-u2` GPU Spot batch (`g5.2xlarge` / `g5.xlarge` with `vLLM Qwen2.5-Coder-14B-Instruct-AWQ` + `TEI jina-embeddings-v2-base-code` and deterministic auto-termination):
  `kaizen-kit radar spot-run --repo <name=path> ...`

