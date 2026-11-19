# Agent Agency & Multi-Provider SDK Matrix (`docs/AGENCY_MATRIX.md`)

The `kaizen-engineering-kit` decouples agent persona specifications, domain rules, and deterministic gates from any single framework. It supports **Google ADK**, **OpenAI Agents SDK / Swarm**, **Anthropic Claude Agent SDK**, **LangGraph**, **Microsoft AutoGen**, and **CrewAI** across local open-weights and frontier cloud models.

---

## 1. The 5-Tier Agent Routing Matrix

Specialist personas are tiered by cognitive difficulty, required context depth, and cost profile:

| Tier | Model Class | Scope & Mandate | Example Specialist Roles |
| :--- | :--- | :--- | :--- |
| **Tier 3: Supreme Judge / RCA** | Frontier Reasoning (`Claude Opus 5.5`, `OpenAI o3`, `DeepSeek-R1-32B`) | Multi-repo architecture, Git blame pickaxe forensics, distributed race conditions, Popperian TDD falsification. | `incident-rca-falsifier`, `bedrock-judge`, `reality-checker` |
| **Tier 2: Systems Architect** | High-Throughput Frontier / MoE (`Claude Sonnet 5.5`, `Qwen3.5-35B-A3B`, `Nemotron-30B-A3B`) | Clean Architecture layer boundaries, Evans DDD, Ousterhout APOSD Deep Modules, OpenAPI contract parity. | `systems-architect`, `jvm-service-architect`, `data-sql-architect`, `refactoring-specialist` |
| **Tier 1: Mechanical Worker** | Fast CPU / Edge (`Claude Haiku 5.5`, `Qwen3.5-4B`, `Nemotron-Nano-9B-v2`, `BitNet-2B`) | Guard-clause flattening, AST skeleton extraction, DTO boilerplate, test fixture scaffolding. | `bedrock-worker`, `code-explorer`, `docs-spec-analyst` |
| **Tier 0: Pre-Flight Gate** | Zero-LLM Deterministic CLI / Rust AST (`radar-core`, `check_clean_code`, `senior_review_lint`) | Hard linting, cognitive complexity `<= 10`, secret scans, fast-forward git rebase checks (`<50ms`). | `PreToolUse`, `PostToolUse`, `Stop` hooks |
| **Night Batch Sweeper** | Overnight 50%-Off Batch (`GPT-5.4`, `Claude Sonnet 5.5`, `DeepSeek-V3.2`) | Unattended codebase sweep, dead code elimination, Best-of-N QLoRA pair synthesis. | `NightShiftPlan`, `AutoDream` |

---

## 2. Multi-Provider Agent SDK Support

### SDK Comparison

| Framework | Primary Strength | Recommended Use in Kaizen Kit |
| :--- | :--- | :--- |
| **Google ADK (`google-genai`)** | High context (1M), native structured tools, MapReduce pipelines | Heavy architectural diff synthesis and 7-MCP tool orchestration |
| **OpenAI Agents SDK (`openai-agents` / Swarm)** | Lightweight swarm handoffs, declarative routines, function calling | Fast multi-agent handoffs between Triage, Coder, and Verifier |
| **Anthropic Claude Agent SDK** | 1h Prompt Cache integration, fine-grained tool use & computer use | Deep Clean Architecture reviews and git log pickaxe investigations |
| **LangGraph / LangChain** | Cyclic graph workflows, state persistence, conditional branch gating | Multi-stage verification cascades with human-in-the-loop checkpoints |
| **Microsoft AutoGen** | Multi-agent group chat, conversable agents, isolated sandboxes | Adversarial code review council (Architect vs. Security vs. QA) |
| **CrewAI** | Role-based teams (Manager, Researcher, Developer, Verifier) | Hierarchical sprint execution and PR description drafting |

---

## 3. Implementation Recipes by SDK

### 1. OpenAI Agents SDK (`openai-agents` / Swarm)

```python
from kaizen_kit.agent_sdk_adapters import AgentRoleSpec, create_agent_runner

spec = AgentRoleSpec(
    role_id="systems-architect",
    name="Principal Systems Architect",
    goal="Enforce Clean Architecture and eliminate nested pyramids",
    backstory="Master of Evans DDD, Bloch Effective Java, and APOSD.",
    preferred_model="gpt-5.4",
)

runner = create_agent_runner(spec, sdk="openai")
result = runner.run("Audit dependency direction in src/domain/ vs src/infra/")
```

### 2. Anthropic Claude Agent SDK (Tool Use + 1h Prompt Cache)

```python
from kaizen_kit.agent_sdk_adapters import AgentRoleSpec, create_agent_runner

spec = AgentRoleSpec(
    role_id="incident-rca-falsifier",
    name="Incident RCA Specialist",
    goal="Find root cause via git log -S pickaxe and Popperian TDD falsification",
    backstory="Masters Karl Popper falsification and David Agans 9 debugging rules.",
    preferred_model="claude-sonnet-5-5",
)

runner = create_agent_runner(spec, sdk="anthropic")
result = runner.run("Diagnose race condition in connection pool")
```

### 3. LangGraph Stateful Verification Loop

```python
from kaizen_kit.agent_sdk_adapters import AgentRoleSpec, create_agent_runner

spec = AgentRoleSpec(
    role_id="refactoring-specialist",
    name="Clean Architecture Refactoring Specialist",
    goal="Flatten pyramids, decouple feature envy, and enforce cognitive complexity <= 10",
    backstory="Master of Refactoring.Guru 22 smells, 66 mechanics, and Uncle Bob Clean Code.",
    preferred_model="claude-sonnet-5-5",
)

runner = create_agent_runner(spec, sdk="langgraph")
result = runner.run("Refactor legacy nested pyramid in ingest handler")
```

### 4. Microsoft AutoGen Multi-Agent Council

```python
from kaizen_kit.agent_sdk_adapters import AgentRoleSpec, create_agent_runner

spec = AgentRoleSpec(
    role_id="reality-checker",
    name="Reality Checker & Evidence Collector",
    goal="Default to NEEDS WORK, falsify claims, and verify live test evidence",
    backstory="Skeptical QA verifier rejecting synthetic fixtures and unverifiable claims.",
    preferred_model="claude-opus-5-5",
)

runner = create_agent_runner(spec, sdk="autogen")
result = runner.run("Verify test evidence for authentication PR")
```

### 5. CrewAI Role-Based Team

```python
from kaizen_kit.agent_sdk_adapters import AgentRoleSpec, create_agent_runner

spec = AgentRoleSpec(
    role_id="e2e-systems-visionary",
    name="E2E Systems Visionary",
    goal="Govern p95 latency, Singleflight coalescing, and ALB framing",
    backstory="Kleppmann DDIA and Goldratt TOC systems architect.",
    preferred_model="claude-sonnet-5-5",
)

runner = create_agent_runner(spec, sdk="crewai")
result = runner.run("Analyze bottleneck in telemetry fan-out")
```

---

## 4. Integration with FastMCP & Effort AutoSense

Every agent runner integrates transparently with:
1. **`kaizen_effort_autosense`**: Calibrates thinking effort (`minimal`, `low`, `medium`, `high`, `xhigh`) before dispatching requests to cloud models.
2. **`fastmcp_gateway.py`**: Mounts local tools (`coderag_*`, `zoekt_*`, `kaizen_*`, `memory_*`, `serena_*`) across any agent harness.
3. **`mirix_memory.py` / `kaizen_autodream`**: Ingests agent execution lessons into episodic memory and consolidates them overnight.
