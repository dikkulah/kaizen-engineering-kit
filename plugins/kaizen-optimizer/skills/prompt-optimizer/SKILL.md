---
name: prompt-optimizer
description: Prompt Optimization, Multi-Workstream Decomposition (WS-1..WS-N), and Cross-Chat Decision Ledger sync.
---

# Prompt Optimizer Skill (`prompt-optimizer`)

Decomposes compound developer requests into structured, dependency-ordered workstreams (`WS-1..WS-N`):
1. **Architecture & Git Workstream**: Baseline setup, branch verification, and dependency isolation.
2. **Domain & Backend Workstream**: Core algorithms, data structures, and contract implementations.
3. **Contracts & Cleanup**: OpenAPI / protobuf / JSON schema parity and dead code removal.
4. **Verification Workstream**: Unit, integration, and property-based regression proofs.
