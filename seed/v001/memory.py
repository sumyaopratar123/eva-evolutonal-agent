#!/usr/bin/env python3
"""Episodic memory: EVA's cross-session memory organ.

Sessions die; episodes survive. When a session ends, agent.py distills the
canonical event log into ONE compact, deterministic digest - task, outcome,
tools used, files touched, result - and appends it to state/episodes.jsonl.
Retrieval is LLM-free keyword scoring: the `recall` tool searches episodes AND
the lessons journal on demand, and the most relevant past episodes for a new
task are auto-injected into the session's initial context.

Like the friction backlog this is BEST-EFFORT memory: a failure to remember
must never crash the agent. The digests are derived data - the full truth
stays in the per-session logs under state/sessions/.
"""
from __future__ import annotations

import json
import os
import pathlib
import time


EPISODES_NAME = "episodes.jsonl"
LESSONS_NAME = "lessons.jsonl"

# Tools whose calls MODIFY something - their `path`/`candidate` argument is what a
# future EVA most wants to know ("which files did I touch last time?").
_WRITE_TOOLS = ("write_file", "replace_in_file", "apply_patch")

_STOPWORDS = frozenset(
    "the a an and or not for with into from that this then them your you are was were "
    "have has had can could should would may might will shall its it's about after "
    "und der die das den dem ein eine einen einem mit für von aus auf bitte auch "
    "nicht kein wie was wenn dann dass ist sind war bei nach vor mal noch".split()
)


def _state_dir() -> pathlib.Path:
    """Resolved at CALL time (not import) so tests and tools can retarget the
    organism root via ORGANISM_ROOT, exactly like the self-model does."""
    root = pathlib.Path(os.environ.get("ORGANISM_ROOT", pathlib.Path.cwd())).resolve()
    return root / "state"


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    except Exception:
        pass
    return rows


def _tokens(text: str) -> set[str]:
    words = "".join(c if c.isalnum() else " " for c in (text or "").lower()).split()
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _task_of(events) -> str:
    """The user's task from the first user event, WITHOUT the injected context block
    (agent.py seeds `task + "\n\n" + Mode: ...`), so episodes stay about the task."""
    for e in events:
        if getattr(e, "role", None) == "user":
            text = str(getattr(e, "content", "") or "")
            return text.split("\n\nMode:", 1)[0].strip()[:300]
    return ""


def digest(events, mode: str, outcome: str | None) -> dict | None:
    """Distill a finished session's event log into one compact, deterministic
    episode record. Returns None when nothing memorable happened (no assistant
    activity), so empty sessions leave no residue."""
    task = _task_of(events)
    tools: dict[str, int] = {}
    files: list[str] = []
    result = ""
    retro = ""
    tags: list[str] = []
    for e in events:
        if getattr(e, "role", None) != "assistant":
            continue
        say = str(getattr(e, "content", "") or "").strip()
        if say:
            result = say  # last assistant prose wins; finish summary overrides below
        for tc in (getattr(e, "tool_calls", None) or []):
            name = getattr(tc, "name", "") or ""
            if not name:
                continue
            tools[name] = tools.get(name, 0) + 1
            args = getattr(tc, "arguments", None) or {}
            if name in _WRITE_TOOLS:
                p = str(args.get("path", "") or "").strip()
                if p and p not in files:
                    files.append(p)
            if name == "finish":
                summary = str(args.get("summary", "") or "").strip()
                if summary:
                    result = summary
                # SEMANTIC memory, authored by the model itself at finish time:
                # a reflection in its own words + self-chosen recall tags.
                r = str(args.get("retro", "") or "").strip()
                if r:
                    retro = r
                raw_tags = str(args.get("tags", "") or "")
                if raw_tags:
                    tags = [t.strip().lower() for t in raw_tags.split(",")
                            if t.strip()][:10]
    if not tools and not result:
        return None
    ep = {
        "task": task,
        "mode": mode,
        "outcome": str(outcome or "open"),
        "tools": tools,
        "files": files[:12],
        "result": result[:400],
    }
    if retro:
        ep["retro"] = retro[:400]
    if tags:
        ep["tags"] = tags
    return ep


def record_episode(events, mode: str, outcome: str | None,
                   release: str = "") -> None:
    """Append the session digest to state/episodes.jsonl. Best-effort: memory
    failures never crash the agent."""
    try:
        ep = digest(events, mode, outcome)
        if not ep:
            return
        ep["time"] = time.time()
        if release:
            ep["release"] = release
        state = _state_dir()
        state.mkdir(parents=True, exist_ok=True)
        with (state / EPISODES_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps(ep, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _episodes_deduped() -> list[dict]:
    """All episodes, newest last, deduped by task text (a resumed-and-left session
    digests again on every exit - keep only the freshest telling)."""
    out: dict[str, dict] = {}
    for ep in _read_jsonl(_state_dir() / EPISODES_NAME):
        key = str(ep.get("task", "")).strip().lower() or f"@{len(out)}"
        out[key] = ep  # later rows overwrite earlier ones
    return list(out.values())


def _score(query_tokens: set[str], blob: str) -> int:
    return len(query_tokens & _tokens(blob))


def _episode_blob(ep: dict) -> str:
    return " ".join([str(ep.get("task", "")), str(ep.get("result", "")),
                     str(ep.get("retro", "")),
                     " ".join(ep.get("tags") or []),
                     " ".join(ep.get("files") or []),
                     " ".join((ep.get("tools") or {}).keys())])


def _render(ep: dict) -> str:
    bits = [f"  - [{ep.get('mode', '?')}/{ep.get('outcome', '?')}] {ep.get('task', '(no task)')}"]
    if ep.get("files"):
        bits.append(f"      files: {', '.join(ep['files'][:6])}")
    if ep.get("result"):
        bits.append(f"      result: {str(ep['result'])[:200]}")
    if ep.get("retro"):
        bits.append(f"      retro: {str(ep['retro'])[:200]}")
    if ep.get("tags"):
        bits.append(f"      tags: {', '.join(ep['tags'][:10])}")
    return "\n".join(bits)


def recall(query: str, limit: int = 3) -> str:
    """On-demand memory retrieval for the `recall` tool: deterministic keyword
    scoring over past EPISODES and recorded LESSONS. No embeddings, no LLM -
    auditable and gate-safe. Returns human-readable matches or an honest miss."""
    q = _tokens(query or "")
    if not q:
        return "recall: give me a few keywords (task words, file names, tool names)."
    scored = [(s, ep) for ep in _episodes_deduped()
              if (s := _score(q, _episode_blob(ep))) > 0]
    scored.sort(key=lambda x: -x[0])
    lines: list[str] = []
    if scored:
        lines.append(f"Past episodes matching '{query.strip()[:60]}':")
        lines.extend(_render(ep) for _, ep in scored[:limit])
    hits = [(s, rec) for rec in _read_jsonl(_state_dir() / LESSONS_NAME)
            if (s := _score(q, str(rec.get("lesson", "")))) > 0]
    hits.sort(key=lambda x: -x[0])
    if hits:
        lines.append("Related lessons:")
        seen: set[str] = set()
        for _, rec in hits:
            text = str(rec.get("lesson", "")).strip()
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            cat = str(rec.get("category", "")).strip()
            lines.append(f"  - [{cat}] {text[:200]}" if cat else f"  - {text[:200]}")
            if len(seen) >= limit:
                break
    if not lines:
        return (f"recall: nothing in memory matches '{query.strip()[:60]}'. The full "
                "session logs live under state/sessions/ if you want to grep deeper.")
    return "\n".join(lines)


def relevant(task: str, limit: int = 2, min_score: int = 2) -> str:
    """The most relevant past episodes for a NEW task, for auto-injection into the
    session's initial context. Deliberately strict (min_score) and small (limit):
    a weak match injected as context is noise, not memory."""
    q = _tokens(task or "")
    if not q:
        return ""
    scored = [(s, ep) for ep in _episodes_deduped()
              if (s := _score(q, _episode_blob(ep))) >= min_score]
    if not scored:
        return ""
    scored.sort(key=lambda x: -x[0])
    return "\n".join(_render(ep) for _, ep in scored[:limit])
