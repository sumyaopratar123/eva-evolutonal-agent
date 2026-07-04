#!/usr/bin/env python3
"""Parameter harness: EVA's tunable runtime parameters, declared ONCE.

Numbers like the history budget, the per-turn step limit or read_file's default
size used to live as scattered module constants - invisible to EVA and frozen at
import time. This registry makes them a first-class, self-inspectable part of
the organism:

  - every parameter has a NAME, an ENV VAR, a DEFAULT, hard BOUNDS and a
    one-line description of what it tunes and where it acts;
  - `get(name)` reads the LIVE value (env var wins over default) each call, so
    a change takes effect immediately - no restart, no re-import;
  - `set_value(name, value)` validates against the bounds and writes the env
    var: SESSION-scoped by design (the process env dies with the container),
    so a tuning experiment can never silently become permanent config;
  - `render()` feeds inspect_self("parameters") so EVA can SEE its own knobs.

Durable changes stay a human act (edit .env / docker-compose); EVA adjusting a
knob mid-session (via the set_param tool, human-approved) is a bounded,
reversible experiment - which is exactly the evolution philosophy in miniature.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Param:
    env: str          # environment variable that carries the live value
    default: int
    lo: int           # hard lower bound (set_value refuses below)
    hi: int           # hard upper bound (set_value refuses above)
    desc: str         # what it tunes + where it acts


REGISTRY: dict[str, Param] = {
    "history_budget": Param(
        "ORGANISM_HISTORY_BUDGET", 96000, 8000, 2_000_000,
        "compaction budget in CHARS (~4 chars/token) for the visible history "
        "(agent.py _CompactSession); older events beyond it are summarized"),
    "history_keep": Param(
        "ORGANISM_HISTORY_KEEP", 16, 2, 200,
        "how many most-recent events stay verbatim when history is compacted "
        "(agent.py _CompactSession)"),
    "max_steps": Param(
        "ORGANISM_MAX_STEPS", 50, 5, 500,
        "step budget per agent turn-loop (core.run_agent_loop); on exhaustion "
        "EVA pauses and asks to continue instead of aborting"),
    "max_auto_continues": Param(
        "ORGANISM_MAX_AUTO_CONTINUES", 2, 0, 20,
        "extra step batches an UNATTENDED (--yes) run may auto-continue after "
        "hitting max_steps (agent.py run_mode)"),
    "pivot_threshold": Param(
        "ORGANISM_PIVOT_THRESHOLD", 3, 1, 20,
        "how often the SAME friction signature must recur in the backlog "
        "before it is proposed as an evolution (agent.py)"),
    "read_file_max_chars": Param(
        "EVA_READ_FILE_MAX_CHARS", 6000, 200, 200_000,
        "default max_chars of the read_file tool (tools.py); deliberately "
        "SMALL vs. history_budget so one big read cannot evict the context"),
    "fetch_url_max_chars": Param(
        "EVA_FETCH_URL_MAX_CHARS", 6000, 200, 200_000,
        "default max_chars of the fetch_url tool (tools.py)"),
    "shell_max_output": Param(
        "EVA_SHELL_MAX_OUTPUT", 6000, 500, 200_000,
        "max chars of captured stdout/stderr per shell command (tools.py)"),
    "llm_timeout": Param(
        "LLM_TIMEOUT", 180, 10, 3600,
        "HTTP timeout in seconds for one model request (adapters.py)"),
}


def get(name: str) -> int:
    """LIVE value of a parameter: env var (tolerating ''/garbage) else default.
    Read at every call site on purpose - a set_value takes effect immediately."""
    p = REGISTRY[name]
    try:
        v = int(os.environ.get(p.env, "") or p.default)
    except (TypeError, ValueError):
        return p.default
    return max(p.lo, min(p.hi, v))


def set_value(name: str, value) -> str:
    """Validate + apply a SESSION-scoped override (writes the process env var).
    Returns a human-readable confirmation; raises ValueError on a bad name/value."""
    p = REGISTRY.get(name)
    if p is None:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"unknown parameter '{name}'; known: {known}")
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} needs an integer, got {value!r}")
    if not (p.lo <= v <= p.hi):
        raise ValueError(f"{name}={v} is outside the allowed bounds [{p.lo}, {p.hi}]")
    old = get(name)
    os.environ[p.env] = str(v)
    return (f"{name}: {old} -> {v} (session-scoped via {p.env}; resets on restart. "
            f"A durable change is a human .env edit.)")


def render() -> str:
    """The whole harness for inspect_self('parameters'): name, live value,
    default, bounds and what each knob does."""
    lines = ["Parameter harness (live values; adjustable session-scoped via set_param):"]
    for name in sorted(REGISTRY):
        p = REGISTRY[name]
        cur = get(name)
        mark = "" if cur == p.default else f"  [override: default {p.default}]"
        lines.append(f"  - {name} = {cur}{mark}")
        lines.append(f"      env {p.env}, bounds [{p.lo}, {p.hi}]: {p.desc}")
    lines.append("")
    lines.append("set_param changes are SESSION-scoped (env of this process) and bounded; "
                 "durable config stays a human edit of .env. Tune when the defaults pinch "
                 "- e.g. raise history_budget for a long refactor, or max_steps for a "
                 "big autonomous task.")
    return "\n".join(lines)
