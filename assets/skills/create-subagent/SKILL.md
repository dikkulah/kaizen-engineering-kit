---
name: create-subagent
description: >-
  Create, configure, and register custom subagents for specialized engineering tasks
  in Google Antigravity. Use when you need a new specialized persona (e.g. Parity Auditor,
  Migration Specialist, Test Fixture Scaffolder) with targeted tools and system prompt.
---

# Creating Custom Antigravity Subagents (`create-subagent`)

This skill defines the canonical workflow for minting and registering new subagents in Google Antigravity—either **dynamically for the current session** or **permanently across conversations**.

---

## 1. Dynamic (In-Session) vs. Permanent Subagents

| Subagent Type | Storage / Tool | Lifetime | Best For |
| :--- | :--- | :--- | :--- |
| **Dynamic In-Session** | `define_subagent` tool | Current conversation only | One-off refactoring swarms, ad-hoc exploratory tasks, temporary checkers. |
| **Permanent Global** | `~/.gemini/config/agents/<name>/agent.md` | All conversations & workspaces | Core recurring roles (e.g. `parity-runner-verifier`, `jvm-service-architect`). |
| **Plugin-Scoped** | `~/.gemini/config/plugins/<plugin>/agents/<name>.md` | As long as plugin is enabled | Packaged team capabilities (e.g. `agent-orchestrator`). |

---

## 2. Antigravity Subagent Manifest Format

Permanent subagents are written in Markdown with YAML frontmatter:

```markdown
---
name: parity-verifier
description: Executes live, cache-free U2 parity comparison runs between Legacy CloudGatewayApi and Target team-Engine.
tools:
  - read_file
  - write_file
  - edit_file
  - run_command
  - manage_task
---

# Parity Verifier Agent Instructions

You are a dedicated parity verification specialist.
When invoked:
1. Always run cold warmup first before taking benchmark measurements.
2. Compare HTTP status codes AND recursive JSON body values.
3. Classify diffs strictly: NEVER mark MATCH if populated data in CloudGatewayApi returns 0/null in target.
4. Report results in structured Markdown tables.
```

### Frontmatter Schema

| Field | Type | Requirement | Description |
| :--- | :--- | :--- | :--- |
| `name` | string | **Mandatory** | Lowercase alphanumeric with hyphens (`kebab-case`). |
| `description` | string | **Mandatory** | Concise trigger description. The primary agent uses this to decide when to delegate. |
| `tools` | list | **Optional** | List of tools granted to the subagent (e.g. `read_file`, `write_file`, `edit_file`, `run_command`, `manage_task`). |
| `hidden` | boolean | **Optional** | If `true`, hides the agent from top-level user slash menus while keeping it invokable by parent agents. |

---

## 3. 5-Tier Model Assignment Policy

When invoking or defining the subagent, assign the optimal model tier:

* **Tier 1 (`flash_lite` / `haiku`)**: Bulk mechanical tasks, repetitive DTO mapping, large log searches, formatting.
* **Tier 2 (`flash` / `sonnet`)**: Systems programming (Rust, Java 21 Loom), SQL DAG design, architecture reviews.
* **Tier 3 (`pro` / `opus` / `argon`)**: Adversarial council review, falsification, complex root-cause analysis (RCA).

---

## 4. Execution Steps to Create a Subagent

1. **Clarify the Specialty**:
   - Determine name, specific responsibility, and whether it should be permanent or dynamic.
2. **If Dynamic (Current Session)**:
   - Call `define_subagent(Name="...", Description="...", SystemPrompt="...", EnableWriteTools=True)`.
   - Invoke via `invoke_subagent(TypeName="...", Role="...", Prompt="...")`.
3. **If Permanent (Global)**:
   - Create `~/.gemini/config/agents/<name>/agent.md` (or in `~/.gemini/config/plugins/agent-orchestrator/agents/<name>.md`).
   - Include clear guidelines, guardrails, and error checklists.
   - Verify discovery via subagent inventory.
