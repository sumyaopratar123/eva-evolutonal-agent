EVA — Evolvable Virtual Agent
<p align="center">
  <img src="docs/Eva-thumbnail.png" alt="EVA — Evolvable Virtual Agent" width="920">
</p>
<p align="center">
  <strong>A sandboxed, self-evolving AI agent with gated and reversible releases.</strong>
</p>
<p align="center">
  <img src="https://img.shields.io/badge/status-experimental-orange" alt="Experimental">
  <img src="https://img.shields.io/badge/Python-3.12-blue" alt="Python 3.12">
  <img src="https://img.shields.io/badge/Runtime-Docker-2496ED" alt="Docker">
  <img src="https://img.shields.io/badge/LLM-Ollama%20%7C%20OpenAI--compatible-6B46C1" alt="LLM providers">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="MIT License">
</p>
> [!WARNING]
> **EVA is experimental and self-modifying.** It can run shell commands and rewrite parts of its own source. Run it only inside the provided Docker sandbox, keep approval gates enabled, and review any proposed changes. Do not expose it to secrets or data you cannot afford to lose.
Overview
EVA (Evolvable Virtual Agent) is an experimental AI agent designed to improve its own implementation through controlled, testable releases. Rather than changing the live runtime in place, EVA creates a candidate version, runs its checks and gates, and promotes the candidate only when it passes. Releases are recorded so changes can be reviewed and rolled back.
This repository is based on the open-source EVA — Evolvable Virtual Agent project by arturkorb3. This working setup adds a local Ollama workflow and a personalized model named Sumit AI. The model is a custom configuration built on top of an existing open model; it is not a model trained from scratch.
Key features
Gated self-evolution: EVA creates candidate copies and promotes them only after configured checks pass.
Reversible releases: release history and rollback mechanisms help recover from unsuccessful changes.
Generated self-model: EVA derives a description of its capabilities from the active code.
Persistent learning records: sessions, recurring friction, and lessons can inform future work.
Provider-neutral model adapter: supports OpenAI-compatible endpoints and Anthropic, depending on the selected adapter and configuration.
Docker sandbox: the intended execution boundary for this experimental, self-modifying agent.
Local AI option: use Ollama on Windows so everyday tests do not require OpenAI API credits.
Architecture at a glance
```text
User prompt
    │
    ▼
EVA agent (inside Docker)
    │
    ▼
Model adapter ──► key-guard proxy ──► configured LLM endpoint
                                      ├─ Ollama on Windows (local)
                                      └─ Cloud model API (optional)

Self-change workflow:
seed release → candidate copy → tests and gates → promotion or discard
```
With key-guard enabled, the credential is held by the proxy sidecar rather than being placed in the EVA container. Key-guard reduces the chance of key theft from the agent container; it does not prevent the agent from using the model endpoint. Keep provider spending limits enabled when using a paid API.
Requirements
For the Windows + local Ollama setup:
Windows 10/11 with PowerShell
Docker Desktop installed and running
Docker Compose v2 or a compatible Docker Compose installation
Ollama for Windows
Enough RAM and disk space for the model you choose
A small local model may run on a machine with 8 GB RAM, but speed and tool-calling quality vary by model and workload. Larger models generally need more resources. Cloud providers can be configured instead, but may require separate API billing.
Quick start — Windows + local Sumit AI
Run commands from the repository root, for example:
```powershell
cd "$HOME\OneDrive\Desktop\eva-evolutional-agent"
```
1. Confirm Docker and Ollama
```powershell
docker --version
docker compose version
ollama --version
```
Start Docker Desktop and ensure Ollama is running before continuing.
2. Download a base model
For example, download Qwen 2.5 3B:
```powershell
ollama pull qwen2.5:3b
```
Test the model before connecting EVA:
```powershell
ollama run qwen2.5:3b
```
Enter a short prompt. Type `/bye` to leave the Ollama chat.
3. Create a personalized Sumit AI model
Create a file named `Modelfile.sumit` in the repository root. In PowerShell, you can create it with:
```powershell
@'
FROM qwen2.5:3b

SYSTEM """
You are Sumit AI, an AI assistant configured by Sumit.
When asked your name, introduce yourself as Sumit AI, a personal AI assistant.
Do not claim to be a human.
Help with programming, learning, projects, and general questions.
Reply in the same language as the user whenever possible.
"""
'@ | Set-Content -Encoding utf8 .\Modelfile.sumit

ollama create sumit-ai -f .\Modelfile.sumit
```
Optional: create a second variant from another model if you have downloaded it. For example, after `ollama pull qwen3:4b`, create `Modelfile.sumit-v2`:
```powershell
@'
FROM qwen3:4b
PARAMETER num_ctx 8192

SYSTEM """
You are Sumit AI, an AI assistant configured by Sumit.
Answer ordinary questions directly.
When asked for code, provide complete working code and a short explanation.
Use tools only when needed for the user's request.
Do not claim to be a human.
"""
'@ | Set-Content -Encoding utf8 .\Modelfile.sumit-v2

ollama create sumit-ai-v2 -f .\Modelfile.sumit-v2
```
Test the custom model directly before connecting it to EVA:
```powershell
ollama run sumit-ai "Hello! Introduce yourself."
ollama run sumit-ai-v2 "Write a simple Python program to check whether a number is prime."
```
Use `sumit-ai` or `sumit-ai-v2` below, depending on which model you created and tested.
4. Configure EVA
Copy the example configuration if `.env` does not exist:
```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```
For local Ollama, set these active entries in `.env`:
```dotenv
EVA_PROVIDER=openai_chat
EVA_TOOL_MODE=native
EVA_ENDPOINT=http://host.docker.internal:11434/v1/chat/completions
EVA_MODEL=sumit-ai
EVA_API_KEY=ollama
LLM_TIMEOUT=600
ORGANISM_HISTORY_BUDGET=12000
ORGANISM_HISTORY_KEEP=3
```
If you chose the second model, use `EVA_MODEL=sumit-ai-v2` instead. Keep only one active value for each setting; comment out or remove duplicate active entries. The value `ollama` is a placeholder credential for local use, not an OpenAI API key. Keep key-guard enabled; do not set `EVA_KEYGUARD=0` unless you fully understand the consequences.
`host.docker.internal` lets the Docker container reach services on the Windows host. Ollama must be running and listening on its local API port.
5. Launch EVA
From the repository root:
```powershell
.\run.ps1
```
When EVA starts, try a simple interaction first:
```text
Say exactly: Hello Sumit. Do not use any tools.
```
Then try a coding request:
```text
Write a simple Python program to check whether a number is prime. Show the complete code.
```
Local inference can take time, particularly on the first request. If a simple prompt works but a tool-heavy task does not, see Troubleshooting below. A successful standalone Ollama response does not guarantee that every model will correctly follow EVA's tool-calling protocol.
Common EVA commands
Commands available from the repository root depend on the checked-out version. The upstream project documents the following commands:
```powershell
.\run.ps1 help
.\run.ps1 status
.\run.ps1 changes
.\run.ps1 rollback
```
Inside the EVA chat, use `/help` to see supported in-chat commands. Type `exit` to end a session. Do not enable autonomous shell approval for convenience; keep interactive approvals enabled while learning and testing.
Troubleshooting
`HTTP 429` — no credits remaining
This error came from the cloud API, not Docker. A cloud API key can be active while the account has no usable credits. To avoid cloud API usage for this configuration, point EVA to the local Ollama endpoint shown above and use an Ollama model name.
`HTTP 404` — model does not exist or is unavailable
Check the model name in `.env`:
```powershell
ollama list
Select-String -Path .env -Pattern '^EVA_PROVIDER=|^EVA_ENDPOINT=|^EVA_MODEL=|^EVA_TOOL_MODE='
```
The `EVA_MODEL` value must exactly match a model listed by `ollama list`.
Proxy timeout or EVA waits too long
First test the model directly:
```powershell
ollama run sumit-ai "Reply with only: Hello Sumit"
```
Then test the OpenAI-compatible endpoint from PowerShell:
```powershell
$body = @{
    model = "sumit-ai"
    messages = @(@{ role = "user"; content = "Reply with only: Hello Sumit" })
    stream = $false
} | ConvertTo-Json -Depth 6

Invoke-RestMethod `
    -Uri "http://localhost:11434/v1/chat/completions" `
    -Method Post `
    -ContentType "application/json" `
    -Body $body |
    Select-Object -ExpandProperty choices |
    Select-Object -ExpandProperty message |
    Select-Object -ExpandProperty content
```
If direct Ollama tests work but EVA times out, inspect the proxy log:
```powershell
docker compose -f docker-compose.yml -f docker-compose.keyguard.yml logs --tail 50 llm-proxy
```
Remove secrets from logs before sharing them publicly.
`no JSON protocol object found in model output`
If you set `EVA_TOOL_MODE=json_text` and receive this error, the model did not return the JSON object expected by EVA's protocol. Return to `EVA_TOOL_MODE=native` to test native function calling, and restart EVA. Small local models can still fail to select tools reliably.
EVA repeats `inspect_self` or other tools
Stop the request rather than waiting indefinitely. Use `Ctrl+C` if needed, leave the EVA session, and test the selected Ollama model independently with a simple prompt. If direct generation works while EVA repeats tools, the issue is in agent/model tool interaction rather than basic Ollama connectivity. Try a model with stronger tool-use capabilities or review the EVA adapter and system prompt before editing code.
Protect API keys
Never paste API keys into chat, GitHub issues, screenshots, or logs. If a key has been exposed, revoke it and create a replacement. Do not share the contents of `.env`. For local Ollama, a real OpenAI API key is not needed.
Safety notes
Use the provided Docker sandbox only. Do not run EVA directly on the host.
Keep key-guard, approval gates, and resource limits enabled.
Review candidate diffs and test results before promoting self-modifications.
Do not run autonomous commands with `--yes --allow-shell` while learning or debugging.
Keep `.env` out of source control. Verify `.gitignore` includes `.env` and never commit credentials.
Local models can produce incorrect, outdated, or incomplete answers. Verify important information and review generated code before running it.
Attribution and license
This working setup is based on EVA — Evolvable Virtual Agent by arturkorb3. Refer to the repository's `LICENSE` file for the applicable MIT license terms and preserve upstream notices when redistributing modified copies.
Original project documentation: https://arturkorb3.github.io/eva-evolutional-agent/
---
<p align="center"><em>Built for controlled experimentation with self-evolving AI systems. Start small, keep it sandboxed, and verify every change.</em></p>