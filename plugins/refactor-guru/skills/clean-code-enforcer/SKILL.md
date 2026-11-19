---
name: clean-code-enforcer
description: Enforces production-grade Clean Code, Cognitive Complexity <= 10, Guard Clauses, and Zero Nested Pyramids.
---

# Clean Code Enforcer Skill (`clean-code-enforcer`)

Enforces production-grade Clean Code invariants across languages:
1. **Cognitive Complexity `<= 10`**: Break down monolithic methods with high branching.
2. **Guard Clauses & Inverted Logic**: Maximum nesting depth is 1. Early-exit guards replace nested `if-else` pyramids.
3. **Deep Modules (Ousterhout APOSD)**: Simple interfaces hiding rich, robust implementations.
4. **Define Just Before Use**: Compute expensive parameters only when validated and immediately needed.
