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
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Protocol

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
