"""macOS ``launchd`` backend for pods — the darwin twin of :mod:`kiro_crew.pod.unit`.

The pod runtime's platform-neutral core (name validation, port derivation,
checkout resolution and pinning, env scrubbing, token minting, ``boot``, and the
``cleanup_home`` teardown safety check) is reused unchanged. Only the
service-manager mechanics differ, and they differ in four ways that are worth
knowing before reading the code:

**1. No template units.** systemd installs ONE template
(``kirocrew-pod@.service``) and instantiates it per pod via ``%i``. launchd has
no such concept, so each pod gets its own plist under the pod plane's own directory
(``cfg.pods_dir/dev.kirocrew.pod.<name>.plist`` — deliberately NOT
``~/Library/LaunchAgents``, which launchd loads at login and would resurrect
pods after a reboot; see :func:`plist_path`), written fresh on every
``up``. That is strictly better in one respect: the systemd unit bakes an
absolute ``kirocrew`` path at *install* time, which goes stale when the worktree
it points into is pruned (``unit_exec_ok``'s self-heal exists for exactly that);
re-rendering per ``up`` cannot go stale.

**2. No ``ExecStopPost``.** systemd guarantees zero-residue teardown by running
``kirocrew pod _cleanup <name>`` after the service stops, whatever stopped it.
launchd has no post-stop hook, so the ``down`` path must invoke
``runtime.cleanup_home`` itself after ``bootout`` succeeds. The consequence is
real and documented rather than hidden: a pod whose process dies without an
explicit ``down`` (host crash, force-reboot) leaves its isolated HOME behind on
macOS where Linux would have reaped it. :func:`orphan_homes` exists so ``pod ls`` can REPORT those
(reclaim is `pod down <name>`, run by the user — nothing deletes them
automatically).

**3. No journal.** ``journalctl --user -u <unit>`` has no launchd equivalent, so
the plist routes stdout/stderr to files under the pod artifacts dir and the log
verb tails them. This is a second mechanism, not a variation on unit management.

**4. No cgroups — the resource ceiling is NOT enforced on macOS.** The systemd
unit sets ``MemoryMax=4G`` and ``CPUQuota=200%``: kernel-enforced cgroup ceilings
that the pod documentation advertises as an isolation *guarantee*. macOS has no
cgroups and **no mechanism that bounds the total memory of a process tree or hard
-caps its CPU**. The options were to emit a weaker, non-equivalent bound and risk
it being read as the guarantee, or to state plainly that the ceiling is absent.
This backend does the latter: it emits **no** resource keys at all. Do not
"improve" this by adding ``SoftResourceLimits``/``HardResourceLimits`` and
calling it the 4G cap — ``RLIMIT_AS`` bounds one process's *virtual address
space*, not resident memory and not the subprocess tree, and the gateway spawns
agent subprocesses that each get their own limit, so the total stays unbounded.
(``ProcessType=Background`` would add scheduling de-prioritisation — real but
still not a ceiling. It is deliberately not set here; it is a knob a future
change could add *as* de-prioritisation, never as the cap.)

Every other isolation property is unchanged on macOS: own ``KIROCREW_HOME``, own
derived port, no tunnel, ``--no-crons``, and the refusal to bind the live port.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

try:  # POSIX only; the backend refuses to run where launchd is absent anyway
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from kiro_crew.pod.config import PodConfig, environment_vars
from kiro_crew.pod.unit import _kirocrew_bin

# Reverse-DNS label namespace. One label per pod; the name has already been
# through runtime.validate_name (single safe segment, no '/', no '..'), which is
# what makes it legal to splice into a label and a filename.
LABEL_PREFIX = "dev.kirocrew.pod"

# `launchctl print` on a label that is not loaded fails with this message (and
# rc 113 on current macOS). An OPERATIONAL print failure — permissions, a
# transient daemon error — is also nonzero but does NOT mean the service is
# gone, and confusing the two would let teardown delete a live pod's state.
_ABSENT_MARKERS = ("could not find service", "no such process")


def _service_absent(cp: subprocess.CompletedProcess) -> bool:
    """True only for the explicit not-loaded result, not any print failure."""
    if cp.returncode == 0:
        return False
    text = f"{cp.stdout or ''}\n{cp.stderr or ''}".lower()
    return any(m in text for m in _ABSENT_MARKERS)


# `launchctl print` fields we parse. Deliberately narrow: launchctl's output
# is an unstable, human-oriented dump, so we read only the facts the runtime
# actually asks for.
_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE)
_LAST_EXIT_RE = re.compile(r"^\s*last exit (?:code|status)\s*=\s*(-?\d+)\s*$", re.MULTILINE)
_STATE_RE = re.compile(r"^\s*state\s*=\s*(\S+)\s*$", re.MULTILINE)


class LaunchdError(RuntimeError):
    """launchd is not usable on this host."""


def require_backend() -> None:
    """Fail loudly and early when launchd cannot be driven.

    The systemd gate has three stages (Linux, systemctl on PATH, a session bus).
    launchd needs only the binary: the ``gui/<uid>`` domain is addressed directly,
    so there is no D-Bus/XDG equivalent to probe and no ``loginctl enable-linger``
    remedy to explain.
    """
    if shutil.which("launchctl") is None:
        raise LaunchdError(
            "pods need `launchctl`, which was not found on PATH. This host looks "
            "like macOS but is missing launchd; run `kirocrew pod` from a normal "
            "user session."
        )


def domain() -> str:
    """The launchd domain a per-user agent lives in.

    ``os.getuid`` does not exist on Windows; pods there are refused by
    ``require_backend`` long before this is called, but the module must stay
    importable (the test suite imports it on every platform), so mirror the
    ``getattr`` guard runtime.py uses for the same reason.
    """
    uid = getattr(os, "getuid", lambda: -1)()
    return f"gui/{uid}"


def pod_label(cfg: PodConfig, name: str) -> str:
    """Label for pod *name*. Replaces systemd's ``<prefix>@<name>.service``.

    ``cfg.unit_prefix`` is honoured so a hermetic test plane
    (``KIROCREW_POD_UNIT_PREFIX``) cannot collide with a developer's real pods,
    exactly as it cannot on systemd.
    """
    # EVERY plane carries its prefix segment — including the default one.
    # Omitting it for the default made "dev.kirocrew.pod." a strict prefix of
    # every custom plane's labels, so a hermetic plane's pods surfaced in the
    # default plane's `active_names` as bogus "<prefix>.<pod>" rows.
    return f"{LABEL_PREFIX}.{cfg.unit_prefix}.{name}"


def service_target(cfg: PodConfig, name: str) -> str:
    """``gui/<uid>/<label>`` — what the modern launchctl verbs address."""
    return f"{domain()}/{pod_label(cfg, name)}"


def plist_path(cfg: PodConfig, name: str) -> Path:
    """Where this pod's agent definition lives — deliberately NOT
    ``~/Library/LaunchAgents``.

    launchd loads everything in ``LaunchAgents`` at login, so a plist there with
    ``RunAtLoad`` would resurrect a pod after a reboot — as a login item, with no
    resource ceiling. The systemd path has the opposite semantics on purpose:
    pods are started transiently (``systemctl start``, never ``enable``) and stay
    down after a reboot. ``launchctl bootstrap`` accepts a plist at any path, so
    keeping it under the pod plane's own directory preserves that transience:
    the agent exists only for the session that bootstrapped it.
    """
    return cfg.pods_dir / f"{pod_label(cfg, name)}.plist"


_MUTEX_STATE = threading.local()


@contextlib.contextmanager
def pod_mutex(cfg: PodConfig, name: str):
    """Serialize this pod's lifecycle transactions per name.

    ``down`` and ``up`` are independent entry points (CLI and Dev Fleet, which
    shells out to the CLI) with no other per-name coordination. Without a mutex,
    a stop that has just confirmed the label absent races a concurrent start:
    the start writes its checkout pin and a fresh plist, and the stop's sweep
    then deletes the NEW pod's definition, HOME, or env file — each a
    review-blocking data-loss bug in earlier rounds. An exclusive flock on a
    sibling lock file makes the whole transaction (pin + plist + bootstrap on
    the up side; bootout + confirmation + HOME sweep + env unlink on the down
    side) atomic with respect to the same name.

    **Reentrant within a thread** so the CLI can hold it across a transaction
    while ``runtime.start_pod``/``stop_pod`` re-acquire it internally (their own
    protection for direct callers): flock is per open-file-description, so a
    naive second acquisition in the same thread would deadlock against itself.

    Advisory and cooperative by design: every mutating path routes through
    here. On platforms without ``fcntl`` the mutex degrades to a no-op —
    acceptable because launchd never runs there (:func:`require_backend`
    refuses); only unit tests do. The lock file is deliberately never deleted:
    unlinking a lock file another process may be opening reintroduces the race
    the lock exists to close.
    """
    if fcntl is None:
        yield
        return
    held = getattr(_MUTEX_STATE, "held", None)
    if held is None:
        held = _MUTEX_STATE.held = {}
    key = pod_label(cfg, name)
    if held.get(key, 0):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    cfg.pods_dir.mkdir(parents=True, exist_ok=True)
    lock_file = cfg.pods_dir / f"{key}.lock"
    with open(lock_file, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        held[key] = 1
        try:
            yield
        finally:
            held[key] = 0
            fcntl.flock(fh, fcntl.LOCK_UN)


def log_paths(cfg: PodConfig, name: str) -> tuple[Path, Path]:
    """stdout/stderr files that stand in for the journal."""
    d = cfg.artifacts_dir / name
    return d / "pod.out.log", d / "pod.err.log"


def _kirocrew_argv() -> list[str]:
    """``ProgramArguments`` needs a real argv, not systemd's command string.

    ``unit._kirocrew_bin()`` may return ``"<python> -m kiro_crew"`` (two words)
    when no console script is installed, so it is split rather than used as a
    single path.
    """
    return shlex.split(_kirocrew_bin())


def render_plist(cfg: PodConfig, name: str) -> dict[str, object]:
    """The plist body for one pod. Returned as a dict so tests can assert on keys
    rather than parsing XML."""
    out_log, err_log = log_paths(cfg, name)
    body: dict[str, object] = {
        "Label": pod_label(cfg, name),
        # Boot the isolated instance. The name is baked in (no %i equivalent);
        # boot logic stays in Python (kiro_crew.pod.runtime.boot), so nothing
        # shell-shaped is shipped, same as the systemd unit.
        "ProgramArguments": [*_kirocrew_argv(), "pod", "_run", name],
        # Start as soon as the agent is bootstrapped — the launchd analogue of
        # Type=simple + WantedBy=default.target.
        "RunAtLoad": True,
        # Self-heal a crash but do not fight a deliberate `down`: launchd will
        # restart only on a non-zero exit, so `bootout` stays authoritative.
        "KeepAlive": {"SuccessfulExit": False},
        # systemd's RestartSec=5. launchd's own default floor is 10s.
        "ThrottleInterval": 5,
        # No journal on macOS: own the files instead.
        "StandardOutPath": str(out_log),
        "StandardErrorPath": str(err_log),
    }
    env = environment_vars(cfg)
    if env:
        body["EnvironmentVariables"] = env
    # NOTE: no MemoryMax/CPUQuota analogue is emitted. See the module docstring —
    # macOS cannot enforce the cgroup ceiling and a weaker key here would read as
    # if it could.
    return body


def write_plist(cfg: PodConfig, name: str) -> Path:
    """Render and install this pod's agent definition. Returns the plist path."""
    dst = plist_path(cfg, name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out_log, _ = log_paths(cfg, name)
    out_log.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("wb") as fh:
        plistlib.dump(render_plist(cfg, name), fh, fmt=plistlib.FMT_XML)
    return dst


def launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """The single chokepoint for talking to launchd.

    Mirrors ``runtime.systemctl``'s role so tests monkeypatch one seam. No
    environment backfill is needed (systemd's ``XDG_RUNTIME_DIR`` /
    ``DBUS_SESSION_BUS_ADDRESS`` plumbing has no launchd counterpart).
    """
    require_backend()
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=check,
    )


def start(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    """``bootstrap`` the freshly-written plist. No ``daemon-reload`` equivalent —
    launchd reads the file at bootstrap time."""
    return launchctl("bootstrap", domain(), str(plist_path(cfg, name)))


def stop(cfg: PodConfig, name: str, *, timeout: float = 15.0) -> subprocess.CompletedProcess:
    """``bootout`` the agent, wait for it to actually go, and drop its plist.

    Two things here are not obvious and were both found by a real bring-up:

    **``bootout`` is asynchronous.** It returns before launchd has finished
    unloading, so immediately afterwards ``launchctl print`` still resolves the
    label and the gateway process is still alive. Returning at that point makes
    the caller reap the pod's HOME from under a live process — the removal then
    fails silently (``cleanup_home`` uses ``ignore_errors``) while the CLI reports
    zero residue. So poll until the label is really gone.

    **A per-pod plist must not outlive its pod.** systemd's template unit is
    machine-wide and deliberately persists; a launchd plist is per-pod, so leaving
    it behind makes :func:`orphan_homes` classify the leftover HOME as "installed,
    not orphaned" and never collect it, and leaves a stale definition that could be
    bootstrapped later.

    **The unload result must be authoritative.** If the label is STILL loaded
    when the wait expires, this returns a synthetic failure and keeps the plist —
    the caller must not tear down state (the HOME) belonging to a possibly-live
    gateway. A ``bootout`` rc of 3 ("no such process") with the label absent is a
    no-op, not a failure.

    The caller still owns the HOME removal (see ``runtime.stop_pod``) because that
    goes through ``cleanup_home``'s name re-validation.
    """
    cp = launchctl("bootout", service_target(cfg, name))
    deadline = time.monotonic() + timeout
    unloaded = False
    while time.monotonic() < deadline:
        probe = _print(cfg, name)
        # Only the EXPLICIT not-loaded result confirms the unload. Any other
        # nonzero print (permissions, transient daemon error) proves nothing —
        # treating it as gone was itself a review-blocking bug: teardown would
        # delete a possibly-live pod's state on a flaky probe.
        if _service_absent(probe):
            unloaded = True
            break
        time.sleep(0.2)
    if not unloaded:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=cp.stdout or "",
            stderr=(
                f"launchd did not unload {pod_label(cfg, name)} within "
                f"{timeout:.0f}s (bootout rc={cp.returncode}). The pod may still "
                "be running; its HOME and plist were preserved."
            ),
        )
    plist_path(cfg, name).unlink(missing_ok=True)
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=cp.stdout or "", stderr="")


def restart(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    return launchctl("kickstart", "-k", service_target(cfg, name))


def _print(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    return launchctl("print", service_target(cfg, name))


def is_active(cfg: PodConfig, name: str) -> bool:
    """Whether this pod has a live process.

    ``launchctl print`` exits non-zero when the label is not loaded at all. A
    loaded-but-dead agent still prints, so a live ``pid =`` line is what
    distinguishes running from merely registered — the distinction systemd's
    ``is-active --quiet`` makes for us.
    """
    cp = _print(cfg, name)
    if cp.returncode != 0:
        if _service_absent(cp):
            return False
        # An OPERATIONAL failure (permissions, transient daemon error) proves
        # nothing. Reporting "not running" here fails OPEN: down/removal guards
        # would see no pod and delete metadata or a live checkout. Refuse.
        raise LaunchdError(
            f"launchctl print failed (rc={cp.returncode}) for "
            f"{pod_label(cfg, name)}; cannot tell whether the pod is running, "
            f"refusing to report it absent: {(cp.stderr or cp.stdout or '').strip()}"
        )
    return _PID_RE.search(cp.stdout or "") is not None


def unit_state(cfg: PodConfig, name: str) -> tuple[str, int]:
    """``(state, restarts)`` shaped like the systemd backend's return.

    launchd exposes no ``NRestarts``. Rather than report 0 and silently lose the
    crash-loop signal the caller uses, a dead-but-loaded agent with a non-zero
    last exit is reported as one restart — enough for ``_wait_healthy`` to stop
    waiting on a pod that is failing to boot. The count is therefore a *signal*,
    not a tally; do not present it to a user as a restart count.
    """
    cp = _print(cfg, name)
    if cp.returncode != 0:
        return "inactive", 0
    text = cp.stdout or ""
    if _PID_RE.search(text):
        return "active", 0
    m = _LAST_EXIT_RE.search(text)
    if m and m.group(1) != "0":
        return "failed", 1
    state = _STATE_RE.search(text)
    return (state.group(1) if state else "inactive"), 0


def active_names(cfg: PodConfig) -> set[str]:
    """Names of pods with a live process.

    ``launchctl print <domain>`` dumps every label in the user domain; we filter
    to our prefix and then confirm liveness per label, because the domain listing
    includes loaded-but-dead agents that systemd's ``--state=active`` filter would
    have excluded.
    """
    cp = launchctl("print", domain())
    if cp.returncode != 0:
        # Same fail-open hazard as is_active: an empty set on an operational
        # error would tell callers (pod ls, Dev Fleet, orphan reporting) that
        # nothing is running while pods may be live. Refuse instead.
        raise LaunchdError(
            f"launchctl print {domain()} failed (rc={cp.returncode}); cannot "
            f"enumerate pods: {(cp.stderr or cp.stdout or '').strip()}"
        )
    prefix = pod_label(cfg, "")  # e.g. "dev.kirocrew.pod." (trailing dot kept)
    names: set[str] = set()
    for tok in re.findall(rf"{re.escape(prefix)}[A-Za-z0-9._-]+", cp.stdout or ""):
        candidate = tok[len(prefix):]
        if candidate and is_active(cfg, candidate):
            names.add(candidate)
    return names


def recent_journal(cfg: PodConfig, name: str, lines: int = 50) -> str:
    """The journal stand-in: the tail of this pod's own stderr/stdout files."""
    out_log, err_log = log_paths(cfg, name)
    chunks: list[str] = []
    for path in (err_log, out_log):
        try:
            tail = path.read_text(errors="replace").splitlines()[-lines:]
        except OSError:
            continue
        if tail:
            chunks.append(f"== {path.name} ==\n" + "\n".join(tail))
    if not chunks:
        return (
            f"no pod log yet at {err_log.parent} — launchd has no journal, so a pod "
            "that never started writes nothing here."
        )
    return "\n\n".join(chunks)


def orphan_homes(cfg: PodConfig) -> list[str]:
    """Pod HOMEs left behind with no live pod and no installed plist.

    Only reachable on macOS: with no ``ExecStopPost``, a pod killed without an
    explicit ``down`` leaves its isolated HOME. Reported (not deleted) here so the
    caller decides, and so deletion still goes through
    ``runtime.cleanup_home``'s re-validation rather than a raw recursive remove.
    """
    try:
        entries = [p for p in cfg.pod_root.iterdir() if p.is_dir()]
    except OSError:
        return []
    live = active_names(cfg)
    out = []
    for p in entries:
        if p.name.startswith("."):
            continue
        if p.name in live:
            continue
        if plist_path(cfg, p.name).exists():
            continue
        out.append(p.name)
    return sorted(out)
