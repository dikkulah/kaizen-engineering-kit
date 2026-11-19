# Domain Pack: MR Hygiene, Jenkins CI & Cloud Hotpatch Deployment (`mr-ci-deploy`)

Loaded on-demand for Git commits/MRs, Jenkins build analysis, AWS `kaizen-u2` deployment, and `release-hygiene-guardian`.

## 1. Product MR Hygiene (Strict Pre-Commit Gate)
Before every `git commit` or `git push`:
1. Run `git status`, `git diff --stat`, and `kaizen-kit check --pre-flight .`.
2. **Never commit to product bugfix/feature MRs**:
   - Local agent notes (`docs/architecture/**`, `*-inventory-*.csv`, `triage-findings/**`)
   - IDE/editor/agent tooling (`.editorconfig`, `.vscode/`, `.idea/`, `.cursor/`, `.claude/`, `.agents/`)
   - Formatting-only or import-reorder-only diffs in untouched files.
3. **English MR Descriptions**: Write GitLab MR titles/descriptions and Gerrit commit messages in clear English (`Summary` + `Test plan`).
4. **Full Terminal Output (`No tail/head`)**: Never pipe `mvn`, `cargo`, `npm`, or test runs through `| tail -N` or `| head -N`.

## 2. Jenkins Pre-Flight & Release Diagnostics
- **Fast-Forward Invariant**: Verify `git merge-base --is-ancestor origin/master HEAD` before pushing.
- **S3 Version Collision (`UNSTABLE`)**: Re-releasing an existing artifact version marks Jenkins builds `UNSTABLE`; verify version bump first.
- **Lambda Deployment Guard**: Always run `kaizen-kit story-guard check` (Step 0) before packaging `/tmp/ondemand-lambda.zip` (`bootstrap`), and require explicit user approval before mutating `--profile kaizen-u2`.
