---
name: cloudwatch-otel-finops-auditor
description: >
  Audit repository logging configurations (`logback.xml`, `application*.yml`, OpenTelemetry,
  RUM, Rust/Java hot-loop logging) and AWS CloudWatch Log Groups for noisy verbosity,
  missing retention policies, and monthly FinOps ($) waste.
---

# CloudWatch, OpenTelemetry & Logback FinOps Auditor (`cloudwatch-otel-finops-auditor`)

## 1. Scope & Safety (`SEC-AWS-TENANCY-ISOLATION`)
- **Local Repo Scan (0 Network Calls)**: Audits `logback*.xml`, `application*.yml`, OTEL/RUM configs, and source files for `DEBUG`/`TRACE` leaks, un-sampled per-envelope logs, and synchronous console appenders.
- **Read-Only CloudWatch Scan (`--aws-profile`)**: Inspects CloudWatch Log Groups (`describe-log-groups`) for missing `retentionInDays` (*Never Expire*) and highest stored/ingested GBs (`$0.50/GB` ingestion + `$0.03/GB-month` storage). Never mutates AWS without explicit `u2` user approval.

## 2. CLI Usage
```bash
python $AI_KIT_HOME/assets/skills/cloudwatch-otel-finops-auditor/scripts/log_finops_audit.py --workspace c:/work-space
```


