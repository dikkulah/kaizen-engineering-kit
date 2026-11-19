---
name: triage-issue
description: >
  Triage bug reports, error messages, and stack traces by searching for duplicates in Jira (`kaizen-atlassian` MCP)
  and local 4-year Git/Jira history (`history.sqlite`), suggesting fields, checking Confluence specs, and creating or updating Jira issues (`UC-*`, `RDM-*`, `COPS-*`).
  Use when the user pastes stack traces, asks if a bug exists, requests a duplicate check, or wants to file/update a Jira issue.
---

# Standardized Kaizen Issue Triage (`triage-issue`)

## 1. MCP Server & Tool Mapping (`kaizen-atlassian`)
Always call `call_mcp_tool` with `ServerName: "kaizen-atlassian"` (read schema in `~/.gemini/antigravity/mcp/kaizen-atlassian/<tool>.json` if needed):
- **Duplicate & Keyword Search**: `jira_search` (`jql`, `fields`, `limit`)
- **Issue Details & Comments**: `jira_get_issue` (`issue_key`), `jira_batch_get_changelogs`
- **Project Metadata**: `jira_get_project_issue_types`, `jira_get_create_fields`
- **Create / Comment / Link**: `jira_create_issue`, `jira_add_comment`, `jira_create_issue_link`
- **Confluence Alignment**: `confluence_search`, `confluence_get_page` (for verifying against PRDs/specifications)

## 2. Deterministic Triage Workflow
1. **Extract Signatures & Signals**:
   - Error signatures: Exception types, HTTP status codes (`500`, `502`, `504`), stack trace lines.
   - Domain & Product tokens: Endpoints (`/cloud_gateway_api/v3/...`, `/cloud_gateway_api/v4/...`), MAC/Home IDs, customer codes (ATT, Telia, DTAG), component names (`CloudGatewayApi`, `AAS`, `SQE`, `OnDemand`, `TelemetryDashboard`, `NCV`, `NetworkMap`).
   - Execution context: Environment (Prod, Staging, UAT), reproduction hints.
2. **Search Duplicates (Narrow to Wide)**:
   - **Narrow JQL first**: Check local SQLite history if present (`history.sqlite`) or run `jira_search` with exact project & keyword:
     `project in (UC, RDM, COPS) AND (summary ~ "<signature>" OR description ~ "<signature>") ORDER BY updated DESC`
   - **Widen JQL if zero results**: Broaden to component/label bucket and date window:
     `project in (UC, RDM, COPS) AND component = "<component>" AND created >= -180d ORDER BY updated DESC`
   - Present top matches with issue `key`, summary, similarity explanation, and status.
3. **Decide & Act**:
   - **Duplicate Found**: Cite matching keys and overlapping symptoms; summarize root cause; suggest linking via `jira_create_issue_link` or commenting via `jira_add_comment`.
   - **Net-New Bug**: Draft structured issue description (Steps to Reproduce, Expected Behavior, Actual Behavior, Environment, Logs/Traces); suggest `components`, `labels`, and `priority`; confirm with user before invoking `jira_create_issue`.
4. **RCA & Falsification Delegation**:
   - If the issue is a `UC-*` bug with `.har` / `.avro` / CloudWatch traces, delegate in-depth telemetry analysis to the `uc-bug-analyze` skill and `incident-rca-falsifier` subagent.
5. **Confluence Spec Alignment (Optional)**:
   - If user references a specification or PRD, search Confluence via `confluence_search` to align field names and expected behaviors.

## 3. Safety & Privacy Guardrails
- **Ambiguity Guard**: Do not create or update issues if the target product or environment is ambiguous; ask one clarifying question first.
- **Privacy / Credentials**: Redact internal tokens, passwords, customer emails, or private credentials found in pasted logs before publishing to Jira.
