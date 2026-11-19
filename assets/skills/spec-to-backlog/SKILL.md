---
name: spec-to-backlog
description: >
  Convert Confluence specification documents into structured Jira Epics and implementation
  tickets using the `kaizen-atlassian` MCP server and `sprint-jira-effort-sizer`.
---

# Standardized Spec-to-Backlog Generator (`spec-to-backlog`)

## 1. MCP Server & Tool Mapping (`kaizen-atlassian`)
Use `call_mcp_tool` with `ServerName: "kaizen-atlassian"`:
- **Read Spec**: `confluence_get_page`, `confluence_get_page_children`, `confluence_get_comments`
- **Inspect Jira Schema**: `jira_get_project_issue_types`, `jira_get_create_fields`, `jira_get_project_components`
- **Create Epic & Child Tasks**: `jira_create_issue`, `jira_batch_create_issues`, `jira_link_to_epic`

## 2. Workflow
1. **Fetch & Parse Spec**: Read the target Confluence page (`confluence_get_page`) and extract functional requirements, API contracts, and verification gates.
2. **Decompose & Size Tasks**:
   - Group into dependency-ordered layers (`Backend/Rust/Java`, `SQL/Data`, `Frontend/UI`, `CI/E2E`).
   - Optionally run `sprint-jira-effort-sizer` to attach empirical `person-days` / Story Point estimates from historical Git/Jira data.
3. **User Approval Gate**: Present the proposed Epic + child ticket table (Summary, Component, Effort, Acceptance Criteria) to the user before mutating Jira.
4. **Execute Creation**: Create the Epic via `jira_create_issue`, then batch-create child issues via `jira_batch_create_issues` and link via `jira_link_to_epic`.
