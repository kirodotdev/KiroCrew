"""What a readiness answer says: the status payload and the commands it serves.

``PrerequisiteStatus`` is the payload the dashboard's setup gate renders. This module
also holds the text that payload carries: the official install page, the sign-in and
update commands (and their absolute-path forms for a bundled or off-PATH binary), the
idle ``operation`` shim older dashboard builds still read, the host's platform label,
and the sanitizer every dashboard-facing string passes through.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field

from kiro_crew import platform_compat
from kiro_crew.kiro_cli import is_bundled_kiro_cli
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

OFFICIAL_INSTALL_DOCS_URL = "https://kiro.dev/cli/"
# The exact command the user runs to sign in. A CODE CONSTANT, never a catalog
# value: a translated command cannot be typed or executed (see
# website/docs/i18n-catalog.md — "A literal token the user must type must never be
# a catalog value"). Served in the status payload so the UI has one source of
# truth for it rather than hardcoding a second copy that can drift.
# The argument tails are split out so login_commands_for() can compose the
# bundled-copy form (absolute path + same tail) without duplicating flag
# literals that would drift. They carry the leading space DELIBERATELY: the
# read-only-probe tripwire in test_kiro_prerequisite_handlers.py forbids the standalone
# quoted token a spawn argv element would have, and these are display strings
# the user types, never argv.
_LOGIN_TAIL = " login"
_SSO_LOGIN_TAIL = " login --use-device-flow --license pro"
KIRO_CLI_LOGIN_COMMAND = f"kiro-cli{_LOGIN_TAIL}"
# The organization-SSO counterpart, served alongside the bare command so the gate
# can offer both instead of one ambiguous line. Both flags are load-bearing:
# ``--use-device-flow`` is what makes the others take effect at all (kiro-cli
# discards every login flag unless the environment is remote OR that flag is set,
# falling back to a browser portal), and ``--license pro`` then selects Identity
# Center directly. The pair cannot yield a Builder ID session, which is the
# failure this exists to prevent: on the portal, a free Builder ID sits as a
# visual peer of organization SSO, so a user on an SSO plan can sign in to the
# wrong tier and only discover it when models are missing.
KIRO_CLI_SSO_LOGIN_COMMAND = f"kiro-cli{_SSO_LOGIN_TAIL}"


def login_commands_for(
    binary: str,
    environ: Mapping[str, str],
) -> tuple[str, str, bool]:
    """Sign-in commands for the RESOLVED binary, plus whether it is bundled.

    The desktop app's bundled kiro-cli (``KIROCREW_BUNDLED_KIRO_DIR``) is not
    on the user's shell PATH, so when it is the copy that resolved, the served
    commands must carry the binary's absolute path — otherwise the setup gate
    asserts "installed" while its only on-screen instruction fails with
    command-not-found on a fresh machine. POSIX paths use shell quoting. Windows
    commands name PowerShell explicitly, which also lets the same copied command
    run from cmd.exe. An operator's ``KIROCREW_KIRO_BIN`` override gets the same
    treatment when its resolved file is not what the user's shell would run as
    ``kiro-cli``: the bare command would then fail or sign in a different
    install. Any other resolution keeps the bare constants: the binary is on
    PATH by construction there.
    """
    bundled = is_bundled_kiro_cli(binary, environ)
    if bundled or _is_off_path_override(binary, environ):
        if platform_compat.IS_WINDOWS:
            # A quoted executable path alone is data in PowerShell; its call
            # operator is required. Wrapping the command in powershell.exe keeps
            # this one display string runnable from both PowerShell and cmd.exe.
            # The user's terminal is outside the gateway's process tree, so the
            # KIRO_NO_AUTO_UPDATE the Electron shell sets does not reach this
            # run; upstream compiles the startup self-update only for Windows,
            # and without the switch a fresh sign-in would rewrite the sealed
            # bundled exe. ``Set-Item`` rather than ``$env:`` because an outer
            # interactive PowerShell interpolates ``$env:`` inside the double
            # quotes before powershell.exe ever sees it.
            quoted = binary.replace("'", "''")
            prefix = (
                'powershell.exe -NoProfile -Command "Set-Item Env:KIRO_NO_AUTO_UPDATE 1; '
                f"& '{quoted}'"
            )
            suffix = '"'
        else:
            prefix = shlex.quote(binary)
            suffix = ""
        return f"{prefix}{_LOGIN_TAIL}{suffix}", f"{prefix}{_SSO_LOGIN_TAIL}{suffix}", bundled
    return KIRO_CLI_LOGIN_COMMAND, KIRO_CLI_SSO_LOGIN_COMMAND, False


def _is_off_path_override(binary: str, environ: Mapping[str, str]) -> bool:
    """Whether *binary* came from ``KIROCREW_KIRO_BIN`` and sits in no ``PATH``
    directory, so a bare ``kiro-cli`` typed in the user's shell would not run it.

    Pure string work, like :func:`is_bundled_kiro_cli`: the callers run on the
    event loop, so no ``which``/stat may happen here. A directory match is the
    whole test -- an override named ``kiro-cli-chat`` inside a ``PATH`` dir is
    left bare, which is the pre-existing behaviour for every ``PATH`` install.
    """
    override = environ.get("KIROCREW_KIRO_BIN", "")
    if not override or not binary:
        return False
    if os.path.normpath(override) != os.path.normpath(binary):
        return False
    binary_dir = os.path.normpath(os.path.dirname(binary))
    path_dirs = {
        os.path.normpath(entry) for entry in environ.get("PATH", "").split(os.pathsep) if entry
    }
    return binary_dir not in path_dirs


# Why ``update_cli`` refuses the desktop app's bundled copy. Shown verbatim in the
# gate's ``cli_update_error``: the binary sits inside a signed, read-only app
# bundle, so an in-place self-update either fails or breaks the app's signature
# seal, and the next app update is what replaces it.
BUNDLED_CLI_UPDATE_REFUSAL = (
    "This kiro-cli ships inside the desktop app and is updated with the app, "
    "not on its own. Update Kiro Crew to get a newer kiro-cli."
)
# The command that updates the CLI in place. Unlike the install/sign-in steps
# above, which Kiro Crew only ever NAMES for the user to run, this one IS run on
# the user's behalf (see :meth:`KiroPrerequisiteService.update_cli`): it is the
# CLI's own self-update subcommand, the same one the auto-update path already
# invokes, so executing it introduces no new privileged surface — it is not a
# remote installer script, just the installed binary updating itself. Offered
# when the installed CLI is too old to expose the ``acp`` subcommand Kiro Crew
# drives every session through. A CODE CONSTANT, never a catalog value: a
# translated command cannot be typed or executed.
KIRO_CLI_UPDATE_COMMAND = "kiro-cli update"


# Compatibility shim, not live state. Nothing performs an operation any more, but an
# OLDER dashboard build reads ``status.operation.status``
# unconditionally in its refetch-interval callback — the optional chain there
# guards ``status``, not ``operation`` — and that callback runs for every user, not
# only the first-run gate. A tab left open across a gateway upgrade would therefore
# throw on its next poll and drop to the root error screen. Serving a permanently
# idle object keeps those tabs working; it can be deleted once no shipped client
# reads the field.
_LEGACY_IDLE_OPERATION: dict[str, str] = {
    "kind": "",
    "status": "idle",
    "message": "",
    "detail": "",
    "url": "",
    "error": "",
}


def legacy_idle_operation() -> dict[str, str]:
    """Return a fresh idle ``operation`` for the pre-upgrade-client shim."""

    return dict(_LEGACY_IDLE_OPERATION)


_MAX_VISIBLE_DETAIL = 4_000


# Marker the offline E2E harness sets on the gateway it spawns. It grants NO
# privilege: the packaged fake ACP backend is launched by the ordinary in-place
# path like any other runnable executable. It is kept purely as a "this gateway
# is a test rig" signal for the harness contract (test/test_harness.py).
FAKE_ACP_TEST_MODE_ENV = "KIROCREW_FAKE_ACP_TEST_MODE"


@dataclass
class PrerequisiteStatus:
    """Last known Kiro CLI readiness state."""

    platform: str
    installed: bool = False
    authenticated: bool = False
    ready: bool = False
    # Something on the host needs an owner-driven repair before ``ready`` can go
    # true. Only the agent-spec overlay sets it — a missing CLI is NOT a repair
    # (the user installs it from OFFICIAL_INSTALL_DOCS_URL, which Kiro Crew has no
    # action for), so a false value here says nothing about ``installed``.
    repair_required: bool = False
    initial_setup_complete: bool = False
    docs_url: str = OFFICIAL_INSTALL_DOCS_URL
    # What the user runs to sign in. Kiro Crew never runs it for them.
    login_command: str = KIRO_CLI_LOGIN_COMMAND
    # The organization-SSO alternative, offered next to ``login_command`` so the
    # tier is an explicit choice rather than whichever option the sign-in page
    # happens to make prominent.
    sso_login_command: str = KIRO_CLI_SSO_LOGIN_COMMAND
    # True when the resolved binary is the desktop app's own bundled copy. The
    # gate uses it to explain why ``login_command`` is an absolute path into the
    # app's resources rather than the bare name the user's shell would resolve
    # (the bundled copy is not on their PATH).
    bundled_cli: bool = False
    # Whether the installed CLI exposes the ``acp`` subcommand every Kiro Crew
    # session is launched through. Defaults True so nothing regresses when the
    # probe cannot answer (a sandbox refusal, a timeout): those are reported by
    # their own fields, and asserting "too old" off a probe that never ran would
    # push the user to update a CLI that is fine. Only a CLEAN "unknown
    # subcommand" verdict from ``acp --help`` sets it False — at which point the
    # CLI runs but cannot start a session, regardless of sign-in, so it narrows
    # ``ready`` exactly like a rejected spec. The remedy is an UPDATE, not a
    # reinstall: the binary is present and only out of date.
    acp_supported: bool = True
    # What the user's CLI is updated with when ``acp_supported`` is False. Unlike
    # ``login_command`` (which Kiro Crew only names), this one is also run FOR the
    # user by ``update_cli`` — it is the CLI's own in-place self-update. Served in
    # the payload so the UI has one source of truth for the string.
    update_command: str = KIRO_CLI_UPDATE_COMMAND
    # Exception / failure text from an ``update_cli`` attempt. Empty when no
    # update was attempted or it succeeded. Shown verbatim, untranslated: it
    # names why the self-update did not complete, which is what a support
    # conversation needs.
    cli_update_error: str = ""
    # A Kiro CLI binary that is present and executable but could not be VERIFIED
    # (verification runs the binary inside the sandbox) is a categorically
    # different condition from a missing binary, and a failed sandbox build
    # carries zero information about whether the CLI is installed. Without these
    # fields the two collapse into ``installed=False`` and the dashboard tells
    # the user to install a CLI that is already there and authenticated.
    sandbox_unavailable: bool = False
    # Host capability independent of Kiro candidate probes. Kiro can delegate
    # to its own sandbox on Windows; other harnesses need a Crew OS backend.
    # This stays "the host has an OS sandbox backend" (the setup screen's copy
    # keys on that exact meaning); whether a session could nonetheless START
    # without one is the separate ``unsandboxed_exec_permitted`` below.
    sandbox_backend_available: bool = False
    # Whether execution is permitted on this host even with NO OS sandbox
    # backend — the platform default (native Windows) or an operator opt-in,
    # the same verdict ``wrap_argv`` gates a backend-less spawn on. A
    # non-enforced harness (Claude Code, KAS) needs no Crew OS mask, so on such
    # a host it can start and finish first-run setup even though
    # ``sandbox_backend_available`` is false; an enforced harness is still held
    # back by ``sandbox_blocked_backends``. False when the host has a backend
    # and does not permit unsandboxed exec, or when the field cannot be read.
    unsandboxed_exec_permitted: bool = False
    # Runtime-enforced harnesses whose credential mask cannot apply at the
    # effective configured tier, even when an OS backend exists.
    sandbox_blocked_backends: list[str] = field(default_factory=list)
    # Machine-readable: "transient" | "foreign_sandbox" | "no_backend". The
    # presentation layer maps this to its own translated remedy copy instead of
    # parsing English prose out of ``sandbox_detail``.
    sandbox_failure_kind: str = ""
    # Technical probe reason, e.g. "unshare(CLONE_NEWNS) failed with errno 1
    # (EPERM)". Names the failing step, so it is shown verbatim, untranslated.
    sandbox_detail: str = ""
    # Machine-readable host mechanism behind a Linux userns denial — one of the
    # sandbox ``REMEDY_*`` tokens, or "" when unknown. Without it the gate could
    # only show the raw errno, which is a dead end: the probe knows the fix is an
    # AppArmor profile and the user cannot tell.
    sandbox_remedy: str = ""
    # The verification probe hit ``_PROBE_TIMEOUT_SECS`` instead of answering. A
    # THIRD condition, distinct from both a missing binary and a sandbox refusal:
    # the spawn was accepted and raised no typed failure, so every ``sandbox_*``
    # field stays empty and none of them can carry it. Without this the timeout
    # collapsed into bare ``installed=False`` with all ``sandbox_*`` empty — a
    # combination that does not merely fail to help, it actively rules out the true
    # cause and sends the operator to reinstall or re-login, neither of which can
    # work on a host whose CLI is installed, signed in, and serving turns the whole
    # time (such a host records ``probe_version`` events in SEL with
    # ``outcome=failed error=timeout`` and nothing else to go on).
    probe_timed_out: bool = False
    # Why the last version probe did not verify the CLI when NONE of the typed
    # conditions above (sandbox refusal, timeout) explains it: the probe's own
    # failure text, or the tail of its output when it exited non-zero. Empty
    # when the probe passed, never ran (no candidate) or a typed field carries
    # the cause. Shown verbatim, untranslated — the desktop's "Setup Check
    # Unavailable" screen otherwise has NOTHING to say about why.
    probe_error: str = ""
    # The failed probe's exit status, ``None`` when it did not exit (never ran,
    # timed out, sandbox refused) or when it passed.
    probe_status: int | None = None
    # Kiro Crew's own agent specs (~/.kiro/agents/kirocrew*.json). ``ready``
    # requires these on disk, not merely a viable binary and a good ``whoami``:
    # without them kiro-cli answers every ``session/set_mode`` with
    # "Mode '<name>' not found", so an install missing one cannot run a single
    # turn. Filenames rather than a bool so the surface can name what is missing.
    missing_agent_specs: list[str] = field(default_factory=list)
    # Exception text from the repair attempt the Refresh / Check again button
    # makes when specs are missing. Empty when no repair was attempted or it
    # succeeded. Shown verbatim, untranslated: it names the failing install step,
    # which is the one thing a support conversation actually needs.
    agent_spec_repair_error: str = ""
    # Kiro Crew's own specs that are PRESENT but which kiro-cli refuses to load.
    # Presence and acceptance are different questions: a spec on disk that the
    # installed kiro-cli rejects is dropped from its agent table entirely, so
    # ``--agent kirocrew`` resolves to the default agent with only a line on
    # stderr — every Kiro Crew MCP server silently absent from the session. That
    # is indistinguishable from a working install to anything that only stats the
    # file, which is why ``missing_agent_specs`` cannot cover it.
    #
    # The oracle is the binary, never a schema mirrored here: kiro-cli is asked
    # whether it accepts each spec, so a future release that changes the spec
    # schema is reported rather than guessed at.
    rejected_agent_specs: list[str] = field(default_factory=list)
    # kiro-cli's own reason for the first rejection above, sanitized. Its message
    # names the offending file and construct, which is the only thing that turns
    # "my agents stopped working" into an actionable report.
    agent_spec_rejection_detail: str = ""


def _platform_label(platform_name: str) -> str:
    if platform_name == "darwin":
        return "macOS"
    if platform_name == "win32":
        return "Windows"
    if platform_name.startswith("linux"):
        return "Linux"
    return platform_name or "Unknown"


def _sanitize_detail(text: str) -> str:
    safe, _ = redact_exfiltration_urls(str(text or ""))
    safe, _ = redact_credentials(safe)
    return safe[-_MAX_VISIBLE_DETAIL:]
