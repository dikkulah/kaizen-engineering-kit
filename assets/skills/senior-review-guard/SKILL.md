---
name: senior-review-guard
description: Kaizen Cloud Senior Code Reviewer Simulator & Jenkins Pre-Flight CI Gate. Audits Git diffs, files, or PRs against the exact standards and pet peeves of senior engineers (Core-Java, Concurrency-Security, Vue-State, Identity-Boundary, FinOps-Observability, Resilience-DTO, DTO-Hygiene) and prevents Jenkins validation build failures.
---

# Senior Review Guard & Jenkins Pre-Flight CI Gate

Use this skill whenever:
1. Writing or modifying Java, Rust, TypeScript/Vue, or Docker/Compose code.
2. Preparing to push a commit or open a GitLab Merge Request (MR).
3. The user asks to "review my code like our seniors", "kıdemli inceleme standardına göre kontrol et", "jenkins validation fail olmasın", or "MR öncesi kontrol et".

---

## 1. The 6-Senior Review Simulation Workflow

When triggered, run an automated scan across the current git diff (`git diff HEAD~1` or unstaged diff):

```
+-------------------------------------------------------------+
|                     SENIOR REVIEW COUNCIL                   |
+-------------------------------------------------------------+
| 1. Core Java & Clean Architecture      -> Java Clean Code, Single-line logs, |
|                          No AI inline paths, Guard Clauses  |
| 2. Systems Concurrency & Fail-Closed Security     -> Singleflight principal keys,       |
|                          Fail-closed auth, Deep immutability|
| 3. Vue 3 & Reactive UI State       -> Vue dynamic prop watchers,         |
|                          Pagination reset, No table wiping  |
| 4. Identity & Service Boundaries      -> Core Keycloak tokens, Clean models |
| 5. FinOps & CloudWatch Observability     -> 40x/50x error logs, Cache FinOps   |
| 6. Resilience & DTO Hygiene-> Generic exception catch, Clean DTOs|
+-------------------------------------------------------------+
                              |
                              v
+-------------------------------------------------------------+
|                 JENKINS PRE-FLIGHT SHIELD                   |
| 1. git fetch origin master && rebase check (Fast-Forward)   |
| 2. Local typecheck/compile gate (0 errors)                  |
| 3. Cross-repo dependency publishing check                   |
+-------------------------------------------------------------+
```

---

## 2. Reviewer Checklist Matrix

### Core Java & Clean Architecture Checklist:
- [ ] No `java.util.*` or full package names in code bodies.
- [ ] All `LOG.*` statements are single-line, using `{}` SLF4J formatting.
- [ ] No "Log & Throw" in catch blocks.
- [ ] No line splitting under 120 chars for simple calls.
- [ ] "Define just before use" respected; no variable computed before early exit.
- [ ] Spring `@Service` / `@Component` used instead of static methods.
- [ ] `StringUtils`, `CollectionUtils`, `MapUtils`, `ObjectUtils` used.
- [ ] Java 21 `list.getFirst()` / `list.getLast()` used.
- [ ] No repeated method invocations without local variable caching.

### Systems Concurrency & Fail-Closed Security Checklist:
- [ ] Singleflight cache keys include caller identity / token principal.
- [ ] Missing auth metadata fails closed (`deny`).
- [ ] Cached DTOs are Java records or defensively copied.
- [ ] End-to-end operation deadline used; no POST/PUT timeout retries.
- [ ] Lambda native dependencies pinned (`--os=linux --cpu=arm64`).
- [ ] OpenAPI examples match domain constants exactly.

### Vue 3 & Reactive UI State Checklist:
- [ ] Composable / editor initialized with prop has dynamic `watch`.
- [ ] Table records not cleared before async fetch succeeds.
- [ ] `resetPagination()` invoked on refresh / delete / create.
- [ ] Search preserves active filters.
- [ ] No name collision between local computed refs and auto-imported utils.
- [ ] Form submit buttons hidden / disabled in read-only mode (`v-if="!isReadOnly"`).

### Identity & Service Boundaries & FinOps & CloudWatch Observability Checklist:
- [ ] Tokens acquired from core Keycloak.
- [ ] Upstream HTTP 40x/50x responses logged as explicit errors.
- [ ] High-frequency production logs set to `DEBUG`.
- [ ] DynamoDB cached schedule checked before decompressing hourly data.

### Resilience & DTO Hygiene Checklist:
- [ ] Generic `catch (Exception e)` present alongside specific catches.
- [ ] Typed result objects returned instead of booleans.
- [ ] Zero dead lines or orphan variables.

---

## 3. Jenkins Pre-Flight CI Checklist

Before pushing to GitLab:
0. **Automated 6-Senior & Fast-Forward Lint Gate**:
   ```powershell
   kaizen-kit senior-lint . --pre-flight
   ```
1. **Rebase Check**:
   ```bash
   git fetch origin master
   git merge-base --is-ancestor origin/master HEAD
   # If false, RUN: git rebase origin/master
   ```
2. **Local Validation**:
   - Java: `mvn test-compile`
   - TypeScript: `npm run typecheck && npm run lint`
   - Rust: `cargo check --all-targets`
