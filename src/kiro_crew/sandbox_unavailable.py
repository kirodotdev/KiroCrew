"""What a spawn does when no OS backend can confine it.

The operator's unsandboxed-exec consent, the SSH agent forward, the no-backend
guidance and warnings, the first-party passthrough, and the refusal errors. One of the
owners ``kiro_crew.sandbox`` is carved into: every name here stays readable and
patchable as ``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shlex
import sys

from kiro_crew import platform_compat
from kiro_crew.sandbox_argv import _sandbox_env_scrub_keys, _unset_env_argv
from kiro_crew.sandbox_delegation import _command_log_label
from kiro_crew.sandbox_probe import (
    _PROBE_STEP_MOUNT_PRIVATE,
    _PROBE_STEP_NEWNS,
    _PROBE_STEP_NEWUSER,
    _inside_macos_sandbox,
    _macos_sandbox_state,
    _remedy_for_step,
)
from kiro_crew.sandbox_tiers import (
    _CGROUP_AGENTS_SLICE,
    _IN_SANDBOX_MARKER,
    _SANDBOX_MODE_ALIASES,
    _STANDARD_DIRS,
    _sandbox_policy,
    effective_sandbox_mode,
)

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


def _allow_no_isolation() -> bool:
    """Whether the operator has explicitly opted into running the agent
    subprocess without OS-level credential isolation.

    Read lazily from config to avoid an import cycle with the config loader
    (sandbox.py is a low-level dependency of much of the codebase).
    """
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: sandbox is a low-level dep of config.loader
        )

        return bool(getattr(KiroCrewConfig.load().agent, "sandbox_allow_no_isolation", False))
    except Exception:
        return False


#: ``_unsandboxed_exec_grant()`` verdicts. Kept as constants because the warning
#: text, the SEL ``resources`` string and the tests all have to name the same
#: two cases, and a typo in any one of them would silently read as "not granted".
UNSANDBOXED_BY_OPERATOR = "operator"
UNSANDBOXED_BY_PLATFORM = "platform"


def _allow_unsandboxed_exec() -> bool:
    """Whether execution is permitted when NO sandbox backend is available.

    The one boolean gate ``wrap_argv`` consults, and the seam a test patches to
    pin a host as permitting or refusing. A plain read of
    ``agent.sandbox_allow_unsandboxed_exec``, because that field CARRIES the
    effective policy — :func:`~kiro_crew.config.loader
    .unsandboxed_exec_platform_default` is resolved into it at load time, so an
    undeclared key reads ``True`` on a platform with no installable
    backend and a declared ``false`` reads ``False`` everywhere.

    Deliberately not keyed on whether the operator DECLARED the key: the value
    already carries the effective policy, so every reader takes it from one
    field. A full-document ``KiroCrewConfig.save()`` builds its snapshot through
    ``asdict(self.agent)`` but strips this key unless
    :func:`~kiro_crew.config.loader.unsandboxed_exec_declared` says the operator
    recorded it, so writing the snapshot back leaves "never decided" undecided.

    An unreadable config yields ``False``: a broken config must never be a way to
    obtain a LOOSER sandbox than the operator configured.
    """
    try:
        from kiro_crew.config.loader import (  # circular import: sandbox is a low-level dep
            KiroCrewConfig,
            unsandboxed_exec_platform_default,
        )
        from kiro_crew.edition_locks import unsandboxed_exec_floor

        allow = bool(getattr(KiroCrewConfig.load().agent, "sandbox_allow_unsandboxed_exec", False))
        # Enterprise: a declared ``true`` reads as this platform's default, so local
        # config cannot widen it (RFC enterprise edition, Red "Minimum sandbox level").
        return unsandboxed_exec_floor(allow, unsandboxed_exec_platform_default())
    except Exception:
        return False


def _unsandboxed_grant_source(permitted: bool) -> str:
    """Which permission let a spawn through, for the warning and the SEL event.

    Derived FROM the gate's own verdict rather than resolved independently, so the
    two can never contradict each other about one spawn — including when a test
    patches :func:`_allow_unsandboxed_exec`, where an independent read would
    describe the real host while the gate described the pinned one.

    The split is not cosmetic: it is what lets the audit log say whether a host ran
    unconfined because an operator accepted the risk or because the platform
    default did, and only the operator case survives a later change to that
    default. A declaration is what distinguishes them, so an unreadable config
    reports the platform — the gate can only have said ``True`` there via the
    platform branch anyway.
    """
    if not permitted:
        return ""
    return UNSANDBOXED_BY_OPERATOR if _unsandboxed_exec_key_declared() else UNSANDBOXED_BY_PLATFORM


def _unsandboxed_exec_key_declared() -> bool:
    """Whether the operator declared the opt-in key at all, for message wording.

    Thin, failure-tolerant wrapper over
    :func:`~kiro_crew.config.loader.unsandboxed_exec_declared` so a refusal can
    tell "you set this to false" apart from "you never set it" without a broken
    config turning the refusal itself into an exception. Diagnostic only — it never
    decides whether a spawn runs.
    """
    try:
        from kiro_crew.config.loader import (  # circular import: sandbox is a low-level dep
            unsandboxed_exec_declared,
        )

        return unsandboxed_exec_declared()
    except Exception:
        return False


#: Tiers whose sandbox leaves ``~/.ssh`` readable to the agent child. On these
#: the forward is the default: a child that can already open the private key
#: files gains nothing new from the agent socket except reach to keys that never
#: sit in a readable file (a passphrase, a hardware token, an external agent).
#: Spelled as an ALLOW set so an unknown or newly added tier falls to the consent
#: path rather than forwarding by omission.
_SSH_VISIBLE_TIERS = frozenset({"off", "standard", "cc"})


def _ssh_dir_masked(hidden_dirs: tuple[str, ...] | list[str]) -> bool:
    """Whether any entry of *hidden_dirs* masks ``~/.ssh`` or anything inside it.

    An entry that is ``~/.ssh`` itself, one of its ancestors (a mask over the
    whole home), or a leaf inside it all count: each means the child cannot read
    every key file, so the socket would reach further than the files do.
    """
    ssh_dir = os.path.abspath(os.path.join(os.path.expanduser("~"), ".ssh"))
    for raw in hidden_dirs:
        hidden = os.path.abspath(os.path.expanduser(str(raw)))
        if hidden == ssh_dir:
            return True
        if ssh_dir.startswith(hidden.rstrip(os.sep) + os.sep):
            return True
        if hidden.startswith(ssh_dir + os.sep):
            return True
    return False


def _tier_lists_ssh(tier: str) -> bool:
    """Whether *tier*'s own hidden-dir list names ``.ssh`` or something inside it.

    Read from the same source the confinement plan reads: ``cc`` from the active
    PlatformContext (a companion may extend it), ``standard`` from the module list,
    ``off`` hides nothing.
    """
    if tier == "off":
        return False
    entries = _STANDARD_DIRS if tier == "standard" else _sandbox_policy().cc_dirs()
    for entry in entries:
        head = str(entry).replace("\\", "/").strip("/").split("/", 1)[0]
        if head in (".ssh", "", "~"):
            return True
    return False


def _forward_ssh_auth_sock(mode: str, hidden_dirs: tuple[str, ...] | list[str]) -> bool:
    """Whether this agent spawn keeps ``SSH_AUTH_SOCK`` in its environment.

    The socket grants USE of the operator's ssh-agent keys, not possession. Two
    routes keep it:

    * By default, when the spawn's sandbox leaves ``~/.ssh`` readable: the
      effective tier (after the governed ``sandbox.min_level`` clamp, the same
      one :func:`wrap_argv` applies) is in :data:`_SSH_VISIBLE_TIERS`, that
      tier's own hidden-dir list (``cc`` is a PlatformContext extension
      point) does not name ``.ssh``, AND no
      entry of *hidden_dirs* -- the per-harness credential mask an enforced
      host adds -- covers ``~/.ssh``. Scrubbing the socket there would block
      git-over-SSH while the key files themselves stay open to the child, and
      the ``standard`` tier keeps them visible precisely so git-over-SSH works.
    * Otherwise (``strict``, or a harness whose mask hides ``~/.ssh``), only
      with the operator's recorded consent on the KEYSTONE leaf
      ``ssh_auth_sock_consent.json``. There the socket would reach keys the
      sandbox hides, so it stays an authorization the agent cannot grant
      itself: the OS sandbox mounts the keystone read-only for the agent's
      shell and ``is_sensitive_path`` fences the file tools.

    Fail-closed: any failure to resolve the tier or read consent returns False,
    so the socket is scrubbed. Read lazily to avoid an import cycle with the
    config loader.

    Windows has no ``SSH_AUTH_SOCK`` (Win32 OpenSSH's agent is a named pipe, not
    a Unix-domain socket), so the forward is a no-op there on every route. This
    mirrors ``_resolve_ssh_auth_sock`` returning early on Windows.
    """
    if platform_compat.IS_WINDOWS:
        return False
    try:
        return _forward_for_effective_tier(effective_sandbox_mode(mode), hidden_dirs)
    except Exception:
        return False


def _forward_for_effective_tier(effective: str, hidden_dirs: tuple[str, ...] | list[str]) -> bool:
    """The forward verdict for an ALREADY-CLAMPED tier; reads no governance floor.

    :func:`wrap_argv` calls this with the tier it is about to apply, so a floor
    raised between the launch tail's resolution and the wrap's own floor read
    can only take the forward away, never leave it on a tier that hides
    ``~/.ssh``. Raises on a consent read error; callers fail closed.
    """
    if _forwards_without_consent(effective, hidden_dirs):
        return True
    from kiro_crew import (
        ssh_auth_sock_consent,  # circular import: sandbox is a low-level dep of config.loader
    )

    return ssh_auth_sock_consent.is_granted()


def _forwards_without_consent(effective: str, hidden_dirs: tuple[str, ...] | list[str]) -> bool:
    """Whether an ALREADY-CLAMPED tier keeps the socket with no consent at all.

    The default route of :func:`_forward_for_effective_tier`, alone: the tier
    leaves ``~/.ssh`` readable and no entry of *hidden_dirs* masks it. False
    means the consent leaf decides. Shared with
    :func:`ssh_auth_sock_consent_notice` so the notice names exactly the spawns
    the forward sends to the consent route.
    """
    tier = _SANDBOX_MODE_ALIASES.get(effective, effective)
    return (
        tier in _SSH_VISIBLE_TIERS
        and not _tier_lists_ssh(tier)
        and not _ssh_dir_masked(hidden_dirs)
    )


def ssh_auth_sock_consent_notice(
    backend: str, mode: str, hidden_dirs: tuple[str, ...] | list[str]
) -> str:
    """The operator-facing line for a spawn whose agent socket waits on consent.

    Non-empty only when the forward for this spawn is decided by the
    ``ssh_auth_sock_consent.json`` leaf (``strict``, or a harness whose
    credential mask covers ``~/.ssh``) and that consent is not granted, so the
    child starts without ``SSH_AUTH_SOCK``. The line names the setting and how
    to grant it, because the child itself can only report that no ssh keys are
    available.

    Diagnostic only: it decides nothing. :func:`_forward_ssh_auth_sock` stays
    the one verdict the spawn applies, and this reads the same inputs through
    the same helpers. Any failure to resolve the tier or read consent returns
    ``""`` -- a notice that cannot be computed is omitted, never guessed. The
    text carries no socket path.
    """
    if platform_compat.IS_WINDOWS:
        return ""
    try:
        if _forwards_without_consent(effective_sandbox_mode(mode), hidden_dirs):
            return ""
        from kiro_crew import (
            ssh_auth_sock_consent,  # circular import: sandbox is a low-level dep of config.loader
        )
        from kiro_crew.config.loader import ssh_auth_sock_consent_path

        if ssh_auth_sock_consent.is_granted():
            return ""
        consent_path = ssh_auth_sock_consent_path()
    except Exception:
        return ""
    return (
        f"ssh-agent not forwarded to the {backend} agent: its sandbox hides ~/.ssh, "
        "so SSH_AUTH_SOCK is kept there only with the ssh-agent forward consent, "
        "which is not granted. git over SSH and SSH commit signing inside that "
        'session will report no keys. To allow it, write {"enabled": true} to '
        f"{consent_path} from a terminal outside Kiro Crew; the next {backend} "
        "agent that starts picks it up. The consent applies to every agent whose "
        "sandbox hides ~/.ssh, and lets the commands those agents run use the keys "
        "in your ssh-agent."
    )


def unsandboxed_exec_permitted_by() -> str:
    """Public read of the no-backend execution verdict, for diagnostics.

    The same resolution :func:`wrap_argv` gates on, exposed so ``doctor`` reports
    the policy the spawn path will actually apply instead of re-deriving it from
    the config field — which records only what the operator DECLARED, and reads
    ``False`` both for "locked down" and for "never decided".

    Returns ``UNSANDBOXED_BY_OPERATOR``, ``UNSANDBOXED_BY_PLATFORM`` or ``""``.

    Deliberately does NOT fold in the governance ``sandbox.min_level`` floor: the
    floor is resolved per spawn against the mode that spawn requested, so a single
    host-level answer would be wrong for some of them. A caller that reports this
    verdict must say that a floor overrides it.
    """
    return _unsandboxed_grant_source(_allow_unsandboxed_exec())


def _bundled_cli_invocation() -> str | None:
    """Absolute, shell-quoted path to the CLI this process was started from.

    The AppImage persona is defined by the install guide as needing "no Python,
    pip, npm, or Node" (docs/guides/install.md), so there is usually no
    ``kirocrew`` on their PATH at all — the CLI is bundled INSIDE the AppImage.
    Printing the bare command would hand exactly the affected user a
    ``command not found`` and leave them with only the opt-out, which is the
    opposite of the point.

    ``shutil.which("kirocrew")`` is deliberately NOT trusted as evidence here:
    this string is generated inside the gateway process, which inherits the
    AppImage's own PATH, but it is pasted into the user's shell, which does not.
    A hit would prove the bundle can find its own CLI, not that the user can.

    Returns None when the path cannot be established, so the caller can fall back
    to the bare name rather than print something invented.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0:
        return None
    try:
        resolved = os.path.realpath(argv0)
    except OSError:
        return None
    name = os.path.basename(resolved).lower()
    if not name.startswith("kirocrew") or not os.path.isfile(resolved):
        return None
    return shlex.quote(resolved)


def _apparmor_userns_restricted() -> bool:
    """True when this kernel is the Ubuntu AppArmor userns-restriction case.

    Read straight from /proc rather than importing
    :mod:`kiro_crew.service.apparmor`: ``sandbox`` is a low-level dependency of
    config loading, and pulling the service package in here would create an
    import cycle. One file read, no subprocess.
    """
    try:
        with open(
            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns", encoding="utf-8"
        ) as handle:
            return handle.read().strip() == "1"
    except OSError:
        return False


def _no_backend_guidance() -> str:
    """Remedy text for a genuine no-backend host, specific to WHY it has none.

    The generic "install a sandbox backend, or opt out" advice is actively
    unhelpful on the single most common affected host: stock Ubuntu 23.10+, where
    a backend exists and is one AppArmor profile away from working. Worse, the
    only concrete thing that text suggests is the opt-out, which turns off the
    isolation the message exists to protect.

    The remedy differs by HOW Kiro Crew was launched, so it is named per shape:

    * AppImage / desktop app — nothing applies a profile to a directly launched
      binary, so attach one to it (``kirocrew sandbox install-profile``).
    * anything else on such a host — ``kirocrew service install`` installs a
      profile attached by path to the resolved kirocrew launcher script (never the
      shared interpreter, which would grant unprivileged userns to every Python
      process on the machine). Any launch through that launcher is confined,
      foreground included; other entry points stay unconfined.

    Deliberately does NOT tell the user to set the sysctl to 0: that trades a
    kernel-wide protection for one app's need, and the per-application profile
    exists so they do not have to.

    The ``sandbox_allow_unsandboxed_exec`` opt-out is still named in every case,
    because it is the documented escape hatch and withholding it would leave a
    stuck user with no way out. What changes is the ORDER: on a host where the
    sandbox is one profile away from working, the profile is the remedy and the
    opt-out is the last resort, where the previous text offered the opt-out as
    the only concrete suggestion.
    """
    optout = (
        "As a last resort, agent.sandbox_allow_unsandboxed_exec=true in "
        "~/.kiro/crew/config.json allows unsandboxed execution, but that removes "
        "the isolation this check exists to protect. "
    )
    if sys.platform.startswith("linux") and _apparmor_userns_restricted():
        base = (
            "This host restricts unprivileged user namespaces via AppArmor "
            "(kernel.apparmor_restrict_unprivileged_userns=1, the default on "
            "Ubuntu 23.10+ and derivatives). A sandbox backend DOES exist here — "
            "it needs a per-application AppArmor profile granting 'userns', "
            "exactly as stock Ubuntu already ships for chrome, brave, 1password "
            "and Discord. "
        )
        appimage = os.environ.get("APPIMAGE", "").strip()
        if appimage:
            # Name the CLI by ABSOLUTE PATH, not as `kirocrew`. An AppImage user
            # has no kirocrew on PATH (see _bundled_cli_invocation), so the bare
            # command would fail for exactly the person reading this. The bundled
            # path is valid while the app is running, which is when they will run
            # it, and it is the same binary the desktop app already spawns.
            cli = _bundled_cli_invocation() or "kirocrew"
            where = (
                " (that path is inside the running app, so run it while Kiro Crew " "is open)"
                if cli != "kirocrew"
                else ""
            )
            # shlex.quote, not bare interpolation: this string is printed for the
            # user to paste into a shell, and a filename is attacker-influenced in
            # the cases that matter (a downloaded or unpacked AppImage). An
            # AppImage named `Kiro-Crew-$(...).AppImage` would otherwise have its
            # substitution executed by the paste, turning a diagnostic into a
            # command-injection vector. Mirrors the quoting the desktop side
            # already does in website/electron/sandbox-profile.js.
            return (
                base
                + (
                    "This is an AppImage launch, which no profile is attached to yet. "
                    "Run this in a terminal (it needs sudo, so it cannot be done from "
                    f"the app): {cli} sandbox install-profile --path "
                    f"{shlex.quote(appimage)}{where} — then restart the app. Do NOT "
                    "set the sysctl to 0: that disables a kernel-wide protection for "
                    "every application on the machine. "
                )
                + optout
            )
        return (
            base
            + (
                "Run `kirocrew service install` to install the profile, attached by "
                "path to the kirocrew launcher; any launch through that launcher, "
                "foreground included, is then confined. Do NOT set the sysctl to 0: "
                "that disables a kernel-wide protection for every application on the "
                "machine. "
            )
            + optout
        )
    return (
        "If this host genuinely lacks a sandbox backend, set "
        "agent.sandbox_allow_unsandboxed_exec=true in "
        "~/.kiro/crew/config.json to explicitly allow unsandboxed "
        "execution, or install a supported sandbox backend "
        "(Linux user namespaces, or macOS sandbox-exec). "
    )


def _classify_unavailable(transient: bool) -> str:
    """Name why no backend is available, given an already-read transient flag.

    One implementation of the rule shared by ``wrap_argv``'s
    ``SandboxUnavailableError.kind`` and the public :func:`unavailable_kind`, so
    the two can never drift into disagreeing about the same host.
    """
    if transient:
        return "transient"
    return "foreign_sandbox" if _inside_macos_sandbox() else "no_backend"


def agent_confinement_evidence(cgroup_path: str = "/proc/self/cgroup") -> str | None:
    """Evidence that THIS process runs under agent-shell confinement, or ``None``.

    The runtime half of the operator-attestation check used by authorization
    gates whose input must come from a human at a host terminal and never from
    an agent-spawned process (the app dev-mode out-of-install confirmation), and
    the single confinement probe behind the signing-key ``"masked"`` state
    (``token_secret.signing_key_health``, its doctor and boot-preflight callers).
    Returns a short human-readable reason when there is ANY evidence of
    confinement, ``None`` when there is none.

    Three signals, each read in a way that survives the next one being absent:

    * ``KIROCREW_SANDBOX_ACTIVE`` — exported only by the namespace launcher /
      seatbelt env prefix. Cheap, but ``cli.main()`` pops it before any
      subcommand, so it is an extra, never the sole signal a subcommand sees.
    * the macOS kernel's Seatbelt verdict — the only cheap signal on Darwin,
      which has no cgroup to read.
    * on Linux, membership in the ``kirocrew-agents.slice`` cgroup scope that
      ``cgroup_scope_argv`` places every agent process under. ``cli.main()``
      cannot pop a cgroup, and ``/proc/self/cgroup`` cannot be edited from
      inside, so this is the durable signal that outlives an environment scrub.
      Matched against :data:`sandbox_cgroup._CGROUP_AGENTS_SLICE`, never a
      literal, so a renamed slice moves in one place.

    Deny-direction only, and deliberately so: each signal is unforgeable *in
    the direction of refusal*. A present ``KIROCREW_SANDBOX_ACTIVE`` marker in
    a genuinely unsandboxed process only over-refuses (and proves the marker
    was forged or leaked — see :func:`_macos_sandbox_state`); a kernel verdict
    of "Seatbelt-confined" for a non-KiroCrew sandbox (kiro-cli's internal
    seatbelt, an operator-wrapped process) still means "not a bare operator
    terminal", which is the question being asked. An unreadable or absent
    ``/proc/self/cgroup`` yields no evidence, which is fail-safe here: it only
    withholds the extra reason, never fabricates one, so a confined process is
    never read as unconfined on the basis of this signal (the converse — a
    ``None`` proving a human — is never claimed). That converse is NOT
    guaranteed in general: a ``None`` does not prove a human — an agent can
    strip the marker with ``env -u``, and off a cgroup host Linux offers no
    cheap kernel verdict — which is why callers pair this with a structural
    probe of a sealed artifact (``_CREW_READONLY_LEAVES``) that the OS sandbox
    denies regardless of the environment. Never use this function to *grant*
    anything.
    """
    if os.environ.get(_IN_SANDBOX_MARKER):
        return f"the {_IN_SANDBOX_MARKER} marker is set (agent-sandboxed process)"
    if _macos_sandbox_state() is True:
        return "the kernel reports this process is Seatbelt-confined"
    # The single name for the slice is sandbox_tiers (beside _IN_SANDBOX_MARKER),
    # a lower layer this module already depends on — never a literal.
    try:
        with open(cgroup_path, encoding="utf-8") as fh:
            if _CGROUP_AGENTS_SLICE in fh.read():
                return f"this process is in the {_CGROUP_AGENTS_SLICE} cgroup scope"
    except OSError:
        pass
    return None


def agent_confinement_is_kirocrew_own(cgroup_path: str = "/proc/self/cgroup") -> bool:
    """Whether THIS process is confined by Kiro Crew's OWN sandbox, specifically.

    A strict subset of :func:`agent_confinement_evidence`: it reads only the two
    signals that mean *Kiro Crew built this confinement* — the
    ``KIROCREW_SANDBOX_ACTIVE`` marker and membership in the
    ``kirocrew-agents.slice`` cgroup scope — and deliberately does NOT consult the
    macOS Seatbelt verdict, which is true for ANY Seatbelt profile including one
    Kiro Crew did not create (kiro-cli's own, an operator-wrapped process).

    The signing-key ``"masked"`` state uses it to tell apart the two reasons the
    key read is withheld: inside Kiro Crew's own sandbox the launcher masks the
    key (unreadable where it can, but a *readable* empty stand-in on a Linux host
    whose private-tmpfs mount failed), so the mask must be honoured regardless of
    readability; whereas the Seatbelt-only case cannot prove the mask is ours, so
    that path additionally requires the open to be refused. Fail-safe like its
    sibling: an unreadable/absent cgroup file yields ``False`` (withholds the
    stronger "ours" claim), never a spurious ``True``.
    """
    if os.environ.get(_IN_SANDBOX_MARKER):
        return True
    try:
        with open(cgroup_path, encoding="utf-8") as fh:
            return _CGROUP_AGENTS_SLICE in fh.read()
    except OSError:
        return False


def _warn_mode_off_unconfined(argv: list[str], is_kiro_spawn: bool) -> None:
    """Emit a once-per-process SECURITY warning when mode='off' results in
    no OS-level isolation and no verified delegation.

    This covers the gap where the documented mutual-exclusion invariant (above
    ``_KIRO_INTERNAL_SETTINGS_PATH``) is violated by an explicit mode='off'
    config without the kiro-cli delegation being active.
    """
    # Honour the same acknowledgment as _warn_no_isolation (SEC-009 opt-in).
    if _allow_no_isolation():
        if not getattr(_warn_mode_off_unconfined, "_info_logged", False):
            _warn_mode_off_unconfined._info_logged = True  # type: ignore[attr-defined]
            logger.info(
                "agent.sandbox='off' with no active delegation; operator opted "
                "in via sandbox_allow_no_isolation. Command: %s",
                _command_log_label(argv),
            )
        return

    # Per-branch latch so a non-kiro spawn doesn't suppress the kiro-spawn warning.
    _warned_set: set = getattr(_warn_mode_off_unconfined, "_warned_set", set())

    if is_kiro_spawn and sys.platform == "darwin":
        if "darwin_kiro" in _warned_set:
            return
        _warned_set.add("darwin_kiro")
        logger.warning(
            "SECURITY: agent.sandbox='off' but kiro-cli's internal sandbox is "
            "NOT enabled (~/.kiro/settings/amazon-internal.json). Both isolation "
            "layers are inactive — ~/.aws, ~/.ssh and other secrets are readable "
            "by the agent subprocess and only the bypassable app-level "
            "security.py checks remain. Set agent.sandbox='auto' or enable "
            "kiro-cli's internal sandbox to restore OS-level confinement. "
            "Command: %s",
            _command_log_label(argv),
        )
    elif sys.platform.startswith("linux"):
        if "linux" in _warned_set:
            return
        _warned_set.add("linux")
        logger.warning(
            "SECURITY: agent.sandbox='off' on Linux — there is no kiro-cli "
            "delegation mechanism on this platform, so the agent subprocess "
            "runs with NO OS-level confinement. ~/.aws, ~/.ssh and other "
            "secrets are readable by it and only the bypassable app-level "
            "security.py checks remain. Set agent.sandbox='auto' to engage "
            "namespace isolation. Command: %s",
            _command_log_label(argv),
        )
    elif sys.platform == "win32":
        if "win32" in _warned_set:
            return
        _warned_set.add("win32")
        logger.warning(
            "SECURITY: agent.sandbox='off' on Windows — no OS-level sandbox "
            "backend exists on this platform. The agent subprocess runs with "
            "full filesystem access. Command: %s",
            _command_log_label(argv),
        )
    else:
        if "other" in _warned_set:
            return
        _warned_set.add("other")
        logger.warning(
            "SECURITY: agent.sandbox='off' for a non-kiro-cli subprocess — "
            "running without OS-level confinement. Set agent.sandbox='auto' "
            "to engage seatbelt isolation. Command: %s",
            _command_log_label(argv),
        )

    _warn_mode_off_unconfined._warned_set = _warned_set  # type: ignore[attr-defined]


def _warn_first_party_unconfined_once(argv: list[str]) -> None:
    """One-shot loud SECURITY warning for the first-party no-backend carve-out.

    Per-process sentinel, mirroring :func:`_warn_mode_off_unconfined`'s latch
    style: the trigger is the HOST having no backend, so without the latch every
    managed MCP probe would repeat the same paragraph on every discovery cycle.
    """
    if getattr(_warn_first_party_unconfined_once, "_warned", False):
        return
    _warn_first_party_unconfined_once._warned = True  # type: ignore[attr-defined]
    logger.warning(
        "SECURITY: no OS-level sandbox backend on this host — spawning a "
        "first-party fixed-argv Kiro Crew helper UNCONFINED (its full command "
        "line is derived inside this package with no agent, repo, or "
        "user-config input; the credential environment is scrubbed). "
        "Hostile-input spawn paths are unaffected: they keep failing closed "
        "and still require agent.sandbox_allow_unsandboxed_exec=true. "
        "Command: %s",
        _command_log_label(argv),
    )


def _first_party_no_backend_passthrough(
    argv: list[str], sandbox_level: str, strip_python_env: bool
) -> tuple[list[str], str | None]:
    """Allowed path of the first-party carve-out in :func:`wrap_argv`.

    Reached only when the caller passed ``first_party_fixed_argv=True``, the
    backend unavailability class is ``no_backend``, and no governance
    ``sandbox.min_level`` floor is active (all checked by the caller). Applies
    the same env scrub as the other unconfined-but-deliberate paths, warns
    loudly once per process, and SEL-audits with a DISTINCT third outcome:
    ``unconfined`` — deliberately neither ``denied`` (nothing was refused) nor
    the nested-passthrough ``allowed`` (nothing confines this spawn).

    SEL failure here is log-and-proceed, matching the ``mode="off"`` delegation
    precedent: the spawn is first-party with a package-derived argv, and the
    alternative is bricking built-in tooling on audit hiccups.

    Deliberately NOT ``critical=True``: unlike the fail-closed ``denied`` and
    nested-passthrough ``allowed`` audits — rare, one-per-condition events —
    this fires for every managed-server probe on every discovery cycle of a
    backend-less host, and the critical path drains + flushes SYNCHRONOUSLY on
    the caller's thread, which here is the gateway event loop (async
    ``probe_server``). A best-effort async write keeps the loop responsive; the
    tamper-evident record still lands via the background writer.
    """
    _warn_first_party_unconfined_once(argv)
    try:
        from kiro_crew.sel import sel  # circular import: sandbox is low-level

        sel().log_tool_invocation(
            session_key="sandbox",
            agent="system",
            source="sandbox.wrap_argv",
            tool_name=_command_log_label(argv),
            tool_kind="subprocess",
            outcome="unconfined",
            resources="first-party fixed argv, no sandbox backend (issue #1563 carve-out)",
        )
    except Exception:
        logger.warning(
            "SEL audit failed for first-party unconfined spawn — proceeding "
            "unaudited: the argv is package-derived and denying the spawn "
            "would brick built-in tooling whenever SEL hiccups (matches the "
            "mode=off delegation posture). Command: %s",
            _command_log_label(argv),
            exc_info=True,
        )
    # Same env scrub as the seatbelt / delegation paths, via the trusted
    # absolute-path ``env`` binary (never a PATH-resolved shim). Where no such
    # binary exists — Windows, the main no-backend host — the argv-level scrub
    # cannot run; that is acceptable ONLY because every ratchet-allowlisted
    # caller routes through ``sandboxed_spawn_argv``, whose ``scrub_env`` drops
    # a superset of these keys from the child environment it returns.
    scrub_keys = _sandbox_env_scrub_keys(sandbox_level, strip_python_env)
    if scrub_keys:
        env_argv = _unset_env_argv(tuple(scrub_keys))
        if env_argv is not None:
            return [*env_argv, *argv], None
        logger.warning(
            "first-party unconfined spawn: no trusted `env` binary for the "
            "argv-level scrub; relying on the chokepoint's scrub_env for the "
            "child environment"
        )
    return list(argv), None


#: Env marker the cron *script* launcher sets on its child -- the one way to tell
#: that child apart at a spawn site every caller shares.
CRON_SCRIPT_CHILD_ENV = "_KIROCREW_CRON_SCRIPT_CHILD"


class UnauditedSpawnRefused(BaseException):
    """A cron script child refused to proceed after an ENOSYS audit failure.

    ``BaseException`` because it is raised inside the user function's own stack
    (``ctx.call_tool`` re-enters ``wrap_argv``), and a script's own ``except
    Exception`` must not be able to swallow it and return a success envelope.
    """


# Named by the path callers import it from: an error chain renders each raised class
# as ``module.qualname`` (``subagent._describe_exception``), and that text reaches the
# Subagents panel and tracebacks, so this module's name must not leak into it.
UnauditedSpawnRefused.__module__ = "kiro_crew.sandbox"


def refuse_unaudited_on_dead_fs(exc: BaseException, what: str) -> None:
    """Turn a best-effort audit degrade into a refusal, for a cron script child.

    Log-and-proceed is right for the gateway: denying a spawn on an audit hiccup
    would brick built-in tooling and every in-sandbox MCP call. It is wrong for a
    detached cron child, whose ``ENOSYS`` write means its filesystem is gone, so it
    can neither audit nor persist what it does next. Both conditions are required.
    """
    if os.environ.get(CRON_SCRIPT_CHILD_ENV) != "1":
        return
    # SEL wraps its writes, so the errno can sit a link or two down the chain;
    # ``seen`` bounds a cyclic one.
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, OSError) and cur.errno == errno.ENOSYS:
            raise UnauditedSpawnRefused(
                f"{what}: the security-event log write failed with ENOSYS (errno 38), "
                "so this cron child can neither audit nor persist -- refusing to "
                "continue unaudited."
            ) from exc
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__


class SandboxUnavailableError(RuntimeError):
    """``wrap_argv`` fail-closed because this host could not build a sandbox.

    A typed error so a caller can tell "the sandbox refused this spawn" apart
    from any other spawn failure **structurally**, instead of inferring it from
    host capability or pattern-matching English prose. That distinction matters:
    verification is not sandboxed on every platform (``_run_process`` skips the
    wrap on Windows) and the ``sandbox_allow_unsandboxed_exec`` opt-in bypasses
    it entirely, so "this host has no backend" does NOT imply "the sandbox is
    why this particular spawn failed". Reporting it that way would recreate the
    misdiagnosis class of #613 on a different platform.

    Subclasses ``RuntimeError`` so existing ``except RuntimeError`` handlers keep
    working unchanged.

    ``kind`` is machine-readable so a presentation layer can select its own
    translated remedy copy: ``"transient"`` (momentary resource pressure — not
    cached, retrying works, and callers must NOT advise disabling the sandbox),
    ``"foreign_sandbox"`` (an outer Seatbelt sandbox Kiro Crew did not create
    already confines this process and Seatbelt cannot nest — this host's sandbox
    is fine), or ``"no_backend"`` (the host genuinely offers no mechanism).

    ``detail`` is the technical probe reason, which names the failing step (e.g.
    ``"unshare(CLONE_NEWNS) failed with errno 1 (EPERM)"``).

    ``remedy`` is a machine-readable ``REMEDY_*`` token naming the host mechanism
    behind a Linux userns denial (``""`` when unknown), so a presentation layer
    can render the concrete fix for that mechanism rather than a bare errno.
    """

    def __init__(self, message: str, kind: str, detail: str, remedy: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail
        self.remedy = remedy


# Named by the path callers import it from: an error chain renders each raised class
# as ``module.qualname`` (``subagent._describe_exception``), and that text reaches the
# Subagents panel and tracebacks, so this module's name must not leak into it.
SandboxUnavailableError.__module__ = "kiro_crew.sandbox"


#: Leading text of every line the Linux launcher writes when it ends WITHOUT
#: exec'ing its child: ``_mount_or_die`` and the seccomp/prctl installs say
#: ``sandbox: BLOCKED``, the two unshare steps say ``sandbox: unshare(``, a
#: broken parent/child handshake, an unreadable ``known_hosts`` or a command
#: the kernel will not exec says ``sandbox: FATAL``, and a launcher invoked
#: with no command says ``sandbox_launcher:``. The launcher is the only Kiro
#: Crew code that writes a line shaped this way to a child's stderr, and it
#: exits right after, so a refused child's captured output carries exactly one
#: of these. Public because
#: it is the ONE definition every consumer keys on — :func:`launcher_refusal`
#: here and the clone probe's exit classifier in
#: ``apps/builtins/auto_improvement/backend/clone_setup.py`` — and each prefix
#: is pinned against the generated launcher by a round-trip test so the tuple
#: cannot drift from the script that emits it. ``sandbox: WARNING`` is
#: deliberately absent: the launcher warns and then still runs the child.
LAUNCHER_EXIT_PREFIXES = (
    "sandbox: BLOCKED",
    "sandbox: FATAL",
    "sandbox: unshare(",
    "sandbox_launcher:",
)
_LAUNCHER_REFUSAL_RE = re.compile(
    r"^sandbox: (?:BLOCKED (?:--|—) )?(?P<what>.+?) failed: errno (?P<errno>\d+)"
)
#: The launcher's failed-exec line, ``sandbox: FATAL — cannot exec <command>
#: (<reason>)``, matched against one line of stderr. The sandbox was built and the
#: command did not start inside it, so no reader takes the line for a refusal:
#: :func:`launcher_refusal` returns ``None`` for it, and the worktree handler in
#: ``dashboard/handlers/worktree.py`` reads it as git's own failure. Both match it
#: before reading anything else in the line, because the command's path can hold any
#: words. The dash is any one token: the launcher inherits the gateway's locale, and
#: where stderr is not UTF-8 the dash arrives as the escape for U+2014.
LAUNCHER_EXEC_FAILURE_RE = re.compile(r"^sandbox: FATAL \S+ cannot exec ")


def launcher_refusal(output: str) -> tuple[str, str, str] | None:
    """Classify the launcher's OWN refusal line in a refused child's output.

    ``wrap_argv`` raises :class:`SandboxUnavailableError` when the sandbox
    cannot be built BEFORE the spawn. The launcher can also refuse AFTER it — a
    host that passed the probe can still deny a control at spawn time, and a
    probe result cached under one policy can outlive a policy change. That
    refusal reaches the caller only as a non-zero exit with the launcher's line
    in the captured stderr, which every caller so far read as "the child is
    broken": a present, signed-in kiro-cli was reported as not installed because
    a container refused the launcher's first mount.

    Returns the same ``(kind, detail, remedy)`` triple the typed error carries,
    or ``None`` when *output* holds no launcher refusal. Keys ONLY on the
    launcher's fixed prefixes, never on host capability.

    **The line is untrusted, and this function is the CLASSIFIER, not the
    verdict.** The child the launcher would have exec'd is the unverified
    candidate itself, running inside the sandbox this module built, and a
    planted binary can print the same prefix to stderr and exit 1. A caller that
    reported this triple as a sandbox verdict would let that binary make the
    first-run gate announce "sandbox unavailable" and hand the operator the
    opt-out that disables the isolation it is running under. Callers deciding a
    verdict use :func:`corroborate_launcher_refusal`, which re-runs the real
    launcher around a trusted no-op with the refused spawn's own options and
    classifies THAT run's own stderr through this function; the candidate's line
    only decides whether to make that trusted run. This function is for
    messaging (a diagnostic that quotes the line) and for that corroboration's
    hint.

    * A refused ``mount(2)`` or ``unshare(2)`` is ``no_backend`` — the host
      cannot build the sandbox — and its errno is classified exactly as the
      probe classifies the same step, so the remedy names the same mechanism
      (a hiding mount refused with EACCES is the same policy that refuses the
      propagation mount, so it shares :data:`REMEDY_MOUNT_DENIED`).
    * The seccomp/prctl installs are ``no_backend`` with no mechanism token: the
      host offers no way to apply the filter.
    * A broken parent/child handshake (``FATAL ... did not publish``) is
      ``transient``: the pipe protocol failed, which says nothing about the
      host and self-heals on the next spawn.
    * A refusal about HOST STATE is NOT a sandbox failure at all — a hardlinked
      credential, an unreadable ``known_hosts``: the sandbox worked and found
      something it must not paper over. A command the kernel will not exec is
      the same kind: the sandbox was built, and the command is missing or not
      executable inside it. Reported as ``None`` so the launcher's
      line reaches the operator as the child's own complaint, which names the
      file to fix. A launcher invoked with no command (``sandbox_launcher:``)
      is the caller's defect, ``None`` for the same reason. Advisory
      ``sandbox: WARNING`` lines never match: the launcher continued past them.
    """
    for raw in output.splitlines():
        line = raw.strip()
        if not line.startswith(LAUNCHER_EXIT_PREFIXES):
            continue
        if line.startswith("sandbox_launcher:"):
            return None
        if line.startswith("sandbox: FATAL"):
            # A failed exec's line carries the command's own path, which can hold any
            # words, the handshake's among them, so that line is matched first.
            if LAUNCHER_EXEC_FAILURE_RE.match(line):
                return None
            return ("transient", line, "") if "did not publish" in line else None
        if "hardlink" in line:
            return None
        match = _LAUNCHER_REFUSAL_RE.match(line)
        if match is None:
            return ("no_backend", line, "")
        what = match.group("what")
        err = int(match.group("errno"))
        if what.startswith("unshare(NEWUSER)"):
            step = _PROBE_STEP_NEWUSER
        elif what.startswith("unshare(NEWNS)"):
            step = _PROBE_STEP_NEWNS
        else:
            step = _PROBE_STEP_MOUNT_PRIVATE
        return ("no_backend", line, _remedy_for_step(step, err))
    return None


#: Wall-clock ceiling for the trusted corroboration run. The child is ``true``,
#: which returns at once, so the budget is for the launcher's own setup (the
#: namespace unshares, the propagation mount, the seccomp install) and a
#: momentarily loaded host, and it bounds the worker thread so a wedged launcher
#: cannot stall the caller.
_TRUSTED_LAUNCHER_TIMEOUT_SECS = 15.0
