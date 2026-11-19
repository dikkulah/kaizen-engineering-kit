---
name: prompt-optimizer
description: >
  Always-On Prompt Optimizer, Multi-Workstream Decomposer, Slash-Command Collision
  Resolver (/grill-me vs /teamwork), Cross-Chat Context Bridge (@[conversation:"..."]),
  and Historical Prompt Auditor. Runs automatically via PreInvocation hook on complex
  prompts, or manually via /prompt-optimizer to audit prompt efficiency or structure
  multi-workstream requests.
---

# Prompt Optimizer & Execution Alignment Skill (v2.0 — Always-On)

This skill is backed by the **Always-On `prompt-optimizer` plugin** (`~/.gemini/config/plugins/prompt-optimizer/`), which automatically intercepts complex user prompts at `PreInvocation` (`scripts/hook_prompt_optimizer.py`) and injects a zero-fat `ephemeralMessage` execution blueprint before the model runs.

## 1. How Always-On `PreInvocation` Optimization Works (`0` Extra LLM Calls)

On the first invocation of every user turn (`last_step_type == "USER_INPUT"`):
1. **Simple Prompt Bypass**: Short, single-intent messages (e.g., `"yap"`, `"devam"`, `<45` chars without slash commands) bypass injection completely (`{}`).
2. **Stream-of-Consciousness Multi-Workstream Decomposition (`WS-1..WS-N`)**: Splits compound prompts across newlines, `2+` space gaps (`   `), and Turkish/English transition markers (`birde`, `ayrıca`, `yan bir iş olarak`, `ondan sonra`, `sonrasında`), then sorts them by engineering dependency (`Git/MR` -> `E2E Architecture` -> `Rust/Systems` -> `SQL DAG` -> `JVM Backend` -> `Incident RCA` -> `Frontend/UI`) and binds each workstream to its canonical subagent.
3. **Slash-Command Collision Gate (`PHASED_GATE`)**: When interactive alignment commands (`/grill-me`, `/plan`) are combined in the same prompt with swarm commands (`/teamwork`, `/boost`, `/goal`), enforces **Alignment First (`ask_question` / plan) Before Spawning Subagent Swarms** (unless the user explicitly writes *"soruları siz cevaplayın"* / *"hemen uygula"*). Also fixes typos (`/pla` -> `/plan`, `/tam` -> `/goal`), expands wildcards (`/xx`, `/xxx`), and filters filesystem path false-positives (`/tmp`, `/home`, `/cloud_gateway_api`, `/batches`).
4. **Cross-Chat Bridge (`@[conversation:"..."]`)**: Automatically resolves conversation titles against `~/.gemini/antigravity/conversation_summaries.db` to exact `conversation_id` and `~/.gemini/antigravity/brain/<id>` paths.
5. **Domain Shorthand & Safety Normalization**: Normalizes `ncv`/`nvc` (TelemetryDashboard `unified-frontend`), `sq`/`sqe` (`kaizen-cloud-sqe`), `legacy_batch_worker` (`kaizen-OptiWiFi-legacy_batch_worker-docker`), `çocuk kitabı` (English Storybook `C1-C15` `//!` headers — never literal Turkish in code), and AWS tenancy rules (`u2` approval gate vs `uca`/`ucprd` read-only).

## 2. Run Historical Prompt & Workflow Audit (`--audit`)

To audit all historical user prompts, slash command collisions, workstream routing distribution, and re-work rates:

```bash
python3 $HOME/.gemini/config/plugins/prompt-optimizer/scripts/optimize_prompt.py --audit
```

## 3. Optimize a Specific Prompt or File Manually (`--input`)

```bash
python3 $HOME/.gemini/config/plugins/prompt-optimizer/scripts/optimize_prompt.py --input "<raw prompt or file>"
```

## 4. Check Always-On Prompt Optimization Log

```bash
tail -n 30 ~/.gemini/antigravity/prompt_optimizer_audit.log 2>/dev/null || echo "No complex prompts logged yet."
```

