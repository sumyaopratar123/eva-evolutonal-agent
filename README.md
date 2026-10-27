<div align="center">

# SUMIT AI
### A Personal Local AI Assistant — Built by Sumit Ganesh Ade

**Private-by-default local inference • Ollama • Windows • Experimental agent workflow**

![Project](https://img.shields.io/badge/Project-Sumit%20AI-2563eb?style=for-the-badge)
![Local AI](https://img.shields.io/badge/AI-Local%20Inference-16a34a?style=for-the-badge)
![Ollama](https://img.shields.io/badge/Runtime-Ollama-black?style=for-the-badge&logo=ollama)
![Platform](https://img.shields.io/badge/Platform-Windows%2011-0078D4?style=for-the-badge&logo=windows)

*A customized AI assistant configured, tested, and documented by Sumit.*

</div>

---

## About the Project

**Sumit AI** is my personal AI assistant project. I configured a local language model with Ollama, created a custom assistant identity, and connected the model to an experimental agent workflow. The aim is to explore a useful, extensible AI assistant for coding, learning, project work, and everyday questions—without needing paid cloud-model credits for local inference.

The assistant's local model is named **`sumit-ai-v2`**, based on **`qwen3:4b`** with a custom Modelfile and system prompt. Its actual capabilities depend on the model, computer hardware, agent configuration, and enabled tools.

> **Project ownership and transparency:** Sumit AI is my configured and customized project. This repository also contains open-source components and their existing license notices. Those notices must remain with the applicable code; the project-specific configuration, model customization, and documentation are presented here under my project branding.

## Table of Contents

- [Highlights](#highlights)
- [Current Status](#current-status)
- [Technology Stack](#technology-stack)
- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [Set Up Sumit AI](#set-up-sumit-ai)
- [Connect Sumit AI to the Agent](#connect-sumit-ai-to-the-agent)
- [Test the Local API](#test-the-local-api)
- [Everyday Commands](#everyday-commands)
- [Troubleshooting](#troubleshooting)
- [Security and Responsible Use](#security-and-responsible-use)
- [Planned Improvements](#planned-improvements)
- [Built By](#built-by)
- [License and Third-Party Notices](#license-and-third-party-notices)

## Highlights

- **Local language model:** Runs inference through Ollama on the Windows host.
- **Personal assistant identity:** Uses a custom model named `sumit-ai-v2` and a prompt tailored for Sumit AI.
- **Coding support:** Can generate and explain sample programs, including Python, when the model responds correctly.
- **Agent integration:** Can be connected to the repository's terminal-based agent workflow.
- **OpenAI-compatible local API:** Ollama exposes a Chat Completions-compatible endpoint for supported clients.
- **No OpenAI API credits for local inference:** Text generation performed by the local Ollama model does not use OpenAI API credits. Local hardware and disk space are still required.
- **Windows-friendly workflow:** Setup and commands below use PowerShell.

## Current Status

| Component | Status |
|---|---|
| Ollama installed on Windows | Tested in the development setup |
| Base model `qwen3:4b` downloaded | Tested in the development setup |
| Custom model `sumit-ai-v2` created | Tested in the development setup |
| Direct Ollama prompt response | Tested successfully |
| Local `/v1/chat/completions` endpoint | Tested successfully |
| Agent workflow and tool calling | Experimental; requires further testing |
| Self-evolution and long-running agent tasks | Not guaranteed with a small local model |

A direct model response does **not** prove that every agent tool, autonomous workflow, or self-evolution feature works end-to-end. Validate those separately before relying on them.

## Technology Stack

| Area | Technology |
|---|---|
| Local inference runtime | Ollama |
| Base language model | Qwen3 4B (`qwen3:4b`) |
| Custom model name | `sumit-ai-v2` |
| Agent interface | Existing terminal-based agent in this repository |
| Container sandbox | Docker Desktop and Docker Compose, when running the agent through its provided sandbox |
| Operating system used for setup | Windows 11 / PowerShell |
| Model API | Ollama OpenAI-compatible Chat Completions endpoint |

## How It Works

```text
You (PowerShell / agent terminal)
              |
              v
    Agent interface (optional)
              |
              v
       Model adapter
              |
              v
   Key-guard proxy (if enabled)
              |
              v
 Ollama API on the Windows host
              |
              v
       sumit-ai-v2
              |
              v
       Generated response
```

When the agent runs inside Docker and Ollama runs on Windows, `host.docker.internal` allows the container to reach the host's Ollama API. The key-guard proxy's upstream endpoint must point to Ollama too. If the proxy is still configured for a cloud API, the agent may use the wrong upstream or time out.

## Requirements

- Windows 10/11 with PowerShell.
- Ollama for Windows.
- Enough free disk space to download the base model.
- For the repository's Docker-sandboxed agent: Docker Desktop with Docker Compose working.
- The repository cloned locally, with its own `LICENSE` file preserved.

**Important:** If Docker Desktop or its WSL components have been uninstalled, the local Ollama model can still run independently, but the provided Docker-based agent launcher will not work until its required container runtime is restored or the project is deliberately adapted to a different runtime.

## Set Up Sumit AI

### 1. Install or verify Ollama

Install Ollama for Windows from the official download page:

<https://ollama.com/download/windows>

Open PowerShell and check:

```powershell
ollama --version
```

### 2. Download the base model

```powershell
ollama pull qwen3:4b
```

### 3. Create the custom Modelfile

In the project directory, create a file named **`Modelfile.sumit-v2`** with the following contents:

```text
FROM qwen3:4b
PARAMETER num_ctx 8192

SYSTEM """
You are Sumit AI, a personal AI assistant configured by Sumit Ganesh Ade.
Answer ordinary questions directly and clearly.
When asked for code, provide complete working code and a short explanation.
Use tools only when needed for the user's actual request.
Do not repeatedly inspect yourself when the user asks an ordinary question.
Do not claim to be a human.
Reply in the same language as the user whenever possible.
"""
```

Create the custom model:

```powershell
ollama create sumit-ai-v2 -f .\Modelfile.sumit-v2
```

### 4. Test the model directly

```powershell
ollama run sumit-ai-v2 "Reply with only: Hello Sumit"
```

You should get a short response. Exit an interactive Ollama session by entering `/bye`.

## Connect Sumit AI to the Agent

> This section applies only if the Docker-based agent launcher is installed and working. Keep the repository's sandbox and approval protections enabled.

Open the environment file:

```powershell
notepad .env
```

Set the following active values, with **only one active line per setting**:

```dotenv
EVA_PROVIDER=openai_chat
EVA_TOOL_MODE=native
EVA_ENDPOINT=http://host.docker.internal:11434/v1/chat/completions
EVA_MODEL=sumit-ai-v2
EVA_API_KEY=ollama
LLM_TIMEOUT=600
ORGANISM_HISTORY_BUDGET=12000
ORGANISM_HISTORY_KEEP=3
```

Notes:

- `ollama` is a placeholder credential for the local endpoint; it is not an OpenAI API key.
- Do not paste real API keys into this file's endpoint line.
- Save `.env` locally, but do not commit it to GitHub.
- A key-guard sidecar may configure its own upstream endpoint. Confirm the proxy itself points to Ollama if requests time out.
- Keep autonomous shell-approval options disabled unless you fully understand the risks.

Start the agent from the repository root:

```powershell
.\run.ps1
```

Try a simple prompt first:

```text
Say exactly: Hello Sumit. Do not use any tools.
```

Then test a coding prompt:

```text
Write a simple Python program to check whether a number is prime. Show the complete code.
```

If the model repeatedly invokes tools unnecessarily, produces malformed tool-call JSON, or hangs, treat that as an agent/model compatibility problem. A small model can work well for direct chat while struggling with complex agent tool loops.

## Test the Local API

This command tests the local Ollama HTTP API directly from Windows PowerShell:

```powershell
$body = @{
    model = "sumit-ai-v2"
    messages = @(
        @{ role = "user"; content = "Reply with only: Hello Sumit" }
    )
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

Expected output should be a short reply such as `Hello Sumit`. If this works but the agent fails, the issue is more likely to be in the agent/proxy/tool configuration than in the direct local API call.

## Everyday Commands

| Task | Command |
|---|---|
| Check Ollama | `ollama --version` |
| List local models | `ollama list` |
| Run Sumit AI interactively | `ollama run sumit-ai-v2` |
| Ask one prompt | `ollama run sumit-ai-v2 "Explain recursion simply"` |
| Stop an interactive model session | Type `/bye` |
| Open local agent settings | `notepad .env` |
| Start Docker-based agent | `.\run.ps1` |
| Inspect proxy logs | `docker compose -f docker-compose.yml -f docker-compose.keyguard.yml logs --tail 50 llm-proxy` |

## Troubleshooting

### Ollama works, but the agent times out

1. Test the model directly with `ollama run sumit-ai-v2 "Reply with only: Hello Sumit"`.
2. Test the local API using [Test the Local API](#test-the-local-api).
3. If Docker is enabled, inspect the proxy logs:

   ```powershell
   docker compose -f docker-compose.yml -f docker-compose.keyguard.yml logs --tail 50 llm-proxy
   ```

4. Look for the upstream URL and timeout errors. Do not post logs containing API keys, tokens, or other secrets.

### `no JSON protocol object found in model output`

This means a text-based JSON tool protocol did not receive the structured response it expected. Use the mode recommended by the agent's documentation and verify the selected model supports the required tools. Do not assume switching modes will always solve the problem.

### The agent repeatedly calls a self-inspection tool

The model may be overusing tools or failing to decide when a tool is necessary. Test direct local prompts first, use concise tasks, and confirm the model and tool-calling configuration. If the behavior persists, the agent's tool policy or model adapter may need a code-level change.

### `HTTP 429` from a cloud provider

A cloud API may have a separate balance or quota. Local inference through Ollama does not need OpenAI API credits, provided the agent and proxy are both configured to route to the local Ollama endpoint.

### Docker or WSL has been removed

Direct Ollama commands can continue to work on Windows. The Docker-based agent launcher will need Docker Desktop and its required backend restored before it can run again.

## Security and Responsible Use

- Run the self-modifying agent only in its intended sandbox.
- Keep the key-guard and human approval controls enabled.
- Do not enable autonomous shell approval unless you understand and accept the risk.
- Never commit `.env`, API keys, passwords, access tokens, private logs, or personal data.
- If an API key has been exposed, revoke it and create a new one.
- Review generated code before running it and verify important factual claims.
- Back up data before deleting Docker containers, volumes, or WSL distributions.

## Planned Improvements

- Improve reliability of tool selection and structured tool calls.
- Add automated tests for direct model access and agent integration.
- Create clear setup scripts and diagnostics for Windows users.
- Add multilingual response testing for English, Hindi, and Marathi.
- Improve error reporting for proxy timeouts and model incompatibility.
- Document which agent workflows have been tested with the local model.

## Built By

**Sumit Ganesh Ade**  
Computer Science and Engineering (CSE)  
DYPCET, Kolhapur · Maharashtra, India

This project represents my Sumit AI configuration, local model customization, integration work, testing, and documentation.

## License and Third-Party Notices

See the repository's **`LICENSE`** file for applicable terms. This repository contains open-source components; keep all existing copyright notices and license files with the relevant code when modifying, sharing, or redistributing it. The Sumit AI branding and project-specific configuration documented here do not replace or remove those requirements.

---

<div align="center">

**SUMIT AI**  
*Built and customized by Sumit Ganesh Ade · Powered by local AI through Ollama*

</div>
