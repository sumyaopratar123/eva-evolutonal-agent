#!/usr/bin/env python3
import ast
import difflib
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time


ROOT = pathlib.Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
RELEASES = RUNTIME / "releases"
CURRENT = RUNTIME / "CURRENT"
LAST_GOOD = RUNTIME / "LAST_GOOD"
STATE = ROOT / "state"
WORKSPACE = ROOT / "workspace"
PROMOTION = STATE / "promotion_request.json"
KERNEL_LOG = STATE / "kernel_history.jsonl"
# Append-only release ledger: one row per promotion/rollback, so the lineage
# (parent -> release, why, when) is auditable and rollback can step back MORE than
# one level instead of only to a single LAST_GOOD pointer.
LEDGER = STATE / "release_ledger.jsonl"

# Evolvable system-dependency manifest: one apt package name per line (comments with
# '#'). The FREE sandbox is ephemeral (--rm), so apt installs vanish between sessions;
# skills with native deps (e.g. a browser engine) would silently break next run. EVA
# appends the packages a skill needs here (state/ persists), and the kernel re-
# provisions them at every FREE start. With the apt cache bind-mounted (compose free
# override), re-installs are seconds, not downloads.
SYSTEM_DEPS = STATE / "system-deps.txt"

# Evolution lock: a single-writer guard for the SHARED release store (runtime/
# releases, CURRENT, LAST_GOOD, ledger). Only evolution-capable paths take it
# (improve, evolve, promotion, rollback); work/review run lock-free so parallel
# WORK sessions stay possible while self-evolution stays serialized. Cross-
# container safe: separate `docker compose run` containers don't share a PID
# namespace, so liveness is by TIMESTAMP (a live holder heartbeats the lock; a
# crashed holder's timestamp goes stale and the lock becomes reclaimable).
LOCK = STATE / "evolution.lock"
LOCK_TTL = 90

# This file is the tiny non-evolving kernel. It seeds v001, starts the active
# release, performs final promotion checks, and can roll back.
# The evolving organism lives in runtime/releases/<version>/.
#
# The initial genome is NOT embedded as string constants. It lives as normal,
# reviewable files under seed/v001/ and is copied into the runtime on first
# start, after an integrity check against the hashes pinned in its manifest.
SEED = ROOT / "seed" / "v001"


def log(kind, data):
    STATE.mkdir(exist_ok=True)
    with KERNEL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.time(), "kind": kind, "data": data}, ensure_ascii=False) + "\n")


def ledger_append(entry):
    """Append one row to the release ledger (the audit trail of lineage)."""
    STATE.mkdir(exist_ok=True)
    rec = {"time": time.time()}
    rec.update(entry)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ledger_rows():
    if not LEDGER.exists():
        return []
    rows = []
    for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def parent_of(release):
    """The release `release` was promoted FROM, per the ledger (latest matching
    promotion). None if it was never promoted (e.g. the seed v001) - that is where
    multi-level rollback bottoms out."""
    parent = None
    for e in ledger_rows():
        if e.get("kind") == "promoted" and e.get("release") == release:
            parent = e.get("parent")
    return parent


# --------------------------------------------------------------------------- #
# Release traceability (`changes`): what changed between releases, for humans
# --------------------------------------------------------------------------- #
def _release_file_hashes(path):
    """Relative filename -> content hash for every file in a release dir.
    CRLF-normalized so a Windows checkout never reports phantom diffs."""
    out = {}
    for p in sorted(path.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(path).as_posix()
        if rel.startswith((".", "__pycache__/")) or "/__pycache__/" in rel:
            continue
        data = p.read_bytes().replace(b"\r\n", b"\n")
        out[rel] = hashlib.sha256(data).hexdigest()
    return out


def _release_checks(path):
    """The ratchet-check names a release's tests.py declares (feature proxy:
    every promotion that adds behavior pins it with a new check_*)."""
    tests = path / "tests.py"
    if not tests.exists():
        return set()
    src = tests.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"^def (check_[A-Za-z0-9_]+)\s*\(", src, re.M))


def diff_releases(parent_path, release_path):
    """Structured what-changed summary between two release dirs: file-level
    added/removed/modified plus the check-name delta from tests.py."""
    a, b = _release_file_hashes(parent_path), _release_file_hashes(release_path)
    ca, cb = _release_checks(parent_path), _release_checks(release_path)
    return {
        "files_added": sorted(set(b) - set(a)),
        "files_removed": sorted(set(a) - set(b)),
        "files_modified": sorted(f for f in set(a) & set(b) if a[f] != b[f]),
        "checks_added": sorted(cb - ca),
        "checks_removed": sorted(ca - cb),
        "checks_total": len(cb),
    }


def _read_norm_lines(path):
    """A file's lines with CRLF normalized (matches _release_file_hashes), so
    per-file diffs never show phantom line-ending changes."""
    try:
        return path.read_bytes().replace(b"\r\n", b"\n").decode(
            "utf-8", errors="replace").splitlines()
    except Exception:
        return []


def _file_line_stats(parent_path, release_path, name):
    """(+added, -removed) line counts for one modified file, or None."""
    try:
        a = _read_norm_lines(parent_path / name)
        b = _read_norm_lines(release_path / name)
        add = rem = 0
        for line in difflib.unified_diff(a, b, lineterm="", n=0):
            if line.startswith("+") and not line.startswith("+++"):
                add += 1
            elif line.startswith("-") and not line.startswith("---"):
                rem += 1
        return add, rem
    except Exception:
        return None


def _print_file_diffs(parent_path, release_path, diff, only=None):
    """Full unified diffs (parent -> release) for the modified/added/removed
    files; `only` narrows to one filename. Needs both release dirs on disk."""
    names = (diff.get("files_modified") or []) + (diff.get("files_added") or []) \
        + (diff.get("files_removed") or [])
    if only:
        if only not in names:
            print(f"\n  (no change in '{only}' - changed files: " + ", ".join(names) + ")")
            return
        names = [only]
    for name in names:
        a = _read_norm_lines(parent_path / name)
        b = _read_norm_lines(release_path / name)
        lines = list(difflib.unified_diff(
            a, b, fromfile=f"{parent_path.name}/{name}",
            tofile=f"{release_path.name}/{name}", lineterm=""))
        if lines:
            print()
            print("\n".join(lines))


def _print_promotion(row, diff_target=None):
    """One human-readable block per promotion: who begat whom, why, and what
    changed. Prefers a live dir-diff; falls back to the summary stored in the
    ledger row, so history survives even if old release dirs get pruned.
    `diff_target` ('' = all files, name = one file) adds full unified diffs."""
    rel = (row.get("release") or "?").rsplit("/", 1)[-1]
    par = (row.get("parent") or "?").rsplit("/", 1)[-1]
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row.get("time", 0)))
    print(f"\n{rel}  (from {par})  \u00b7  {when}")
    reason = (row.get("reason") or "").strip()
    if reason:
        print(textwrap.indent(textwrap.fill(reason, width=76), "  "))

    diff = None
    rel_p, par_p = RELEASES / rel, RELEASES / par
    live = rel_p.is_dir() and par_p.is_dir()
    if live:
        diff = diff_releases(par_p, rel_p)
    elif row.get("diff"):
        diff = row["diff"]
    if not diff:
        print("  (no diff available: release dirs pruned and no summary in ledger)")
        return
    for label, key in (("added", "files_added"), ("modified", "files_modified"),
                       ("removed", "files_removed")):
        names = diff.get(key) or []
        if names:
            if live and key == "files_modified":
                # Enrich with per-file line stats while both dirs still exist.
                parts = []
                for n in names:
                    st = _file_line_stats(par_p, rel_p, n)
                    parts.append(f"{n} (+{st[0]}/-{st[1]})" if st else n)
                names = parts
            print(f"  files {label:8s}: " + ", ".join(names))
    for label, key in (("new checks", "checks_added"), ("dropped checks", "checks_removed")):
        names = diff.get(key) or []
        if names:
            print(f"  {label:14s}: " + ", ".join(names))
    total = diff.get("checks_total")
    if total:
        print(f"  ratchet       : {total} checks")
    if diff_target is not None:
        if live:
            _print_file_diffs(par_p, rel_p, diff, only=diff_target or None)
        else:
            print("  (--diff needs both release dirs on disk; one was pruned)")


def changes(show_all=False, diff_target=None):
    """`changes` answers the user's question after a promotion or rollback:
    WHAT is different now? It walks the ledger (newest first by default) and
    prints, per promotion, the reason plus the file- and check-level delta.
    `--diff [file]` adds full unified diffs for the LATEST promotion."""
    ensure_seed()
    rows = ledger_rows()
    promos = [r for r in rows if r.get("kind") == "promoted"]
    if not promos:
        print("No promotions yet - the seed release is unchanged.")
        print("Lineage starts once the first candidate is promoted (improve/evolve).")
        return
    current = CURRENT.read_text(encoding="utf-8").strip()
    print("Current release:", current.rsplit("/", 1)[-1])
    shown = promos if show_all else promos[-1:]
    for i, row in enumerate(reversed(shown)):
        # Full diffs only for the newest promotion - older ones stay summary-only.
        _print_promotion(row, diff_target=diff_target if i == 0 else None)
    if show_all:
        for r in rows:
            if r.get("kind") == "rollback":
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("time", 0)))
                print(f"\nrollback  {r.get('from', '?').rsplit('/', 1)[-1]} -> "
                      f"{r.get('to', '?').rsplit('/', 1)[-1]}  \u00b7  {when}")
    hints = []
    if not show_all:
        hints.append("`changes --all` for the full lineage")
    if diff_target is None:
        hints.append("`changes --diff [file]` for the full per-file diff")
    if hints:
        print("\n(use " + " \u00b7 ".join(hints) + ")")


# --------------------------------------------------------------------------- #
# Evolution lock (single-writer guard for the shared release store)
# --------------------------------------------------------------------------- #
_lock_stop = None


def _lock_info():
    try:
        return json.loads(LOCK.read_text(encoding="utf-8"))
    except Exception:
        return None


def _lock_heartbeat(stop):
    # Refresh the lock timestamp so a LIVE holder is never mistaken for crashed,
    # while a crashed holder's timestamp ages past LOCK_TTL and becomes reclaimable.
    while not stop.wait(LOCK_TTL / 3.0):
        try:
            info = _lock_info()
            if not info or info.get("pid") != os.getpid():
                return  # someone reclaimed it; stop touching it
            info["ts"] = time.time()
            LOCK.write_text(json.dumps(info), encoding="utf-8")
        except Exception:
            return


def acquire_evolution_lock(mode, force=False):
    """Acquire the single-writer evolution lock. Returns True on success. Refuses
    (returns False, prints who holds it) if a live holder exists; reclaims a stale
    lock (crashed holder, ts older than LOCK_TTL) or any lock when force=True."""
    global _lock_stop
    STATE.mkdir(exist_ok=True)
    refusal = None
    for _ in range(2):  # at most one reclaim-and-retry
        try:
            fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"mode": mode, "ts": time.time(), "pid": os.getpid()}, f)
            _lock_stop = threading.Event()
            threading.Thread(target=_lock_heartbeat, args=(_lock_stop,),
                             daemon=True).start()
            return True
        except FileExistsError:
            info = _lock_info() or {}
            age = time.time() - float(info.get("ts") or 0)
            if force or age > LOCK_TTL:
                try:
                    LOCK.unlink()
                except FileNotFoundError:
                    pass
                log("lock_reclaimed", {"stale_age": round(age, 1),
                                       "forced": bool(force), "was": info})
                continue
            refusal = (f"Evolution lock is held (mode={info.get('mode')}, "
                       f"{round(age, 1)}s ago). Refusing to run a second "
                       f"evolution/promotion concurrently. If you are sure the "
                       f"holder is dead: `python organism.py unlock`.")
            break
    if refusal:
        print(refusal)
    return False


def release_evolution_lock():
    global _lock_stop
    if _lock_stop is not None:
        _lock_stop.set()
        _lock_stop = None
    try:
        info = _lock_info()
        if info and info.get("pid") == os.getpid():
            LOCK.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def rehash_release_manifest(path, version_name):
    """Make a promoted release's manifest HONEST. Evolutions edit release files but do
    not always rehash manifest.json, which would otherwise keep stale hashes (and a
    stale version) - the manifest would lie about its own content. At promotion the
    kernel recomputes the pinned hashes from the actual files, stamps the live version,
    and drops stray __pycache__ so the live bundle is clean and auditable."""
    try:
        pycache = path / "__pycache__"
        if pycache.exists():
            shutil.rmtree(pycache, ignore_errors=True)
        mf = path / "manifest.json"
        man = json.loads(mf.read_text(encoding="utf-8"))
        man["version"] = version_name
        hashes = man.get("hashes") or {}
        for name in list(hashes.keys()):
            f = path / name
            if f.exists():
                hashes[name] = _normalized_sha256(f)
        man["hashes"] = hashes
        mf.write_text(json.dumps(man, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log("manifest_rehashed", {"release": "runtime/releases/" + path.name,
                                  "version": version_name})
    except Exception as exc:
        log("manifest_rehash_failed", {"release": str(path), "error": str(exc)})


def _normalized_sha256(path):
    # Normalize CRLF -> LF so the integrity check is stable across platforms and
    # git autocrlf checkouts (the genome is plain text).
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def verify_seed(src):
    # Reproducible, tamper-evident bootstrap: the seed manifest pins a sha256 of
    # every genome file. Refuse to materialize a corrupt or altered embryo.
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    hashes = manifest.get("hashes") or {}
    if not hashes:
        raise RuntimeError("Seed manifest has no hashes; cannot verify integrity.")

    for name, expected in hashes.items():
        f = src / name
        if not f.exists():
            raise RuntimeError(f"Seed missing file: {name}")
        if _normalized_sha256(f) != expected:
            raise RuntimeError(f"Seed integrity check failed for {name}")

    return manifest


def ensure_seed():
    RELEASES.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(exist_ok=True)
    WORKSPACE.mkdir(exist_ok=True)

    v1 = RELEASES / "v001"
    if not v1.exists():
        if not SEED.exists():
            raise RuntimeError(f"Seed genome not found at {SEED}")
        verify_seed(SEED)
        shutil.copytree(SEED, v1)
        log("seeded", {"from": str(SEED), "to": "runtime/releases/v001"})
        for p in v1.glob("*.py"):
            p.chmod(0o755)

    if not CURRENT.exists():
        CURRENT.write_text("runtime/releases/v001", encoding="utf-8")

    objective = WORKSPACE / "OBJECTIVE.md"
    if not objective.exists():
        objective.write_text(
            "# Objective\n\n"
            "Build a useful work agent that can safely evolve its supervisor, agent, tests, and prompts.\n",
            encoding="utf-8",
        )


def safe_release_rel(rel):
    rel = str(rel).strip()

    if not rel.startswith("runtime/releases/"):
        raise RuntimeError("Release path must start with runtime/releases/")

    p = (ROOT / rel).resolve()
    base = RELEASES.resolve()

    if p != base and not str(p).startswith(str(base) + os.sep):
        raise RuntimeError("Release path escapes runtime/releases")

    return rel, p


def current_release():
    ensure_seed()
    rel = CURRENT.read_text(encoding="utf-8").strip()
    return safe_release_rel(rel)


def run(cmd, release_path=None, timeout=180):
    # Quiet on success (one summary line) so gate runs don't flood the terminal and
    # push the conversation out of scrollback; FULL output on failure (that's when
    # it matters) or with EVA_GATE_VERBOSE=1.
    env = os.environ.copy()
    env["ORGANISM_ROOT"] = str(ROOT)

    if release_path is not None:
        env["ACTIVE_RELEASE"] = str(release_path)

    print("$", " ".join(map(str, cmd)))

    r = subprocess.run(
        [str(x) for x in cmd],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    ok = r.returncode == 0
    verbose = os.environ.get("EVA_GATE_VERBOSE", "").strip().lower() in ("1", "true", "yes", "on")
    if ok and not verbose:
        tail = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()]
        print("  ok" + (f" \u00b7 {tail[-1].strip()}" if tail else ""))
    else:
        if r.stdout:
            print(r.stdout)
        if r.stderr:
            print(r.stderr)

    return ok


def kernel_capability_floor(path):
    # Constitutional identity: the few capabilities that define EVA as a
    # self-improving organism and may never SILENTLY vanish. Enforced by the
    # immutable kernel so an evolved release cannot quietly erode them.
    #
    # Deliberately LOOSE and concept-based (synonym sets), so the organism stays
    # free to rename, refactor and redesign. Only TOTAL removal of a concept is
    # blocked - not any particular implementation or name.
    try:
        agent = (path / "agent.py").read_text(encoding="utf-8", errors="replace").lower()
    except Exception:
        print("Kernel floor: cannot read candidate agent.py")
        return False

    required_concepts = {
        "a memory of friction/problems": ("backlog", "journal", "friction"),
        "a path to self-improvement": ("pivot", "improve"),
    }

    for label, alternatives in required_concepts.items():
        if not any(a in agent for a in alternatives):
            print("Kernel floor: candidate lost capability:", label)
            return False

    return True


# Constitutional test bodies: a SMALL set of checks whose LOGIC must never be
# silently weakened. The count-ratchet proves a candidate runs no FEWER checks; it
# cannot prove a specific check's body wasn't gutted (e.g. rewritten to `assert
# True` while the count stays the same). The kernel therefore pins a normalized hash
# of each constitutional check's body. Changing one of these checks is legitimate
# ONLY as a deliberate, visible act: edit the check AND update the hash here - which
# means editing the immutable kernel and rebuilding the image, never something an
# autonomous evolution can do on its own. Keep this set small and focused on
# identity / security / gate-integrity invariants.
CONSTITUTIONAL_TEST_BODIES = {
    # the per-mode permission matrix (who may write / shell / promote)
    "check_mode_policy_table":
        "26e4ff2934d2e8d3908050f87260f17540e59629415a0c42e39e70dae6721e12",
    # the exact mode set, pinned across every layer that carries modes
    "check_exact_mode_set_is_single_source_consistent":
        "a69c8efbfa3a9a9aa48f6c169a5790ffa474323cd3f3a2be3b7747dbf66d8bf5",
    # the ratchet counts EXECUTED checks (can't be inflated by dead test code)
    "check_ratchet_count_is_executed_not_static":
        "789340d85d11bfd44fa66a8ce312a318664f804989922685e42d2865a3d0a553",
    # the provider-neutral seam: core.py imports no provider/transport
    "check_core_is_provider_neutral":
        "40b7f921571964ff92df649f65f27e0cf5d573f80893705c56376fac2272ce86",
}


def _check_body_hash(text, name):
    # Normalized hash of a check's LOGIC: find the FunctionDef, drop a leading
    # docstring (wording may legitimately change), and hash ast.unparse of the
    # remaining body statements. This is stable across reformatting, comments and
    # docstring edits - and across CPython versions (verified 3.12 == 3.13) - while
    # staying sensitive to real logic changes (asserts, calls, comparisons).
    try:
        tree = ast.parse(text)
    except Exception:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(getattr(body[0], "value", None), ast.Constant) and \
                    isinstance(body[0].value.value, str):
                body = body[1:]
            if not body:
                return None
            try:
                canon = "\n".join(ast.unparse(stmt) for stmt in body)
            except Exception:
                return None
            return hashlib.sha256(canon.encode("utf-8")).hexdigest()
    return None


def _is_substantive_check(node):
    # A check function is "substantive" if its body (docstring aside) contains at
    # least one of: a function call (executes code that can throw), a raise, or an
    # assert whose condition references program state (a Name or Attribute inside
    # the test expression). Degenerate bodies - `pass`, `assert True`,
    # `assert 1 == 1` - contain none of these and fail. This is a floor against
    # silent gutting, not a proof of semantic quality.
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and \
            isinstance(getattr(body[0], "value", None), ast.Constant) and \
            isinstance(body[0].value.value, str):
        body = body[1:]
    for stmt in body:
        for x in ast.walk(stmt):
            if isinstance(x, (ast.Call, ast.Raise)):
                return True
            if isinstance(x, ast.Assert) and any(
                    isinstance(t, (ast.Name, ast.Attribute)) for t in ast.walk(x.test)):
                return True
    return False


def substantive_test_floor(path):
    # The count-ratchet proves a candidate runs no FEWER checks; the constitutional
    # hashes pin a few check BODIES. This floor covers the gap in between: EVERY
    # check_* function must contain real substance (see _is_substantive_check), so
    # the ratchet count cannot be held up by hollowed-out checks. Deterministic,
    # AST-based, LLM-free - and loose enough to never constrain what a check tests,
    # only that it tests SOMETHING.
    try:
        text = (path / "tests.py").read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except Exception:
        print("Kernel floor: cannot read/parse candidate tests.py")
        return False
    hollow = [node.name for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef) and node.name.startswith("check_")
              and not _is_substantive_check(node)]
    if hollow:
        print("Kernel floor: hollow checks (no call, no raise, no state-referencing assert):")
        for name in hollow:
            print("  -", name)
        return False
    return True


def constitutional_test_floor(path):
    # Verify the candidate's constitutional check bodies match the kernel-pinned
    # hashes, so a security / identity / gate-integrity check cannot be silently
    # weakened while keeping the ratchet count. A missing check or a changed body
    # fails the gate.
    try:
        text = (path / "tests.py").read_text(encoding="utf-8", errors="replace")
    except Exception:
        print("Kernel floor: cannot read candidate tests.py")
        return False
    for name, expected in CONSTITUTIONAL_TEST_BODIES.items():
        got = _check_body_hash(text, name)
        if got is None:
            print("Kernel floor: constitutional check missing or unparsable:", name)
            return False
        if got != expected:
            print("Kernel floor: constitutional check body changed:", name)
            print("  expected", expected)
            print("  got     ", got)
            print("  (weakening a constitutional check requires updating the kernel + rebuild)")
            return False
    return True


def kernel_gate(candidate_rel):
    rel, path = safe_release_rel(candidate_rel)

    print("\nKernel gate for:", rel)

    required = ["supervisor.py", "agent.py", "tests.py", "manifest.json"]
    for name in required:
        if not (path / name).exists():
            print("Missing:", name)
            return False

    if not kernel_capability_floor(path):
        return False

    if not constitutional_test_floor(path):
        return False

    if not substantive_test_floor(path):
        return False

    # Kernel-level floor. The active/evolved supervisor may have stronger gates,
    # but these checks cannot be skipped.
    checks = [
        [sys.executable, path / "tests.py", "--self"],
        [sys.executable, path / "supervisor.py", "--smoke"],
        [sys.executable, path / "supervisor.py", "--qualification-round", "kernel-1"],
        [sys.executable, path / "supervisor.py", "--qualification-round", "kernel-2"],
    ]

    for cmd in checks:
        if not run(cmd, release_path=path, timeout=240):
            return False

    return True


def approve(prompt, auto_yes=False):
    if auto_yes:
        print(prompt + " [auto-yes]")
        return True
    try:
        return input(prompt + " [y/N] ").strip().lower() == "y"
    except EOFError:
        # Non-interactive stdin (piped input exhausted): fail closed, like the
        # agent-side human gate does, instead of crashing mid-promotion.
        print("(no interactive stdin - declining)")
        return False


def next_version_name():
    # The kernel assigns clean, sequential version names (v001, v002, ...). A
    # promoted candidate is renamed to the next free number so the live release
    # never carries a "-candidate" suffix.
    nums = []
    for p in RELEASES.iterdir():
        if p.is_dir():
            m = re.fullmatch(r"v(\d+)", p.name)
            if m:
                nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    name = f"v{n:03d}"
    while (RELEASES / name).exists():
        n += 1
        name = f"v{n:03d}"
    return name


def maybe_promote(auto_yes=False, locked=False):
    # Promotion writes the shared release store, so it must hold the evolution lock.
    # Callers already inside a locked evolution pass locked=True; standalone callers
    # (work/review/bare) self-lock here, and only AFTER confirming there is something
    # to promote, so the lock-free fast path stays lock-free.
    if not PROMOTION.exists():
        return False
    if locked:
        return _promote_locked(auto_yes=auto_yes)
    if not acquire_evolution_lock("promote"):
        print("(promotion deferred: evolution lock is busy)")
        return False
    try:
        return _promote_locked(auto_yes=auto_yes)
    finally:
        release_evolution_lock()


def _promote_locked(auto_yes=False):
    if not PROMOTION.exists():
        return False

    req = json.loads(PROMOTION.read_text(encoding="utf-8"))
    candidate = req.get("candidate")
    reason = req.get("reason", "")

    print("\nKernel saw promotion request:")
    print("Candidate:", candidate)
    print("Reason:", reason)

    if not req.get("supervisor_qualified"):
        print("Warning: request has no supervisor_qualified flag. Kernel will still run its own gate.")

    if not kernel_gate(candidate):
        print("Kernel rejected candidate.")
        PROMOTION.unlink()
        log("promotion_rejected", {"candidate": candidate, "reason": "kernel gate failed"})
        # Learning signal for the agent's self-model (lineage/vitals): failed
        # promotions belong in the ledger, not only in the kernel log.
        ledger_append({"kind": "promotion_rejected", "candidate": candidate,
                       "reason": reason})
        return False

    # Make the decision reviewable, not blind: before asking, show WHAT the
    # candidate actually changes vs the active release (files, line stats, new
    # checks). Best-effort - a diff failure must never block a gated promotion.
    try:
        cur_rel = CURRENT.read_text(encoding="utf-8").strip()
        _, cur_path = safe_release_rel(cur_rel)
        _, cand_p = safe_release_rel(candidate)
        d = diff_releases(cur_path, cand_p)
        print(f"\nWhat this candidate changes (vs {cur_rel.rsplit('/', 1)[-1]}):")
        for label, key in (("added", "files_added"), ("modified", "files_modified"),
                           ("removed", "files_removed")):
            names = d.get(key) or []
            if names:
                if key == "files_modified":
                    names = [f"{n} (+{st[0]}/-{st[1]})" if (st := _file_line_stats(
                        cur_path, cand_p, n)) else n for n in names]
                print(f"  files {label:8s}: " + ", ".join(names))
        for label, key in (("new checks", "checks_added"), ("dropped checks", "checks_removed")):
            names = d.get(key) or []
            if names:
                print(f"  {label:14s}: " + ", ".join(names))
        print("  (after promotion: `eva changes --diff [file]` shows the full diff)")
    except Exception:
        pass

    if not approve("Promote candidate after kernel gate?", auto_yes=auto_yes):
        print("Promotion declined.")
        PROMOTION.unlink()
        log("promotion_declined", {"candidate": candidate})
        ledger_append({"kind": "promotion_declined", "candidate": candidate,
                       "reason": reason})
        return False

    old_rel = CURRENT.read_text(encoding="utf-8").strip()

    # Rename a promoted "*-candidate" to the next clean version so the live release
    # never keeps a candidate name. The kernel owns version numbering.
    final_rel = candidate
    _, cand_path = safe_release_rel(candidate)
    if cand_path.name.endswith("-candidate"):
        new_name = next_version_name()
        cand_path.rename(RELEASES / new_name)
        final_rel = "runtime/releases/" + new_name

    # Make the promoted release auditable: rehash its manifest to its real content and
    # stamp the live version, so the manifest never lies about an evolved release.
    _, final_path = safe_release_rel(final_rel)
    rehash_release_manifest(final_path, final_path.name)

    LAST_GOOD.write_text(old_rel, encoding="utf-8")
    CURRENT.write_text(final_rel, encoding="utf-8")
    PROMOTION.unlink()

    # Stamp a what-changed summary INTO the ledger row, so `changes` can still
    # explain this promotion even after old release dirs are pruned.
    diff = None
    try:
        _, old_path = safe_release_rel(old_rel)
        diff = diff_releases(old_path, final_path)
    except Exception:
        pass

    ledger_append({"kind": "promoted", "parent": old_rel, "release": final_rel,
                   "candidate": candidate, "reason": reason, "diff": diff,
                   "requested_by": req.get("requested_by"),
                   "supervisor_qualified": bool(req.get("supervisor_qualified"))})
    log("promoted", {"from": old_rel, "to": final_rel, "candidate": candidate, "reason": reason})
    # End-of-evolution recap: after screens of gate output the human should not have
    # to scroll back to learn WHAT was evolved and where they stand now.
    gates = ("supervisor" + (" \u00b7 qualified" if req.get("supervisor_qualified") else "")
             + " \u00b7 kernel floor \u00b7 all checks passed")
    rows = [("promoted", f"{final_rel.rsplit('/', 1)[-1]}  (was {old_rel.rsplit('/', 1)[-1]})")]
    if reason:
        rows.append(("changed", reason[:500]))
    rows.append(("gates", gates))
    rows.append(("rollback", "previous release kept as last_good \u2014 `organism.py rollback` restores it"))
    print("\n" + _summary_box("\u2726 promoted", rows))
    return True


def _summary_box(title, rows, width=76):
    """Render labeled rows in a rounded box; ANSI-tinted only on a TTY."""
    tty = sys.stdout.isatty()
    dim = "\x1b[38;5;242m" if tty else ""
    bold = "\x1b[1;32m" if tty else ""
    label_c = "\x1b[38;5;108m" if tty else ""
    rst = "\x1b[0m" if tty else ""
    label_w = max(len(l) for l, _ in rows)
    inner = width - 2
    text_w = inner - label_w - 4
    lines = []
    for label, value in rows:
        chunks = textwrap.wrap(value, text_w) or [""]
        for i, chunk in enumerate(chunks):
            lab = label if i == 0 else ""
            pad = inner - 3 - label_w - len(chunk)
            lines.append(f"{dim}\u2502{rst} {label_c}{lab:<{label_w}}{rst}  {chunk}"
                         + " " * max(0, pad) + f"{dim}\u2502{rst}")
    head = f" {title} "
    side = (width - 2 - len(head)) // 2
    bar = "\u2500"
    top = (dim + "\u256d" + bar * side + rst + bold + head + rst
           + dim + bar * (width - 2 - side - len(head)) + "\u256e" + rst)
    bottom = dim + "\u2570" + bar * (width - 2) + "\u256f" + rst
    return "\n".join([top] + lines + [bottom])


def run_current_supervisor(args, auto_yes=False, allow_shell=False):
    rel, path = current_release()
    supervisor = path / "supervisor.py"

    env = os.environ.copy()
    env["ORGANISM_ROOT"] = str(ROOT)
    env["ACTIVE_RELEASE"] = str(path)
    # Persistent shared-library shelf for evolved skills' native deps (survives the
    # ephemeral container; works even in SAFE). Injected ONLY into the agent child
    # env - the kernel's own process must never load libs from an EVA-writable dir.
    shelf = "/eva/.local/lib"
    prior = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = shelf + (":" + prior if prior else "")

    if auto_yes:
        env["ORGANISM_AUTO_YES"] = "1"
    if allow_shell:
        env["ORGANISM_ALLOW_SHELL"] = "1"

    print("\nActive release:", rel)

    p = subprocess.Popen(
        [sys.executable, str(supervisor)] + list(args),
        cwd=ROOT,
        env=env,
    )
    while True:
        try:
            return p.wait()
        except KeyboardInterrupt:
            # Ctrl+C reaches the whole foreground group; the supervisor/agent below
            # handle it as a graceful session end. Keep waiting so a pending
            # promotion still gets gated (maybe_promote runs after this returns).
            continue


def parse_flag_value(args, flag, default):
    if flag not in args:
        return default
    i = args.index(flag)
    if i + 1 >= len(args):
        raise SystemExit(f"{flag} needs a value")
    return args[i + 1]


def status():
    ensure_seed()
    current = CURRENT.read_text(encoding="utf-8").strip()
    last = LAST_GOOD.read_text(encoding="utf-8").strip() if LAST_GOOD.exists() else "(none)"

    print("Current:", current)
    print("Last good:", last)
    print("\nReleases:")

    for p in sorted(RELEASES.iterdir()):
        if p.is_dir():
            marker = " *" if ("runtime/releases/" + p.name) == current else ""
            print("-", p.name + marker)


def rollback(force=False):
    # Rollback rewrites CURRENT + ledger -> serialize it behind the evolution lock.
    if not acquire_evolution_lock("rollback", force=force):
        raise SystemExit(2)
    try:
        _rollback_locked()
    finally:
        release_evolution_lock()


def _rollback_locked():
    ensure_seed()

    current = CURRENT.read_text(encoding="utf-8").strip()

    # Multi-level: step back along the ledger lineage (current's parent). Fall back to
    # the single LAST_GOOD pointer for sessions that predate the ledger.
    target = parent_of(current)
    if not target and LAST_GOOD.exists():
        lg = LAST_GOOD.read_text(encoding="utf-8").strip()
        if lg and lg != current:
            target = lg
    if not target:
        print("Nothing to roll back to (already at the base release).")
        return

    safe_release_rel(target)
    CURRENT.write_text(target, encoding="utf-8")
    # Surface the NEXT rollback target as LAST_GOOD for visibility.
    nxt = parent_of(target)
    if nxt:
        LAST_GOOD.write_text(nxt, encoding="utf-8")
    ledger_append({"kind": "rollback", "from": current, "to": target})
    log("rollback", {"from": current, "to": target})
    print("Rolled back to:", target)


def provision_system_deps():
    """FREE-sandbox boot provisioning: re-install the apt packages listed in
    state/system-deps.txt so evolved skills with native deps survive the ephemeral
    container. Best-effort - a broken package list must never block a session."""
    if (os.environ.get("EVA_SANDBOX", "safe") or "safe").strip().lower() != "free":
        return
    if (os.environ.get("EVA_SYSTEM_DEPS", "") or "").strip() == "0":
        return  # opt-out
    if getattr(os, "geteuid", lambda: -1)() != 0:
        return  # apt needs root; free override runs as root
    try:
        pkgs = [line.split("#", 1)[0].strip()
                for line in SYSTEM_DEPS.read_text(encoding="utf-8").splitlines()]
        pkgs = [p for p in pkgs if p and re.fullmatch(r"[a-z0-9][a-z0-9+.:~-]*", p)]
    except FileNotFoundError:
        return
    except Exception as e:
        print("(system-deps: unreadable, skipping:", e, ")")
        return
    if not pkgs:
        return
    print(f"(provisioning {len(pkgs)} system package(s) from state/system-deps.txt ...)")
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    # Keep downloaded .debs so the bind-mounted apt cache makes re-provisioning big
    # packages a local re-install, not a re-download. The slim image's docker-clean
    # hook force-deletes archives via DPkg::Post-Invoke - drop it (rootfs is writable
    # and ephemeral in FREE, so this is per-session and harmless).
    try:
        os.remove("/etc/apt/apt.conf.d/docker-clean")
    except OSError:
        pass
    apt = ["apt-get", "-o", "APT::Keep-Downloaded-Packages=true"]
    try:
        r = subprocess.run([*apt, "install", "-y", "--no-install-recommends", *pkgs],
                           capture_output=True, text=True, timeout=600, env=env)
        if r.returncode != 0:
            # stale/missing package lists are the common cause -> update once, retry
            subprocess.run([*apt, "update"], capture_output=True, text=True,
                           timeout=300, env=env)
            r = subprocess.run([*apt, "install", "-y", "--no-install-recommends", *pkgs],
                               capture_output=True, text=True, timeout=600, env=env)
        if r.returncode == 0:
            print("(system deps ok)")
        else:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            print("(system-deps install FAILED - continuing without:")
            for line in tail:
                print("   ", line)
            print(" )")
    except Exception as e:
        print("(system-deps provisioning failed - continuing:", e, ")")


def main():
    ensure_seed()
    provision_system_deps()

    args = sys.argv[1:]

    auto_yes = "--yes" in args
    allow_shell = "--allow-shell" in args
    force = "--force" in args
    args = [a for a in args if a not in {"--yes", "--allow-shell", "--force"}]

    if not args:
        run_current_supervisor([], auto_yes=auto_yes, allow_shell=allow_shell)
        maybe_promote(auto_yes=auto_yes)
        return

    cmd = args[0]

    if cmd == "status":
        status()
        return

    if cmd == "changes":
        diff_target = None
        rest = args[1:]
        if "--diff" in rest:
            i = rest.index("--diff")
            nxt = rest[i + 1] if i + 1 < len(rest) else ""
            diff_target = "" if nxt.startswith("--") else nxt
        changes(show_all="--all" in rest, diff_target=diff_target)
        return

    if cmd == "rollback":
        rollback(force=force)
        return

    if cmd == "unlock":
        info = _lock_info()
        if LOCK.exists():
            try:
                LOCK.unlink()
            except Exception:
                pass
            print("Cleared evolution lock. Prior holder:", info)
        else:
            print("No evolution lock is held.")
        return

    if cmd == "evolve":
        rounds = int(parse_flag_value(args, "--rounds", "1"))

        # Self-evolution mutates the SHARED release store; serialize the whole run
        # (build candidate + promote) behind the single-writer evolution lock.
        if not acquire_evolution_lock("evolve", force=force):
            raise SystemExit(2)
        try:
            for i in range(1, rounds + 1):
                print(f"\n=== evolution round {i}/{rounds} ===")
                run_current_supervisor(["evolve-one"], auto_yes=auto_yes, allow_shell=allow_shell)
                maybe_promote(auto_yes=auto_yes, locked=True)
        finally:
            release_evolution_lock()

        return

    # improve can build + promote a candidate -> it must hold the evolution lock
    # for the whole run, so two improves can't build conflicting candidates.
    if cmd == "improve":
        if not acquire_evolution_lock("improve", force=force):
            raise SystemExit(2)
        try:
            run_current_supervisor(args, auto_yes=auto_yes, allow_shell=allow_shell)
            maybe_promote(auto_yes=auto_yes, locked=True)
        finally:
            release_evolution_lock()
        return

    # work/review never write the release store -> they run lock-free (parallel-
    # safe). maybe_promote() self-locks only if a stale promotion request exists.
    if cmd in {"work", "review"}:
        run_current_supervisor(args, auto_yes=auto_yes, allow_shell=allow_shell)
        maybe_promote(auto_yes=auto_yes)
        return

    print("Usage:")
    print("  python organism.py work [resume [id] | --list]")
    print("  python organism.py improve [resume]")
    print("  python organism.py review [resume]")
    print("  python organism.py evolve --rounds N [--yes] [--allow-shell]")
    print("  python organism.py status")
    print("  python organism.py changes [--all] [--diff [file]]   # what changed per promotion")
    print("  python organism.py rollback [--force]")
    print("  python organism.py unlock        # clear a dead evolution lock")
    raise SystemExit(2)


if __name__ == "__main__":
    main()
