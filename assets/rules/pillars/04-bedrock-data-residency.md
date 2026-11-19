# Pillar 4: Corporate AWS Bedrock Claude 5.5 Residency & Dual-Runtime Standard

1. **Corporate Data Residency Boundary**: Keep proprietary Kaizen source code and customer telemetry strictly inside the corporate Claude / AWS tenant boundary (`arn:aws:bedrock:eu-west-1:111122223333:...`).
2. **Dual-Runtime Interoperability & ModelRouter Isolation**:
   - **Corporate Bedrock `Claude 5.5`**: `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-fable-5`. `ModelRouter` keeps Bedrock isolated on Windows (`127.0.0.1:18889` / `boto3` `kaizen-claude`) and never copies local AWS credentials to EC2, while routing OSS pool models (`qwen3b`, `bitnet`, `phi4`, `gemma3`, `nemotron`, `qwen3-coder-30b-a3b`) through EC2 Cloud-Brain (`LiteLLM :4000`, `JEV Gateway :8765`, `FastMCP :8770`).
   - **Native Claude Code CLI (`claude`)**: Uses the developer's existing authenticated Claude Code configuration (`~/.claude/settings.json`), augmented non-destructively with `[kaizen-kit]` lifecycle hooks, 15 `@` specialist subagents, and curated engineering skills.
   - **AWS Bedrock Bridge & ADK (`kaizen-kit bridge` / `kaizen-kit adk` / `kaizen-kit flow`)**: Optional OpenAI-compatible local bridge (`127.0.0.1:18889`) backed by `boto3` (`AWS_PROFILE=kaizen-claude`, `eu-west-1`) for terminal and Google ADK orchestration without external third-party API keys.
3. **Zero-Stall Offline Fallback**: Check `http://127.0.0.1:18889/health` with a `<= 1s` timeout at most once when using SDK mode; if offline, proceed directly with native workspace tools and built-in subagents without stalling.
