#!/usr/bin/env python3
"""EVA provider-neutral core.

This module knows NOTHING about OpenAI, Chat Completions, the Responses API,
Ollama, LM Studio, or any wire format. It defines EVA's OWN semantic agent
model: events, tools, tool calls, observations and a model-agnostic turn loop.

The separation is deliberate (see the project critique):

    EVA-Core  ->  talks ONLY to a ModelAdapter (never to OpenAI directly)
    ModelAdapter  ->  may be OpenAI-native, Chat-native, or JSON-text based
    ToolRuntime   ->  decides, gates, logs and executes tool calls (EVA owns this)
    HumanInterface / ApprovalPolicy  ->  how a human is asked, swappable
    SessionStore  ->  the canonical append-only event log (source of truth)

The core only orchestrates these collaborators. It is the stable seam that lets
the rest of EVA evolve without re-binding to a provider.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


# --------------------------------------------------------------------------- #
# Canonical semantic types
# --------------------------------------------------------------------------- #
@dataclass
class Tool:
    """An EVA tool, defined ONCE, provider-independently.

    Adapters render this into whatever the provider understands (native function
    calling, or an embedded JSON-text protocol). The core never cares which.
    """
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    """A model's request to run a tool. EVA decides whether/how to honour it."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolObservation:
    """The result of executing a ToolCall, fed back to the model next turn."""
    call_id: str
    name: str
    output: str


@dataclass
class Event:
    """One entry in the canonical event log (the single source of truth).

    role is one of: "system", "user", "assistant", "tool".
    assistant events may carry tool_calls; tool events carry a tool_call_id.
    """
    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # Provider-neutral image attachments: {"url": "data:<mime>;base64,..."} dicts.
    images: list[dict] = field(default_factory=list)
    # Audit trail: ts, mode, step, model/adapter identity, tool id. Provider-neutral
    # and free-form so an evolving system stays accountable (who/what/when/which model)
    # without coupling the log to any wire format.
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments}
                for c in self.tool_calls
            ],
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "images": list(self.images or []),
            "meta": dict(self.meta or {}),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(
            role=d.get("role", "user"),
            content=d.get("content", "") or "",
            tool_calls=[
                ToolCall(id=c.get("id", ""), name=c.get("name", ""),
                         arguments=c.get("arguments", {}) or {})
                for c in (d.get("tool_calls") or [])
            ],
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name"),
            images=d.get("images") or [],
            meta=d.get("meta") or {},
        )


@dataclass
class ModelResult:
    """What an adapter returns for one turn: free-text + zero or more tool calls.

    `final` lets an adapter signal "I am done, no tool needed" even when it makes
    no tool call (e.g. a plain answer). The loop also stops when a `finish` tool
    is executed - that path keeps the protocol uniform across adapters.
    """
    say: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    final: bool = False


@dataclass
class AgentTurn:
    """Everything an adapter needs to produce the next ModelResult."""
    system: str
    events: list[Event]
    tools: list[Tool]
    mode: str


# --------------------------------------------------------------------------- #
# Collaborator protocols (duck-typed; no inheritance required)
# --------------------------------------------------------------------------- #
class ModelAdapter(Protocol):
    supports_native_tools: bool

    def run_turn(self, turn: AgentTurn,
                 on_delta: "Callable[[str], None] | None" = None) -> ModelResult:
        ...


class ToolRuntime(Protocol):
    def execute(self, call: ToolCall, mode: str) -> ToolObservation:
        ...


class SessionStore(Protocol):
    def events(self) -> list[Event]:
        ...

    def append(self, event: Event) -> None:
        ...


# Tool names that terminate the loop when successfully executed.
FINISH_TOOL = "finish"

# Loop breaker: after this many IDENTICAL calls (same tool, same arguments) that
# produced the SAME output within one turn-loop, further repeats are answered with
# a warning instead of executed. Repeating a call whose result hasn't changed adds
# no information - it is the signature of a compaction-induced context loop.
REPEAT_CALL_LIMIT = 2

_REPEAT_WARNING = (
    "Repeated identical call blocked: you already ran this exact tool call with the "
    "same arguments {n} times this turn and got the SAME result each time - it is in "
    "your recent context and in the condensed progress summary above. Re-running it "
    "will not produce new information. Use the result you already have to proceed; "
    "if something is genuinely missing, read a DIFFERENT range/path or ask the user."
)


def _call_key(call: ToolCall) -> str:
    try:
        args = json.dumps(call.arguments or {}, sort_keys=True, default=str)
    except Exception:
        args = repr(call.arguments)
    return f"{call.name}:{args}"


# Soft loop-detection signal: the hard breaker above only catches EXACT repeats,
# but a context-starved model loops semantically - overlapping reads of the same
# file, varied greps against the same target. From this many accesses to the SAME
# target within one turn-loop, the (still executed) observation gets a visible
# nudge: use what you have, write findings down, or raise history_budget.
LOOP_SIGNAL_THRESHOLD = 4

_LOOP_SIGNAL = (
    "\n[loop signal: access #{n} to '{target}' this turn. If you are re-reading "
    "because earlier findings are no longer visible, your compacted context may be "
    "evicting them. State what you have already learned and proceed with it - or "
    "raise the visible-history budget via set_param('history_budget', ...).]"
)

_PATH_TOKEN = re.compile(r"""["']?([~.\w-]*/[\w./*-]+|[\w.-]+\.[A-Za-z]{1,4})["']?""")


def _call_targets(call: ToolCall) -> "list[str]":
    """Coarse 'what is this call about' extraction: the path argument of read_file,
    or path-like tokens inside a shell command. Empty for everything else."""
    args = call.arguments or {}
    if call.name == "read_file":
        p = str(args.get("path", "") or "").strip()
        return [p] if p else []
    if call.name == "shell":
        cmd = str(args.get("cmd", "") or "")
        return sorted({m.group(1) for m in _PATH_TOKEN.finditer(cmd)})
    return []


def run_agent_loop(
    *,
    adapter: ModelAdapter,
    runtime: ToolRuntime,
    session: SessionStore,
    tools: list[Tool],
    system: str,
    mode: str,
    max_steps: int = 50,
    on_say: Callable[[str], None] | None = None,
    on_say_delta: Callable[[str], None] | None = None,
    on_tool_call: Callable[[ToolCall], None] | None = None,
    on_observation: Callable[[ToolObservation], None] | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Run one provider-neutral agent turn-loop until it terminates.

    Returns an outcome string: "finish" | "final" | "maxsteps" | "error" | "stopped".
    The core never inspects provider wire formats; it only asks the adapter for
    a ModelResult, executes any tool calls through the runtime, and records
    everything as Events in the session (the canonical log). `should_stop` lets the
    caller end the loop promptly between actions (e.g. a pivot request) instead of
    running until the model happens to finish.

    `on_say_delta`, if given, is forwarded to the adapter as a streaming sink: an
    adapter that supports streaming calls it with assistant text fragments as they
    arrive. Adapters that do not stream simply ignore it; the final ModelResult is
    identical either way, so the rest of the loop is unaffected.
    """
    # Adapter identity (model/adapter/endpoint) is stamped into each event's audit
    # meta. Optional + duck-typed so the core stays provider-neutral.
    try:
        identity = dict(adapter.identity()) if hasattr(adapter, "identity") else {}
    except Exception:
        identity = {}

    step = 0
    # (tool name + args) -> (output hash, identical-result count) for the repeat guard.
    repeat_seen: dict[str, tuple[str, int]] = {}
    # target path -> access count for the soft loop-detection signal.
    target_hits: dict[str, int] = {}
    for _ in range(max_steps):
        step += 1
        turn = AgentTurn(system=system, events=session.events(), tools=tools, mode=mode)

        try:
            result = adapter.run_turn(turn, on_delta=on_say_delta)
        except Exception as exc:  # adapter / transport failure
            if on_error:
                on_error("model", exc)
            return "error"

        asst_meta = {"ts": time.time(), "mode": mode, "step": step,
                     "kind": "assistant"}
        asst_meta.update(identity)
        session.append(Event(role="assistant", content=result.say,
                             tool_calls=list(result.tool_calls), meta=asst_meta))
        if on_say and result.say:
            on_say(result.say)

        if not result.tool_calls:
            # No tool requested: the model produced a plain answer / question.
            return "final"

        terminated = False
        for call in result.tool_calls:
            if on_tool_call:
                on_tool_call(call)
            key = _call_key(call)
            prev = repeat_seen.get(key)
            if (call.name != FINISH_TOOL and prev is not None
                    and prev[1] >= REPEAT_CALL_LIMIT):
                # Loop breaker: don't execute yet another identical call whose
                # result has already proven stable - answer with guidance instead.
                obs = ToolObservation(call_id=call.id, name=call.name,
                                      output=_REPEAT_WARNING.format(n=prev[1]))
            else:
                try:
                    obs = runtime.execute(call, mode)
                except Exception as exc:
                    if on_error:
                        on_error("tool", exc)
                    obs = ToolObservation(
                        call_id=call.id, name=call.name,
                        output=f"Tool '{call.name}' crashed: {type(exc).__name__}: {exc}",
                    )
                digest = hashlib.sha256((obs.output or "").encode(
                    "utf-8", "replace")).hexdigest()
                if prev is not None and prev[0] == digest:
                    repeat_seen[key] = (digest, prev[1] + 1)
                else:
                    repeat_seen[key] = (digest, 1)
                # Soft loop signal for near-repeats the hard breaker cannot see.
                for target in _call_targets(call):
                    n = target_hits.get(target, 0) + 1
                    target_hits[target] = n
                    if n >= LOOP_SIGNAL_THRESHOLD:
                        obs = ToolObservation(
                            call_id=obs.call_id, name=obs.name,
                            output=(obs.output or "")
                            + _LOOP_SIGNAL.format(n=n, target=target))
                        break
            tool_meta = {"ts": time.time(), "mode": mode, "step": step,
                         "kind": "tool", "tool": obs.name,
                         "tool_call_id": obs.call_id}
            session.append(Event(role="tool", content=obs.output,
                                 tool_call_id=obs.call_id, name=obs.name,
                                 meta=tool_meta))
            if on_observation:
                on_observation(obs)
            if should_stop and should_stop():
                return "stopped"
            if call.name == FINISH_TOOL:
                terminated = True

        if terminated:
            return "finish"

    return "maxsteps"
