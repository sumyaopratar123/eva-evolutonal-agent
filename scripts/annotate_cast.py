#!/usr/bin/env python3
"""Annotate an asciicast v2 recording with milestone markers + framed banners.

Reads a raw cast, inserts at chosen event indices:
  - an asciicast marker event  -> visible on the asciinema-player timeline, jump with [ / ]
  - a framed ANSI banner       -> drawn into the terminal output
  - a dwell pause              -> playback lingers so the milestone can be read

Adjust MILESTONES (event index -> (tag, label)) for a new recording; list the
events with their text first, e.g.:
  py -c "import json;[print(i,e[0],json.loads(l)[2][:100]) ...]"

Usage:  py scripts/annotate_cast.py <in.cast> <out.cast> [milestones.json] [max_gap]

  milestones.json  optional: {"<event index>": ["TAG", "label"], ...} replaces the
                   built-in MILESTONES (so new demos need no edit here). May also be
                   {"milestones": {...}, "fast": [[start_idx, end_idx, scale], ...]} -
                   "fast" ranges play in time-lapse: inter-event gaps inside
                   [start, end) are divided by scale (output stays intact).
  max_gap          optional: cap the pause between consecutive events (seconds).
                   Long waits (downloads, installs) compress; output stays intact.
"""
import json
import sys

DWELL = 4.5  # seconds to linger after each milestone

# Event index in the RAW cast -> (tag, label). Tuned for the improve demo run.
MILESTONES = {
    9:  ("INPUT", "Task: give yourself a soul - soul.md + ratchet check + promotion"),
    14: ("STEP 1", "Candidate created: v001-candidate (clone of the active release)"),
    28: ("STEP 2", "soul.md written - EVA's one-sentence identity"),
    49: ("STEP 3", "Promotion requested - supervisor & kernel will gate it"),
    51: ("MILESTONE", "Turn finished: 87/87 ratchet checks pass (86 + new check_soul)"),
    83: ("GATE", "Supervisor gate passed - kernel makes the final decision"),
    98: ("PROMOTED", "EVA is now v002 - self-modification complete"),
}


def banner(tag: str, label: str) -> str:
    text = f" \u2605 {tag}: {label} "
    w = max(len(text) + 2, 60)
    pad = text.ljust(w - 2)
    c, r = "\x1b[1;38;5;213m", "\x1b[0m"
    return ("\r\n" + c + "\u256d" + "\u2500" * (w - 2) + "\u256e" + r + "\r\n"
            + c + "\u2502" + pad + "\u2502" + r + "\r\n"
            + c + "\u2570" + "\u2500" * (w - 2) + "\u256f" + r + "\r\n")


def main(src: str, dst: str, milestones=None, max_gap: float = 0.0,
         fast=None) -> None:
    ms = milestones if milestones is not None else MILESTONES
    lines = open(src, encoding="utf-8").read().splitlines()
    header, events = lines[0], [json.loads(l) for l in lines[1:]]
    if max_gap > 0 or fast:  # compress waits / time-lapse ranges, keep all output
        prev_raw, t_new = None, 0.0
        for i, e in enumerate(events):
            gap = 0.0 if prev_raw is None else max(e[0] - prev_raw, 0.0)
            if max_gap > 0:
                gap = min(gap, max_gap)
            for a, b, scale in (fast or []):
                if a <= i < b:
                    gap /= scale
                    break
            prev_raw, t_new = e[0], t_new + gap
            e[0] = round(t_new, 4)
    out, offset = [], 0.0
    for i, e in enumerate(events):
        t = round(e[0] + offset, 4)
        out.append([t, e[1], e[2]])
        if i in ms:
            tag, label = ms[i]
            out.append([round(t + 0.10, 4), "m", f"{tag}: {label}"])
            out.append([round(t + 0.15, 4), "o", banner(tag, label)])
            offset += DWELL
    with open(dst, "w", encoding="utf-8") as f:
        f.write(header + "\n")
        for e in out:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"annotated: {len(out)} events, {len(ms)} markers, "
          f"ends at {out[-1][0]:.1f}s -> {dst}")


if __name__ == "__main__":
    if not 3 <= len(sys.argv) <= 5:
        sys.exit(__doc__)
    milestones, fast = None, None
    if len(sys.argv) >= 4:
        raw = json.load(open(sys.argv[3], encoding="utf-8"))
        if "milestones" in raw:
            fast = raw.get("fast")
            raw = raw["milestones"]
        milestones = {int(k): tuple(v) for k, v in raw.items()}
    max_gap = float(sys.argv[4]) if len(sys.argv) == 5 else 0.0
    main(sys.argv[1], sys.argv[2], milestones, max_gap, fast)
