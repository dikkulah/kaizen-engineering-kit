---
name: capture-tasks-from-meeting-notes
description: >
  Extract action items from meeting notes or Confluence pages and create assigned
  Jira tasks using the `kaizen-atlassian` MCP server.
---

# Standardized Meeting Action-Item Capturer (`capture-tasks-from-meeting-notes`)

## 1. MCP Server & Tool Mapping (`kaizen-atlassian`)
Use `call_mcp_tool` with `ServerName: "kaizen-atlassian"`:
- **Read Meeting Page**: `confluence_get_page`, `confluence_search`
- **Resolve Assignees**: `jira_search_assignable_users`, `jira_get_user_profile`
- **Create Tasks**: `jira_create_issue`, `jira_batch_create_issues`, `jira_link_to_epic`

## 2. Workflow
1. **Parse Notes**: Extract explicit action items, owners, deadlines, and referenced Jira keys (`UC-*`, `RDM-*`, `COPS-*`).
2. **Resolve Users & Project Fields**: Look up assignee account IDs via `jira_search_assignable_users` and required fields via `jira_get_create_fields`.
3. **Preview Before Creation**: Show a compact Markdown table (`Task Summary | Assignee | Project | Parent Epic`) for user confirmation, then execute `jira_batch_create_issues`.
