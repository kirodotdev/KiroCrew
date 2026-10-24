"""Provision a worktree so it can be podded: venv + built SPA dist.

A pod boots the worktree's OWN ``.venv/bin/kirocrew gateway`` serving its OWN
``static/dist`` bundle. Both are prerequisites of "a worktree that can run a
gateway at all" — not pod inventions — but they're the on-ramp friction, so this
module collapses them into one command (``kirocrew pod provision`` /
``pod up --provision``).

Cost asymmetry drives the design:
  * venv  — editable install, idempotent → safe to auto-run. By default it is a
            plain pip install, ~1 min and ~400 MB per worktree. With
            :data:`USE_UV_ENV` set it is built by ``uv`` instead: ~10 s, and its
            site-packages are copy-on-write clones out of uv's global cache
            (~10 MB of unique disk per worktree on a reflink filesystem; a
            full copy elsewhere).
  * dist  — the Vite/npm SPA build, minutes → only on explicit consent.

So plain ``pod up`` auto-builds the venv but never the dist (it fails loud and
points at provision); provision / ``--provision`` does the full chain.
"""

from __future__ import annotations

import collections
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.code_fingerprint import hardened_git_env
from kiro_crew.constants import env_flag_enabled
from kiro_crew.env import find_node_tool, node_augmented_path, resolve_uv
from kiro_crew.subprocess_utf8 import UTF8_TEXT

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


#: Bound on the ``py`` launcher probe. It only reads the registry and prints a
#: path, so a launcher that has not answered by now is not going to.
_PY_LAUNCHER_TIMEOUT_S = 10


def _find_python_via_launcher(version: str) -> str | None:
    """Ask the Windows ``py`` launcher where pythonX.Y lives, or None.

    A python.org install on Windows ships ``python.exe`` and never a
    ``pythonX.Y.exe``, so the name the POSIX lookup asks for does not exist
    there however many interpreters are installed. The launcher is the
    platform's own index of them (PEP 397 / PEP 514), and ``py -X.Y`` selects
    exactly one. The candidate prints its own ``sys.executable``, so an answer
    is a real interpreter of that version that actually ran, not a path guess.
    """
    launcher = shutil.which("py")
    if not launcher:
        return None
    try:
        out = subprocess.check_output(
            [
                launcher,
                f"-{version}",
                "-I",
                "-X",
                "utf8",
                "-c",
                "import sys; print(sys.executable)",
            ],
            timeout=_PY_LAUNCHER_TIMEOUT_S,
            stderr=subprocess.DEVNULL,
            **UTF8_TEXT,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        # No such version registered, or the launcher could not run it.
        return None
    return out if out and Path(out).is_file() else None


#: The interpreter floor, ``requires-python`` in ``pyproject.toml`` (pinned by
#: ``test_pod``). A worktree venv on any newer minor is as valid as one on this.
MIN_PYTHON = (3, 12)

#: Minor versions asked for by name (``python3.N``), after the gateway's own.
#: Bounded rather than open-ended because a name lookup per minor is the only
#: way to find a versioned binary; ``python3`` below catches anything newer.
_NAMED_MINORS = tuple(range(MIN_PYTHON[1], 16))

#: Bound on one candidate probe: a ``venv --without-pip`` into a temp dir plus
#: one interpreter start. Seconds on any healthy host.
_PROBE_TIMEOUT_S = 60

#: Variables that can make an interpreter start in the PROBE and not in the
#: venv the probe vouches for: a relocatable CPython (the Builder Toolbox
#: build) only finds ``libpython`` through a loader path its launcher passes,
#: and the provision steps do not carry one.
_PROBE_DROPPED_ENV = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH")

#: Runs INSIDE the throwaway venv: the version floor, and ``ensurepip``, which
#: the pip path needs and a stripped bundled interpreter (the desktop app's)
#: does not ship.
_PROBE_SNIPPET = (
    "import sys, ensurepip; " f"sys.exit(0 if sys.version_info[:2] >= {MIN_PYTHON!r} else 3)"
)


def _python_in(venv_dir: Path) -> Path:
    """A venv's interpreter, for the venv at *venv_dir* (any name, not just .venv)."""
    if platform_compat.IS_WINDOWS:
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _probe_python(candidate: str) -> str | None:
    """``None`` when *candidate* can build a working venv, else why it cannot.

    Existence and the execute bit prove nothing. A version-manager shim (mise,
    pyenv, asdf) exists and is executable for a version it has no install for,
    and fails only when run. A relocatable interpreter starts fine through the
    wrapper that sets its library path, and the venv built from it does not,
    because the venv links the real binary. So the probe does what provisioning
    will do, at the smallest scale: create a venv without pip in a temp dir,
    then run that venv's own interpreter.
    """
    env = {k: v for k, v in os.environ.items() if k not in _PROBE_DROPPED_ENV}
    with tempfile.TemporaryDirectory(prefix="kc-pyprobe-") as tmp:
        venv_dir = Path(tmp) / "v"
        steps = (
            [candidate, "-I", "-m", "venv", "--without-pip", str(venv_dir)],
            [str(_python_in(venv_dir)), "-I", "-c", _PROBE_SNIPPET],
        )
        for argv in steps:
            try:
                proc = subprocess.run(  # nosec B603 - argv list, no shell
                    argv,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=_PROBE_TIMEOUT_S,
                    **UTF8_TEXT,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return f"could not run ({exc.__class__.__name__}: {exc})"
            if proc.returncode == 3:
                return f"older than {MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
            if proc.returncode != 0:
                lines = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()]
                detail = lines[-1].strip()[:200] if lines else ""
                return f"exit {proc.returncode}" + (f": {detail}" if detail else "")
    return None


def _uv_found_python() -> str | None:
    """The interpreter ``uv python find`` resolves for the floor, or None.

    uv-managed interpreters are standalone builds that venvs work from, which is
    what a host whose own Python is relocatable or stripped needs. Asked whether
    or not :data:`USE_UV_ENV` is set: this only FINDS an interpreter, it builds
    nothing, and it never downloads one (``--no-python-downloads``).
    """
    uv = resolve_uv()
    if not uv:
        return None
    try:
        out = subprocess.check_output(  # nosec B603 - argv list, no shell
            [uv, "python", "find", "--no-python-downloads", f">={MIN_PYTHON[0]}.{MIN_PYTHON[1]}"],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_S,
            **UTF8_TEXT,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def _python_candidates():
    """Interpreters to try, best first, lazily (the uv lookup is a subprocess).

    1. This process's base interpreter -- the gateway's (or the CLI's) own, so a
       pod runs on the Python version its operator already runs.
    2. ``python3.N`` by name, the gateway's minor first, then the floor upward,
       each in ``~/.local/bin``, the platform's system bin directories and on
       ``PATH``; then ``python3``. On Windows, which ships no versioned names,
       the ``py`` launcher per minor.
    3. ``uv python find`` for the floor.
    """
    base = getattr(sys, "_base_executable", "") or sys.executable
    if base:
        yield base
    own = sys.version_info[1]
    minors = [own] + [m for m in _NAMED_MINORS if m != own]
    for minor in minors:
        name = f"python3.{minor}"
        user_bin = Path.home() / ".local" / "bin" / name
        if user_bin.exists() and os.access(user_bin, os.X_OK):
            yield str(user_bin)
        found = platform_compat.trusted_system_bin_quiet(name)
        if found:
            yield found
        found = shutil.which(name)
        if found:
            yield found
    found = shutil.which("python3")
    if found:
        yield found
    if platform_compat.IS_WINDOWS:
        for minor in minors:
            found = _find_python_via_launcher(f"3.{minor}")
            if found:
                yield found
    found = _uv_found_python()
    if found:
        yield found


def _find_python() -> str | None:
    """The first candidate that can build a working venv, or None.

    Every rejection is said, so a failure lists each interpreter that was tried
    and why it was refused, rather than claiming none exists.

    A relative candidate is skipped, not absolutized: ``shutil.which`` returns
    one when the ``PATH`` entry it matched is relative (``.``, ``bin``), and
    the provision steps run with ``cwd=<checkout>``, where it does not
    resolve. A binary found that way is whatever sits in the current
    directory, the same rule :func:`kiro_crew.env.resolve_uv` applies to uv.
    """
    seen: set[str] = set()
    for candidate in _python_candidates():
        if not os.path.isabs(candidate):
            _say(f"[provision] skipping {candidate}: found through a relative PATH entry")
            continue
        # The literal path, not the resolved one: every version-manager shim
        # resolves to the SAME manager binary, and each one selects a different
        # interpreter by the name it was invoked as.
        key = os.path.abspath(candidate)
        if key in seen:
            continue
        seen.add(key)
        reason = _probe_python(candidate)
        if reason is None:
            return candidate
        _say(f"[provision] skipping {candidate}: {reason}")
    return None


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


#: Per-checkout provenance manifest for the artifacts git does NOT already
#: fingerprint — the venv and ``node_modules``. The dist is deliberately absent:
#: ``website/vite.config.ts``'s ``buildIdPlugin`` already stamps ``HEAD:website``
#: into ``dist/build-id.json`` on every ``vite build`` (and
#: ``frontend._write_build_source_fingerprint`` writes the same id beside a
#: synced dist), so :func:`has_dist` reads that existing record rather than a
#: third one that would drift. The manifest lives INSIDE the git directory
#: (``git rev-parse --git-path``), not the working tree, so it is invisible to
#: ``git status``/``_rebase_locked``/"Prune merged" with no ignore rule — a
#: worktree whose revision predates this change is never dirtied by provision.
_PROVENANCE_FILE = "kirocrew-provision.json"

#: Manifest artifact keys, each mapped to the git pathspec whose oid IS its build
#: input. Staleness keys on the INPUT, not on HEAD, so a commit that changes
#: neither input leaves the artifacts current and does not force a needless
#: rebuild. The venv is an editable install whose dependency set is DYNAMIC —
#: ``pyproject.toml`` declares ``dynamic = ["dependencies", ...]`` and the actual
#: ``install_requires`` / extras live in ``setup.cfg`` — so both files are inputs
#: and a dependency bump in ``setup.cfg`` restales the venv. ``node_modules`` is
#: determined by ``website/package-lock.json`` AND ``website/package.json``: ``npm
#: ci`` installs exactly the lockfile, but the ``npm install --no-package-lock``
#: fallback resolves ``package.json`` directly, so a dependency added there
#: without a lockfile bump must still restale the deps. A source edit under
#: ``website/`` that touches neither manifest does not throw the deps away.
_ART_VENV = "venv"
_ART_NODE_MODULES = "node_modules"
_ART_INPUTS: dict[str, tuple[str, ...]] = {
    _ART_VENV: ("pyproject.toml", "setup.cfg"),
    _ART_NODE_MODULES: ("website/package-lock.json", "website/package.json"),
}


def _git_exe() -> str:
    """Resolve ``git`` to a TRUSTED absolute executable, never through the
    inherited ``PATH``.

    The Dev Fleet gateway's service PATH leads with agent-writable directories
    (a worktree venv, ``~/.local/bin``), and ``_build_fleet`` calls ``has_venv``
    / ``has_dist`` on every poll — so a bare ``git`` here would let an agent that
    planted a ``git`` shim run code in the credential-bearing standard tier.
    ``trusted_git_bin`` resolves only an executable inside a fixed system/Homebrew
    bin dir (the same seam ``stale_bundle_guard`` and ``code_fingerprint`` use);
    when it finds none the probe treats the revision as unresolvable (see
    :func:`_resolve_oid`) rather than spawning an untrusted one.
    """
    return platform_compat.trusted_git_bin() or ""


def _resolve_inputs(checkout: Path, pathspecs: tuple[str, ...]) -> str | None:
    """Combine the oids of several build-input pathspecs into one stamp, or None
    when ANY of them cannot be resolved.

    An artifact whose build reads more than one tracked path (the editable venv
    reads both ``pyproject.toml`` and ``setup.cfg``) is current only when EVERY
    input is unchanged, so the stamp is the ordered join of each input's oid. If
    any input is unresolvable (non-git tree, untracked), the whole stamp is None
    and the caller falls back to presence-only — never a partial stamp that could
    match by coincidence."""
    oids = []
    for spec in pathspecs:
        oid = _resolve_oid(checkout, spec)
        if oid is None:
            return None
        oids.append(oid)
    return " ".join(oids)


def _resolve_oid(checkout: Path, pathspec: str) -> str | None:
    """The git object id of ``HEAD:<pathspec>`` in *checkout*, or None when it
    cannot be resolved.

    For a directory pathspec this is the TREE oid; for a file it is that blob's
    oid. Either changes exactly when the tracked content it names changes — the
    artifact's real build input.

    ``None`` is NOT "stale": it means staleness is unknowable (not a git
    checkout, no trusted git, or the path is untracked at HEAD), so the presence
    checks fall back to presence-only rather than hiding a good artifact. A
    tarball install and a test fixture both land here. Git runs under
    ``hardened_git_env`` so a planted repo/global config cannot steer it.
    """
    git = _git_exe()
    if not git:
        return None
    try:
        out = subprocess.run(  # nosec B603 - trusted absolute git, argv list, no shell
            [git, "-C", str(checkout), "rev-parse", f"HEAD:{pathspec}"],
            capture_output=True,
            timeout=10,
            env=hardened_git_env(),
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    oid = out.stdout.strip()
    return oid or None


def _dist_build_tree(checkout: Path) -> str | None:
    """The ``website/`` tree id the STAGED dist was built from, or None.

    Reads the identity the frontend build already records, in the same order
    ``dashboard/stale_bundle_guard`` reads it: ``dist/build-id.json``'s
    ``websiteTree`` (rewritten by every ``vite build``, including provision's own
    and the Dev Fleet sync's), falling back to ``kirocrew-build-source.txt`` for a
    dist with no ``build-id.json``. None when neither carries a tree id (absent,
    unreadable, or built from a dirty/git-less tree) — a "cannot verify" case the
    caller treats as presence-only, never as stale."""
    dist = dist_dir(checkout)
    try:
        data = json.loads((dist / "build-id.json").read_text(encoding="utf-8"))
    except OSError:
        data = None
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        tree = str(data.get("websiteTree") or "").strip()
        return tree or None
    try:
        tree = (dist / "kirocrew-build-source.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return tree or None


def _manifest_path(checkout: Path) -> Path | None:
    """Where this checkout's provenance manifest lives: a per-worktree private
    file inside the git directory, resolved via ``git rev-parse --git-path`` so a
    linked worktree gets its OWN file (the git dir is per-worktree; the object
    store it points at is shared). None when *checkout* is not a git tree."""
    git = _git_exe()
    if not git:
        return None
    try:
        out = subprocess.run(  # nosec B603 - trusted absolute git, argv list, no shell
            [git, "-C", str(checkout), "rev-parse", "--git-path", _PROVENANCE_FILE],
            capture_output=True,
            timeout=10,
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    p = out.stdout.strip()
    if not p:
        return None
    # --git-path yields a path relative to the checkout's cwd for a relative git
    # dir; anchor it on the checkout so a later read from a different cwd agrees.
    path = Path(p)
    return path if path.is_absolute() else (checkout / path)


def _read_provenance(checkout: Path) -> dict[str, str]:
    """The stamped build-input oids for this checkout, keyed by artifact.

    A missing or unreadable manifest reads as ``{}`` — an absent stamp, which the
    matchers treat as "not provisioned from this input". Never raises: a corrupt
    manifest must not take down a presence probe the Fleet view calls on every
    poll."""
    path = _manifest_path(checkout)
    if path is None:
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _stamp_artifact(checkout: Path, artifact: str, input_oid: str | None) -> None:
    """Record, in the manifest, the build-input oid *artifact* was built from.

    ``input_oid`` is captured by the caller BEFORE the build runs and passed in,
    so a revision that advances during a multi-minute build leaves the artifact
    correctly stale rather than certified against the post-build HEAD. ``None``
    (the input could not be resolved) writes nothing: a tree with no resolvable
    input has no provenance to record, and the matchers fall back to
    presence-only there anyway.

    Best-effort: a stamp that cannot be written is not a build failure, it only
    costs the next probe a re-provision (an absent stamp reads as stale). The
    manifest is read-modify-written so stamping one artifact never drops
    another's."""
    if input_oid is None:
        return
    path = _manifest_path(checkout)
    if path is None:
        return
    data = _read_provenance(checkout)
    data[artifact] = input_oid
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def _clear_artifact(checkout: Path, artifact: str) -> None:
    """Drop *artifact*'s manifest entry, so it reads as not-provisioned until a
    build succeeds and re-stamps it.

    Called BEFORE the first mutating build step, so an install interrupted
    part-way (``npm ci`` after ``tsc`` is already present, an in-place venv
    rebuild) cannot leave the PRIOR stamp vouching for the half-overwritten tree.
    Without this, returning to the earlier input and retrying would find the old
    stamp still matching and skip the repair. Best-effort and only touches this
    one key; a manifest that cannot be rewritten just leaves the stamp, which at
    worst costs a spurious rebuild (an absent stamp reads as stale, never as
    falsely-current)."""
    path = _manifest_path(checkout)
    if path is None:
        return
    data = _read_provenance(checkout)
    if artifact not in data:
        return
    del data[artifact]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def _is_current(checkout: Path, artifact: str) -> bool:
    """True when *artifact*'s stamp matches its build input's current oid.

    - input unresolvable -> True: staleness is unknowable (non-git tree, no
      trusted git), so defer to the caller's presence check rather than hide a
      real artifact.
    - stamp absent or mismatched -> False: the artifact's build input moved since
      the build, so it belongs to an earlier input. The next provision re-runs
      once and stamps the current input oid, and it is correct from then on.
    """
    want = _resolve_inputs(checkout, _ART_INPUTS[artifact])
    if want is None:
        return True
    return _read_provenance(checkout).get(artifact) == want


def has_venv(checkout: Path) -> bool:
    binp = venv_bin(checkout)
    return binp.exists() and os.access(binp, os.X_OK) and _is_current(checkout, _ART_VENV)


def has_dist(checkout: Path) -> bool:
    """True when the served ``static/dist`` is present AND built from the
    checkout's current ``website/`` tree.

    Staleness reuses the identity the frontend build already records — the
    ``website/`` tree id in ``dist/build-id.json`` / ``kirocrew-build-source.txt``
    (see :func:`_dist_build_tree`) — compared to ``HEAD:website``. That same
    record is written by a Dev Fleet sync's ``vite build``, so a synced dist is
    recognised as current with no second manifest to keep in step. Either side
    unknown (no build stamp, or no trusted git / non-git tree) falls back to
    presence-only, exactly as ``dashboard/stale_bundle_guard`` does, so a dist
    that predates this record is never hidden."""
    if not dist_dir(checkout).is_dir():
        return False
    want = _resolve_oid(checkout, "website")
    built = _dist_build_tree(checkout)
    if want is None or built is None:
        return True  # cannot verify → presence-only
    return built == want


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


#: Set to a truthy value (``1``/``true``/``yes``/``on``) to build the venv with
#: ``uv`` out of its shared global cache (copy-on-write clones) instead of the
#: default ``python -m venv`` + pip. Opt-in, not default: the pip path is what
#: every contributor runs today and the two must stay byte-for-byte identical in
#: layout until the switch has been exercised beyond the author's host. Flipping
#: the default is Phase 2 of the
#: "Shared Dependency Cache for Worktrees" RFC and waits on that document being
#: accepted on main.
USE_UV_ENV = "KIROCREW_PROVISION_USE_UV"


def _find_uv() -> str | None:
    """Absolute path to ``uv``, or ``None`` when the pip path should be used.

    ``None`` unless :data:`USE_UV_ENV` is set: the pip path is the default, and
    a host opts into uv because every worktree venv is the same few hundred MB
    of wheels — uv installs them as copy-on-write clones out of ONE global cache
    (``uv cache dir``), so the twentieth worktree costs about as much disk as
    the first, and the install itself is seconds rather than a minute.

    Resolution is :func:`kiro_crew.env.resolve_uv` — ``uv`` is a declared
    dependency shipped as a wheel, located through ``uv.find_uv_bin()`` and then
    ``PATH`` — the same ladder the pptx-maker engine uses. ``None`` is a
    reportable condition (the pip path runs), never an exception.
    """
    if not env_flag_enabled(USE_UV_ENV):
        return None
    return resolve_uv()


def _venv_python(checkout: Path) -> Path:
    name = "python.exe" if platform_compat.IS_WINDOWS else "python"
    return venv_bin_dir(checkout) / name


def _ensure_venv_uv(checkout: Path, uv: str, py: str) -> bool:
    """Build the venv with ``uv``. Returns True when ``.venv/bin/kirocrew`` exists
    afterward; False leaves the caller free to fall back to pip.

    ``--link-mode clone`` is explicit on BOTH uv steps rather than left to uv's
    default: ``uv venv --seed`` installs pip and setuptools from the same
    global cache as ``uv pip install`` does, so the seed step needs the same
    mode or the seeded packages would be whatever uv picks. Both flags are
    accepted by ``uv venv`` (``uv venv --help``: ``--seed`` "install seed
    packages (one or more of: pip, setuptools, and wheel)"; ``--link-mode``
    "the method to use when installing packages from the global cache",
    values clone/copy/hardlink/symlink; ``--allow-existing`` "preserve any
    existing files or directories at the target path"), all older than the
    declared floor (``--allow-existing`` since uv 0.1.40, seed-package link
    mode since 0.1.32), and ``--group`` on ``uv pip install``
    exists since uv 0.6.7, which is the floor ``setup.cfg`` declares. uv picks
    ``clone`` only on macOS, and on Linux its default has been observed to copy
    on a host whose filesystem supports reflinks. Clone means copy-on-write: on
    XFS with reflink, btrfs, APFS and ReFS the venv shares blocks with uv's
    cache until something writes to a file, and that write copies the block, so
    an edit to one venv can never reach the cache or a sibling venv. Where the
    filesystem cannot reflink (ext4, XFS formatted without reflink, a cache on a
    different filesystem) uv falls back to a plain copy — the install still
    succeeds and is still faster than pip, it just does not share disk.
    Hardlink mode was rejected: it shares the inode itself, so one in-place
    write would land in every venv and in the cache. ``--project`` points
    ``--group`` at the worktree's ``pyproject.toml`` regardless of the caller's
    cwd (the Dev Fleet backend and a login shell provision from different
    directories).
    ``--seed`` installs ``pip`` into the venv, which ``uv venv`` otherwise omits:
    nothing here needs it, but ``make backend`` drives ``$(VENV)/bin/pip`` and a
    contributor's ad-hoc ``.venv/bin/pip …`` must keep working on a worktree a
    pod provisioned — a venv that differs from the pip-built one only by lacking
    pip is a trap, not a saving.
    """
    venv_dir = checkout / ".venv"
    _say(f"[provision] creating venv for {checkout.name} with uv (one-time, ~10 s)…")
    seed = [
        uv,
        "venv",
        "--seed",
        "--allow-existing",
        "--link-mode",
        "clone",
        "--python",
        py,
        str(venv_dir),
    ]
    if _run_uv(seed, checkout) != 0:
        return False
    install = [
        uv,
        "pip",
        "install",
        "--link-mode",
        "clone",
        "--python",
        str(_venv_python(checkout)),
        "--project",
        str(checkout),
        "--editable",
        str(checkout),
        "--group",
        "dev",
    ]
    if _run_uv(install, checkout) != 0:
        return False
    # Presence only, NOT has_venv: the provenance stamp is written by the CALLER
    # after this returns true, so gating on it here would read False on every git
    # checkout and send a perfectly good uv venv down the pip fallback.
    binp = venv_bin(checkout)
    return binp.exists() and os.access(binp, os.X_OK)


def _run_uv(cmd: list[str], checkout: Path) -> int:
    """:func:`_run` for a uv step, with a spawn failure folded into the rc.

    ``resolve_uv`` trusts the wheel locator's path after ``os.path.isfile`` only,
    so a uv binary that exists but cannot be executed (mode bits stripped, a
    ``noexec`` mount) reaches ``Popen`` and raises ``OSError`` instead of
    running. Every other uv failure is a nonzero exit that the caller already
    turns into the pip fallback; a spawn failure must land on the same path, not
    escape ``ensure_venv`` as a traceback the bool contract cannot express.
    """
    try:
        return _run(cmd, checkout)
    except OSError as exc:
        _say(f"[provision] uv could not be started ({exc}); falling back to pip")
        return 1


def ensure_venv(checkout: Path) -> bool:
    """Create the worktree's editable venv if missing. Idempotent. Returns True if
    the venv is ready afterward.

    Builds with ``python -m venv`` + pip by default. When :data:`USE_UV_ENV` is
    set it tries ``uv`` first (shared global cache, copy-on-write site-packages,
    seconds) and falls back to the pip path when uv cannot be located or fails
    part-way. Neither path deletes ``.venv``: ``uv venv`` runs with
    ``--allow-existing`` because its default is to REMOVE a virtual environment
    already at the target, and ``python -m venv`` runs over whatever uv left,
    exactly as it already does over a venv an interrupted pip provision left
    behind, so two provisioners racing on one checkout (CLI and Dev Fleet) can
    never remove each other's finished venv.
    """
    if has_venv(checkout):
        return True
    py = _find_python()
    if not py:
        _say(
            f"FATAL: no Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]} that can build a venv "
            "(each candidate tried is listed above). Install one, e.g. "
            f"`uv python install {MIN_PYTHON[0]}.{MIN_PYTHON[1]}`, and re-run provision"
        )
        return False
    venv_dir = checkout / ".venv"
    # Capture the build input (pyproject.toml's oid) BEFORE building, so a
    # revision that advances during the install stamps the input the venv was
    # actually built from, never a later one.
    venv_input = _resolve_inputs(checkout, _ART_INPUTS[_ART_VENV])
    # Invalidate any prior stamp BEFORE the first mutating step: an in-place venv
    # rebuild interrupted part-way must not leave the old stamp certifying the
    # half-overwritten venv on a later retry (F4).
    _clear_artifact(checkout, _ART_VENV)
    uv = _find_uv()
    if uv:
        if _ensure_venv_uv(checkout, uv, py):
            _stamp_artifact(checkout, _ART_VENV, venv_input)
            # Revalidate: if a rebase changed pyproject.toml/setup.cfg DURING the
            # uv build, the stamp (the pre-build input) fails to match the current
            # input, so has_venv is False and we must report that rather than a
            # bare True — otherwise the caller starts a pod on a venv already stale.
            return has_venv(checkout)
        _say("[provision] uv provisioning failed — falling back to python -m venv + pip")
    _say(f"[provision] creating venv for {checkout.name} (one-time, ~1 min)…")
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
    if (
        _run(
            [str(pip), "install", "--editable", str(checkout), "--group", "dev"],
            checkout,
        )
        != 0
    ):
        _say(
            "[provision] `pip install --group dev` failed (pip < 25.1?) — falling "
            "back to a runtime-only editable install; dev tools (pytest/flake8) "
            "were skipped, so the build gate can't run in this venv"
        )
        if _run([str(pip), "install", "--editable", str(checkout)], checkout) != 0:
            return _fail()
    _stamp_artifact(checkout, _ART_VENV, venv_input)
    return has_venv(checkout)


def _has_node_modules(website: Path) -> bool:
    """True when ``website/`` already has installed npm deps (with ``tsc``) built
    from the current ``website/package-lock.json``, so the install step can be
    skipped on the fast idempotent path. ``tsc`` is the build's key binary; its
    presence stands in for "deps are installed".

    The provenance check compares against the CHECKOUT's
    ``website/package-lock.json`` oid (the checkout is ``website``'s parent),
    because ``npm ci`` installs exactly the lockfile — a lockfile bump pulled in
    by a rebase/sync is the only source change that invalidates the installed
    deps, and a source edit under ``website/`` that leaves the lockfile untouched
    does not. An unresolvable input defers to presence-only (see
    :func:`_is_current`)."""
    return (website / "node_modules" / ".bin" / "tsc").exists() and _is_current(
        website.parent, _ART_NODE_MODULES
    )


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
    # Capture the input (the package-lock.json oid) BEFORE installing, so a
    # concurrent rebase during the install cannot certify these deps against a
    # later lockfile.
    node_input = _resolve_inputs(website.parent, _ART_INPUTS[_ART_NODE_MODULES])
    # Invalidate any prior stamp BEFORE the first mutating step: an install
    # interrupted after tsc is present but before success must not leave the old
    # stamp certifying the half-written node_modules on a later retry (F4).
    _clear_artifact(website.parent, _ART_NODE_MODULES)
    if _run([npm, "ci"], website, env) == 0:
        _stamp_artifact(website.parent, _ART_NODE_MODULES, node_input)
        # Revalidate the INPUT: a lockfile change DURING the install leaves the
        # stamp stale, so report _is_current rather than a bare True (same
        # reasoning as the uv venv path) — the build must not continue on deps
        # already stale. (_is_current, not _has_node_modules: we are re-checking
        # the captured input's freshness, not re-probing the tree npm just wrote.)
        return _is_current(website.parent, _ART_NODE_MODULES)
    _say(
        "[provision] `npm ci` failed (lockfile drift?) — falling back to "
        "`npm install --no-package-lock` (non-mutating: won't rewrite the "
        "tracked package-lock.json)"
    )
    if _run([npm, "install", "--no-package-lock"], website, env) != 0:
        return _fail()
    _stamp_artifact(website.parent, _ART_NODE_MODULES, node_input)
    return _is_current(website.parent, _ART_NODE_MODULES)


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
    # No manifest stamp for the dist: `npm run build` above already wrote
    # dist/build-id.json's websiteTree, which has_dist reads. copytree staged it
    # into the served dist along with the bundle.
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
