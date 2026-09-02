#!/usr/bin/env python3
"""Batch fleet probe — the deterministic half of the pipeline conductor's patrol.

One invocation answers, for every watched worker session, "does anything need
judgment this cycle?" — plus host posture — so a quiet patrol cycle costs one
script call and a couple of output lines instead of N transcript reads.

Usage:
    python3 fleet_probe.py --config <probe-config.json>
    python3 fleet_probe.py --config <probe-config.json> --mark-handled KEY TAG DIGEST

Config (JSON):
    {
      "sessions": ["dashboard_chat-601-1788099254", ...],   # slot keys to watch
      "idle_alert_secs": 900,      # silent longer than this -> IDLE alert
      "tail_bytes": 200000,        # per-session transcript read cap
      "load_alert_per_cpu": 1.5,   # 1-min loadavg / cpu above this -> hot
      "err_res": [...],            # extra error-tail regexes (optional)
      "banned_process_res": [...]  # cmdline regexes for banned ops (optional)
    }

Paths are DERIVED, never configurable -- the same containment rule for every
location this script touches, because its config is authored by a no-write
agent and a config-chosen path would quietly widen what an approved run can
reach:

  * transcripts are read from ``<data home>/sessions`` only, where the data
    home is ``$KIROCREW_HOME`` (else ``~/.kiro/crew``) -- the gateway this
    conductor belongs to, not an arbitrary directory;
  * the handled-set state file is ALWAYS ``<config path>.state.json``;
  * the banned-process scan reads ``/proc`` (``$KIROCREW_PROBE_PROC_ROOT``
    exists for the test harness).

A session key that does not match a transcript stem directly is also tried as
``dashboard_<key>`` (and with ``:`` as ``_``): ``session_create`` answers slot
keys while the store prefixes the surface, and a raw key must not read as a
missing session -- GONE triggers reclaim, and a false GONE is how an active
item gets duplicate-dispatched.

The data home is ``$KIROCREW_HOME`` when set, else ``~/.kiro/crew`` — the same
resolution every pipeline script uses.

Output (text, one line per FIRING signal; suppressed sessions print nothing):
    🔔 <session-key> <age>s <TAG> d=<digest>

Metadata only, by design: transcript-derived text never appears in the output,
so no private session content crosses into the caller's context whatever keys
the config watches. Content, when a ruling needs it, is read through the
workspace-authorized session tools.
    BANNED pid=<pid> <cmdline>
    OK <n> watched, <m> fired | load/cpu <x> (<posture>) | mem <G>G | banned <k>

Tags: the worker protocol words (``WORKING/PR/GREEN/BLOCKED/STANDDOWN/
PROPOSAL``), ``ERR`` for an error/throttle tail, ``IDLE`` for silence past the
threshold, ``-`` when a tail carries no tag (never fires on its own).

THE HANDLED SET replaces the overnight run's hand-grown ``grep -vE`` exclusion
pipe. Every fired line carries a ``d=<digest>`` field; ``--mark-handled KEY TAG
DIGEST`` records exactly that digest into the state file (compare-and-set: if
the tail moved on since the probe, the mark is REFUSED with exit 3 so the
caller re-probes instead of suppressing a payload nobody read). A signal is
then suppressed while (tag, digest) both still match. A new payload under the
same tag re-fires (a second GREEN with a new PR number is a new signal).
``IDLE`` marks expire after another ``idle_alert_secs``
so a nudged-but-still-silent worker re-alerts instead of vanishing.

Deliberately boring properties, do not weaken:
  * No subprocess, ever. Reads transcripts, ``/proc`` and ``loadavg`` directly.
  * The only write is the probe's own state file, atomically, and only on
    ``--mark-handled``.
  * A per-session problem (unreadable file, malformed line) degrades that one
    row, never the cycle. Exit 0 when the probe ran; 2 on malformed config.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROTO = re.compile(r"^(GREEN|PR|BLOCKED|STANDDOWN|PROPOSAL|WORKING)\b")

#: Error shapes observed in real worker tails during the 2026-08-30 fleet run.
DEFAULT_ERR_RES = (
    r"Bedrock is throttling",
    r"dispatch failure",
    r"initialize timed out",
)

#: Banned-operation cmdline shapes: a pytest without an explicit bounded worker
#: count (the repo's ``-n auto`` addopts means a bare pytest forks one worker
#: per core; ``-n 4``, ``-n=4``, ``-n4`` and ``--numprocesses=4`` all count as
#: bounded, ``-n auto`` does not), and a bare full-suite vitest with no file
#: arguments.
DEFAULT_BANNED_RES = (
    r"\bpytest\b(?!.*(?:-n|--numprocesses)\s*=?\s*\d)",
    r"\bvitest\b\s+run\s*$",
)

IDLE_TAG = "IDLE"
_FIRING = {"GREEN", "PR", "BLOCKED", "STANDDOWN", "PROPOSAL", "ERR", IDLE_TAG}

#: A session key is a filename STEM, never a path: one path-safe token. This is
#: what keeps an agent-authored key (``../../etc/foo``, an absolute path) from
#: steering the transcript read outside the sessions directory.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def data_home() -> Path:
    env = os.environ.get("KIROCREW_HOME")
    return Path(env) if env else Path.home() / ".kiro" / "crew"


def _atomic_write(path: Path, payload: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _text_of(entry: dict[str, Any]) -> str:
    content = entry.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(entry.get("text", ""))


def _tail_entries(path: Path, max_bytes: int) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes()[-max_bytes:]
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            parsed = json.loads(line)
        except Exception:
            continue  # a truncated first line is expected when tailing
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def _classify(entries: list[dict[str, Any]], err_res: list[re.Pattern[str]]) -> tuple[str, str]:
    """Return (tag, tail_text) for one session's transcript tail."""
    last_assistant = next(
        (
            _text_of(entry).strip()
            for entry in reversed(entries)
            if entry.get("role") == "assistant" and _text_of(entry).strip()
        ),
        "",
    )
    last_any = entries[-1] if entries else {}
    last_any_text = _text_of(last_any)
    if "error" in str(last_any.get("role", "")).lower() or any(
        rx.search(last_any_text) for rx in err_res
    ):
        return "ERR", (last_any_text.strip() or last_assistant)
    match = PROTO.match(last_assistant)
    return (match.group(1) if match else "-"), last_assistant


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _load_state(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _suppressed(handled: dict[str, Any], key: str, tag: str, digest: str, idle_secs: int) -> bool:
    entry = handled.get(key)
    if not isinstance(entry, dict):
        return False
    if entry.get("tag") != tag or entry.get("digest") != digest:
        return False
    if tag == IDLE_TAG:
        marked = entry.get("ts")
        return isinstance(marked, (int, float)) and time.time() - marked < idle_secs
    return True


def _host_lines(cfg: dict[str, Any]) -> tuple[list[str], str]:
    """Banned-process lines plus the host summary fragment."""
    banned_res = [
        re.compile(rx) for rx in cfg.get("banned_process_res") or list(DEFAULT_BANNED_RES)
    ]
    lines: list[str] = []
    # /proc, with an env seam for the test harness only -- not a config key,
    # for the same containment reason as the other paths.
    proc_root = Path(os.environ.get("KIROCREW_PROBE_PROC_ROOT") or "/proc")
    banned = 0
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmd = (
                    (entry / "cmdline")
                    .read_bytes()
                    .replace(b"\0", b" ")
                    .decode("utf-8", "replace")
                    .strip()
                )
            except OSError:
                continue
            if cmd:
                matched = next((rx.pattern for rx in banned_res if rx.search(cmd)), None)
                if matched is not None:
                    banned += 1
                    # pid + WHICH RULE fired is everything the conductor needs
                    # (stop the owner, re-seed with the directive). The argv is
                    # deliberately not echoed: a command line can carry
                    # credentials or presigned URLs, and this line lands in the
                    # conductor's model context.
                    lines.append(f"BANNED pid={entry.name} rule={matched}")
    per_cpu = None
    if hasattr(os, "getloadavg"):
        try:
            per_cpu = os.getloadavg()[0] / max(os.cpu_count() or 1, 1)
        except OSError:
            per_cpu = None
    mem_gb = None
    meminfo = proc_root / "meminfo"
    try:
        for line in meminfo.read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                mem_gb = int(line.split()[1]) / 1_048_576
                break
    except (OSError, ValueError, IndexError):
        mem_gb = None
    hot = per_cpu is not None and per_cpu > float(cfg.get("load_alert_per_cpu", 1.5))
    load_part = (
        f"load/cpu {per_cpu:.2f} ({'hot' if hot else 'ok'})"
        if per_cpu is not None
        else "load/cpu n/a"
    )
    mem_part = f"mem {mem_gb:.0f}G" if mem_gb is not None else "mem n/a"
    return lines, f"{load_part} | {mem_part} | banned {banned}"


def _sessions_dir() -> Path:
    """DERIVED, never configurable: this gateway's own session store."""
    return data_home() / "sessions"


def _transcript_path(sessions_dir: Path, key: str) -> Path | None:
    """The transcript file for ``key``, or None when no safe transcript exists.

    ``session_create`` answers a slot key while the store prefixes the surface
    (``dashboard_<slot>.jsonl``) and colon-form session keys use ``:`` where
    the filename uses ``_``. A raw key must not read as a missing session:
    GONE triggers reclaim, and a false GONE is how an active item gets
    duplicate-dispatched. The first SAFE existing candidate wins.

    Keys are validated against ``_KEY_RE`` before this is called, and an
    existing candidate is returned only if it resolves to a file directly
    under ``sessions_dir`` -- both halves of one rule: a key is a filename
    stem, never a path. A candidate that exists but resolves elsewhere (a
    symlink out of the store) is treated as MISSING, never returned: None is
    the answer, and None reads as GONE.
    """
    candidates = (key, f"dashboard_{key}", key.replace(":", "_"))
    root = sessions_dir.resolve()
    for candidate in candidates:
        path = sessions_dir / f"{candidate}.jsonl"
        if path.exists() and path.resolve().parent == root:
            return path
    return None


def _handled_of(state: dict[str, Any]) -> dict[str, Any]:
    """The handled map, tolerating a corrupted state file: anything that is
    not a dict reads as empty (worst case a handled signal re-fires once),
    never as a crashed patrol."""
    handled = state.get("handled")
    return handled if isinstance(handled, dict) else {}


def run_probe(cfg: dict[str, Any], state_path: Path) -> int:
    sessions: list[str] = list(cfg.get("sessions") or [])
    sessions_dir = _sessions_dir()
    idle_secs = int(cfg.get("idle_alert_secs", 900))
    tail_bytes = int(cfg.get("tail_bytes", 200_000))
    err_res = [re.compile(rx) for rx in (list(DEFAULT_ERR_RES) + list(cfg.get("err_res") or []))]
    handled = _handled_of(_load_state(state_path))

    fired = 0
    for key in sessions:
        path = _transcript_path(sessions_dir, key)
        age: int | None = None
        if path is not None:
            try:
                age = int(time.time() - path.stat().st_mtime)
            except OSError:
                age = None
        if path is None or age is None:
            # GONE flows through the same suppression as every other tag: an
            # acted-on GONE (item reclaimed, mark-handled) must not re-fire
            # every cycle until the key is dropped from the watch list.
            tag, tail, age_text = "GONE", "transcript missing", "?"
        else:
            tag, tail = _classify(_tail_entries(path, tail_bytes), err_res)
            if tag not in _FIRING and age > idle_secs:
                tag = IDLE_TAG
            if tag not in _FIRING:
                continue
            age_text = str(age)
        digest = _digest(f"{tag}:{tail}")
        if _suppressed(handled, key, tag, digest, idle_secs):
            continue
        fired += 1
        # Metadata ONLY: key, age, tag, digest. Transcript-derived text is
        # deliberately never printed -- the conductor's action table is
        # tag-keyed, and content, when a ruling needs it, is read through the
        # workspace-authorized session tools, not through this script. That
        # keeps the probe's output free of private session text no matter
        # which keys an (agent-authored) config watches.
        print(f"🔔 {key:<28} {age_text:>5}s {tag:<9} d={digest}")

    banned_lines, host = _host_lines(cfg)
    for line in banned_lines:
        print(line)
    print(f"OK {len(sessions)} watched, {fired} fired | {host}")
    return 0


def mark_handled(cfg: dict[str, Any], state_path: Path, key: str, tag: str, digest: str) -> int:
    if not _KEY_RE.fullmatch(key):
        print(f"malformed key {key!r}: keys are stems, never paths", file=sys.stderr)
        return 2
    tail_bytes = int(cfg.get("tail_bytes", 200_000))
    err_res = [re.compile(rx) for rx in (list(DEFAULT_ERR_RES) + list(cfg.get("err_res") or []))]
    path = _transcript_path(_sessions_dir(), key)
    if path is not None and path.exists():
        current_tag, tail = _classify(_tail_entries(path, tail_bytes), err_res)
        del current_tag  # the digest is keyed on the CALLER's tag, like the probe's
    else:
        tail = "transcript missing"  # mirror run_probe's GONE payload exactly
    current = _digest(f"{tag}:{tail}")
    if current != digest:
        # Compare-and-set: a new same-tag payload arrived between the probe
        # and this mark. Digesting what is there NOW would suppress a signal
        # nobody has read -- refuse, so the caller re-probes and acts on the
        # payload that actually exists.
        print(
            f"refused: {key} payload changed since the probe (re-probe and act on it)",
            file=sys.stderr,
        )
        return 3
    state = _load_state(state_path)
    handled = _handled_of(state)
    state["handled"] = handled
    handled[key] = {
        "tag": tag,
        "digest": current,
        "ts": int(time.time()),
    }
    state["updated_at"] = int(time.time())
    _atomic_write(state_path, json.dumps(state, indent=1, sort_keys=True) + "\n")
    print(f"handled {key} {tag}")
    return 0


def _config_error(cfg: dict[str, Any]) -> str | None:
    """The first problem with a parsed config, or None. Typed misconfiguration
    is malformed config (exit 2 with a message), never an uncaught crash."""
    for key in ("sessions", "err_res", "banned_process_res"):
        value = cfg.get(key)
        if value is not None and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            return f"{key} must be a list of strings"
    for item in cfg.get("sessions") or []:
        if not _KEY_RE.fullmatch(item):
            return f"session key {item!r} is not a plain key (keys are stems, never paths)"
    for key in ("idle_alert_secs", "tail_bytes", "load_alert_per_cpu"):
        value = cfg.get(key)
        if value is None:
            continue
        # bool is an int subclass, and JSON permits NaN/Infinity: neither is a
        # usable threshold, and int(NaN) raises -- reject both up front.
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value != value
            or value in (float("inf"), float("-inf"))
            or value < 0
        ):
            return f"{key} must be a finite non-negative number"
    for rx in list(cfg.get("err_res") or []) + list(cfg.get("banned_process_res") or []):
        try:
            re.compile(rx)
        except re.error as exc:
            return f"bad regex {rx!r}: {exc}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--mark-handled",
        nargs=3,
        metavar=("KEY", "TAG", "DIGEST"),
        help="record the fired signal as handled; DIGEST is the d= field of the"
        " fired line, and a stale digest is refused (exit 3)",
    )
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("config must be a JSON object")
    except (OSError, ValueError) as exc:
        print(f"malformed config: {exc}", file=sys.stderr)
        return 2
    problem = _config_error(cfg)
    if problem is not None:
        print(f"malformed config: {problem}", file=sys.stderr)
        return 2
    # Derived, never configurable -- see the module docstring: a config-chosen
    # destination would make this no-write agent's one approved writer an
    # arbitrary-path file replacer.
    state_path = Path(f"{config_path}.state.json")
    if args.mark_handled:
        return mark_handled(cfg, state_path, *args.mark_handled)
    return run_probe(cfg, state_path)


if __name__ == "__main__":
    sys.exit(main())
