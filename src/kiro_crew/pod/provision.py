"""Provision a worktree so it can be podded: venv + built SPA dist.

A pod boots the worktree's OWN ``.venv/bin/kirocrew gateway`` serving its OWN
``static/dist`` bundle. Both are prerequisites of "a worktree that can run a
gateway at all" — not pod inventions — but they're the on-ramp friction, so this
module collapses them into one command (``kirocrew pod provision`` /
``pod up --provision``).

Cost asymmetry drives the design:
  * venv  — pure pip editable install, ~1 min, idempotent → safe to auto-run.
  * dist  — the Vite/npm SPA build, minutes → only on explicit consent.

So plain ``pod up`` auto-builds the venv but never the dist (it fails loud and
points at provision); provision / ``--provision`` does the full chain.
"""

from __future__ import annotations

import collections
import itertools
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.env import find_node_tool, node_augmented_path

#: How many trailing stderr lines of the step that FAILED provisioning are
#: re-emitted as ``::steperr::`` markers. Four covers the shape pip and npm use
#: -- a headline, the offending path, the remedy sentence -- without turning the
#: failure notice into a log window; the full log is one click away either way.
_STEPERR_TAIL = 4

#: How long to wait for a step's pumps to reach EOF after the step exits. A
#: GRANDCHILD (npm spawns several) inherits the write end of the pipe, so a
#: survivor keeps it open and EOF never arrives; late lines are dropped instead
#: of wedging the provision.
_STEPERR_DRAIN_S = 5.0

#: The Dev Fleet gateway reads this process's merged output with
#: ``asyncio.StreamReader.readline()``, whose limit is 64 KiB of BYTES
#: (``dev_fleet/runtime.py``). A line past it raises there, and that handler
#: reaps the whole process tree -- so the ceiling forwarded under is not ours to
#: pick.
_GATEWAY_LINE_BYTES = 64 * 1024

#: Per-read ceiling on a step's output, in characters. DERIVED, not picked: the
#: stream is a text wrapper (characters) while the gateway's limit counts bytes.
#: A character encodes to at most 4 UTF-8 bytes and the relay appends one
#: newline, so the worst-case encoded line is ``cap * 4 + 1`` bytes; dividing
#: the byte ceiling by that is the whole derivation. A step runs
#: worktree-controlled code (pip lifecycle scripts, a vite config), so it can
#: write a newline-free blob of any length; ``readline(cap)`` bounds each read
#: and a longer run arrives as cap-sized pieces, every one forwarded.
_STEPERR_READ_CAP = (_GATEWAY_LINE_BYTES - 1) // 4

#: Ceiling on ONE remembered tail line, in characters. The tail names a failure
#: in a one-notice banner, not in a log; the full piece still reaches the log.
_STEPERR_LINE_CHARS = 500

#: Serializes every write to stderr. Two pump threads relay a step's stdout and
#: stderr concurrently, and the gateway reads the merged stream line by line, so
#: without one writer per line a piece of one stream could splice into the
#: middle of the other's and the merged inter-newline run could exceed the byte
#: ceiling however tightly each read was capped.
_WRITE_LOCK = threading.Lock()

#: Running index of the steps this process has run, for the ``<idx>`` slot of
#: ``::steperr::<idx>::<line>``. Provisioning emits no ``::step::`` markers (its
#: phases are recognized from the ``[provision] ...`` lines), so the index only
#: has to tell one step's tail apart from another's.
_STEP_INDEX = itertools.count()

#: ``(step index, stderr tail)`` of the most recent step that exited non-zero,
#: consumed by :func:`_fail`. Held here rather than returned because the CALLER,
#: not the step, knows whether a non-zero exit ends provisioning: ``pip install
#: --group dev`` and ``npm ci`` each fall back to a second command, and a
#: recovered failure must not be named as the reason provisioning failed.
_last_failed_step: tuple[int, list[str]] | None = None


def _say(msg: str) -> None:
    """Progress goes to STDERR so a ``pod up --json`` stdout stays pure JSON.

    Every line this process writes goes through here, under one lock -- the
    pumps relaying a step's output included -- so each line the gateway reads is
    whole. A writer that bypasses it reintroduces the splice the lock prevents.
    """
    with _WRITE_LOCK:
        print(msg, file=sys.stderr, flush=True)


def _find_python(version: str = "3.12") -> str | None:
    """Locate a pythonX.Y interpreter for the venv."""
    candidates = [
        Path.home() / ".local" / "bin" / f"python{version}",
        Path(f"/usr/bin/python{version}"),
        Path(f"/usr/local/bin/python{version}"),
    ]
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            return str(c)
    return shutil.which(f"python{version}")


def venv_bin_dir(checkout: Path) -> Path:
    """Directory holding the worktree venv's console scripts.

    POSIX venvs use ``.venv/bin``, Windows ``.venv\\Scripts``. This is the ONE
    place that knows the layout — :func:`venv_bin` and the pod runtime both
    derive from it, so a built worktree is judged the same way everywhere.
    """
    return checkout / ".venv" / ("Scripts" if platform_compat.IS_WINDOWS else "bin")


def venv_bin(checkout: Path) -> Path:
    """Path to the worktree venv's ``kirocrew`` entry point.

    Booting a pod is Linux-only, but :func:`has_venv` is called on EVERY
    platform to report build state in the Dev Fleet view — so a POSIX-only path
    here would report a perfectly built Windows worktree as unbuilt.
    """
    name = "kirocrew.exe" if platform_compat.IS_WINDOWS else "kirocrew"
    return venv_bin_dir(checkout) / name


def dist_dir(checkout: Path) -> Path:
    return checkout / "src" / "kiro_crew" / "static" / "dist"


def has_venv(checkout: Path) -> bool:
    binp = venv_bin(checkout)
    return binp.exists() and os.access(binp, os.X_OK)


def has_dist(checkout: Path) -> bool:
    return dist_dir(checkout).is_dir()


def _pump(stream, tail: "collections.deque[str] | None") -> None:
    """Relay one of a step's streams to our stderr, optionally keeping its tail.

    Runs on a thread for the step's whole lifetime, so a step that writes more
    than a pipe buffer's worth never blocks waiting for a reader, and each piece
    is written through immediately, so the dashboard's "current activity" line
    keeps advancing exactly as it did when the step wrote to the descriptor
    itself. Reads are bounded by :data:`_STEPERR_READ_CAP`, so a newline-free
    blob cannot become one unbounded allocation here.

    *tail* is a deque for the stream whose last lines name a failure (stderr)
    and ``None`` for the one that does not (stdout). Blank pieces are forwarded
    but never remembered -- they pad a diagnosis, they are never the diagnosis.
    """
    while True:
        chunk = stream.readline(_STEPERR_READ_CAP)
        if not chunk:
            break
        line = chunk.rstrip("\n")
        _say(line)
        if tail is not None and line.strip():
            tail.append(_trim_tail_line(line))


def _trim_tail_line(line: str) -> str:
    """Cut a remembered line to banner length, MARKED when it was cut.

    A diagnosis trimmed without a marker reads as the whole sentence, which is
    the same class of harm as naming a progress line -- the reader believes
    something the output does not support.
    """
    if len(line) <= _STEPERR_LINE_CHARS:
        return line
    return line[:_STEPERR_LINE_CHARS] + "..."


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> int:
    """Run a provisioning step, streaming its output to STDERR (so a concurrent
    ``pod up --json`` keeps a clean stdout). Returns the exit code.

    **Both of the step's streams are piped, and stderr's tail is remembered,
    because the ORDER of a failed step's output in one merged pipe is a lie.**
    The Dev Fleet gateway spawns ``pod provision`` with ``stderr=STDOUT``, so
    with a step writing straight to the inherited descriptors its stdout and
    stderr land in ONE pipe -- and a child block-buffers stdout to a pipe while
    writing stderr unbuffered, so the stdout buffer flushes at EXIT, after the
    diagnostic. The last line of that stream is then a progress line, and a
    failure notice built from it names nothing the user can act on. Relaying
    each stream through its own pump keeps the log and the live activity line
    unchanged while the stderr tail is kept as DATA for :func:`_fail`.

    ``PYTHONIOENCODING`` is assigned on every step because the relay decodes as
    UTF-8: a Python step (pip) re-derives its encoding from the locale and would
    otherwise encode a non-ASCII checkout path with the codepage. Non-Python
    steps (npm) ignore it. Assigned rather than defaulted: the reader's encoding
    is fixed, so a divergent inherited value would be the defect.
    """
    global _last_failed_step
    idx = next(_STEP_INDEX)
    _say(f"  $ {' '.join(cmd)}  (cwd={cwd})")
    child_env = dict(os.environ if env is None else env)
    child_env["PYTHONIOENCODING"] = "utf-8:replace"
    tail: collections.deque[str] = collections.deque(maxlen=_STEPERR_TAIL)
    proc = subprocess.Popen(  # nosec B603 - argv list, no shell
        cmd,
        cwd=str(cwd),
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    # Daemon so an undrainable pipe (see _STEPERR_DRAIN_S) cannot hold up
    # interpreter exit either. stdout gets no tail: the tail names a failure, and
    # naming it from stdout is the defect this relay exists to remove.
    pumps = [
        threading.Thread(target=_pump, args=(proc.stdout, None), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, tail), daemon=True),
    ]
    for pump in pumps:
        pump.start()
    try:
        rc = proc.wait()
        # AFTER wait(): the child is gone, so the pumps are draining what is
        # left in the pipes and reach EOF unless a surviving grandchild holds
        # one open.
        for pump in pumps:
            pump.join(timeout=_STEPERR_DRAIN_S)
    except BaseException:
        # An interrupt (Ctrl-C on a long pip/npm step, a shutdown signal)
        # unwinds this frame; without this the step keeps mutating the
        # worktree after provisioning appears to have stopped. Kill and reap
        # before re-raising -- the contract ``subprocess.run`` gives its
        # callers, kept here because the relay took over its spawn.
        proc.kill()
        proc.wait()
        raise
    if rc != 0:
        _last_failed_step = (idx, list(tail))
    return rc


def _fail(msg: str | None = None) -> bool:
    """Name the step whose failure ends provisioning; always returns ``False``.

    Says *msg* when given, then re-emits the stderr tail of the most recent
    failed step as ``::steperr::<idx>::<line>`` markers, so the dashboard can
    name the failure from the stream diagnostics arrive on rather than from the
    last line of a merged pipe. Called only where a non-zero exit is terminal --
    a step whose failure the caller recovers from by falling back is never named
    -- and the tail is consumed, so a later ``_fail`` cannot re-report it.

    This is still LOG TEXT, not a diagnosis: the lines are presented as the raw
    tail they are, so a worktree-run step that prints a plausible sentence to
    stderr gains exactly what it already had -- its output shown verbatim.
    """
    global _last_failed_step
    if msg is not None:
        _say(msg)
    failed = _last_failed_step
    _last_failed_step = None
    if failed is not None:
        idx, tail = failed
        for line in tail:
            _say("::steperr::%d::%s" % (idx, line))
    return False


def _npm_env() -> dict[str, str]:
    """Environment for an npm step, with the node toolchain on ``PATH``.

    Resolving the ``npm`` executable is not enough: npm spawns its own
    run-scripts (``tsc``, ``vite``) whose shebang is ``#!/usr/bin/env node``, so
    ``node`` must be findable by NAME inside the child too.
    """
    env = dict(os.environ)
    env["PATH"] = node_augmented_path(env.get("PATH", ""))
    return env


def _npm_bin() -> str | None:
    """Absolute path to ``npm``, or ``None`` with an actionable message emitted.

    ``pod provision`` runs from whatever spawned it. A login shell has the user's
    version manager active, but the Dev Fleet backend pins ``PATH`` to system bin
    dirs, and a systemd/launchd gateway inherits no version manager at all --
    so a bare ``["npm", ...]`` raised ``FileNotFoundError`` as an unhandled
    traceback. Resolve explicitly and fail with a remedy instead.
    """
    npm = find_node_tool("npm")
    if npm:
        return npm
    _say(
        "FATAL: npm not found. Kiro Crew looks for a Node toolchain in "
        "<data-home>/node-bin-dir (written by ensure-node.sh), then in "
        "mise / asdf / nvm / fnm / volta install dirs, then on PATH.\n"
        "  Fix: run `bash ensure-node.sh` in the main checkout to install "
        "Node, or set KIROCREW_NODE_BIN_DIR=/abs/path/to/node/bin."
    )
    return None


def ensure_venv(checkout: Path) -> bool:
    """Create the worktree's editable venv if missing. Idempotent. Returns True if
    the venv is ready afterward."""
    if has_venv(checkout):
        return True
    py = _find_python()
    if not py:
        _say("FATAL: no python3.12 found (need it to build the venv)")
        return False
    _say(f"[provision] creating venv for {checkout.name} (one-time, ~1 min)…")
    venv_dir = checkout / ".venv"
    if _run([py, "-m", "venv", str(venv_dir)], checkout) != 0:
        return _fail()
    pip = venv_bin_dir(checkout) / ("pip.exe" if platform_compat.IS_WINDOWS else "pip")
    # Upgrade pip first — `pip install --group` (PEP 735) needs pip >= 25.1, and a
    # fresh `python -m venv` ships an older pip on many hosts.
    _run([str(pip), "install", "--quiet", "--upgrade", "pip"], checkout)
    # Install runtime deps AND the PEP 735 `dev` dependency-group (pytest, flake8,
    # isort, mypy, …) so the documented build gate can run inside the pod venv.
    # If `--group` is unsupported (pip < 25.1) the command exits
    # nonzero, so fall back to a runtime-only editable install and warn — never
    # hard-fail provisioning just because the dev extras could not be installed.
    if _run(
        [str(pip), "install", "--editable", str(checkout), "--group", "dev"],
        checkout,
    ) != 0:
        _say(
            "[provision] `pip install --group dev` failed (pip < 25.1?) — falling "
            "back to a runtime-only editable install; dev tools (pytest/flake8) "
            "were skipped, so the build gate can't run in this venv"
        )
        if _run([str(pip), "install", "--editable", str(checkout)], checkout) != 0:
            return _fail()
    return has_venv(checkout)


def _has_node_modules(website: Path) -> bool:
    """True when ``website/`` already has installed npm deps (with ``tsc``), so the
    install step can be skipped on the fast idempotent path. ``tsc`` is the build's
    key binary; its presence stands in for "deps are installed"."""
    return (website / "node_modules" / ".bin" / "tsc").exists()


def ensure_node_modules(website: Path) -> bool:
    """Install ``website/`` npm dependencies if missing.

    A fresh worktree has no ``website/node_modules`` (gitignored), so ``npm run
    build`` dies with ``tsc: command not found``. Install deps first: prefer
    ``npm ci`` (clean, lockfile-exact) and, if that fails (e.g. lockfile drift),
    fall back to ``npm install --no-package-lock``. The ``--no-package-lock``
    flag keeps the fallback NON-MUTATING: it installs into ``node_modules``
    without rewriting the tracked ``website/package-lock.json`` — provisioning
    must never dirty tracked files (accidental lockfile churn would block
    prune/rebase). Skips entirely when ``node_modules`` is already present, so
    re-provisioning stays fast. Returns True when deps are ready."""
    if _has_node_modules(website):
        return True
    npm = _npm_bin()
    if npm is None:
        return False
    env = _npm_env()
    _say("[provision] installing website npm deps (node_modules missing)…")
    if _run([npm, "ci"], website, env) == 0:
        return True
    _say(
        "[provision] `npm ci` failed (lockfile drift?) — falling back to "
        "`npm install --no-package-lock` (non-mutating: won't rewrite the "
        "tracked package-lock.json)"
    )
    if _run([npm, "install", "--no-package-lock"], website, env) != 0:
        return _fail()
    return True


def build_dist(checkout: Path) -> bool:
    """Build the worktree's SPA dist (the slow step): ``npm run build`` in
    ``website/`` (→ ``website/dist``), then stage it into the served
    ``src/kiro_crew/static/dist``. Returns True if the dist exists afterward."""
    if has_dist(checkout):
        return True
    website = checkout / "website"
    if not website.is_dir():
        _say(f"FATAL: no website/ directory at {website}")
        return False
    _say(
        f"[provision] building dist for {checkout.name} "
        f"(slow — Vite SPA build, several minutes)…"
    )
    # A fresh worktree has no website/node_modules (gitignored); install deps
    # before building or `npm run build` dies with `tsc: command not found`.
    if not ensure_node_modules(website):
        _say("FATAL: failed to install website npm deps")
        return False
    npm = _npm_bin()
    if npm is None:
        return False
    if _run([npm, "run", "build"], website, _npm_env()) != 0:
        return _fail("FATAL: npm run build failed")
    src_dist = website / "dist"
    if not src_dist.is_dir():
        _say(f"FATAL: npm build produced no dist at {src_dist}")
        return False
    # Stage website/dist → the served static/dist (replace any stale copy).
    dst = dist_dir(checkout)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # A LINK is checked before is_dir()/is_file() so a DANGLING one is still
    # replaced -- and via is_link_or_junction because this very path is
    # published as a link by `frontend._ensure_tree_dist`, through
    # `platform_compat.symlink_or_junction`, which falls back to a directory
    # JUNCTION on Windows (a directory symlink there needs
    # SeCreateSymbolicLinkPrivilege). A dangling junction answers False to
    # is_symlink(), is_file() AND is_dir(), so it fell through every branch and
    # the copytree below hit an entry that still existed: FileExistsError,
    # unhandled. `unlink_link_or_junction` removes the link itself -- os.unlink
    # for a symlink, exactly as before; rmdir for a junction -- never the
    # target's contents.
    if platform_compat.is_link_or_junction(dst):
        platform_compat.unlink_link_or_junction(dst)
    elif dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    shutil.copytree(src_dist, dst)
    return has_dist(checkout)


def provision(checkout: Path, build: bool = True) -> bool:
    """Full on-ramp: ensure venv (always) + build dist (when build=True).

    Returns True only when the worktree is fully pod-able afterward. When
    build=False, returns True if the venv is ready (dist left to the caller).
    """
    if not ensure_venv(checkout):
        return False
    if not build:
        return True
    if not build_dist(checkout):
        return False
    _say(f"[provision] {checkout.name} is ready — `kirocrew pod up {checkout.name}`")
    return True
