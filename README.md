# Sumit AI — EVA Local Agent

<p align="center">
  <strong>Sumit AI</strong><br />
  A locally configured AI assistant built with Ollama and the EVA (Evolvable Virtual Agent) project
</p>

<p align="center">
  <img src="https://img.shields.io/badge/AI-local%20LLM-blue" alt="Local LLM" />
  <img src="https://img.shields.io/badge/Model-Qwen3%204B-green" alt="Qwen3 4B" />
  <img src="https://img.shields.io/badge/Runtime-Ollama-black" alt="Ollama" />
  <img src="https://img.shields.io/badge/Platform-Windows%2011-blue" alt="Windows 11" />
</p>

## Project owner

**Customized and maintained by:** Sumit Ganesh Ade  
**Area of study:** Computer Science and Engineering (CSE)  
**Institution:** D. Y. Patil College of Engineering and Technology (DYPCET), Kolhapur  
**Location:** Maharashtra, India

> This repository setup customizes the local model and configuration for a personal Sumit AI assistant. The underlying EVA agent is an existing open-source project; its original authorship and license remain acknowledged below. This README does not claim that the original EVA architecture was created from scratch by Sumit.

## Overview

Sumit AI is a local assistant configuration that uses Ollama to run a language model on a Windows computer and connects that model to EVA's agent interface. The goal is to experiment with coding help, learning support, project assistance, and natural-language interaction while reducing dependence on paid cloud API credits.

The local model created during setup is named `sumit-ai-v2`. It is based on `qwen3:4b` and uses a custom system prompt to identify itself as Sumit AI. The actual quality of responses and agent tool use depends on the selected model, hardware, EVA configuration, and enabled tools.

## What it can do

- Answer general questions using a local language model.
- Generate and explain sample code, including Python programs.
- Support learning and project brainstorming.
- Run locally through Ollama, without requiring OpenAI API credits for local inference.
- Use EVA's agent workflow when the configured model and tool-calling mode are compatible.

**Current limitation:** A working response from Ollama does not automatically mean every EVA tool, self-evolution workflow, or long-running task works correctly. Test those capabilities individually before relying on them.

## Technology stack

| Component | Technology |
|---|---|
| Operating system used during setup | Windows 11 / PowerShell |
| Local model runtime | Ollama |
| Base model for `sumit-ai-v2` | `qwen3:4b` |
| Custom local model name | `sumit-ai-v2` |
| Agent framework | EVA — Evolvable Virtual Agent |
| Local OpenAI-compatible endpoint | `http://host.docker.internal:11434/v1/chat/completions` when called from the Docker container |
| Container sandbox (for EVA) | Docker Desktop, if installed and enabled |

## Architecture

```text
User
  │
  ▼
EVA terminal interface
  │
  ▼
EVA model adapter
  │
  ▼
Key-guard proxy (when enabled in the EVA Docker setup)
  │
  ▼
Ollama OpenAI-compatible endpoint
  │
  ▼
Local model: sumit-ai-v2
  │
  ▼
Response to the user
```

The `host.docker.internal` address is used when a process inside Docker needs to reach Ollama running on the Windows host. If Docker is removed, this EVA-in-Docker connection will no longer work until Docker is reinstalled or the project is adapted to a non-Docker runtime.

## Setup on Windows

### 1. Install Ollama

Install Ollama for Windows from the official site:

<https://ollama.com/download/windows>

Open PowerShell and verify:

```powershell
ollama --version
```

### 2. Download the base model

```powershell
ollama pull qwen3:4b
```

### 3. Create the Sumit AI model

In the project directory, create a file named `Modelfile.sumit-v2` with the following contents:

```text
FROM qwen3:4b
PARAMETER num_ctx 8192

SYSTEM """
You are Sumit AI, an AI assistant configured by Sumit Ganesh Ade.
Answer ordinary questions directly.
When asked for code, provide complete working code and a brief explanation.
Use tools only when they are needed for the user's request.
Do not repeatedly inspect yourself when the user asks an ordinary question.
Do not claim to be a human.
Reply in the same language as the user whenever possible.
"""
```

Build the custom model:

```powershell
ollama create sumit-ai-v2 -f .\Modelfile.sumit-v2
```

### 4. Test the model without EVA

```powershell
ollama run sumit-ai-v2 "Write a simple Python program to check whether a number is prime. Show complete code."
```

A direct response confirms that the local model can generate an answer. It does **not** by itself verify EVA's tool-calling features.

## Configure EVA to use Sumit AI

If running EVA through its provided Docker sandbox, open the repository's `.env` file and set the following active values. Keep only one active line for each setting:

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

Here `ollama` is a placeholder credential for the local Ollama endpoint; it is not a real OpenAI API key. Do not place a real API key in the endpoint field.

**Important:** EVA's `run.ps1` script layers in the Docker key-guard configuration by default. Depending on the repository's proxy configuration, the proxy may pin the upstream endpoint separately from the values read by EVA. If EVA hangs or times out, inspect the proxy logs and configuration instead of repeatedly increasing timeouts.

Start EVA from the repository folder:

```powershell
.\run.ps1
```

Try a small prompt first:

```text
Say exactly: Hello Sumit. Do not use any tools.
```

Then test a coding prompt:

```text
Write a simple Python program to check whether a number is prime. Show the complete code.
```

If native tool calling repeatedly invokes tools unnecessarily, hangs, or fails to return code, treat that as a model/tool-compatibility issue to debug—not proof that all EVA features are working.

## Troubleshooting

### Ollama works, but EVA times out

1. Test the model directly:
   ```powershell
   ollama run sumit-ai-v2 "Reply with only: Hello Sumit"
   ```
2. Test the local HTTP API from Windows:
   ```powershell
   $body = @{
       model = "sumit-ai-v2"
       messages = @(@{ role = "user"; content = "Reply with only: Hello Sumit" })
       stream = $false
   } | ConvertTo-Json -Depth 6

   Invoke-RestMethod -Uri "http://localhost:11434/v1/chat/completions" `
       -Method Post -ContentType "application/json" -Body $body |
       Select-Object -ExpandProperty choices |
       Select-Object -ExpandProperty message |
       Select-Object -ExpandProperty content
   ```
3. If using Docker, inspect the key-guard proxy logs:
   ```powershell
   docker compose -f docker-compose.yml -f docker-compose.keyguard.yml logs --tail 50 llm-proxy
   ```
4. Redact secrets before sharing logs. Do not paste API keys into chat, screenshots, issues, or commits.

### `no JSON protocol object found in model output`

This indicates EVA's `json_text` tool protocol did not receive the expected structured response. If returning to `native` mode, confirm whether the local model can reliably call the specific tools EVA exposes. A small local model may answer ordinary prompts successfully but still struggle with a complex agent tool loop.

### OpenAI API reports no credits

This local Ollama configuration does not require OpenAI API credits for local inference. Ensure the `.env` file points to Ollama and the key-guard proxy is configured for the local endpoint. Do not disable security controls merely to work around a connection error.

### Docker or WSL was uninstalled

The provided EVA project uses Docker for its sandbox. If Docker Desktop or its WSL components have been uninstalled, `.\run.ps1` will not be able to launch its Docker-based environment until Docker is restored or the project is explicitly ported to another runtime. The local Ollama model can still be tested independently with `ollama run sumit-ai-v2`.

## Security notes

- Keep the EVA agent inside its intended Docker sandbox; the upstream project is experimental and self-modifying.
- Do not enable autonomous shell approval options unless you understand the risks.
- Never commit `.env`, API keys, tokens, personal data, or private logs.
- If an API key has been exposed, revoke it and create a replacement.
- A local model can generate incorrect or outdated information; verify critical claims and review generated code before running it.

## Upstream project and license

This configuration is based on the open-source project:

**EVA — Evolvable Virtual Agent**  
<https://github.com/arturkorb3/eva-evolutional-agent>

The upstream repository provides EVA's agent architecture and Docker-based sandbox. Preserve its original `LICENSE` file and attribution notices when redistributing or modifying its code. Check the upstream repository's MIT license for the governing terms. The local model customization and this README describe Sumit's setup; they do not transfer ownership of the upstream EVA code.

## Maintainer

**Sumit Ganesh Ade**  
Computer Science and Engineering (CSE)  
Maharashtra, India

---

<p align="center">
  Built and customized by Sumit Ganesh Ade · Powered by local AI through Ollama
</p>
