#!/usr/bin/env python3
"""Tool runtime: EVA's canonical tools and their sandboxed execution.

The model may only ASK to run a tool. EVA owns the decision, the gating, the
logging and the execution. This is where "the agent" actually touches the world.

The starting toolset is intentionally tiny and shell-centric (the cheap,
elementary tool): `shell` plus `finish`. Questions to the human go through plain
conversation (a no-tool reply ends the turn and the loop prompts the user), not a
dedicated tool. More tools are added by defining a canonical Tool here - adapters
render them automatically.
"""
from __future__ import annotations

import html
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from core import Tool, ToolCall, ToolObservation
from human import (ApprovalPolicy, HumanInterface, RISK_NONE, RISK_PROMOTE,
                   RISK_SHELL, RISK_WRITE)
import params
import self_model


# --------------------------------------------------------------------------- #
# Canonical tool schema (defined ONCE, provider-independent)
# --------------------------------------------------------------------------- #
SHELL_TOOL = Tool(
    name="shell",
    description="Run a shell command in the workspace. Prefer read-only commands "
                "(grep -n, sed -n, head, tail, cat, ls, find, diff) to inspect "
                "code cheaply; use writes/heredocs to edit files.",
    input_schema={
        "type": "object",
        "properties": {
            "cmd": {"type": "string"},
            "timeout": {"type": "integer"},
        },
        "required": ["cmd"],
    },
)

FINISH_TOOL_DEF = Tool(
    name="finish",
    description="End the current turn with a short summary of what was done. "
                "Optionally leave SEMANTIC memory for your future self: `retro` is a "
                "one-or-two-sentence reflection IN YOUR OWN WORDS (what this was really "
                "about, what mattered, what you'd do differently) and `tags` is a short "
                "comma-separated list of concepts/technologies for later recall. Both "
                "flow into your episodic memory - write them for the EVA who will "
                "search for this session weeks from now.",
    input_schema={
        "type": "object",
        "properties": {"summary": {"type": "string"},
                       "retro": {"type": "string"},
                       "tags": {"type": "string"}},
        "required": [],
    },
)

INSPECT_SELF_TOOL = Tool(
    name="inspect_self",
    description="Read your OWN self-model on demand - you are a self-evolving agent "
                "and are not preloaded with full docs. Returns your anatomy "
                "(file->role), skills (tools), ratchet-pinned capabilities, the "
                "per-mode security policy, the current sandbox (safe/free), your live "
                "model/provider backend, your tunable runtime parameters, or your evolution "
                "lineage (promotions/rollbacks from the release ledger). `topic` selects the "
                "slice: 'overview' (default), 'anatomy', 'skills', 'capabilities', 'policy', "
                "'sandbox', 'model', 'parameters', 'lineage', "
                "or the name of a file/capability/skill for detail (a filename also returns "
                "that module's docstring). Use it before editing yourself so you target the "
                "right file - and check 'lineage' before proposing an evolution, so you do "
                "not repeat one that was already promoted or rolled back.",
    input_schema={
        "type": "object",
        "properties": {"topic": {"type": "string"}},
        "required": [],
    },
)

WRITE_FILE_TOOL = Tool(
    name="write_file",
    description="Write a file with its FULL content - the reliable way to edit code. "
                "Prefer this over shell heredocs/sed for any multi-line change. In "
                "work mode it writes into the workspace; in improve/evolve it can "
                "also write into a *-candidate release (e.g. "
                "../runtime/releases/v002-candidate/adapters.py). It can NOT touch the "
                "active release, state/, or the kernel.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
)

READ_FILE_TOOL = Tool(
    name="read_file",
    description="Read a file (your own code under ../runtime/releases/, workspace "
                "files, or the kernel). Use this to SEE current content before "
                "editing - never ask the user for file contents. Prefer TARGETED "
                "reads: pass start_line/num_lines for a specific region (find it "
                "first with grep -n), and keep the default max_chars - whole-file "
                "reads of big files flood your context and push out earlier steps. "
                "Only raise max_chars when you truly need the entire file.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_chars": {"type": "integer"},
            "start_line": {"type": "integer",
                           "description": "1-based first line of a range read"},
            "num_lines": {"type": "integer",
                          "description": "how many lines to return from start_line"},
        },
        "required": ["path"],
    },
)

REPLACE_IN_FILE_TOOL = Tool(
    name="replace_in_file",
    description="Surgically replace an exact text block in a file (old -> new). The "
                "old text must occur EXACTLY ONCE - include enough surrounding lines "
                "to be unique. Best for editing existing code; same write permissions "
                "as write_file.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["path", "old", "new"],
    },
)

APPLY_PATCH_TOOL = Tool(
    name="apply_patch",
    description="Apply SEVERAL surgical edits to ONE file in a single ATOMIC step - the "
                "strong way to make a multi-part change to your own code. Pass `path` and "
                "`edits`: a list of {old, new} replacements. Each `old` must occur EXACTLY "
                "ONCE (include enough surrounding context); edits apply in order. All land "
                "or NONE do: if any `old` is missing/ambiguous - or the result would not be "
                "valid Python for a .py file - nothing is written. Same write permissions as "
                "write_file (workspace, or a *-candidate release in improve/evolve). Prefer "
                "this over many replace_in_file calls when changing several places at once.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"old": {"type": "string"},
                                   "new": {"type": "string"}},
                    "required": ["old", "new"],
                },
            },
        },
        "required": ["path", "edits"],
    },
)

FETCH_URL_TOOL = Tool(
    name="fetch_url",
    description="Fetch a web page (http/https) and return its READABLE text - your window "
                "to the internet for research. HTML is stripped to plain text; pass "
                "`max_chars` to cap the length (default 6000). Use it to read docs, pages, "
                "changelogs or plain-text APIs that you or the user reference.",
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer"},
        },
        "required": ["url"],
    },
)

WEB_SEARCH_TOOL = Tool(
    name="web_search",
    description="Search the web and return the top results (title + url) for a query - use "
                "it to FIND pages, then fetch_url to read one. Keyless and best-effort; if "
                "search is unavailable it says so.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
        },
        "required": ["query"],
    },
)

NOTE_EVOLUTION_NEED_TOOL = Tool(
    name="note_evolution_need",
    description="Record that a user need reveals a capability you LACK or can only do "
                "awkwardly, so it can grow into a real skill. Use judgement: do NOT log a "
                "one-off throwaway request you can just handle now; log a DURABLE need that "
                "is likely to RECUR. Give a STABLE short `signature` - a slug for the "
                "capability CLASS, e.g. 'pdf-text-extraction' - so repeats of the same need "
                "aggregate across sessions; only a need that recurs is proposed as an "
                "evolution. `need` describes it in one line; optional `detail`.",
    input_schema={
        "type": "object",
        "properties": {
            "need": {"type": "string"},
            "signature": {"type": "string"},
            "detail": {"type": "string"},
        },
        "required": ["need"],
    },
)

NOTE_LESSON_TOOL = Tool(
    name="note_lesson",
    description="Record a DURABLE insight in your persistent lessons journal so future "
                "sessions start with it: a failed approach and why, an environment quirk "
                "(e.g. a missing library class), a user preference, or something learned "
                "about your own code. Sessions die - lessons survive. Do NOT log routine "
                "progress or task details; log only what would help a future you avoid a "
                "mistake or a re-discovery. `category` is one of: approach | environment | "
                "user-preference | self.",
    input_schema={
        "type": "object",
        "properties": {
            "lesson": {"type": "string"},
            "category": {"type": "string"},
        },
        "required": ["lesson"],
    },
)

RECALL_TOOL = Tool(
    name="recall",
    description="Search your cross-session memory: past session EPISODES (task, files "
                "touched, outcome) and recorded LESSONS, matched by keywords. Use it "
                "when a task feels familiar ('have I done something like this before?'), "
                "before re-solving a problem, or to find which files an earlier session "
                "changed. Deterministic keyword search - give task words, file names or "
                "tool names as the query.",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)

MAKE_CANDIDATE_TOOL = Tool(
    name="make_candidate",
    description="Clone the ACTIVE release into a fresh *-candidate you can safely edit - the "
                "reliable first step of a self-change (no manual `cp -r`). Returns the "
                "candidate name/path; then edit it with apply_patch/write_file, verify with "
                "run_tests, and request_promotion. Only in improve/evolve.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": [],
    },
)

RUN_TESTS_TOOL = Tool(
    name="run_tests",
    description="Run a candidate release's own test suite (`tests.py --self`) and return "
                "pass/fail with the output - VERIFY a candidate before request_promotion. "
                "Pass the candidate name, e.g. 'v002-candidate'. Only in improve/evolve.",
    input_schema={
        "type": "object",
        "properties": {"candidate": {"type": "string"}},
        "required": ["candidate"],
    },
)

REQUEST_PROMOTION_TOOL = Tool(
    name="request_promotion",
    description="Ask EVA's supervisor + kernel to gate and (if it passes) promote a "
                "candidate release you have finished building. Only in improve/evolve "
                "mode, and only AFTER the candidate is complete. Pass the candidate "
                "name, e.g. 'v002-candidate'.",
    input_schema={
        "type": "object",
        "properties": {
            "candidate": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["candidate"],
    },
)

SET_PARAM_TOOL = Tool(
    name="set_param",
    description="Adjust one of YOUR OWN tunable runtime parameters (history_budget, "
                "max_steps, read_file_max_chars, ...) for THIS session - see "
                "inspect_self('parameters') for the full harness with live values, "
                "bounds and what each knob does. Use it when a default pinches: e.g. "
                "raise history_budget for a long refactor or max_steps before a big "
                "autonomous task. Changes are bounded, human-approved and reset on "
                "restart (a durable change is a human .env edit). Not in review mode.",
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "value": {"type": "integer"},
        },
        "required": ["name", "value"],
    },
)

CANONICAL_TOOLS: list[Tool] = [SHELL_TOOL, READ_FILE_TOOL, WRITE_FILE_TOOL,
                              REPLACE_IN_FILE_TOOL, APPLY_PATCH_TOOL, FETCH_URL_TOOL,
                              WEB_SEARCH_TOOL, INSPECT_SELF_TOOL, NOTE_EVOLUTION_NEED_TOOL,
                              NOTE_LESSON_TOOL, RECALL_TOOL, SET_PARAM_TOOL,
                              FINISH_TOOL_DEF]
# Extra tools available only when the agent may evolve the release: build a candidate,
# run its tests, and request promotion.
EVOLUTION_TOOLS: list[Tool] = CANONICAL_TOOLS + [MAKE_CANDIDATE_TOOL, RUN_TESTS_TOOL,
                                                 REQUEST_PROMOTION_TOOL]


_CANDIDATE_RE = re.compile(r"v[0-9][A-Za-z0-9._-]*-candidate")


def normalize_candidate(candidate: str) -> str:
    """Accept 'v002-candidate' or 'runtime/releases/v002-candidate' and return the
    bare, validated candidate name. Raises ValueError on anything else."""
    name = str(candidate or "").strip()
    if "/" in name:
        name = name.rstrip("/").split("/")[-1]
    if not _CANDIDATE_RE.fullmatch(name):
        raise ValueError("candidate must look like v002-candidate")
    return name


# --------------------------------------------------------------------------- #
# Read-only shell detection (auto-approve safe inspection commands)
# --------------------------------------------------------------------------- #
# Only commands that ACTUALLY exist in the runtime image (shell builtins + coreutils
# + grep/sed/find/diff) belong here. `file` was removed: it is a separate package not
# in the slim image, so auto-approving it produced exit=127 "file: not found" friction.
READ_ONLY_SHELL_CMDS = {
    "grep", "egrep", "fgrep", "sed", "find", "head", "tail", "cat", "wc",
    "sort", "uniq", "cut", "tr", "diff", "ls", "nl", "stat",
    "basename", "dirname", "realpath", "echo", "pwd", "true", "test",
}

_SHELL_WRITE_FLAGS = ("-i", "-delete", "-exec", "-execdir", "-ok", "-fprint", "-fprintf")

# Redirections that only DISCARD output (to /dev/null) or DUP a standard fd cannot write
# a real file, so they must not disqualify an otherwise read-only inspection command like
# `find . 2>/dev/null`. They are stripped before the (quote-aware) scan below; any OTHER
# > or < still forces approval. Stripping is not quote-aware, but it only affects the
# read-only DECISION - the real command is executed verbatim - and the scan still rejects
# any non-whitelisted command or real-file redirect in the chain, so no write slips through.
_DISCARD_REDIR_RE = re.compile(r"(?:[0-9]?>>?|&>)\s*/dev/null|[0-9]?>&[0-9]")


def is_read_only_shell(cmd: str) -> bool:
    """Conservative whitelist: only auto-approve commands that just read/search and
    cannot write. Read-only commands MAY be chained with ; && || and pipes (each
    segment's command must be whitelisted). Redirection to a real FILE, command
    substitution ($( `) and backgrounding (&) are rejected, as are known write flags -
    but DISCARDING output to /dev/null (e.g. `2>/dev/null`) or dup'ing a std fd (`2>&1`)
    is allowed, since it cannot write anything.

    The scan is QUOTE-AWARE: a separator or redirect character INSIDE a quoted argument
    (e.g. grep's `"a\\|b"` alternation, or a literal `>` in a search pattern) is part of
    that argument, not shell syntax, so read-only inspection commands are not wrongly
    rejected. Unbalanced quotes -> reject (fail safe)."""
    text = _DISCARD_REDIR_RE.sub(" ", str(cmd or ""))
    segments: list[str] = []
    cur: list[str] = []
    quote: "str | None" = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote is not None:
            cur.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            cur.append(c)
            i += 1
            continue
        if c in ("<", ">", "`"):
            return False  # redirection / command substitution can hide writes
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            return False
        if c == "&":
            if i + 1 < n and text[i + 1] == "&":  # && = separator
                segments.append("".join(cur)); cur = []; i += 2; continue
            return False  # a lone & = backgrounding
        if c == "|":
            step = 2 if (i + 1 < n and text[i + 1] == "|") else 1
            segments.append("".join(cur)); cur = []; i += step; continue
        if c == ";":
            segments.append("".join(cur)); cur = []; i += 1; continue
        cur.append(c)
        i += 1
    if quote is not None:
        return False  # unbalanced quote
    segments.append("".join(cur))

    for seg in segments:
        if not seg.strip():
            continue
        try:
            parts = shlex.split(seg)
        except ValueError:
            return False
        if not parts:
            continue
        head = os.path.basename(parts[0])
        if head not in READ_ONLY_SHELL_CMDS:
            return False
        if any(f in parts[1:] for f in _SHELL_WRITE_FLAGS):
            return False
    return True


# --------------------------------------------------------------------------- #
# Mode policy: the single, explicit, tested table of what each mode may do.
# --------------------------------------------------------------------------- #
# Security boundaries used to be implicit conditionals scattered across the tool
# handlers. They are now declared ONCE here so they are visible, auditable and
# pinned by a test. The runtime consults policy_for(mode); it never re-decides
# per-mode permissions inline. Tools that only READ (read_file, inspect_self,
# read-only shell) and the human/finish tools are universal and not gated here.
@dataclass(frozen=True)
class ModePolicy:
    write_workspace: bool      # may write_file/replace_in_file inside the workspace
    write_candidate: bool      # may write into a *-candidate release
    run_writing_shell: bool    # may run non-read-only shell (writes/side effects)
    request_promotion: bool    # may ask the supervisor/kernel to promote a candidate


MODE_POLICIES: dict[str, ModePolicy] = {
    #                       ws     cand   shell  promote
    "work":    ModePolicy(True,  False, True,  False),
    "review":  ModePolicy(False, False, False, False),
    "improve": ModePolicy(True,  True,  True,  True),
    "evolve":  ModePolicy(True,  True,  True,  True),
}

# Unknown/unexpected mode = the most restrictive policy (fail safe).
_LOCKED_POLICY = ModePolicy(False, False, False, False)


def policy_for(mode: str) -> ModePolicy:
    return MODE_POLICIES.get(mode, _LOCKED_POLICY)


# --------------------------------------------------------------------------- #
# Web helpers (generic assistant capability - stdlib only, no extra dependency)
# --------------------------------------------------------------------------- #
_HTTP_UA = "Mozilla/5.0 (compatible; EVA-agent/1.0)"


def _html_to_text(html_text: str) -> str:
    """Strip scripts/styles + tags, unescape entities and collapse blank lines - enough to
    READ article/doc content without a heavy HTML parser dependency. Pure function."""
    s = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html_text)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</(p|div|li|h[1-6]|tr|section|article)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    out, blank = [], False
    for ln in (line.strip() for line in s.splitlines()):
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def _parse_ddg(page: str, limit: int) -> list:
    """Parse the DuckDuckGo HTML endpoint: result links carry class="result__a" and an
    href that redirects via `uddg=<urlencoded target>`. Returns [(title, url), ...]."""
    results = []
    for m in re.finditer(r'result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
        href, title = m.group(1), _html_to_text(m.group(2)).strip()
        url = href
        mm = re.search(r"uddg=([^&]+)", href)
        if mm:
            url = urllib.parse.unquote(mm.group(1))
        elif href.startswith("//"):
            url = "https:" + href
        if title:
            results.append((title, url))
        if len(results) >= limit:
            break
    return results


# --------------------------------------------------------------------------- #
# Consistent "human declined" feedback
# --------------------------------------------------------------------------- #
def _declined(call: ToolCall, action: str, feedback: "str | None" = None) -> ToolObservation:
    """Turn a human 'no' into an observation the model can act on sensibly.

    A terse "rejected." made the model re-request the SAME action in a loop. This
    tells it plainly that the human made a deliberate choice, so the right move is
    to acknowledge it, NOT retry the identical call, and ask what the person would
    prefer (a different approach, a narrower step, or moving on) - or just answer
    without the tool. When the human typed a natural-language reply at the prompt,
    that reply is their real instruction and is surfaced here so it is not lost."""
    if feedback:
        return ToolObservation(
            call.id, call.name,
            f"The human did NOT approve this {action}; instead they replied: "
            f"{feedback!r}\nTreat this reply as their instruction for the next step. "
            f"Do NOT repeat the declined call - act on what they said (if it is "
            f"unclear, ask a brief question).",
        )
    return ToolObservation(
        call.id, call.name,
        f"The human declined this {action}. This was a deliberate choice, not an "
        f"error. Do NOT repeat the same request. Acknowledge the decision, briefly "
        f"say what you were trying to achieve, and ask how they would like to "
        f"proceed instead (a different approach, a smaller step, or skip it) - or "
        f"continue without this tool if you can.",
    )


# --------------------------------------------------------------------------- #
# The runtime
# --------------------------------------------------------------------------- #
class ShellToolRuntime:
    """Executes canonical tool calls. `shell` runs in the workspace; `finish`
    returns its summary."""

    def __init__(self, *, workspace: pathlib.Path, approval: ApprovalPolicy,
                 human: HumanInterface, releases: "pathlib.Path | None" = None,
                 state: "pathlib.Path | None" = None, max_output: "int | None" = None,
                 progress=None):
        self.workspace = pathlib.Path(workspace)
        self.approval = approval
        self.human = human
        self.releases = pathlib.Path(releases) if releases else None
        self.state = pathlib.Path(state) if state else None
        # None = read the LIVE params value per call (so set_param acts instantly);
        # an explicit int (tests) stays fixed.
        self._max_output = max_output
        # Optional presentation hook: called (with the tool name) right before a slow op
        # runs (AFTER approval), so the TUI can show a live elapsed timer. Never affects
        # behaviour and is only wired by the CLI.
        self._progress = progress

    def set_progress(self, callback) -> None:
        self._progress = callback

    @property
    def max_output(self) -> int:
        return self._max_output if self._max_output is not None else params.get("shell_max_output")

    def _running(self, name: str) -> None:
        if self._progress:
            try:
                self._progress(name)
            except Exception:
                pass

    def execute(self, call: ToolCall, mode: str) -> ToolObservation:
        handler = self._HANDLERS.get(call.name)
        if handler is None:
            return ToolObservation(call.id, call.name, f"Unknown tool: {call.name}")
        return handler(self, call, mode)

    def _finish(self, call: ToolCall, mode: str) -> ToolObservation:
        summary = str(call.arguments.get("summary", "") or "Finished.")
        return ToolObservation(call.id, call.name, summary)

    def _run_shell(self, call: ToolCall, mode: str) -> ToolObservation:
        cmd = str(call.arguments.get("cmd", "") or "")
        if not cmd:
            return ToolObservation(call.id, call.name, "Denied: empty shell command.")

        read_only = is_read_only_shell(cmd)
        if not read_only and not policy_for(mode).run_writing_shell:
            return ToolObservation(call.id, call.name,
                                   f"Denied: {mode} mode allows only read-only shell.")

        risk = RISK_NONE if read_only else RISK_SHELL
        # The command is shown compactly on the activity (▸) line; the FULL command is
        # passed as detail so the human can reveal it with 'f' before approving.
        decision = self.approval.approve(risk, "Approve shell?", detail=cmd)
        if not decision:
            return _declined(call, "shell command", decision.feedback)

        try:
            timeout = int(call.arguments.get("timeout", 60))
        except (TypeError, ValueError):
            timeout = 60

        self._running("shell")
        r = subprocess.run(
            cmd, cwd=str(self.workspace), shell=True,
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout,
        )
        out = (f"exit={r.returncode}\n"
               f"stdout:\n{r.stdout[-self.max_output:]}\n"
               f"stderr:\n{r.stderr[-self.max_output:]}")
        return ToolObservation(call.id, call.name, out)

    def _resolve_write_target(self, path: str, mode: str):
        """Resolve a write target and enforce where each mode may write (per the
        central MODE_POLICIES table). Returns (resolved_path, None) when allowed,
        else (None, denial_message)."""
        pol = policy_for(mode)
        raw = str(path or "").strip()
        if not raw:
            return None, "Denied: empty path."
        target = (self.workspace / raw).resolve()
        ws = self.workspace.resolve()
        if target == ws or str(target).startswith(str(ws) + os.sep):
            if not pol.write_workspace:
                return None, f"Denied: {mode} mode is read-only (no workspace writes)."
            return target, None
        if self.releases is not None:
            rel = self.releases.resolve()
            if str(target).startswith(str(rel) + os.sep):
                if not pol.write_candidate:
                    return None, "Denied: release writes only in improve/evolve mode."
                release_dir = target.relative_to(rel).parts[0]
                if not release_dir.endswith("-candidate"):
                    return None, ("Denied: may only write inside a *-candidate release, "
                                  "never the active or other releases.")
                return target, None
        return None, "Denied: path is outside the workspace and any candidate release."

    def _write_file(self, call: ToolCall, mode: str) -> ToolObservation:
        target, err = self._resolve_write_target(call.arguments.get("path", ""), mode)
        if err:
            return ToolObservation(call.id, call.name, err)
        content = call.arguments.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        detail = f"write {target}  ({len(content)} chars)"
        if content.strip():
            preview = content if len(content) <= 4000 else content[:4000] + "\n...(truncated)"
            detail += "\n\n" + preview
        decision = self.approval.approve(RISK_WRITE, "Approve file write?", detail=detail)
        if not decision:
            return _declined(call, "file write", decision.feedback)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if target.suffix == ".py":
            try:
                target.chmod(0o755)
            except Exception:
                pass
        return ToolObservation(call.id, call.name,
                               f"Wrote {len(content)} chars to {target}")

    def _read_file(self, call: ToolCall, mode: str) -> ToolObservation:
        raw = str(call.arguments.get("path", "")).strip()
        if not raw:
            return ToolObservation(call.id, call.name, "Denied: empty path.")
        target = (self.workspace / raw).resolve()
        root = self.workspace.parent.resolve()
        if not (target == root or str(target).startswith(str(root) + os.sep)):
            return ToolObservation(call.id, call.name,
                                   "Denied: path is outside the project.")
        if not target.is_file():
            return ToolObservation(call.id, call.name, f"Not a file: {target}")
        # Default kept deliberately SMALL relative to the history budget: one huge
        # read must not evict the rest of the working context (that caused re-read
        # loops). Range reads (start_line/num_lines) are the intended way to see a
        # specific region of a big file. Read LIVE from the params harness so a
        # set_param takes effect immediately.
        default_chars = params.get("read_file_max_chars")
        try:
            max_chars = int(call.arguments.get("max_chars", default_chars))
        except (TypeError, ValueError):
            max_chars = default_chars
        text = target.read_text(encoding="utf-8", errors="replace")
        try:
            start_line = int(call.arguments.get("start_line", 0) or 0)
        except (TypeError, ValueError):
            start_line = 0
        try:
            num_lines = int(call.arguments.get("num_lines", 0) or 0)
        except (TypeError, ValueError):
            num_lines = 0
        if start_line > 0 or num_lines > 0:
            lines = text.splitlines(keepends=True)
            total = len(lines)
            first = max(1, start_line or 1)
            last = min(total, first + num_lines - 1) if num_lines > 0 else total
            text = "".join(lines[first - 1:last])
            header = f"[lines {first}-{last} of {total}]\n"
            if len(text) <= max_chars:
                return ToolObservation(call.id, call.name, header + text)
            return ToolObservation(call.id, call.name,
                                   header + text[:max_chars] +
                                   f"\n...[truncated {len(text) - max_chars} chars; "
                                   "narrow the range or raise max_chars]")
        if len(text) <= max_chars:
            return ToolObservation(call.id, call.name, text)
        return ToolObservation(call.id, call.name,
                               text[:max_chars] +
                               f"\n...[truncated {len(text) - max_chars} chars; use "
                               "start_line/num_lines for a targeted range (grep -n "
                               "finds it) or raise max_chars]")

    def _replace_in_file(self, call: ToolCall, mode: str) -> ToolObservation:
        target, err = self._resolve_write_target(call.arguments.get("path", ""), mode)
        if err:
            return ToolObservation(call.id, call.name, err)
        if not target.is_file():
            return ToolObservation(call.id, call.name,
                                   f"Denied: file does not exist: {target}")
        old = call.arguments.get("old", "")
        new = call.arguments.get("new", "")
        if not isinstance(old, str) or not old:
            return ToolObservation(call.id, call.name,
                                   "Denied: 'old' must be a non-empty string.")
        if not isinstance(new, str):
            new = str(new)
        text = target.read_text(encoding="utf-8", errors="replace")
        n = text.count(old)
        if n == 0:
            return ToolObservation(call.id, call.name, "No match: 'old' text not found.")
        if n > 1:
            return ToolObservation(call.id, call.name,
                                   f"Ambiguous: 'old' occurs {n} times; add more "
                                   "surrounding context to make it unique.")
        detail = f"edit {target}\n  - {old.strip()[:400]}\n  + {new.strip()[:400]}"
        decision = self.approval.approve(RISK_WRITE, "Approve file edit?", detail=detail)
        if not decision:
            return _declined(call, "file edit", decision.feedback)
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        if target.suffix == ".py":
            try:
                target.chmod(0o755)
            except Exception:
                pass
        return ToolObservation(call.id, call.name, f"Replaced 1 occurrence in {target}")

    def _apply_patch(self, call: ToolCall, mode: str) -> ToolObservation:
        # Several surgical edits to ONE file, applied atomically: build the new text in
        # memory, and only write once EVERY edit matched uniquely AND (for .py) the
        # result still compiles. A self-edit that would corrupt EVA's own code is
        # refused before it touches disk.
        target, err = self._resolve_write_target(call.arguments.get("path", ""), mode)
        if err:
            return ToolObservation(call.id, call.name, err)
        if not target.is_file():
            return ToolObservation(call.id, call.name,
                                   f"Denied: file does not exist: {target}")
        edits = call.arguments.get("edits")
        if not isinstance(edits, list) or not edits:
            return ToolObservation(call.id, call.name,
                                   "Denied: 'edits' must be a non-empty list of {old, new}.")
        working = target.read_text(encoding="utf-8", errors="replace")
        previews = []
        for k, ed in enumerate(edits, 1):
            if not isinstance(ed, dict):
                return ToolObservation(call.id, call.name,
                                       f"Denied: edit #{k} is not an {{old, new}} object.")
            old = ed.get("old", "")
            new = ed.get("new", "")
            if not isinstance(old, str) or not old:
                return ToolObservation(call.id, call.name,
                                       f"Denied: edit #{k} 'old' must be a non-empty string.")
            if not isinstance(new, str):
                new = str(new)
            n = working.count(old)
            if n == 0:
                return ToolObservation(call.id, call.name,
                                       f"No match for edit #{k}: 'old' text not found "
                                       "(nothing written).")
            if n > 1:
                return ToolObservation(call.id, call.name,
                                       f"Ambiguous edit #{k}: 'old' occurs {n} times; add "
                                       "more surrounding context (nothing written).")
            working = working.replace(old, new, 1)
            previews.append(f"  #{k} - {old.strip()[:120]}\n      + {new.strip()[:120]}")
        if target.suffix == ".py":
            try:
                compile(working, str(target), "exec")
            except SyntaxError as exc:
                return ToolObservation(call.id, call.name,
                                       f"Denied: result is not valid Python ({exc.msg} at "
                                       f"line {exc.lineno}); nothing written.")
        detail = f"apply_patch {target}  ({len(edits)} edits)\n" + "\n".join(previews)
        decision = self.approval.approve(RISK_WRITE, "Approve file patch?", detail=detail)
        if not decision:
            return _declined(call, "file patch", decision.feedback)
        target.write_text(working, encoding="utf-8")
        if target.suffix == ".py":
            try:
                target.chmod(0o755)
            except Exception:
                pass
        return ToolObservation(call.id, call.name,
                               f"Applied {len(edits)} edits to {target}")

    def _fetch_url(self, call: ToolCall, mode: str) -> ToolObservation:
        # Generic research capability: read a web page as text. Network egress is allowed
        # by the sandbox (same channel the model uses); restrict egress at the container
        # level for stricter isolation.
        url = str(call.arguments.get("url", "") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return ToolObservation(call.id, call.name,
                                   "Denied: url must start with http:// or https://")
        default_chars = params.get("fetch_url_max_chars")
        try:
            max_chars = int(call.arguments.get("max_chars", default_chars))
        except (TypeError, ValueError):
            max_chars = default_chars
        self._running("fetch_url")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _HTTP_UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                ctype = r.headers.get("Content-Type", "")
                raw = r.read(2_000_000)  # cap the download at ~2 MB
        except Exception as exc:
            return ToolObservation(call.id, call.name, f"fetch_url error: {exc}")
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        body = raw.decode(charset, errors="replace")
        is_html = "html" in ctype.lower() or body.lstrip()[:1] == "<"
        text = (_html_to_text(body) if is_html else body).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars; raise max_chars]"
        return ToolObservation(call.id, call.name, f"[{url}]\n{text}")

    def _web_search(self, call: ToolCall, mode: str) -> ToolObservation:
        query = str(call.arguments.get("query", "") or "").strip()
        if not query:
            return ToolObservation(call.id, call.name, "Denied: empty query.")
        try:
            max_results = int(call.arguments.get("max_results", 5))
        except (TypeError, ValueError):
            max_results = 5
        max_results = max(1, min(max_results, 10))
        self._running("web_search")
        try:
            data = urllib.parse.urlencode({"q": query}).encode()
            req = urllib.request.Request("https://html.duckduckgo.com/html/",
                                         data=data, headers={"User-Agent": _HTTP_UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                page = r.read(1_000_000).decode("utf-8", errors="replace")
        except Exception as exc:
            return ToolObservation(call.id, call.name, f"web_search unavailable: {exc}")
        results = _parse_ddg(page, max_results)
        if not results:
            return ToolObservation(call.id, call.name,
                                   f"No results for: {query} (search may be rate-limited).")
        lines = [f"Results for: {query}"]
        for i, (title, url) in enumerate(results, 1):
            lines.append(f"{i}. {title}\n   {url}")
        return ToolObservation(call.id, call.name, "\n".join(lines))

    def _make_candidate(self, call: ToolCall, mode: str) -> ToolObservation:
        if not policy_for(mode).write_candidate:
            return ToolObservation(call.id, call.name,
                                   "Denied: make_candidate is only for improve/evolve.")
        if self.releases is None:
            return ToolObservation(call.id, call.name,
                                   "Denied: no releases directory configured.")
        rel = self.releases.resolve()
        try:
            active = (rel.parent / "CURRENT").read_text(encoding="utf-8").strip()
            active = active.rstrip("/").split("/")[-1]
        except Exception:
            return ToolObservation(call.id, call.name,
                                   "Cannot determine the active release (missing CURRENT).")
        src = rel / active
        if not src.is_dir():
            return ToolObservation(call.id, call.name,
                                   f"Active release not found: {active}")
        name = str(call.arguments.get("name", "") or f"{active}-candidate").strip()
        try:
            name = normalize_candidate(name)
        except ValueError as exc:
            return ToolObservation(call.id, call.name, f"Denied: {exc}")
        dst = rel / name
        if dst.exists():
            return ToolObservation(call.id, call.name,
                                   f"Candidate already exists: {name} (edit it, or pick another name).")
        decision = self.approval.approve(RISK_WRITE, "Approve create candidate?",
                                         detail=f"clone {active} -> {name}")
        if not decision:
            return _declined(call, "candidate-release creation", decision.feedback)
        try:
            shutil.copytree(src, dst,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        except Exception as exc:
            return ToolObservation(call.id, call.name, f"make_candidate error: {exc}")
        return ToolObservation(call.id, call.name,
                               f"Created candidate {name} (from {active}) at "
                               f"../runtime/releases/{name}. Edit it, run_tests, then request_promotion.")

    def _run_tests(self, call: ToolCall, mode: str) -> ToolObservation:
        if not policy_for(mode).write_candidate:
            return ToolObservation(call.id, call.name,
                                   "Denied: run_tests is only for improve/evolve.")
        if self.releases is None:
            return ToolObservation(call.id, call.name,
                                   "Denied: no releases directory configured.")
        try:
            name = normalize_candidate(call.arguments.get("candidate", ""))
        except ValueError as exc:
            return ToolObservation(call.id, call.name, f"Denied: {exc}")
        tests = self.releases.resolve() / name / "tests.py"
        if not tests.is_file():
            return ToolObservation(call.id, call.name, f"No tests.py in {name}")
        self._running("run_tests")
        try:
            r = subprocess.run([sys.executable, str(tests), "--self"],
                               cwd=str(tests.parent), stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=240)
        except Exception as exc:
            return ToolObservation(call.id, call.name, f"run_tests error: {exc}")
        tail = (r.stdout or "")[-self.max_output:]
        status = "PASS" if r.returncode == 0 else "FAIL"
        return ToolObservation(call.id, call.name,
                               f"tests {status} (exit={r.returncode})\n{tail}")

    def _note_evolution_need(self, call: ToolCall, mode: str) -> ToolObservation:
        # Presentation/echo only: agent.py owns the friction backlog and the recurrence
        # threshold. This just acknowledges the note (and its stable signature) in the
        # conversation; whether it becomes a skill proposal depends on RECURRENCE, decided
        # in agent.py - a one-off note never triggers an evolution on its own.
        need = str(call.arguments.get("need", "") or "").strip()
        if not need:
            return ToolObservation(call.id, call.name,
                                   "Denied: describe the capability need in `need`.")
        sig = str(call.arguments.get("signature", "") or "").strip()
        tag = f" [{sig}]" if sig else ""
        return ToolObservation(call.id, call.name,
                               f"Noted a possible evolution need{tag}: {need}. A one-off "
                               "is fine to just handle now; this is proposed as a real "
                               "skill only if the SAME need recurs.")

    def _note_lesson(self, call: ToolCall, mode: str) -> ToolObservation:
        # Presentation/echo only, like note_evolution_need: agent.py owns the lessons
        # journal (state/lessons.jsonl) via its tool-call hook. Lessons are EVA's
        # episodic memory across sessions - the store must outlive any one release
        # conversation, so it lives in state/, not in the session log.
        lesson = str(call.arguments.get("lesson", "") or "").strip()
        if not lesson:
            return ToolObservation(call.id, call.name,
                                   "Denied: describe the insight in `lesson`.")
        cat = str(call.arguments.get("category", "") or "").strip()
        tag = f" [{cat}]" if cat else ""
        return ToolObservation(call.id, call.name,
                               f"Lesson recorded{tag}: {lesson[:200]}. It will be "
                               "surfaced to future sessions.")

    def _recall(self, call: ToolCall, mode: str) -> ToolObservation:
        # Read-only cross-session memory retrieval, available in every mode.
        # memory.py owns the scoring; failures degrade to an honest message.
        query = str(call.arguments.get("query", "") or "").strip()
        try:
            import memory
            return ToolObservation(call.id, call.name, memory.recall(query))
        except Exception as exc:
            return ToolObservation(call.id, call.name,
                                   f"recall unavailable: {type(exc).__name__}: {exc}")

    def _inspect_self(self, call: ToolCall, mode: str) -> ToolObservation:
        # Read-only self-knowledge, available in every mode. EVA queries its own
        # anatomy/skills/capabilities on demand instead of being preloaded with them.
        topic = str(call.arguments.get("topic", "") or "overview")
        try:
            text = self_model.lookup(topic)
        except Exception as exc:  # never let introspection crash a turn
            text = f"inspect_self failed: {type(exc).__name__}: {exc}"
        return ToolObservation(call.id, call.name, text)

    def _set_param(self, call: ToolCall, mode: str) -> ToolObservation:
        # Tuning a runtime knob CHANGES behaviour, so it follows the write policy:
        # read-only review may not tune; work/improve/evolve may, with approval.
        if not policy_for(mode).write_workspace:
            return ToolObservation(call.id, call.name,
                                   f"Denied: {mode} mode is read-only and may not "
                                   "tune runtime parameters.")
        name = str(call.arguments.get("name", "") or "").strip()
        value = call.arguments.get("value")
        p = params.REGISTRY.get(name)
        detail = (f"set_param {name} = {value}"
                  + (f"  (now {params.get(name)}, bounds [{p.lo}, {p.hi}]; {p.desc})"
                     if p else ""))
        decision = self.approval.approve(RISK_WRITE, "Approve parameter change?",
                                         detail=detail)
        if not decision:
            return _declined(call, "parameter change", decision.feedback)
        try:
            msg = params.set_value(name, value)
        except ValueError as e:
            return ToolObservation(call.id, call.name, f"Denied: {e}")
        return ToolObservation(call.id, call.name, msg)

    def _request_promotion(self, call: ToolCall, mode: str) -> ToolObservation:
        if not policy_for(mode).request_promotion:
            return ToolObservation(call.id, call.name,
                                   "Denied: promotion only in improve/evolve mode.")
        if self.releases is None or self.state is None:
            return ToolObservation(call.id, call.name,
                                   "Denied: promotion is not available in this context.")
        try:
            name = normalize_candidate(call.arguments.get("candidate", ""))
        except ValueError as e:
            return ToolObservation(call.id, call.name, f"Denied: {e}")

        dest = self.releases / name
        if not dest.exists():
            return ToolObservation(call.id, call.name,
                                   f"Denied: candidate {name} does not exist under "
                                   "runtime/releases/.")

        reason = str(call.arguments.get("reason", "") or "")
        decision = self.approval.approve(RISK_PROMOTE, "Approve promotion request?",
                                         detail=f"promote {name}")
        if not decision:
            return _declined(call, "promotion request", decision.feedback)

        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "promotion_request.json").write_text(json.dumps({
            "candidate": "runtime/releases/" + name,
            "reason": reason,
            "requested_by": "agent",
            "time": time.time(),
        }, indent=2), encoding="utf-8")
        return ToolObservation(call.id, call.name,
                               f"Promotion requested for {name}. Finish now; the "
                               "supervisor and kernel will gate it.")

    # Single dispatch table: tool name -> handler. Every handler takes (call, mode)
    # so permission checks stay a per-handler concern (via policy_for), never
    # re-decided in the dispatcher. Defined after the handlers so the methods exist.
    _HANDLERS = {
        "finish": _finish,
        "shell": _run_shell,
        "read_file": _read_file,
        "write_file": _write_file,
        "replace_in_file": _replace_in_file,
        "apply_patch": _apply_patch,
        "fetch_url": _fetch_url,
        "web_search": _web_search,
        "make_candidate": _make_candidate,
        "run_tests": _run_tests,
        "note_evolution_need": _note_evolution_need,
        "note_lesson": _note_lesson,
        "recall": _recall,
        "inspect_self": _inspect_self,
        "set_param": _set_param,
        "request_promotion": _request_promotion,
    }
