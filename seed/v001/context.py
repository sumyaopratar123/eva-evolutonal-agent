#!/usr/bin/env python3
"""Context compaction: its own policy layer.

The canonical event log (session.py) is the TRUTH. What gets SENT to the model is a
compacted VIEW of it - this module owns that transformation, kept separate so the
policy can grow (full / last-n / deterministic summary / tool-result compression /
image-reference policy / mode-specific budgets) without touching the session store or
the turn loop.

Deterministic and LLM-free: compaction never calls a model, so it adds zero token cost
and stays reproducible for tests, resume and audit.
"""
from __future__ import annotations

import re

from core import Event


def chars(events) -> int:
    """Total character weight of an event list (the budget metric)."""
    return sum(len(e.content or "") for e in events)


def _outcome_snippet(result: "str | None") -> str:
    """First meaningful line of a tool result (e.g. 'exit=0' or an error), so the
    model can still tell success from failure after the step is condensed away.
    A truncated tool output is flagged, so the model knows a past read was partial
    without having to redo it just to check."""
    text = (result or "").strip()
    if not text:
        return ""
    snip = text.splitlines()[0].strip()[:60]
    if "[truncated" in text:
        snip += " (truncated)"
    return snip


# Argument keys worth preserving in the condensed summary, most identifying first.
# WHAT a call touched (path/cmd/query) is exactly what the model needs to know it
# already did something - dropping it caused redundant re-reads after compaction.
_ARG_KEYS = ("path", "cmd", "query", "url", "candidate", "topic", "signature",
             "start_line", "num_lines")


def _args_snippet(arguments) -> str:
    """Compact `key=value` view of a tool call's identifying arguments."""
    args = arguments or {}
    parts = []
    for key in _ARG_KEYS:
        if key in args and args[key] not in (None, ""):
            parts.append(f"{key}={str(args[key])[:50]}")
    return ", ".join(parts)


def summarize_dropped(dropped) -> "str | None":
    """A deterministic, LLM-free progress summary of the turns we drop from the sent
    view. One line per assistant step: its short say + EVERY tool it called (parallel
    calls included) with an outcome snippet - so compaction never hides whether a
    prior step succeeded, which would otherwise force redundant re-investigation."""
    results = {}
    for ev in dropped:
        if ev.role == "tool" and ev.tool_call_id:
            results[ev.tool_call_id] = ev.content
    lines = []
    step = 0
    for ev in dropped:
        if ev.role == "assistant":
            step += 1
            say = (ev.content or "")[:80]
            if ev.tool_calls:
                calls = []
                for tc in ev.tool_calls:
                    args = _args_snippet(tc.arguments)
                    label = f"{tc.name}({args})" if args else tc.name
                    snip = _outcome_snippet(results.get(tc.id))
                    calls.append(f"{label}: {snip}" if snip else label)
                tool = "; ".join(calls)
            else:
                tool = "-"
            lines.append(f"{step}. {say} [{tool}]")
    if not lines:
        return None
    return "Progress so far (older steps, condensed):\n" + "\n".join(lines)


# Matches the soft loop-detection nudge core.py appends to observations.
_LOOP_SIG = re.compile(r"\[loop signal: access #(\d+) to '([^']+)'")


def _loop_status(dropped) -> "str | None":
    """Sticky counterpart of the per-observation loop signal: the nudge lives inside
    tool outputs, so compaction would EVICT exactly the advice that warns about
    eviction. Re-surface the flagged targets in the condensed view so the warning
    survives every compaction for the rest of the session."""
    hits: dict[str, int] = {}
    for ev in dropped:
        if ev.role == "tool":
            for m in _LOOP_SIG.finditer(ev.content or ""):
                target = m.group(2)
                hits[target] = max(hits.get(target, 0), int(m.group(1)))
    if not hits:
        return None
    tops = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
    listing = ", ".join(f"'{t}' x{n}" for t, n in tops)
    return ("LOOP STATUS (sticky): repeated accesses to the same files were flagged "
            f"earlier: {listing}. Those reads DID happen - their outcomes are in the "
            "progress summary above. Do not re-read them; state what you learned and "
            "act on it, or raise the visible-history budget via "
            "set_param('history_budget', ...).")


def compact(events, budget: int, keep: int) -> "list[Event]":
    """Send the full log while it fits `budget`; past that send system + first task +
    a condensed summary of the dropped middle + the last `keep` events verbatim."""
    evs = list(events)
    if chars(evs) <= budget or len(evs) <= keep + 2:
        return evs

    system = evs[0]
    first = evs[1]
    first_trimmed = Event(role=first.role, content=(first.content or "")[:800])
    # Cut at a TURN boundary: extend the tail backwards over leading tool events so
    # it starts at the assistant step that made those calls. A tail that opens with
    # orphaned tool results loses the pairing (and confuses strict provider APIs).
    cut = len(evs) - keep
    while cut > 2 and evs[cut].role == "tool":
        cut -= 1
    tail = evs[cut:]
    dropped = evs[2:cut]

    view = [system, first_trimmed]
    summary = summarize_dropped(dropped)
    status = _loop_status(dropped)
    if status:
        # merged into ONE user event: consecutive same-role messages upset strict
        # provider APIs, and the status belongs to the condensed context anyway
        summary = f"{summary}\n\n{status}" if summary else status
    if summary:
        view.append(Event(role="user", content=summary))
    view.extend(tail)
    return view
