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

import json
import re

from core import Event

# The cut point advances in blocks of CUT_STRIDE events instead of every turn.
# A cut that moves each turn rewrites the synthetic summary each turn, which
# invalidates the provider's prompt cache for the WHOLE prefix on every request.
# Quantizing the cut keeps the prefix byte-stable for many turns (cache hits),
# at the price of a tail that grows up to one stride beyond `keep`.
CUT_STRIDE = 8

# The youngest slice of the dropped middle is kept at MID fidelity: assistant
# prose verbatim (the model's own recent reasoning), tool outputs trimmed to an
# outcome snippet. Only older turns degrade to one-line summaries - fidelity
# falls off gradually with age instead of a verbatim/one-liner cliff.
RECENT_WINDOW = 8

# Tool calls that mark session milestones. They are pinned into the condensed
# view no matter how old they are: what was evolved, whether tests passed and
# what needs were noted is exactly the state the model must never lose.
MILESTONE_TOOLS = ("make_candidate", "run_tests", "request_promotion",
                   "note_evolution_need")


def chars(events) -> int:
    """Total character weight of an event list (the budget metric). Tool-call
    arguments count too: a write_file call can carry kilobytes in its `content`
    argument, and a budget that ignores them undercounts what the provider sees."""
    total = 0
    for e in events:
        total += len(e.content or "")
        for tc in (e.tool_calls or []):
            try:
                total += len(json.dumps(tc.arguments or {}))
            except (TypeError, ValueError):
                total += len(str(tc.arguments))
    return total


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


# Prefixed to the synthetic summary event so the model treats it as MEMORY of older
# turns, never as a fresh instruction from the human (the event must use role="user"
# because strict provider APIs reject unknown roles).
MEMORY_MARK = "[condensed context - memory of earlier turns, not a new instruction]"


def human_said(dropped) -> "str | None":
    """User messages from the dropped middle, preserved verbatim (condensed). Older
    corrections, decisions and constraints ('only edit X', 'never do Y', 'I rejected
    approach Z') stay binding even after the turns that carried them are evicted -
    dropping them from the sent view is how an agent silently violates instructions
    it was given twenty turns ago. Deduplicated, per-line and total bounded."""
    notes = []
    seen = set()
    for ev in dropped:
        if ev.role != "user":
            continue
        text = (ev.content or "").strip()
        if not text or text.startswith(MEMORY_MARK):
            continue  # never re-quote an earlier synthetic summary
        line = " ".join(text.split())[:200]
        if line not in seen:
            seen.add(line)
            notes.append(line)
    if not notes:
        return None
    notes = notes[-10:]  # keep the most recent ten; the full text stays in the log
    return ("Human said earlier (may still be binding - decisions, constraints, "
            "corrections):\n" + "\n".join(f"- {n}" for n in notes))


def milestones(dropped) -> "str | None":
    """Milestone pinning: calls to MILESTONE_TOOLS from the dropped middle survive
    as their own section regardless of age. Candidate names, test PASS/FAIL and
    noted evolution needs are session STATE, not progress - a model that loses
    them re-creates candidates or re-requests promotions it already made."""
    results = {}
    for ev in dropped:
        if ev.role == "tool" and ev.tool_call_id:
            results[ev.tool_call_id] = ev.content
    lines = []
    for ev in dropped:
        if ev.role != "assistant":
            continue
        for tc in (ev.tool_calls or []):
            if tc.name in MILESTONE_TOOLS:
                args = _args_snippet(tc.arguments)
                label = f"{tc.name}({args})" if args else tc.name
                snip = _outcome_snippet(results.get(tc.id))
                lines.append(f"- {label}: {snip}" if snip else f"- {label}")
    if not lines:
        return None
    return "Milestones (older, still true):\n" + "\n".join(lines[-12:])


def _trim_tool_event(ev: Event) -> Event:
    """MID-fidelity view of a tool result: same pairing (id/name), content reduced
    to its outcome snippet - keeps strict provider APIs happy while shedding bulk."""
    text = (ev.content or "")
    if len(text) <= 240:
        return ev
    return Event(role="tool", content=_outcome_snippet(text) + " [trimmed by compaction]",
                 tool_call_id=ev.tool_call_id, name=ev.name)


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


def _turn_boundary(evs, cut: int) -> int:
    """Pull a cut index backwards over tool events so the slice after it starts at
    the assistant step that made those calls. A slice that opens with orphaned tool
    results loses the pairing (and confuses strict provider APIs)."""
    while cut > 2 and evs[cut].role == "tool":
        cut -= 1
    return cut


def compact(events, budget: int, keep: int) -> "list[Event]":
    """Send the full log while it fits `budget`; past that, degrade by age:
    system + trimmed first task + ONE condensed-memory event (human-said /
    milestones / progress / loop status) + a MID-fidelity recent window (assistant
    verbatim, tool outputs trimmed) + the last `keep` events verbatim.

    The verbatim cut is quantized to CUT_STRIDE so the sent prefix stays
    byte-identical across many turns - provider prompt caches survive."""
    evs = list(events)
    if chars(evs) <= budget or len(evs) <= keep + 2:
        return evs

    system = evs[0]
    first = evs[1]
    first_trimmed = Event(role=first.role, content=(first.content or "")[:800])
    # Quantized cut: never tighter than `keep`, advances only every CUT_STRIDE
    # events (prompt-cache hysteresis), then aligned to a turn boundary.
    cut = len(evs) - keep
    cut -= (cut - 2) % CUT_STRIDE
    cut = _turn_boundary(evs, max(2, cut))
    tail = evs[cut:]
    # Graduated middle: the youngest RECENT_WINDOW dropped events keep MID
    # fidelity (own prose intact, tool bulk shed); everything older is condensed.
    mid_cut = _turn_boundary(evs, max(2, cut - RECENT_WINDOW))
    recent = [(_trim_tool_event(e) if e.role == "tool" else e)
              for e in evs[mid_cut:cut]]
    dropped = evs[2:mid_cut]

    view = [system, first_trimmed]
    # merged into ONE user event: consecutive same-role messages upset strict
    # provider APIs, and all sections belong to the same condensed context.
    # Loop status scans the recent window too - its signals live in tool outputs
    # that _trim_tool_event may have just shed.
    parts = [p for p in (human_said(dropped), milestones(dropped),
                         summarize_dropped(dropped),
                         _loop_status(evs[2:cut])) if p]
    if parts:
        view.append(Event(role="user", content=MEMORY_MARK + "\n" + "\n\n".join(parts)))
    view.extend(recent)
    view.extend(tail)
    return view
