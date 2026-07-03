#!/usr/bin/env python3
"""Record an EVA improve session as asciicast v2 (runs INSIDE the container).

Spawns `python /eva/organism.py improve --allow-shell` under a real pty,
auto-types the demo task, answers approval prompts with "y", types "exit"
after the turn finishes, and writes /eva/workspace/improve.cast.

Run from the host (repo root):
  docker compose -f docker-compose.yml -f docker-compose.keyguard.yml \
    run -T --rm --entrypoint python -v ./scripts:/eva/scripts:ro \
    eva /eva/scripts/record_cast.py

The recording lands in ./data/workspace/improve.cast. Annotate milestones with
scripts/annotate_cast.py, preview with scripts/preview.html (serve the workspace
dir: py -m http.server), convert to GIF with ghcr.io/asciinema/agg.
"""
import fcntl
import json
import os
import pty
import select
import struct
import sys
import termios
import time

COLS, ROWS = 110, 32
TIMEOUT = 1500  # hard cap, seconds
CAST = "/eva/workspace/improve.cast"
TASK = ("Give yourself a soul: create soul.md with one sentence about who you are, "
        "protect it with a new ratchet check, run the tests and request promotion.")

pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"
    os.environ["COLUMNS"], os.environ["LINES"] = str(COLS), str(ROWS)
    os.execvp("python", ["python", "/eva/organism.py", "improve", "--allow-shell"])

fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

start = time.monotonic()
events = []
typed_task = False
finished_seen = False
typed_exit = False
seen = 0      # cumulative count of [y/N prompts observed
answered = 0  # how many we already answered
carry = ""    # tail overlap so prompts split across chunks still count
buf_since_input = ""


def log(msg):
    print(f"[recorder] {msg}", file=sys.stderr, flush=True)


def send(text, label):
    time.sleep(0.6)
    for ch in text:
        os.write(fd, ch.encode())
        time.sleep(0.02)
    os.write(fd, b"\r")
    log(f"sent {label}")


while True:
    now = time.monotonic() - start
    if now > TIMEOUT:
        log("timeout reached, killing child")
        os.kill(pid, 9)
        break
    r, _, _ = select.select([fd], [], [], 1.0)
    if not r:
        try:
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
        except ChildProcessError:
            break
        continue
    try:
        data = os.read(fd, 65536)
    except OSError:
        break
    if not data:
        break
    text = data.decode("utf-8", errors="replace")
    events.append([round(time.monotonic() - start, 4), "o", text])

    window = carry + text
    seen += window.count("[y/N")
    carry = window[-8:]
    buf_since_input += text

    if not typed_task and "\u276f you" in buf_since_input:
        send(TASK, "task")
        typed_task = True
        buf_since_input = ""
        continue
    if "finished" in buf_since_input:  # TUI prints "✓ finished" at turn end
        finished_seen = True
    while answered < seen:
        send("y", f"approval #{answered + 1}")
        answered += 1
        buf_since_input = ""
    if typed_task and not typed_exit and finished_seen and "\u276f you" in buf_since_input:
        send("exit", "exit")
        typed_exit = True
        buf_since_input = ""

try:
    os.waitpid(pid, 0)
except ChildProcessError:
    pass

header = {"version": 2, "width": COLS, "height": ROWS,
          "timestamp": int(time.time()),
          "title": "EVA - improve session (self-modification with gated promotion)",
          "env": {"TERM": "xterm-256color", "SHELL": "/bin/sh"}}
with open(CAST, "w", encoding="utf-8") as f:
    f.write(json.dumps(header) + "\n")
    for ev in events:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
log(f"wrote {CAST} with {len(events)} events")
