---
name: generate-status-report
description: >
  Generate engineering project status reports and executive snapshots from Jira issues (`UC-*`, `RDM-*`, `COPS-*`)
  and publish or update Confluence summary pages using the `kaizen-atlassian` MCP server.
  Use when the user requests a status report, executive snapshot, one-page summary, standup notes, or email-ready digest from Jira keys or JQL.
---

# Standardized Status Report & Executive Snapshot Generator

Unified reporting workflow for engineering progress, executive summaries, sprint metrics, and Confluence status pages.

---

## 1. MCP Server & Tool Mapping (`kaizen-atlassian`)
Always invoke `call_mcp_tool` with `ServerName: "kaizen-atlassian"`:
- **Query Issues & Sprints**: `jira_search`, `jira_get_sprint_issues`, `jira_get_project_epic_hierarchy`
- **Batch Issue Details**: `jira_get_issue` (with lean field selection: `summary,status,priority,assignee,updated,labels`)
- **Publish / Update Pages**: `confluence_create_page`, `confluence_update_page`
- **Local Helper**: `python $AI_KIT_HOME/assets/skills/generate-status-report/scripts/jql_builder.py`

---

## 2. Reporting Formats

### Format A: Quick Executive Snapshot (Standup / Email / Leadership)
Use when the user asks for a quick summary, standup notes, or email-ready bullet points from an issue list or JQL:
1. **Fetch & Cluster Issues**:
   - Query issues via `jira_search` or batch `jira_get_issue`.
   - Cluster by categories:
     - ðŸ”´ **Blocked / Waiting on External**: Issues blocked by external teams, PR dependencies, or customer feedback.
     - ðŸŸ¡ **High Priority / At Risk**: P1/P2 bugs, SLA limits approaching, or critical path tasks.
     - âšª **Stale**: Issues with no updates in >14 days.
     - ðŸŸ¢ **In Progress & Completed**: Work actively advancing.
2. **Output Structure**:
   - 5â€“10 concise bullet points highlighting key milestones achieved.
   - Clear "Decisions Needed / Next Actions" section.
   - No raw JSON dumps; format issue references cleanly as `[KEY-123: Summary]`.

### Format B: Full Project & Sprint Status Report (Confluence Publication)
Use when generating comprehensive sprint or milestone documentation:
1. **Aggregate Metrics**:
   - Story point burndown / completed vs committed points.
   - Deliverables table: Epic, Issue Key, Assignee, Status, Target Date.
2. **Synthesize Executive Report**:
   - Executive Summary paragraph.
   - Workstream Breakdown table.
   - Risks and Mitigation Strategy.
3. **Publish to Confluence (Optional)**:
   - Confirm target space and parent page title with the user.
   - Invoke `confluence_create_page` or `confluence_update_page`.

---

## 3. Communication & Privacy Rules
- Match the audience tone (Executive / Management vs Deep Engineering).
- Support Turkish or English based on user prompt language.
- Redact customer emails, internal secrets, or authentication tokens from ticket bodies.

