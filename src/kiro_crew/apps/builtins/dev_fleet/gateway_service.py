"""Platform backends for controlling the gateway service Dev Fleet acts on.

Dev Fleet's Restart and Make live controls need four things from the OS service
manager that owns the running gateway:

1. **active** — is the gateway actually managed by a service manager we can
   drive? (When it is not — e.g. a packaged ``.app`` spawned the backend — the
   controls must be reported as unavailable WITH A REASON, never silently
   hidden.)
2. **start identity** — an opaque token that changes the instant the
   replacement main process starts, so the dashboard can wait past its own
   winding-down process instead of treating the first 200 as "recovered".
3. **detached restart** — a restart that survives the death of the very
   process that requested it (the restart kills us).
4. **stage / rollback a new live target** — repoint the service at a different
   checkout, atomically, with the previous state recoverable on failure.

``systemd`` (Linux) and ``launchd`` (macOS) satisfy these very differently:

===================  ==========================================  ==================================================
concern              systemd                                     launchd
===================  ==========================================  ==================================================
manageable           ``systemctl --user cat <unit>`` rc == 0      ``launchctl print gui/<uid>/<label>`` rc == 0
start identity       ``ExecMainStartTimestampMonotonic``          the job's PID (launchd exposes no monotonic stamp)
detached restart     ``systemd-run --user --collect systemctl     ``launchctl kill TERM gui/<uid>/<label>`` returns
                     --user restart <unit>``                      immediately; ``KeepAlive`` respawns after the
                                                                  graceful SIGTERM, with ``ExitTimeOut`` as the
                                                                  SIGKILL ceiling
stage a new target   a drop-in overriding ExecStart,             atomically rewrite the ``live-gateway`` launcher
                     WorkingDirectory and PATH, then             script the agent's ProgramArguments executes
                     ``daemon-reload``
===================  ==========================================  ==================================================

The launchd staging mechanism deserves its own note, because the obvious
approach does not work. launchd has no drop-in concept, so repointing means
changing ``ProgramArguments`` — and a changed plist only takes effect on
``bootout`` + ``bootstrap`` (``kickstart`` restarts launchd's *in-memory* job
definition and never re-reads the file). ``bootout`` kills us, so the
``bootstrap`` half would never run: the gateway would stop and never come back.

So the plist never changes. It points permanently at a generated launcher script
and staging is an atomic rewrite of that script plus a restart request.
launchd owns the stop and respawn; rollback restores the previous script if the
restart request is rejected.

The launcher is a script rather than a symlink to the binary because a cutover
must also move the working directory and put the target checkout's venv first on
``PATH`` — the same three things the systemd drop-in overrides. A binary-only
swap would leave PATH-resolved subprocesses re-invoking the OLD install while the
gateway ran the new one, which is exactly the mixed-version fleet the Linux
design prevents.

The consequence is that make-live on macOS requires an agent installed WITH
that indirection (``kirocrew service install`` from a build that has it).
An older, directly-pointed agent is reported as ``agent_not_indirected`` — the
launchd analogue of systemd's ``no_user_unit``: installed, but not controllable
this way, and actionable rather than a silent false success. An indirected agent
with the legacy restart contract reports ``agent_restart_contract_outdated``. A
loaded agent whose launcher has been deleted is reported as ``live_program_missing``.

Hosts where NEITHER manager can be driven (:data:`FOREGROUND_ELIGIBLE`) get one
last resort: :class:`ForegroundBackend`, which hands the bounce to a detached
``kirocrew restart`` instead of a service manager. Strictly ordered
systemd > launchd > foreground, and fail-safe by construction — it never
signals anything itself, so a failed spawn leaves the running gateway
untouched and the operator keeps the manual-restart advisory.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import IO, Awaitable, Callable, Protocol

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir

# service.* is import-safe on every platform (it only touches launchctl/systemctl
# when called) and never imports apps.*, so there is no cycle to dodge here.
from kiro_crew.service.common import launchd_live_program
from kiro_crew.service.macos import (
    PLIST_PATH,
    loaded_restart_contract_current,
    render_live_program,
    restart_contract_current,
    write_live_program,
)

# (rc, stdout, stderr) — server.py's sandboxed subprocess chokepoint. Injected
# rather than imported so every spawn stays audited through the one seam the
# tests already patch, and so this module has no import cycle with server.py.
RunCmd = Callable[..., Awaitable[tuple[int, str, str]]]
#: ``shutil.which``-shaped tool lookup, injected for the same reason as the
#: platform string (see :func:`backend`).
Which = Callable[[str], "str | None"]

# --- eligibility codes -------------------------------------------------------
# These strings cross the HTTP boundary into the dashboard, which maps them to
# actionable copy. They are deliberately BACKEND-SPECIFIC rather than unified:
# the systemd pair is the pre-existing wire contract the frontend and its tests
# already assert, and renaming it would be a gratuitous breaking change for the
# platform that already worked.
#
#   ok                     the gateway is a running unit/agent we can drive
#   no_systemd  (systemd)  not Linux, or systemctl absent
#   no_user_unit(systemd)  systemctl present, but the gateway is not a loaded
#                          --user unit (a `service install` SYSTEM unit, or
#                          nothing installed)
#   no_launchd  (launchd)  not macOS, or launchctl absent
#   no_agent    (launchd)  launchctl present, but no such agent is loaded —
#                          e.g. the packaged .app spawned the backend
#   agent_not_indirected   the agent is loaded but its ProgramArguments does not
#               (launchd)  go through the live-gateway symlink, so staging would
#                          be a silent no-op. Remedy: `kirocrew service install`
#   agent_restart_contract_outdated
#               (launchd)  the plist lacks KeepAlive=true or the expected
#                          ExitTimeOut value for detached graceful restart
STATUS_OK = "ok"


class _UnsafeTargetValue(ValueError):
    """A path cannot be safely serialised into a service definition.

    Raised for a value containing a newline, NUL or other control character.
    Such a value would split or truncate a systemd drop-in and — because the
    broken override is PERSISTED — would poison every subsequent restart of the
    live unit, so it is rejected before anything is written.
    """


# Control chars (C0 range + DEL) are unrepresentable in a single systemd
# directive value: NUL/newline split or truncate the unit, a tab is ambiguous
# whitespace. Rejected on both platforms so a path that is refused on Linux is
# not silently accepted on macOS.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def reject_unsafe(raw: str) -> str:
    """Return *raw* unchanged, or raise :class:`_UnsafeTargetValue`."""
    if _CTRL_RE.search(raw):
        raise _UnsafeTargetValue(repr(raw))
    return raw


def atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (temp sibling + ``os.replace``).

    ``os.replace`` is an atomic same-filesystem rename, so a crash or partial
    write never leaves a half-written service definition behind.
    """
    tmp = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    try:
        tmp.write_text(content)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class GatewayServiceBackend(Protocol):
    """What Dev Fleet needs from the service manager owning the gateway."""

    #: Short manager name for operator-facing messages ("systemd" / "launchd").
    kind: str

    async def status(self) -> str:
        """One of the ``STATUS_*`` codes."""

    async def active(self) -> bool:
        """True when the gateway is a running unit/agent of this manager."""

    async def start_id(self) -> str | None:
        """Identity that changes when the replacement main process starts.

        ``None`` means "identity unavailable"; callers MUST degrade to the
        legacy reload-on-first-response behaviour rather than waiting forever.
        """

    async def restart_detached(self) -> tuple[bool, str]:
        """Schedule a restart that survives our own death. ``(ok, error)``."""

    def plan(self, worktree: Path, kcbin: Path) -> dict:
        """Describe — without mutating anything — what staging *worktree* does."""

    def snapshot(self) -> str | None:
        """Capture the current live target so :meth:`rollback` can restore it.

        ``None`` means "there was nothing here", which :meth:`rollback` restores
        by DELETING the target. A read failure must therefore NOT be reported as
        ``None`` -- doing so would make a failed cutover delete a live definition
        it merely could not read. Raises ``OSError`` instead; the caller refuses
        the cutover before staging anything.
        """

    async def stage(self, worktree: Path, kcbin: Path) -> tuple[bool, str, str]:
        """Point the service at *worktree*. ``(ok, code, error)``."""

    async def reload(self) -> None:
        """Re-read service definitions from disk (a no-op where meaningless)."""

    def rollback(self, prior: str | None) -> bool:
        """Best-effort restore of a :meth:`snapshot`. False on any OSError."""


# --- systemd -----------------------------------------------------------------

class SystemdBackend:
    """``systemctl --user`` backend. Behaviour is unchanged from before the
    adapter existed, including the wire codes the dashboard already maps
    (``no_systemd`` / ``no_user_unit``).

    The drop-in path and renderer are INJECTED from ``server.py`` rather than
    reimplemented here. They are the seam a dozen existing tests patch
    (``monkeypatch.setattr(mod, "_dropin_path", ...)`` /
    ``"_dropin_content"``), and keeping them where the tests already point means
    the Linux paths are covered by exactly the same assertions as before this
    module existed — which is the whole non-regression argument for a change
    that must not disturb Linux.
    """

    kind = "systemd"

    def __init__(self, run_cmd: RunCmd, unit: Callable[[], str], *,
                 platform: str, which: Which,
                 dropin_path: Callable[[], Path],
                 dropin_content: Callable[[Path, Path], str]) -> None:
        self._run = run_cmd
        # A callable, not a string: the unit depends on whether THIS backend
        # runs inside a pod, and that must be resolved per call rather than
        # frozen at construction (a cached pod verdict could bounce the
        # operator's live gateway from a pod plane).
        self._unit = unit
        self._platform = platform
        self._which = which
        self._dropin_path = dropin_path
        self._dropin_content = dropin_content

    def _available(self) -> bool:
        return self._platform == "linux" and bool(self._which("systemctl"))

    async def status(self) -> str:
        if not self._available():
            return "no_systemd"
        # Gate on the unit being known to the --user manager: a `service
        # install` SYSTEM unit is not controllable with a --user drop-in, and
        # staging against it would "succeed" while bouncing nothing.
        rc, _out, _err = await self._run(
            ["systemctl", "--user", "cat", self._unit()], timeout=5
        )
        return "ok" if rc == 0 else "no_user_unit"

    async def active(self) -> bool:
        if not self._available():
            return False
        rc, _out, _err = await self._run(
            ["systemctl", "--user", "is-active", self._unit()], timeout=5
        )
        return rc == 0

    async def start_id(self) -> str | None:
        """``ExecMainStartTimestampMonotonic`` — monotonic and tied to the
        ExecStart main-PID spawn, so it changes the instant the NEW process
        starts (a unit can enter ``active`` before its replacement main PID
        exists) and can never repeat or go backwards if NTP steps the clock."""
        if not self._available():
            return None
        rc, out, _err = await self._run(
            ["systemctl", "--user", "show", self._unit(),
             "--property=ExecMainStartTimestampMonotonic", "--value"],
            timeout=5,
        )
        if rc != 0:
            return None
        val = out.strip()
        # "0" == no recorded main-start stamp; "" == property absent. Both are
        # indistinguishable from "unknown" for a handshake, and comparing
        # against a stamp that can never change would hang the UI.
        return None if not val or val == "0" else val

    async def restart_detached(self) -> tuple[bool, str]:
        """The restart tears down THIS backend, so hand it to a transient
        ``systemd-run`` job that outlives us."""
        rc, _out, stderr = await self._run(
            ["systemd-run", "--user", "--collect",
             "systemctl", "--user", "restart", self._unit()],
            timeout=10,
        )
        return (rc == 0, "" if rc == 0 else (stderr.strip()[:200] or "systemd-run failed"))

    def plan(self, worktree: Path, kcbin: Path) -> dict:
        return {
            "unit": self._unit(),
            "dropin_path": str(self._dropin_path()),
            "dropin_content": self._dropin_content(worktree, kcbin),
        }

    def snapshot(self) -> str | None:
        # Only "absent" maps to None. Any other OSError propagates so the caller
        # refuses the cutover instead of staging over a definition whose prior
        # content it never captured (rollback(None) DELETES).
        try:
            return self._dropin_path().read_text()
        except FileNotFoundError:
            return None

    async def stage(self, worktree: Path, kcbin: Path) -> tuple[bool, str, str]:
        dropin = self._dropin_path()
        try:
            content = self._dropin_content(worktree, kcbin)
        except _UnsafeTargetValue as exc:
            return False, "unsafe_path", f"unsafe value in unit directive: {exc}"
        try:
            dropin.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(dropin, content)
        except OSError as exc:
            return False, "write_failed", f"failed to write drop-in: {exc}"
        rc, _out, stderr = await self._run(
            ["systemctl", "--user", "daemon-reload"], timeout=10
        )
        if rc != 0:
            return False, "reload_failed", (stderr.strip()[:200] or "daemon-reload failed")
        return True, "", ""

    async def reload(self) -> None:
        """Re-read unit files. Called on the rollback path so the loaded config
        matches the restored disk state rather than the rejected override."""
        if self._available():
            await self._run(["systemctl", "--user", "daemon-reload"], timeout=10)

    def rollback(self, prior: str | None) -> bool:
        dropin = self._dropin_path()
        try:
            if prior is None:
                dropin.unlink(missing_ok=True)
            else:
                atomic_write_text(dropin, prior)
            return True
        except OSError:
            return False


# --- launchd -----------------------------------------------------------------

class LaunchdBackend:
    """``launchctl`` backend for the per-user gateway LaunchAgent.

    Uses ``print`` for state and a domain-targeted ``kill TERM`` for the
    non-blocking restart transaction. ``unload`` + ``load`` is unsafe because
    unload can terminate the process that would issue load.
    """

    kind = "launchd"

    def __init__(self, run_cmd: RunCmd, label: Callable[[], str], *,
                 platform: str, which: Which) -> None:
        self._run = run_cmd
        self._label = label
        self._platform = platform
        self._which = which

    # -- addressing --
    @staticmethod
    def domain() -> str:
        """``gui/<uid>`` — what the modern launchctl verbs address.

        ``os.getuid`` is absent on Windows; this module must stay importable
        there (the test suite imports it on every platform), so mirror the
        ``getattr`` guard the pod runtime uses for the same reason.
        """
        uid = getattr(os, "getuid", lambda: -1)()
        return f"gui/{uid}"

    def target(self) -> str:
        return f"{self.domain()}/{self._label()}"

    @staticmethod
    def live_program() -> Path:
        """The stable path the agent's ``ProgramArguments`` executes.

        A cutover rewrites THIS file, so the plist never changes and no
        ``bootout``/``bootstrap`` pair (which would kill us before it could
        finish) is needed. Shared with the service installer through
        ``service.common`` so the two can never disagree about where it lives.
        """
        return Path(launchd_live_program())

    @staticmethod
    def plist_path() -> Path:
        return PLIST_PATH

    def _available(self) -> bool:
        return self._platform == "darwin" and bool(self._which("launchctl"))

    # -- state --
    async def _print(self) -> tuple[int, str]:
        rc, out, _err = await self._run(
            ["launchctl", "print", self.target()], timeout=10
        )
        return rc, out

    async def status(self) -> str:
        if not self._available():
            return "no_launchd"
        rc, _out = await self._print()
        if rc != 0:
            return "no_agent"
        # Installed — but make-live can only repoint an agent whose
        # ProgramArguments goes through our launcher. An agent installed by an
        # older build points straight at a bin path; rewriting the launcher would
        # then be a silent no-op, so say so instead.
        try:
            if str(self.live_program()) not in self.plist_path().read_text():
                return "agent_not_indirected"
        except OSError:
            return "agent_not_indirected"
        # The plist is right but the launcher itself is gone (a "reset the app"
        # gesture that deleted Application Support). The agent would fail to
        # spawn at next login with nothing on screen to explain it, so name the
        # repair rather than reporting a healthy service.
        if not self.live_program().exists():
            return "live_program_missing"
        if (
            not restart_contract_current(self.plist_path())
            or not loaded_restart_contract_current(_out)
        ):
            return "agent_restart_contract_outdated"
        return "ok"

    async def active(self) -> bool:
        if not self._available():
            return False
        rc, out = await self._print()
        if rc != 0:
            return False
        # A loaded-but-not-running agent has no pid line. Treat only a live pid
        # as active, mirroring `systemctl is-active`.
        return self._parse_pid(out) is not None

    @staticmethod
    def _parse_pid(printed: str) -> str | None:
        for line in printed.splitlines():
            stripped = line.strip()
            if stripped.startswith("pid = "):
                val = stripped.split("=", 1)[1].strip()
                return val if val.isdigit() else None
        return None

    async def start_id(self) -> str | None:
        """The job's PID.

        launchd exposes no monotonic start stamp, so the PID stands in: it
        changes on every respawn, which is exactly the "the new process is up"
        edge the restart handshake waits for. PIDs can in principle be recycled,
        but a recycled PID would have to land on the same job in the same
        restart window to confuse the handshake. ``None`` when the agent is
        loaded but not running — callers degrade rather than wait forever.
        """
        if not self._available():
            return None
        rc, out = await self._print()
        return None if rc != 0 else self._parse_pid(out)

    async def restart_detached(self) -> tuple[bool, str]:
        """Schedule a bounded graceful restart through launchd.

        Addresses the job by its ``gui/<uid>/<label>`` service target rather
        than through launchctl's legacy label-only ``stop``. That is a hard
        requirement, not a style preference: every Dev Fleet spawn is wrapped in
        ``sandbox-exec`` (see ``server._run_cmd``), and launchd refuses the
        legacy stop routine for a sandboxed caller with "Not privileged to stop
        service." regardless of the seatbelt profile's contents. The
        domain-targeted verbs are unaffected.

        ``kill TERM`` — not ``kickstart -k`` — because the restart must stay
        graceful: launchd delivers SIGTERM, the gateway runs its shutdown, and
        ``KeepAlive`` respawns it, with ``ExitTimeOut`` bounding how long the
        SIGTERM has before SIGKILL. This is safe to call from inside the process
        being restarted, since launchd performs both the signal and the respawn.
        """
        if not restart_contract_current(self.plist_path()):
            return False, (
                "launchd agent restart contract is outdated; re-run "
                "`kirocrew service install`"
            )
        rc, printed = await self._print()
        if rc != 0:
            return False, "launchd agent is not loaded"
        if not loaded_restart_contract_current(printed):
            return False, (
                "loaded launchd restart contract is outdated; re-run "
                "`kirocrew service install`"
            )
        rc, _out, stderr = await self._run(
            ["launchctl", "kill", "TERM", self.target()], timeout=10
        )
        return (rc == 0, "" if rc == 0 else (stderr.strip()[:200] or "launchctl kill failed"))

    # -- staging --
    def plan(self, worktree: Path, kcbin: Path) -> dict:
        # Validate here, not only in stage(): a dry-run plan must reject an
        # unrepresentable path on macOS exactly as it does on Linux, or the
        # preview would promise a cutover the real call then refuses.
        reject_unsafe(str(worktree))
        reject_unsafe(str(kcbin))
        return {
            "label": self._label(),
            "live_program": str(self.live_program()),
            # The exact script the cutover will write, so the preview shows what
            # will really run — the launchd counterpart of dropin_content.
            "live_program_content": self._render(worktree, kcbin),
        }

    def _render(self, worktree: Path, kcbin: Path) -> str:
        """The launcher a cutover to *worktree* installs.

        Sets the working directory and a venv-first PATH as well as the binary,
        mirroring the three directives the systemd drop-in overrides. A
        binary-only swap would leave PATH-resolved subprocesses re-invoking the
        OLD install while the gateway ran the new one.
        """
        return render_live_program(
            str(kcbin),
            working_dir=str(worktree),
            path_prefix=[
                str(worktree / ".venv" / "bin"),
                str(Path.home() / ".local" / "bin"),
                "/usr/local/bin", "/usr/bin", "/bin",
            ],
        )

    def snapshot(self) -> str | None:
        # See SystemdBackend.snapshot: absent is None, unreadable raises.
        try:
            return self.live_program().read_text()
        except FileNotFoundError:
            return None

    async def stage(self, worktree: Path, kcbin: Path) -> tuple[bool, str, str]:
        try:
            content = self._render(worktree, kcbin)
        except _UnsafeTargetValue as exc:
            return False, "unsafe_path", f"unsafe value in live target: {exc}"
        try:
            write_live_program(content, self.live_program())
        except OSError as exc:
            return False, "write_failed", f"failed to write live launcher: {exc}"
        return True, "", ""

    async def reload(self) -> None:
        """No-op: launchd has no ``daemon-reload``. The plist is never rewritten
        by staging, so there is nothing to re-read."""

    def rollback(self, prior: str | None) -> bool:
        try:
            if prior is None:
                self.live_program().unlink(missing_ok=True)
            else:
                write_live_program(prior, self.live_program())
            return True
        except OSError:
            return False


# --- foreground (last resort) --------------------------------------------------

#: Eligibility codes under which the foreground backend may be attempted: the
#: primary manager reported "nothing here to drive" at all. Deliberately
#: EXCLUDES the codes where a manager exists but is mis-set-up
#: (``user_unit_inactive``, ``agent_not_indirected``,
#: ``agent_restart_contract_outdated``, ``live_program_missing``): those hosts
#: have a named remedy, and a foreground kill-and-respawn behind the manager's
#: back would leave the manager's view of its own job wrong.
FOREGROUND_ELIGIBLE = frozenset(
    {"no_systemd", "no_user_unit", "no_launchd", "no_agent"}
)

#: ``(port) -> pid | None`` — the pid sidecar the gateway writes beside its
#: run-marker (see :mod:`kiro_crew.instances.run_marker`).
ReadPid = Callable[[int], "int | None"]
#: ``(port) -> launcher path | None`` — the run-marker's recorded launcher.
ReadLauncher = Callable[[int], "str | None"]
#: ``() -> [port, ...]`` — ports named by run-markers on disk.
MarkerPorts = Callable[[], "list[int]"]
#: ``(pid) -> bool`` — liveness probe (``platform_compat.pid_exists``).
PidExists = Callable[[int], bool]
#: ``(argv) -> None`` — establish a DETACHED process running *argv*, raising
#: ``OSError`` when it cannot be established. The seam the tests inject a fake
#: through; the real one is :func:`default_detached_spawn`.
DetachedSpawn = Callable[[list[str]], None]


#: ``() -> reason | None`` — is THIS process confined in a way a spawned
#: replacement gateway would inherit? See :func:`default_confinement`.
Confinement = Callable[[], "str | None"]


def default_confinement() -> "str | None":
    """Why a process spawned from here would inherit confinement, or ``None``.

    The app backend this code runs in is spawned by the gateway through
    ``wrap_argv`` (+ ``cgroup_scope_argv``): on a sandbox-capable host it lives
    inside a user/mount namespace (or macOS seatbelt) and the
    ``kirocrew-agents.slice`` cgroup scope. Namespaces and cgroups are
    INHERITED by children regardless of ``start_new_session``, so a
    ``kirocrew restart`` spawned from here would produce a replacement gateway
    that (a) sees the sandbox's altered filesystem view forever, (b) carries an
    agent-sized ``MemoryMax``/``TasksMax`` ceiling for the whole gateway, and
    (c) — because ``cli.main()`` clears the inherited ``KIROCREW_SANDBOX_ACTIVE``
    marker — re-wraps its own agent spawns in a NESTED sandbox that may fail.
    Worse, the detached child shares the backend's scope, so a scope kill
    mid-restart could strand the host with NO gateway.

    Detection is deny-by-default on the two purpose-built signals:

    * ``KIROCREW_SANDBOX_ACTIVE`` — exported only by the namespace launcher /
      seatbelt env prefix, i.e. only ever present INSIDE the sandbox.
    * ``kirocrew-agents.slice`` in ``/proc/self/cgroup`` — the transient scope
      ``cgroup_scope_argv`` places every backend in (Linux only; the file does
      not exist elsewhere and an unreadable file is treated as unconfined,
      matching hosts where the scope probe failed and no scope was applied).
    """
    if os.environ.get("KIROCREW_SANDBOX_ACTIVE"):
        return "the Dev Fleet backend runs inside the Kiro Crew OS sandbox"
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    if "kirocrew-agents.slice" in cgroup:
        return "the Dev Fleet backend runs inside the kirocrew-agents cgroup scope"
    return None


def default_detached_spawn(argv: list[str]) -> None:
    """Spawn *argv* detached from this process (new session / own group).

    The command must OUTLIVE us — it is ``kirocrew restart``, which kills the
    gateway that (transitively) owns this backend — so it gets its own session
    on POSIX (immune to our SIGHUP/SIGTERM propagation and to the gateway's
    process-group teardown of app backends) and a detached console-less group
    on Windows, mirroring ``cli_server._spawn_detached_gateway``. Output goes
    to the same ``gateway.log`` the ``logs`` command tails, falling back to
    ``DEVNULL`` when the log cannot be opened (a restart must not be refused
    over a diagnostics file).

    Raises ``OSError`` when the process cannot be established; the caller
    treats that as "no replacement path exists" and falls back to the manual
    advisory WITHOUT having signalled anything.
    """
    stdout: "IO[str] | int"
    try:
        log_path = config_dir() / "gateway.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stdout = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 — closed below
    except OSError:
        stdout = subprocess.DEVNULL
    # argv is a validated launcher + fixed arguments (never LLM- or
    # request-derived), same trust class as the systemd-run invocation the
    # managed path issues.
    try:
        subprocess.Popen(  # noqa: S603
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            close_fds=True,
            cwd=str(Path.home()),
            start_new_session=platform_compat.IS_POSIX,
            creationflags=(
                platform_compat.DETACHED_PROCESS | platform_compat.CREATE_NEW_PROCESS_GROUP
            ),
        )
    finally:
        # Unlike cli_server's short-lived CLI process, this runs inside the
        # long-lived app backend: the child holds its own duplicate of the fd
        # after Popen, so the parent's copy must be closed on BOTH outcomes or
        # every attempt (notably the failing ones, where the backend lives on)
        # leaks a handle.
        if not isinstance(stdout, int):
            stdout.close()


class ForegroundBackend:
    """LAST-RESORT restart for a gateway that no service manager owns.

    On a host where neither systemd nor launchd can be driven (see
    :data:`FOREGROUND_ELIGIBLE`) the gateway is just a foreground/detached
    process, and Make Live used to stage the live-target pointer and stop —
    telling the operator to run ``kirocrew restart`` themselves. This backend
    performs exactly that command FOR them, detached so it survives the death
    of the gateway it bounces, and otherwise reuses the CLI's whole
    kill-and-respawn path (lsof+SIGTERM the incumbent, wait, spawn a detached
    replacement that reads the staged pointer, poll ``/api/ready``) rather than
    reimplementing any of it.

    Selection ordering is strictly systemd > launchd > foreground: callers only
    construct this after the platform backend reported one of the
    :data:`FOREGROUND_ELIGIBLE` codes, so no host that works today changes
    behaviour.

    **Identity.** There is no ``ExecMainStartTimestampMonotonic`` here, so
    ``start_id`` is the pid the gateway records in its ``run/gateway-<port>.pid``
    sidecar (written before readiness is published, cleared/rewritten by the
    replacement) — the same identity ``kirocrew restart`` itself waits on, and
    an opaque changes-on-replacement token exactly like the launchd PID the
    dashboard handshake already consumes.

    **Locating the gateway.** A gateway is a singleton per data home
    (``gateway.lock``), and it prunes other ports' markers at startup, so the
    run-markers normally name exactly one port. This backend requires exactly
    ONE marker whose recorded pid is alive; zero (no gateway / no marker) or
    several (stale crash leftovers that could make ``--port`` ambiguous) make it
    report unavailable and the caller keeps the manual advisory.

    **FAIL SAFE.** This class never signals any process itself. Its only
    mutation is establishing the detached ``kirocrew restart``; when the binary
    cannot be resolved or the spawn fails, nothing has been killed and the
    running gateway is untouched — a failed restart that leaves no gateway is
    far worse than the manual-advisory status quo.

    **Confinement gate.** The Dev Fleet backend usually runs inside the
    gateway-applied OS sandbox and ``kirocrew-agents.slice`` cgroup scope,
    both of which a spawned replacement would inherit (see
    :func:`default_confinement`). When confinement is detected this backend
    reports unavailable and never spawns; only an unconfined backend (sandbox
    off/unavailable on this host) may perform the foreground restart.
    """

    kind = "foreground"

    def __init__(self, *, marker_ports: MarkerPorts, read_pid: ReadPid,
                 read_launcher: ReadLauncher, pid_exists: PidExists,
                 spawn: DetachedSpawn = default_detached_spawn,
                 confinement: Confinement = default_confinement,
                 ) -> None:
        self._marker_ports = marker_ports
        self._read_pid = read_pid
        self._read_launcher = read_launcher
        self._pid_exists = pid_exists
        self._spawn = spawn
        self._confinement = confinement

    # -- discovery --
    def _locate(self) -> "tuple[int, int] | None":
        """``(port, pid)`` of the single live foreground gateway, or ``None``.

        A marker is not proof of liveness (a crash leaves it behind), so only
        markers whose recorded pid still exists count. Ambiguity — more than one
        live (port, pid) — returns ``None`` rather than guessing which gateway
        to bounce.
        """
        live: list[tuple[int, int]] = []
        for port in self._marker_ports():
            pid = self._read_pid(port)
            if pid is not None and self._pid_exists(pid):
                live.append((port, pid))
        return live[0] if len(live) == 1 else None

    @staticmethod
    def _usable_launcher(path: "str | None") -> "str | None":
        """*path* when it is an absolute, executable ``kirocrew`` file."""
        if not path:
            return None
        p = Path(path)
        # Basename check mirrors cli_server._own_console_script: a restart must
        # exec the entry point it claims to be, never whatever an odd marker or
        # PATH entry happens to name. .stem so a Windows ``kirocrew.exe`` would
        # also pass, though callers currently construct this backend POSIX-only.
        if p.stem != "kirocrew" or not p.is_absolute():
            return None
        try:
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
        except OSError:
            return None
        return None

    def _resolve_binary(self, port: int) -> "str | None":
        """The ``kirocrew`` launcher to run the restart with, or ``None``.

        ONLY the run-marker's recorded launcher — written by the running
        gateway about ITSELF into the keystone-fenced ``run/`` dir (0600 inside
        0700, on the sensitive-path floor), so the restart respawns the same
        install/edition being replaced and the path is one no agent file tool
        can have planted. A ``shutil.which("kirocrew")`` fallback is
        deliberately ABSENT: ``PATH`` for this backend includes user-writable
        directories (``~/.local/bin``), so an agent-planted impostor could be
        executed by an operator's Make Live click. A host whose marker records
        no launcher (a source-tree ``python -m kiro_crew`` launch) keeps the
        manual advisory — never a guessed or PATH-trusted binary.
        """
        return self._usable_launcher(self._read_launcher(port))

    # -- protocol subset (status / identity / restart / plan) --
    async def status(self) -> str:
        """``ok`` when a restart can actually be attempted on this host."""
        if self._confinement() is not None:
            return "backend_confined"
        located = self._locate()
        if located is None:
            return "no_foreground_gateway"
        if self._resolve_binary(located[0]) is None:
            return "no_kirocrew_binary"
        return STATUS_OK

    async def start_id(self) -> "str | None":
        """The recorded gateway pid, or ``None`` (callers degrade, never wait)."""
        located = self._locate()
        return None if located is None else str(located[1])

    async def restart_detached(self) -> "tuple[bool, str]":
        """Establish a detached ``kirocrew restart``. ``(ok, error)``.

        Re-resolves rather than trusting an earlier ``status()``: the gateway
        (and its marker) can change between the probe and the act. On ANY
        failure nothing has been signalled — the incumbent keeps running and
        the caller keeps the manual advisory.
        """
        confined = self._confinement()
        if confined is not None:
            # A replacement spawned from inside the sandbox/cgroup scope would
            # inherit that confinement for the gateway's whole lifetime — and a
            # scope kill mid-restart could strand the host with no gateway.
            return False, confined
        located = self._locate()
        if located is None:
            return False, "no single live foreground gateway to restart"
        port, _pid = located
        kcbin = self._resolve_binary(port)
        if kcbin is None:
            return False, "kirocrew binary could not be resolved"
        # --port pins the restart to the gateway the marker names, exactly as
        # cli_server passes the resolved port to its own detached spawn — the
        # child must not re-resolve and disagree.
        try:
            self._spawn([kcbin, "restart", "--port", str(port)])
        except OSError as exc:
            return False, f"could not establish detached restart: {exc}"[:200]
        return True, ""

    def plan(self, worktree: Path, kcbin: Path) -> dict:
        """Describe — without mutating anything — how the restart would run."""
        located = self._locate()
        port = located[0] if located is not None else None
        resolved = self._resolve_binary(port) if port is not None else None
        return {
            "restart_backend": "foreground",
            "restart_command": (
                f"{resolved} restart --port {port}"
                if resolved is not None and port is not None
                else "kirocrew restart"
            ),
        }


# --- selection ---------------------------------------------------------------

def backend(run_cmd: RunCmd, *, unit: Callable[[], str],
            label: Callable[[], str], platform: str, which: Which,
            dropin_path: Callable[[], Path],
            dropin_content: Callable[[Path, Path], str],
            ) -> GatewayServiceBackend | None:
    """The backend for this host, or ``None`` when no manager can apply.

    ``platform`` and ``which`` are INJECTED rather than read from this module's
    own ``sys`` / ``shutil``. The caller resolves them through its own module
    globals, which keeps the existing test seams working: the dev_fleet tests
    drive platform detection by patching ``server.sys`` / ``server.shutil``, and
    a direct read here would silently escape those patches — the Linux paths
    would then be "passing" tests that no longer exercise them.

    ``None`` is NOT "everything is fine, just hide the buttons" — callers must
    surface it as an explicit, reasoned unavailability (see
    ``_gateway_service_state`` in ``server.py``), because a silently hidden
    Restart control is exactly how the macOS gap went unnoticed.
    """
    if platform == "linux":
        return SystemdBackend(run_cmd, unit, platform=platform, which=which,
                              dropin_path=dropin_path,
                              dropin_content=dropin_content)
    if platform == "darwin":
        return LaunchdBackend(run_cmd, label, platform=platform, which=which)
    return None
