---
name: agency-agents
description: Zero-bloat lazy router and NEXUS workflow orchestrator for msitarzewski/agency-agents (280+ specialist personas across 18 divisions, 4 NEXUS scenario runbooks, and standardized handoff contracts). Use when searching for or delegating to specialized engineering, QA/Reality Checker, security, product, SRE, or optimization personas via Corporate Bedrock Claude.
---

# Agency Agents Lazy Router & NEXUS Workflow (`agency-agents`)

Instead of loading 280+ agent definitions into every prompt (which would add ~13,500+ tokens per turn), `agency_router` indexes the full `msitarzewski/agency-agents` roster into `~/.kaizen-kit/agency_catalog.json` and loads specialist personas **lazily on demand**.

## 1. Search, Inspect & Load Any Specialist Persona

```powershell
# Search across all 18 divisions with IDF scoring
kaizen-kit agency search "reality checker"
kaizen-kit agency search "rust refactoring" --division engineering --limit 5

# Inspect sections or load a compact (<1600 char) or full persona prompt
kaizen-kit agency inspect testing-reality-checker
kaizen-kit agency load testing-reality-checker --compact
```

## 2. Run NEXUS Scenario Runbooks & Standardized Handoffs

```powershell
# List or inspect NEXUS scenario rosters (startup-mvp, enterprise-feature, incident-response, marketing-campaign)
kaizen-kit agency runbook
kaizen-kit agency runbook incident-response

# Generate standardized NEXUS handoff / QA gate contracts
kaizen-kit agency handoff --from-agent systems-architect --to-agent testing-reality-checker --mode standard --summary "Fold pipeline updated"
kaizen-kit agency handoff --from-agent reality-checker --to-agent systems-architect --mode qa-fail --summary "Missing Server-Timing proof"
```

## 3. Delegate to Any Agency Persona on Corporate Bedrock Claude

```powershell
# Run any of the 280+ Agency specialists via Kaizen AWS Bedrock (1h prompt cache)
kaizen-kit agency delegate engineering-security-auditor "Audit this diff for CWE flaws"
kaizen-kit flow ask --agent testing-evidence-collector "Define verification checklist for TelemetryDashboard chart"

# Execute full NEXUS Dev <-> Reality-Checker QA loop
kaizen-kit flow nexus "Implement and verify new telemetry filter" --dev-agent systems-architect --qa-agent reality-checker
```

## 4. First-Class High-Impact Specialists in Subagent Roster

Three core Agency roles are also promoted directly as persistent first-class subagents (`invoke_subagent`):
- `reality-checker`: Skeptical final integration & parity gate (defaults to `NEEDS WORK`, enforces `POPULATED_MATCH` & `Server-Timing` proof).
- `autonomous-optimization-architect`: Token/latency governor, shadow-testing & AWS EDP FinOps optimizer.
- `ai-security-auditor`: CWE-mapped AI-code & AWS tenancy/auth security auditor.
