"""The host probes behind the rows of ``kirocrew doctor``.

Each asks the host one question, most of them by running a process or an event loop: is
kiro-cli signed in, does the user's systemd manager linger, what does git say, which
userspace OOM killer runs, how much memory the gateway holds, which AWS profiles exist and
refresh themselves, whether a credential vendor answers, which privileged intents Discord
grants, whether the venv imports its dependencies, and which Node runs. The families in
:mod:`kiro_crew.doctor_checks` and the orchestrator call them through
``kiro_crew.cli_doctor``. A new probe goes here.

Composed onto ``kiro_crew.cli_doctor``, the command's one import path and patch target;
see :mod:`kiro_crew.cli_doctor_parts`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.cli_doctor import (
        _AWS_PROBE_TIMEOUT_SECS,
        _OOM_KILLER_UNITS,
        MIN_NODE_VERSION,
        UTF8_TEXT,
        DefaultCapabilityManager,
        bind_capability_manager,
        credential_vendor_server_ids,
        dep_sync,
        format_node_version,
        intent_probe,
        node_too_old_message,
        node_version_meets_floor,
        parse_node_version,
        platform_compat,
        platform_context,
        resolve_kiro_cli,
        service_linux,
    )


def _kiro_cli_signed_in() -> bool | None:
    """Whether the HOST identity store holds a credential. ``None`` when unknown.

    Three-valued on purpose, mirroring the install probe: a spawn that failed says
    nothing about the store, and reporting that as signed-out would tell an
    operator to re-run a login they already completed.

    Absent binary is ``None`` rather than ``False`` for the same reason -- the
    kiro-cli row above already reports the install, and "not signed in" would send
    someone to ``kiro-cli login`` before there is a ``kiro-cli`` to run it.
    """
    binary = resolve_kiro_cli()
    if not binary:
        return None
    # The same binary the gateway spawns: the desktop app's bundled copy ranks
    # above PATH, so ``shutil.which`` could name a copy no session runs.
    try:
        result = subprocess.run(  # noqa: S603 - argv list, no shell, local binary
            [binary, "whoami"],
            capture_output=True,
            timeout=10,
            **UTF8_TEXT,
        )
    except Exception:
        return None
    return result.returncode == 0


def _linger_enabled(user: str) -> bool | None:
    """Whether ``user``'s systemd instance lingers past logout.

    ``None`` when it cannot be determined (no ``loginctl``, unknown user, or an
    unrecognised value) so the caller can stay quiet rather than guess. Thin
    delegate to the package's one linger probe,
    :func:`kiro_crew.service.linux._linger_enabled`, so the ``loginctl`` argv and
    its parsing live in a single place.
    """
    return service_linux._linger_enabled(user)


def _git_line(repo: Path, *args: str) -> str | None:
    """First stdout line of ``git -C repo *args``, ``None`` on any failure.

    A module-level seam (not inlined) so tests can drive the checkout probe
    without a real repository. Failures are expected states here — a tarball
    install has no ``.git``, a fresh clone may lack ``origin/HEAD`` — so every
    error collapses to ``None`` and the caller renders "could not check".

    ``git`` is resolved through :func:`platform_compat.trusted_git_bin` rather
    than a bare ``PATH`` lookup: doctor runs with operator privileges, and an
    agent-writable directory leading ``PATH`` could plant a ``git`` shim. That
    helper carries the Windows install-root fallback; a miss collapses to
    ``None`` like every other failure here — no spawn at all.
    """
    git = platform_compat.trusted_git_bin()
    if git is None:
        return None
    try:
        res = subprocess.run(
            [git, "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    out = res.stdout.strip()
    return out.splitlines()[0].strip() if out else None


def _detect_userspace_oom_killer() -> str | bool | None:
    """Which userspace OOM killer is active, if any.

    Returns the unit name (``"systemd-oomd"`` / ``"earlyoom"``) when one is
    active, ``False`` when every probe completed and none is active, and
    ``None`` when it cannot be determined (no ``systemctl``, probe timeout or
    failure) so the caller reports "unknown" rather than guessing. ``True`` is
    never returned — the truthy arm carries the unit name.

    Non-privileged and bounded: ``systemctl is-active`` needs no root and each
    probe is capped at 5s, so this can never hang the doctor.
    """
    systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        return None
    determined = True
    for unit in _OOM_KILLER_UNITS:
        try:
            res = subprocess.run(
                [systemctl, "is-active", unit],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            determined = False
            continue
        if res.returncode == 0 and res.stdout.strip() == "active":
            return unit
    return False if determined else None


def _gateway_rss_bytes(pid: int) -> int | None:
    """Resident set size of *pid* in bytes, or None when no route can read it.

    ``platform_compat.proc_rss_bytes_for_pid`` serves Linux (``/proc``) and
    Windows (``GetProcessMemoryInfo``) but has no ctypes-only path on macOS and
    answers None there, so this falls through to ``ps -o rss=`` resolved via
    ``trusted_system_bin`` (ps reports KiB). Without the fallback the doctor
    line reads "RSS unreadable" on every Mac.
    """
    rss = platform_compat.proc_rss_bytes_for_pid(pid)
    if rss is not None or platform_compat.IS_WINDOWS:
        return rss
    ps_bin = platform_compat.trusted_system_bin("ps")
    if ps_bin is None:
        return None
    try:
        out = subprocess.check_output([ps_bin, "-o", "rss=", "-p", str(pid)], timeout=2)
        return int(out.decode().strip()) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _aws_probe_env() -> dict[str, str]:
    """Child environment for the ``aws configure`` probes.

    Drops the two variables that relocate the CLI's files. This section decides
    WHETHER to report from ``~/.aws`` existence but asks the CLI for the profile
    NAMES, and a subprocess inherits the environment — so with
    ``AWS_CONFIG_FILE`` / ``AWS_SHARED_CREDENTIALS_FILE`` set, the two halves
    describe DIFFERENT files. That is not hypothetical: the agent sandbox points
    both at a per-session directory, so ``doctor`` run from such a shell reported
    a profile that is absent from the operator's own config while its own probe
    said that config file does not exist. Dropping them makes the CLI resolve the
    same default locations the existence probe checks, so the halves cannot
    disagree. Everything else is inherited — ``PATH`` still has to work.
    """
    env = dict(os.environ)
    for relocator in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        env.pop(relocator, None)
    return env


def _aws_profile_names() -> list[str] | None:
    """Profiles as the SANCTIONED path reports them. ``None`` = could not ask.

    Deliberately NOT a parse of ``~/.aws/config``. That file sits inside a
    directory ``security._SENSITIVE_HOME_DIRS`` fences from the agent, and
    ``kirocrew doctor`` is reachable from a tool call — so opening it here would
    hand back through a diagnostic exactly what the floor refuses directly,
    which is the "just use a different reader" move this whole feature exists to
    talk the agent out of. ``aws configure list-profiles`` is the command the
    remediation text names and ``test_deny_guidance`` pins as allowed, so the
    report comes through the same door the guidance points at.

    ``None`` rather than ``[]`` when the CLI is absent or fails, because "cannot
    ask" and "asked, and there are none" are different things to tell an
    operator.

    Resolved through :func:`platform_compat.trusted_aws_bin` rather than ``PATH``: a gateway's
    ``PATH`` can lead with a directory the agent itself can write (a worktree
    venv's ``bin``), and this runs when an OPERATOR types ``kirocrew doctor`` —
    outside the agent's sandbox. A miss degrades to the same "cannot ask" answer
    as an absent CLI, which is the honest reading either way.
    """
    aws_bin = platform_compat.trusted_aws_bin()
    if not aws_bin:
        return None
    try:
        proc = subprocess.run(
            [aws_bin, "configure", "list-profiles"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_AWS_PROBE_TIMEOUT_SECS,
            env=_aws_probe_env(),
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    names: list[str] = []
    for line in (proc.stdout or "").splitlines():
        name = line.strip()
        if name and name not in names:
            names.append(name)
    return names


def _aws_auto_refreshes() -> bool | None:
    """Whether the profile the agent will ACTUALLY use auto-refreshes.

    Asked of the CLI rather than by looking for the string ``credential_process``
    somewhere in the config file — that substring test answered "yes" when the
    key belonged to any other profile, so the effective-profile question is both
    more accurate and reachable without a fenced read. One invocation, resolving
    the same default profile the agent's own AWS calls will resolve.

    ``None`` rather than ``False`` when there is no CLI to ask, for the same
    reason :func:`_aws_profile_names` returns it: "asked, and there is no
    ``credential_process``" is a finding, while "could not ask" is not, and
    collapsing them made the report tell an operator their credentials may expire
    mid-task on the strength of a question nobody put.

    Resolved through :func:`platform_compat.trusted_aws_bin` for the same reason as the profile
    probe: this runs under an operator's ``kirocrew doctor``, and a ``PATH`` that
    leads with an agent-writable directory would let a planted shim answer.
    """
    aws_bin = platform_compat.trusted_aws_bin()
    if not aws_bin:
        return None
    try:
        proc = subprocess.run(
            [aws_bin, "configure", "get", "credential_process"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_AWS_PROBE_TIMEOUT_SECS,
            env=_aws_probe_env(),
        )
    except Exception:
        # The CLI resolved but could not be run (timeout, OS error) — still
        # "could not ask", not an answered "no".
        return None
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def _credential_vendor_line() -> str:
    """The edition's credential-vending MCP servers, or "" when there are none.

    Phrased for the OPERATOR, not reused from the agent's refusal hint: that hint
    tells its reader to prefer the vendor and says it supersedes "the guidance
    above", neither of which is true for a human reading a terminal. Only the
    server ids are shared with the refusal path.

    Runs the capability-manager lookup on its own event loop because ``doctor`` is
    synchronous. Degrades to "" on any failure — including an already-running loop
    — since the absence of this line is indistinguishable from the public
    edition's normal state and must never fail the run.
    """
    from kiro_crew.doctor_checks import render

    try:
        manager = platform_context.safe_context_call(
            lambda: platform_context.current_context().capability_manager,
            fallback_factory=lambda: bind_capability_manager(DefaultCapabilityManager()),
            log_message=None,
        )
        if not manager.available():
            return ""
        ids = credential_vendor_server_ids(asyncio.run(manager.list_mcp()))
        if not ids:
            return ""
        listed = ", ".join(render._safe_display(name) for name in ids)
        return (
            f"agents mint credentials through {listed} rather than reading these "
            "files, so the files being unreadable to them is expected, not a fault."
        )
    except Exception:
        return ""


def _discord_intent_grants(token: str) -> intent_probe.IntentGrants:
    """Read Discord's privileged-intent grants on a throwaway event loop.

    ``asyncio.run`` gives the probe its own loop: the doctor is a separate
    process from the gateway, so the probe never shares a loop with live
    message traffic. Every failure is already folded into the result by
    :func:`~kiro_crew.discord.intent_probe.probe_intent_grants`; the guard here
    covers the loop itself failing to start, because a diagnostic that raises
    prints no report at all.
    """
    try:
        return asyncio.run(intent_probe.probe_intent_grants(token))
    except Exception as exc:  # noqa: BLE001 - a diagnostic must always answer
        return intent_probe.IntentGrants(error=type(exc).__name__)


def _venv_deps_ok(venv_py: Path) -> bool:
    """True when *venv_py* ITSELF can import the gateway's core dependencies.

    Routed through :func:`dep_sync._probe_interpreter` (``-I -X utf8`` plus a
    neutral ``cwd``) because the question is about the venv, not the process
    asking: an unisolated ``python -c`` puts the doctor's CWD at
    ``sys.path[0]`` and inherits ``PYTHONPATH``, so a decoy package on either
    route makes the check answer for the caller -- reporting the modules
    available in a venv that cannot actually serve them, a false-healthy from
    the diagnostic whose job is to catch exactly that install.
    """
    try:
        # Windows process creation and first-time Defender scans can consume
        # most of a five-second budget when the host is busy (including during
        # the parallel test suite). Keep the probe bounded, but allow enough
        # time for a healthy interpreter to start and import its dependencies.
        proc = dep_sync._probe_interpreter(
            venv_py, "import websockets, slack_sdk, aiohttp", timeout=15
        )
    except Exception:
        return False
    return proc.returncode == 0


def _report_node(issues: list[str]) -> None:
    """Print the ``node:`` line, judging the full version against ``MIN_NODE_VERSION``.

    A Node below the floor is a failure recorded in *issues*: it lacks APIs the
    code needs. The same constant drives the startup warning in ``cli.py``. An
    unreadable version is shown as present rather than guessed at.
    """
    node = shutil.which("node")
    if node:
        try:
            node_ver_result = subprocess.run(
                # The RESOLVED path, as ``cli._node_ok`` does: on Windows ``which``
                # can answer ``node.CMD``, which a bare ``node`` cannot spawn.
                [node, "-v"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            version = parse_node_version(node_ver_result.stdout)
            if version is None:
                print(f"  node:        ✅ {node}")
            elif node_version_meets_floor(version, MIN_NODE_VERSION):
                print(f"  node:        ✅ {node} (v{format_node_version(version)})")
            else:
                print(
                    f"  node:        ❌ v{format_node_version(version)} < "
                    f"v{format_node_version(MIN_NODE_VERSION)}"
                )
                print(f"               Fix: {node_too_old_message(version, MIN_NODE_VERSION)}")
                issues.append("node")
        except Exception:
            print(f"  node:        ✅ {node}")
    else:
        floor = format_node_version(MIN_NODE_VERSION)
        print(f"  node:        ⚠️  not found (Kiro Crew needs Node v{floor}+)")
        print(
            f"               Fix: install Node.js >= v{floor} (24 LTS recommended) from https://nodejs.org"
        )
