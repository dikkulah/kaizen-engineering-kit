# Master Model Catalog & Orchestration Architecture (`docs/MODEL_CATALOG.md`)

The `kaizen-engineering-kit` employs a **6-Layer Hybrid Compute Architecture** that routes coding and architecture tasks across **50+ local open-weight and frontier cloud models**. It dynamically balances hardware efficiency, token cost, data residency, and deterministic verification.

---

## 1. The 6-Layer Compute Matrix

```
       +-------------------------------------------------------------+
       |   Layer 5: Frontier Night Batch Sweepers (Async 50% Off)    |
       |   OpenAI GPT-5.4/o3 | Anthropic Claude 5.5 | DeepSeek-V3.2  |
       +-------------------------------------------------------------+
                                     ^
                                     | (Escalation / Night Batch)
       +-------------------------------------------------------------+
       |   Layer 4: Ephemeral GPU Burst & Distilled Frontier         |
       |   NVIDIA Nemotron-Super-49B | DeepSeek-R1-32B | Qwen3-32B   |
       +-------------------------------------------------------------+
                                     ^
                                     | (Complex AST / Multi-Repo)
       +-------------------------------------------------------------+
       |   Layer 3: Managed Frontier Cloud APIs & Day Architect      |
       |   Claude Sonnet 5.5 / Opus 5.5 | Gemini 4 Hydrogen/Flex     |
       +-------------------------------------------------------------+
                                     ^
                                     | (Unresolved / High Difficulty)
       +-------------------------------------------------------------+
       |   Layer 2: Always-On CPU Sparse-MoE Pool (m7g.8xlarge DDR5) |
       |   Qwen3-Coder-30B-A3B | Qwen3.5-35B-A3B | Nemotron-30B-A3B  |
       |   GLM-4.7-Flash | Ling-Mini-2.0 | Gemma-4-26B-A4B           |
       +-------------------------------------------------------------+
                                     ^
                                     | (Filter / Triage / First Pass)
       +-------------------------------------------------------------+
       |   Layer 1: Micro Edge Workers & AST Skeletons               |
       |   Qwen3.5-4B | Nemotron-Nano-9B-v2 | Gemma-4-E2B | BitNet   |
       +-------------------------------------------------------------+
                                     ^
                                     | (Prompt / Stream / Embed)
       +-------------------------------------------------------------+
       |   Layer 0: Ultra-Light CPU Guards, Embedders & OCR          |
       |   Prompt-Guard | Qwen3-Guard | Granite-Tiny | BGE-M3        |
       +-------------------------------------------------------------+
```

---

## 2. Model Catalog by Layer

### Layer 0: Ultra-Light CPU Guards, Embedders, Voice & OCR (m7g.8xlarge / Local CPU)
Ultra-fast, deterministic CPU pre-processors running in `<10ms` before any LLM inference:

| Model Alias | HuggingFace / Source Repo | Role | Footprint | Context | Quantization | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `prompt-guard-deberta` | `protectai/deberta-v3-base-prompt-injection-v2` | Security Gate | 0.50 GB | 512 | FP16 | Zero-shot prompt injection & jailbreak detection |
| `qwen3-guard-0.6b` | `Qwen/Qwen3Guard-Gen-0.6B` | Guardrail | 0.65 GB | 32K | Q8_0 | Safe / Controversial / Unsafe tri-class screening |
| `qwen3-guard-stream-0.6b` | `Qwen/Qwen3Guard-Stream-0.6B` | Stream Guard | 0.64 GB | 32K | Q8_0 | Token-by-token streaming output guardrail |
| `granite-4-h-tiny` | `ibm-granite/granite-4.0-h-tiny` | Hybrid Router | 0.98 GB | 32K | Q8_0 | 1B active Mamba-2 MoE for tool routing |
| `bge-m3` | `BAAI/bge-m3` | Vector Embedder | 1.14 GB | 8K | FP16 | Multi-lingual dense + sparse hybrid code embeddings |
| `qwen3-reranker-0.6b` | `Qwen/Qwen3-Reranker-0.6B` | Cross-Encoder | 0.65 GB | 32K | Q8_0 | Code snippet and symbol ranking via llama-server |
| `granite-docling-258m` | `ibm-granite/granite-docling-258M` | Doc OCR | 0.52 GB | 8K | ONNX-FP16 | Layout-preserving PDF/diagram to Markdown parser |
| `whisper-large-v3-turbo`| `openai/whisper-large-v3-turbo` | Speech-to-Text | 0.85 GB | 448 | INT8 | Low-latency voice action extraction |
| `kokoro-82m` | `hexgrad/Kokoro-82M` | Text-to-Speech | 0.17 GB | 2K | ONNX-FP16 | Real-time audio alerts and vocal notifications |

---

### Layer 1: Micro Edge Workers & AST Skeletons (Local / CPU DDR5)
Sub-4B and 1.58-bit models executing fast boilerplate pruning, AST skeleton extraction, and diff filtering:

| Model Alias | HuggingFace Repo | Parameters (Total / Active) | Footprint | Context | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `bitnet-2b` | `microsoft/bitnet-b1.58-2B-4T` | 2.4B / 2.4B | 1.19 GB | 4K | 1.58-bit ternary ARM64 NEON worker for log classification |
| `qwen3.5-0.8b` | `Qwen/Qwen3.5-0.8B` | 0.8B / 0.8B | 0.54 GB | 262K | Sub-1GB Gated DeltaNet classifier & boilerplate filter |
| `qwen3.5-2b` | `Qwen/Qwen3.5-2B` | 2.0B / 2.0B | 1.27 GB | 262K | 262K-context tool router & JSON parameter validator |
| `qwen3.5-4b` | `Qwen/Qwen3.5-4B` | 4.0B / 4.0B | 2.80 GB | 262K | Primary edge micro-coder; AST skeleton pruning & diff triage |
| `gemma-4-e2b` | `google/gemma-4-E2B-it` | 5.1B / 2.3B | 3.11 GB | 131K | Apache-2.0 PLE edge reviewer with mmap embeddings |
| `nemotron-nano-9b-v2` | `nvidia/Nemotron-Nano-9B-v2` | 9.0B / 9.0B | 5.40 GB | 131K | NVIDIA dense 9B code & reasoning engine with 128k context |

---

### Layer 2: Always-On CPU Sparse-MoE Pool (m7g.8xlarge DDR5, <=3.8B Active)
High-throughput, zero-GPU-cost Sparse Mixture-of-Experts serving concurrent requests directly on ARM64 CPU DDR5 RAM:

| Model Alias | HuggingFace Repo | Parameters (Total / Active) | Speed (CPU) | Context | Specialization |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `qwen3-coder-30b-a3b` | `Qwen/Qwen3-Coder-30B-A3B-Instruct` | 30.5B / 3.3B | ~56.5 tok/s | 262K | Primary CPU code generator and bugfix author |
| `qwen3.5-35b-a3b` | `Qwen/Qwen3.5-35B-A3B` | 35.0B / 3.0B | ~52.0 tok/s | 262K | Lead CPU architecture reviewer and tool caller |
| `nemotron-3-nano-30b-a3b`| `nvidia/Nemotron-3-Nano-30B-A3B` | 30.0B / 3.2B | ~54.0 tok/s | 131K | NVIDIA 30B MoE for structured reasoning & doc analysis |
| `glm-4.7-flash` | `zai-org/GLM-4.7-Flash` | 30.0B / 3.0B | ~55.0 tok/s | 202K | MIT-licensed secondary coder and JSON schema enforcement |
| `ling-mini-2.0` | `inclusionAI/Ling-mini-2.0` | 16.0B / 1.4B | ~78.0 tok/s | 131K | Ultra-fast 1.4B active MoE for AutoDream memory consolidation |
| `gemma-4-26b-a4b` | `google/gemma-4-26B-A4B-it` | 25.2B / 3.8B | ~45.0 tok/s | 262K | Disjoint-family Panel-of-LLM (PoLL) review jury member |
| `gemma-4-12b-unified` | `google/gemma-4-12B-it` | 12.0B / 12.0B | ~32.0 tok/s | 131K | Multimodal OCR and architecture diagram inspector |

---

### Layer 3: Managed Frontier Cloud APIs & Daylight Architecture
Cloud and managed models invoked for high-complexity architecture decisions and daylight developer sessions:

| Model Alias | Provider | Context | Cost Strategy | Primary Mandate |
| :--- | :--- | :--- | :--- | :--- |
| `claude-sonnet-5-5` | Anthropic / Bedrock | 200K | 1h Prompt Cache | Lead Systems & Clean Architecture Designer |
| `claude-opus-5-5` | Anthropic / Bedrock | 200K | 1h Prompt Cache | RCA Investigator, Git Blame Forensics & Supreme Judge |
| `claude-haiku-5-5` | Anthropic / Bedrock | 200K | 1h Prompt Cache | Mechanical guard-clause flattening & DTO boilerplate |
| `claude-fable-5` | Anthropic / Bedrock | 200K | 1h Prompt Cache | Spec drafting, RFC generation & sprint story sizing |
| `gemini-4-hydrogen` | Google Gemini | 1M | Flex Tier (50% off) | Default high-throughput cloud coder (public_sanitized) |
| `gemini-4-carbon` | Google Gemini | 1M | Flex Tier (50% off) | Deep reasoning frontier judge (public_sanitized) |
| `gemini-decision-1` | Google Gemini | 262K | 0 Output Tokens | Pre-screening checklist gate with calibrated log-probs |
| `gemini-4-neon` | Google Gemini | 1M | Flex Tier (50% off) | Adaptive thinking effort agentic problem solver |

---

### Layer 4: Ephemeral Spot GPU Burst & Pruned Frontier (NVIDIA GPUs / Spot)
Triggered on demand ($0 idle cost, auto-terminated after 15 min inactivity) for heavy multi-repo analysis and fine-tuning:

| Model Alias | HuggingFace Repo | Parameters | VRAM Target | Hardware Target | Engine |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `qwen3-32b-awq` | `Qwen/Qwen3-32B-AWQ` | 32B Dense | 19.0 GB | `g4dn.12xlarge` (T4) | vLLM (float16 SM75) |
| `deepseek-r1-32b-awq` | `deepseek-ai/DeepSeek-R1-Distill-Qwen-32B` | 32B Dense | 19.0 GB | `g4dn.12xlarge` (T4) | vLLM (float16 SM75) |
| `nemotron-super-49b` | `nvidia/Llama-3.3-Nemotron-Super-49B-v1.5` | 49B Dense | 28.0 GB | `g4dn.12xlarge` (T4) | vLLM (float16 SM75) |
| `qwen3-coder-next` | `Qwen/Qwen3-Coder-Next` | 80B / 3B MoE | 44.0 GB | `g6.12xlarge` (L4) | vLLM (AWQ-Int4 SM89) |
| `qwen3.5-122b-a10b` | `Qwen/Qwen3.5-122B-A10B` | 122B / 10B MoE | 62.0 GB | `g6.12xlarge` (L4) | SGLang RadixAttention |
| `glm-4.5-air` | `zai-org/GLM-4.5-Air` | 106B / 12B MoE | 58.0 GB | `g6.12xlarge` (L4) | vLLM AWQ-Int4 |
| `qwen3-235b-a22b-rpc` | `Qwen/Qwen3-235B-A22B-Instruct-GGUF` | 235B / 22B MoE | 134.0 GB | 16x `g4dn.xlarge` RPC | llama.cpp RPC cluster |
| `olmocr-2-7b` | `allenai/olmOCR-2-7B-1025` | 7B Dense | 4.8 GB | `g4dn.xlarge` (T4) | vLLM Doc OCR |

---

### Layer 5: Frontier Night Batch Sweepers (Async 50% Discount)
Overnight unattended sweepers orchestrated via Night-Shift:

| Model Alias | Provider / Family | Context | Batch Mechanism | Turnaround |
| :--- | :--- | :--- | :--- | :--- |
| `gpt-5.4` | OpenAI | 128K | OpenAI Batch API (`/v1/batches`) | 24 Hours (50% off) |
| `gpt-5.3-codex` | OpenAI | 128K | OpenAI Batch API (`/v1/batches`) | 24 Hours (50% off) |
| `gpt-5` | OpenAI | 128K | OpenAI Batch API (`/v1/batches`) | 24 Hours (50% off) |
| `o3` | OpenAI | 200K | OpenAI Batch API (`/v1/batches`) | 24 Hours (50% off) |
| `claude-sonnet-5-5` | Anthropic | 200K | Anthropic Message Batches (`/v1/messages/batches`) | 24 Hours (50% off) |
| `claude-opus-5-5` | Anthropic | 200K | Anthropic Message Batches (`/v1/messages/batches`) | 24 Hours (50% off) |
| `deepseek-v3.2` | Local OSS / Provider | 164K | GGUF UD-Q2_K_XL on r7g.16xlarge | Overnight ($0 API) |
| `kimi-k2.5` | Local OSS / Provider | 262K | GGUF UD-Q2_K_XL on r7g.16xlarge | Overnight ($0 API) |
| `glm-5` | Local OSS / Provider | 200K | GGUF UD-Q2_K_XL on r7g.16xlarge | Overnight ($0 API) |

---

## 3. AutoDream & MIRIX-2.0 Memory Consolidation

Human engineers consolidate memories during REM sleep; coding agents require an analogous mechanism to prevent context-window saturation and loss of architectural discoveries.

```
+-------------------------------------------------------------------------+
|                         MIRIX-2.0 Memory Flow                           |
+-------------------------------------------------------------------------+
  Daylight Sessions:
    Command Runs / Diffs / Fixes ---> Ingest Queue (memory_ingest)
                                            |
                                            v
                                  [Episodic Memory Pool]
                                            |
  Overnight (Night-Shift / kaizen_autodream):
                                            v
                         +-------------------------------------+
                         | AutoDream Engine (Ling-Mini-2.0)    |
                         | LiteLLM :4000 (~78 tok/s CPU DDR5)  |
                         +-------------------------------------+
                                     /             \
                                    v               v
                         [Semantic Rules]     [Procedural Recipes]
                         "Never coalesce      "Run cargo check before
                          across tenants"      packaging Lambda zip"
                                    \               /
                                     v             v
                              [Hybrid LanceDB + SQLite FTS5]
                                            |
  Next Turn / Session:
    Prompt / Context Query <--- memory_query (Strict Token Budget <=600)
```

### Memory Types
1. **`core`**: Foundational invariants and system constraints (e.g. Clean Architecture boundaries, data residency).
2. **`episodic`**: Raw timestamped records of developer-agent turns, test outputs, and bug incidents.
3. **`semantic`**: Extracted domain concepts, data models, and architectural patterns.
4. **`procedural`**: Step-by-step verified recipes for building, testing, deploying, or debugging.
5. **`user`**: Developer preferences, working habits, and coding conventions.
6. **`assistant`**: Reflection notes and meta-strategies learned by the toolkit.

---

## 4. Night-Shift Multi-Provider Batch & Flex Configuration

Triggered when closing your laptop with `"gece yatıyorum"` or running `kaizen-kit carbon night-shift`:

```json
{
  "tasks": [
    {
      "task_id": "overnight_autodream",
      "title": "MIRIX AutoDream Memory Consolidation",
      "prompt": "Consolidate episodic debugging records into permanent procedural rules.",
      "mode": "ec2_local",
      "model": "ling-mini-2.0"
    },
    {
      "task_id": "openai_refactor_sweep",
      "title": "OpenAI 50%-Off Batch Code Audit",
      "prompt": "Audit all Rust models for Bloch Effective Java Item 16 equivalents.",
      "mode": "openai_batch",
      "model": "gpt-5.4"
    },
    {
      "task_id": "anthropic_contract_verify",
      "title": "Anthropic 50%-Off Message Batch",
      "prompt": "Verify OpenAPI 3.1 contract parity across backend DTOs.",
      "mode": "anthropic_batch",
      "model": "claude-sonnet-5-5",
      "max_output_tokens": 4096
    },
    {
      "task_id": "gemini_flex_research",
      "title": "Gemini Flex 50%-Off Sequential Synthesis",
      "prompt": "Synthesize latest research on quantized KV-cache compression.",
      "mode": "flex",
      "model": "gemini-4-hydrogen",
      "data_classification": "public_sanitized"
    }
  ]
}
```

The Night-Shift orchestrator:
1. Segregates tasks by data classification and provider.
2. Compiles standardized OpenAI Batch JSONL (`/v1/chat/completions`) and Anthropic Message Batch JSONL (`/v1/messages/batches`).
3. Executes local AutoDream on EC2 / CPU DDR5 ($0 API cost).
4. Persists execution state to `night_shift_state.json`.
5. Summarizes everything in `night_shift_morning_report.md` when you wake up (`kaizen-kit carbon night-shift --morning`).
