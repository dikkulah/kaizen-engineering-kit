---
name: documentation-lookup
description: Lookup up-to-date documentation, API references, library specifications, and code examples using native web search. Activates when the user asks setup, configuration, or API reference questions about specific frameworks or libraries (e.g., Rust crates, Spring Boot, React, Vue, AWS SDK).
---

# Documentation Lookup

When working with modern libraries, frameworks, or evolving APIs, use the native `search_web` tool to retrieve authoritative, current documentation rather than guessing or relying solely on older training weights.

## When to Use This Skill

Activate this skill when:
- Setting up or configuring third-party libraries or frameworks (e.g. Tokio, Spring Boot, Next.js, Serde, Actix, Vue 3).
- Looking up specific method signatures, type definitions, or configuration properties.
- Investigating breaking changes between major/minor library versions.
- Needing verified, canonical code patterns directly from official documentation or RFCs.

## Workflow

### 1. Formulate Targeted Search Queries
- Identify the exact library or framework name and specific topic (e.g., `"tokio::sync::broadcast"`, `"Spring Boot 3.4 RestClient timeout configuration"`).
- Target official documentation domains using the `domain` parameter in `search_web` whenever possible:
  - Rust: `docs.rs`, `tokio.rs`
  - Java / Spring: `docs.spring.io`, `baeldung.com`
  - Python: `docs.python.org`, `pypi.org`
  - Web / JS: `developer.mozilla.org`, `nextjs.org`, `vuejs.org`
  - AWS: `docs.aws.amazon.com`

### 2. Execute Web Search via Native Tool
Invoke `search_web`:
```json
{
  "query": "<library_name> <specific_function_or_topic> documentation",
  "domain": "<official_doc_domain>"
}
```

### 3. Extract and Verify
- Verify that code snippets match the target library version used in the project.
- Cross-reference configuration parameters against project requirements.
- Cite the official documentation URL in findings.
