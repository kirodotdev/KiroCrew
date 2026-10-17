"""Mint a fresh pod dashboard URL for the scroll rigs.

Usage: python3 mint-pod-url.py <pod-worktree-name> <session-key> [kirocrew-bin]

Writes the URL to $KIROCREW_SCRATCH/kc-pod-url.txt (or $TMPDIR, else the
current directory) with mode 0600 -- it carries a live bearer token, so it
does not go to a world-writable shared path. Pod dashboard tokens expire within
minutes, so re-mint immediately before every rig run rather than once per
session. `pod up --json` is the supported way to obtain a token; the session
key selects which seeded conversation the rig scrolls (the rigs assume a
long transcript with archived history, e.g. a 1000+ message session).
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

POD = sys.argv[1]
KEY = sys.argv[2]
KC = sys.argv[3] if len(sys.argv) > 3 else "kirocrew"

# Developer-facing helper: argv comes from the developer's own shell, but
# validate anyway so nothing shell-metacharacter-shaped ever reaches the
# subprocess, and resolve the binary explicitly instead of trusting PATH text.
if not re.fullmatch(r"[A-Za-z0-9._-]+", POD):
    raise SystemExit(f"invalid pod name: {POD!r}")
if not re.fullmatch(r"[A-Za-z0-9._-]+", KEY):
    raise SystemExit(f"invalid session key: {KEY!r}")
kc_bin = shutil.which(KC) if "/" not in KC else (KC if pathlib.Path(KC).is_file() else None)
if not kc_bin:
    raise SystemExit(f"kirocrew binary not found: {KC!r}")

# List argv, binary resolved via shutil.which, both args allowlist-validated
# above -- the taint rule cannot see the re.fullmatch sanitizers.
# nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
proc = subprocess.run(
    [kc_bin, "pod", "up", POD, "--json"],  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
    capture_output=True,
    text=True,
    timeout=300,
)
out = proc.stdout
# `pod up` exits without any JSON when the pod is missing or unprovisioned.
# Indexing the empty list then raised IndexError, which reads as a bug in this
# helper rather than as the pod failure it actually is -- and buries the child's
# own stderr, the only place the real reason appears.
json_lines = [line for line in out.splitlines() if line.strip().startswith("{")]
if proc.returncode != 0 or not json_lines:
    detail = (proc.stderr or out or "").strip()[-2000:]
    raise SystemExit(
        f"`pod up {POD}` produced no pod JSON (exit {proc.returncode}). "
        f"Is the pod provisioned?\n{detail}"
    )
d = json.loads(json_lines[-1])
base = d["base_url"].replace("127.0.0.1", "localhost")
url = f"{base}/chat/{KEY}?token={d['token']}&sid={KEY}"
# The URL carries a live dashboard bearer token, so it must not be written
# through a path an attacker can pre-create. A shared /tmp with a permissive
# umask lets a pre-planted symlink redirect the write (leaking the token) or
# truncate a file the victim owns. O_CREAT|O_EXCL refuses an existing path
# rather than following it, O_NOFOLLOW refuses a symlink, and 0600 keeps the
# token off other accounts. A stale file from a previous mint is replaced by
# unlinking it first -- that unlink is on a path we just refused to follow.
# A directory this user owns. $KIROCREW_SCRATCH is reclaimed with the session;
# the shared /tmp is deliberately NOT a fallback (see the write below).
OUT_DIR = os.environ.get("KIROCREW_SCRATCH") or os.environ.get("TMPDIR") or "."
URL_PATH = pathlib.Path(OUT_DIR) / "kc-pod-url.txt"
URL_PATH.unlink(missing_ok=True)
fd = os.open(
    URL_PATH,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
    0o600,
)
with os.fdopen(fd, "w") as fh:
    fh.write(url)
print(f"url written to {URL_PATH} (mode 0600)")
print(url.split("token=")[0] + "token=***")
