"""Decide when kiro-cli's own internal sandbox confines a spawn instead.

Reads kiro-cli's internal sandbox switch, names the layer that confines a spawn, and
builds the delegated argv. One of the owners ``kiro_crew.sandbox`` is carved into: every
name here stays readable and patchable as ``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.config.paths import config_dir
from kiro_crew.sandbox_argv import (
    _SANDBOX_ARTIFACT_PREFIX,
    _pinned_env_bin,
    _sandbox_env_unset_args,
)
from kiro_crew.sandbox_precreate import _DELEGATED_OVERLAP_LEAF_REASONS
from kiro_crew.sandbox_python_env import resolved_python_env_targets as _resolved_python_env_targets
from kiro_crew.sandbox_tiers import (
    _IN_SANDBOX_MARKER,
    _resolved_install_root_targets,
    _resolved_kiro_agents_targets,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


# ── Backend: macOS sandbox-exec ──

# kiro-cli >= 2.13 ships its own internal agent sandbox, toggled by the
# "sandbox" key in this settings file. On macOS its in-process seatbelt init
# cannot nest inside Kiro Crew's sandbox-exec wrap — the kernel returns EPERM
# even under an (allow default) outer profile. Exactly one sandbox layer can be
# active per spawn, so on macOS the layers are mutually exclusive:
# kiro's internal sandbox ON  -> Kiro Crew's seatbelt OFF for kiro-cli spawns
# kiro's internal sandbox OFF -> Kiro Crew's seatbelt ON (unchanged default)
# (``~/.kiro/settings`` is the kiro-cli backend's own directory, distinct from
# Kiro Crew's data home ``~/.kiro/crew``; the filename is the literal kiro-cli ships.)
_KIRO_INTERNAL_SETTINGS_PATH = "~/.kiro/settings/amazon-internal.json"
_KIRO_INTERNAL_SANDBOX_KEY = "sandbox"

# One loud warning per process for the delegation decision (per-spawn logs
# would spam warm-pool refills); every delegated spawn is still SEL-audited.
_kiro_delegation_warned = False


def kiro_internal_sandbox_enabled() -> bool:
    """True when kiro-cli's own internal agent sandbox is enabled.

    Reads the ``"sandbox"`` key from ``~/.kiro/settings/amazon-internal.json``
    (kiro-cli >= 2.13). Absent file, missing key, or parse failure all return
    False, which keeps Kiro Crew's own sandbox engaged — failure resolves
    toward our audited isolation layer, never toward no isolation.

    Deliberately uncached: it is one small-file read per spawn, and caching
    would make a settings flip require a gateway restart (mirrors the
    uncached ``_resolve_kiro_bin`` rationale).
    """
    # Deferred import: sandbox.py is a low-level leaf that deliberately avoids a
    # top-level dependency on hooks (hooks imports sandbox at call time). The
    # read is routed through hooks.safe_read_file (security-controls): the file
    # is user-writable, so the read gets is_sensitive_path() on the RESOLVED
    # target (a symlink into ~/.aws etc. is refused through the link) plus
    # O_NOFOLLOW against a TOCTOU swap of the final component.
    from kiro_crew.hooks import safe_read_file

    try:
        data = json.loads(safe_read_file(_KIRO_INTERNAL_SETTINGS_PATH))
        if not isinstance(data, dict):
            # Valid-but-non-object JSON ([], "str", null, 123) must also
            # resolve toward Kiro Crew's own sandbox, not raise.
            return False
        return bool(data.get(_KIRO_INTERNAL_SANDBOX_KEY, False))
    except (OSError, ValueError, RuntimeError):
        # OSError covers missing file / EACCES / PermissionError (sensitive
        # or symlinked target refused by hooks); ValueError covers JSON
        # decode; RuntimeError covers home-directory resolution failure.
        # Every failure resolves toward Kiro Crew's own sandbox.
        return False


def kiro_internal_sandbox_switch() -> tuple[str, str]:
    """The settings file and key that toggle kiro-cli's internal sandbox.

    Returns ``(path, key)`` so a diagnostic can name the exact switch an
    operator has to edit — ``kiro_internal_sandbox_enabled`` answers *whether*
    delegation is on but not *where* it was decided, and a caller that spelled
    either half itself would drift silently the day kiro-cli renames one.

    Reads the module globals at call time, so a test that repoints
    :data:`_KIRO_INTERNAL_SETTINGS_PATH` gets its own path back here too.
    """
    return _KIRO_INTERNAL_SETTINGS_PATH, _KIRO_INTERNAL_SANDBOX_KEY


#: The two isolation layers an agent spawn can be wrapped by, as the names a
#: diagnostic prints. Constants rather than literals at each raise site: the
#: layer travels into an exception type, a log line and an operator-facing
#: remedy, and three spellings of the same layer is how a remedy ends up naming
#: the wrong switch.
SANDBOX_LAYER_CREW = "kirocrew"
SANDBOX_LAYER_HARNESS = "harness-internal"


def wrapped_by_crew_sandbox(argv: "Sequence[str]") -> bool:
    """Whether *argv* -- as returned by :func:`wrap_argv` -- runs the child
    through Kiro Crew's OWN sandbox layer.

    Read off the wrapped argv rather than re-deriving the decision from mode +
    platform + settings, because that decision is not a single expression: the
    delegated branch still falls back to Crew's seatbelt when the caller asks for
    path masks a delegated sandbox cannot enforce, the governance floor can clamp
    a requested ``off`` back up, and the audit-or-deny step can refuse a
    delegation after it was chosen. A second copy of that reasoning would answer
    differently from the wrap on exactly the hosts where the answer matters. The
    argv is the wrap's own record of what it did.

    Keys on the two things only Crew's wrappers put in an argv -- the
    ``KIROCREW_SANDBOX_ACTIVE`` env assignment the macOS seatbelt wrap prepends,
    and the generated launcher script the Linux namespace wrap execs. The
    delegated and unconfined paths add neither (they prepend at most ``env -u``
    scrub flags), so this is False for both, which is the point: on those paths
    the only sandbox left in the chain belongs to the harness.
    """
    marker = f"{_IN_SANDBOX_MARKER}="
    for token in argv:
        if not isinstance(token, str):
            continue
        if token.startswith(marker):
            return True
        if os.path.basename(token).startswith(_SANDBOX_ARTIFACT_PREFIX):
            return True
    return False


def sandbox_init_remediation(layer: str, *, corroborated: bool) -> str:
    """What an operator must change to get past a sandbox that will not initialize.

    **The switch that turns a layer OFF is emitted only on a CORROBORATED
    verdict**, and that is the whole shape of this function. The signature that
    reaches the caller is the dead child's own stderr, and that child is the
    unverified binary the sandbox exists to contain: a planted one can print any
    line it likes. A message that answered it with "run
    ``kirocrew config set agent.sandbox off``" would let that binary talk the
    operator into removing the isolation it is running under -- the same hazard
    :func:`launcher_refusal` states for its own callers, answered the same way it
    prescribes. *corroborated* must therefore come from
    :func:`corroborate_launcher_refusal` (a real launcher run around a trusted
    no-op, whose stderr no child wrote), never from the child's text.

    Uncorroborated, the message still names the layer -- that comes from the argv
    Kiro Crew itself built, not from the child -- and routes the operator to the
    check that can reach a verdict, which is where the switch lives.

    Corroboration exists for Crew's Linux launcher only. On macOS the probe
    validates an ``(allow default)`` profile against a fixed system binary while
    the real wrap applies the strict generated one, so a passing probe is not
    evidence that the real wrap works and the uncorroborated branch is the honest
    answer there. Closing that gap is the real-wrap self-test, tracked separately.

    It follows that the HARNESS layer never gets a switch from here at all, whatever
    *corroborated* says: the only trusted run available speaks to Crew's launcher,
    so treating its verdict as evidence about the harness's own sandbox would be a
    cross-layer inference -- and on that branch the harness's sandbox is the only
    isolation the child had.
    """
    if layer == SANDBOX_LAYER_HARNESS:
        # Names NO switch, and *corroborated* cannot change that -- which is the
        # point of reading this branch before that flag. Corroboration re-runs
        # KIRO CREW'S OWN launcher, so a verdict from it is evidence about Crew's
        # layer and says nothing whatever about the harness's internal sandbox.
        # Letting it unlock this switch would be a cross-layer inference: a host
        # that cannot build Crew's namespace would hand the operator the key that
        # turns off the OTHER sandbox -- and on this branch that sandbox is the
        # only isolation the child had, so the one confirmed thing would be that
        # isolation is gone. The remaining evidence is the agent's own output, and
        # the agent is the unverified binary that sandbox exists to contain.
        return (
            "the agent reported its own sandbox refusing, and Kiro Crew did not wrap "
            "this spawn -- so that sandbox is the only isolation this child had. The "
            "report above is the agent's own output and Kiro Crew has NOT confirmed "
            "it; check the host's sandbox support"
        )
    if not corroborated:
        return (
            "Kiro Crew wrapped this spawn in its own OS sandbox, but the refusal above "
            "is the agent's own output and not a verdict on this host -- and Kiro Crew's "
            "own trusted sandbox run covers its Linux launcher only, so it has NOT "
            "confirmed the failure here. Check the host's sandbox support before turning "
            "either layer off"
        )
    return (
        "a trusted launcher run confirms this host refuses Kiro Crew's own OS sandbox: "
        "run `kirocrew config set agent.sandbox off`, which spawns agents unconfined "
        "WHERE GOVERNANCE PERMITS IT -- a governance floor clamps the mode back up and "
        "the request has no effect. Where it does take, it removes Kiro Crew's "
        "OS-level isolation for EVERY agent process (no credential-path masks, no "
        "data-home seal); each unconfined spawn is recorded in the security event log"
    )


def delegated_workspace_exposes_sealed_target(
    work_dir: "str | os.PathLike[str] | None",
) -> str | None:
    """Reason a kiro-cli spawn must be refused because its workspace would leave a
    SEALED target writable, or ``None`` when it may proceed.

    One guard over every sealed target. Each seal is a rule of Kiro Crew's OWN
    launcher: the kiro agents tree (:func:`_resolved_kiro_agents_targets`), whose fork
    and template specs decide what the next spawn may do, plus every data-home leaf in
    :data:`_DELEGATED_OVERLAP_LEAF_REASONS`, each with its own stated reason -- for
    example the strict no-alias config leaf ``cloud.json``, which names the container
    image a Fargate launch runs and therefore the image the task's execution role hands
    the model credential to. The gateway's packaged install tree
    (:func:`_resolved_install_root_targets`) and the Python environment around it
    (:func:`_resolved_python_env_targets`) are targets too: they are the code the
    gateway runs on its next start.

    A spawn delegated to kiro-cli's internal sandbox (macOS with that sandbox
    enabled, every first-party Windows spawn) never passes through that launcher,
    and the delegated sandbox treats the workspace as writable — so a workspace
    that IS, CONTAINS or sits INSIDE any target lets the child rewrite it.
    Refusing here, before the spawn, is the only enforcement point left on those
    paths. Where Kiro Crew's launcher does wrap the child every seal holds
    regardless of workspace, so this returns ``None`` and keeps ``$HOME``-rooted
    workspaces working there.

    Deliberately NOT conditioned on what the config currently CONTAINS. An agent
    does not need to swap a field it can create: given a writable path and no
    Fargate block, it writes a complete one and the owner's next launch runs the
    image it chose. A rule that reads the file's contents has that hole whatever
    the contents are, because the contents are what the attacker supplies.

    Same three-layer comparison as :func:`assert_voice_runtime_outside_agent_workspace`:
    the lexical spelling AND the canonical (``realpath``) spelling of both sides,
    then filesystem identity (``st_dev``/``st_ino``) walked along each side's
    ancestor chain — so a symlinked or junctioned workspace that resolves into
    the agents tree (or that the agents tree resolves into) is caught, not just
    the spelling the caller configured. Runs off the event loop (it stats). A
    workspace or agents dir that cannot be stat'ed fails CLOSED with a reason:
    "cannot verify" is not "does not overlap". Never raises.
    """
    if work_dir is None:
        return None
    delegated = (sys.platform == "darwin" and kiro_internal_sandbox_enabled()) or (
        sys.platform == "win32"
    )
    if not delegated:
        return None
    env_targets = _resolved_python_env_targets()
    install_targets = _resolved_install_root_targets() + env_targets
    targets = _resolved_kiro_agents_targets() + [
        os.path.join(str(config_dir()), leaf) for leaf in _DELEGATED_OVERLAP_LEAF_REASONS
    ]
    if not targets and not install_targets:
        return None

    def _reason(target: str, how: str) -> str:
        # Named per target: the consequences differ, and an operator reading this needs to
        # know which seal they are looking at. Looked up in the same mapping the target list
        # is built from, so a covered leaf cannot render another leaf's consequence.
        named = next(
            (
                reason
                for leaf, reason in _DELEGATED_OVERLAP_LEAF_REASONS.items()
                if _norm(target).endswith(os.sep + os.path.normcase(os.path.normpath(leaf)))
            ),
            None,
        )
        # The install directory refuses only a workspace at or inside it, so the advice
        # for that target is to move the workspace out of it; every other target also
        # refuses a workspace that contains it.
        remedy = f"Choose a workspace that does not contain '{target}'."
        if named is None and target in install_targets:
            kind = "Python environment" if target in env_targets else "install directory"
            named = (
                f"Kiro Crew {kind}",
                "the agent could change the code the gateway runs on its next start",
            )
            remedy = f"Choose a workspace outside '{target}'."
        if named is not None:
            what, consequence = named
        else:
            what = "kiro agents directory"
            consequence = (
                "the agent could rewrite template/fork specs and forge its next session's " "grants"
            )
        return (
            f"workspace '{os.fspath(work_dir)}' overlaps the {what} "
            f"'{target}' ({how}); on this platform the spawn is delegated to kiro-cli's "
            f"internal sandbox, which treats the workspace as writable, so {consequence}. "
            f"{remedy}"
        )

    def _norm(path: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))

    def _spellings(path: str) -> tuple[str, ...]:
        # Lexical first, canonical second; de-duplicated when they coincide.
        return tuple(dict.fromkeys((_norm(path), _norm(os.path.realpath(path)))))

    def _disk_spellings(path: str) -> tuple[str, ...]:
        # The same two spellings WITHOUT case folding, for the identity layer: a
        # case-sensitive share (\\wsl.localhost) reached from Windows does not
        # resolve the lowercased spelling that normcase produces.
        absolute = os.path.normpath(os.path.abspath(path))
        canonical = os.path.normpath(os.path.abspath(os.path.realpath(path)))
        return tuple(dict.fromkeys((absolute, canonical)))

    def _lexically_overlaps(a: str, b: str) -> bool:
        try:
            return os.path.commonpath([a, b]) in (a, b)
        except ValueError:
            # Different drives (Windows): cannot overlap.
            return False

    def _identity_in_ancestor_chain(identity: tuple[int, int], path: str) -> bool:
        current = os.path.abspath(path)
        while True:
            info = os.stat(current)
            if (info.st_dev, info.st_ino) == identity:
                return True
            parent = os.path.dirname(current)
            if parent == current:
                return False
            current = parent

    try:
        raw_work = os.fspath(work_dir)
        work_spellings = _spellings(raw_work)
        work_disk = _disk_spellings(raw_work)
    except Exception:
        return _reason((targets or install_targets)[0], "workspace path could not be resolved")
    # The install directory refuses only a workspace AT or INSIDE it. A workspace that
    # merely contains it (a project holding its own non-editable venv) is allowed: that
    # layout is common, and refusing it would stop every spawn in such a project. On
    # these delegated platforms the install directory inside such a workspace stays as
    # writable as the rest of the workspace.
    for target in install_targets:
        for work in work_spellings:
            for sealed in _spellings(target):
                try:
                    inside = os.path.commonpath([work, sealed]) == sealed
                except ValueError:
                    inside = False
                if inside:
                    return _reason(target, "path")
        # Filesystem identity, one direction only (the install directory at or above
        # the workspace): a case-insensitive volume or a link reaches the same
        # directory under a spelling the lexical check above does not match.
        try:
            for sealed in _spellings(target):
                if not os.path.exists(sealed):
                    continue
                sealed_stat = os.stat(sealed)
                sealed_id = (sealed_stat.st_dev, sealed_stat.st_ino)
                for work in work_spellings:
                    existing = os.path.abspath(work)
                    while not os.path.exists(existing) and os.path.dirname(existing) != existing:
                        existing = os.path.dirname(existing)
                    if _identity_in_ancestor_chain(sealed_id, existing):
                        return _reason(target, "alias")
        except OSError as exc:
            return _reason(
                target,
                "cannot verify: "
                f"{getattr(exc, 'filename', None) or raw_work}: "
                f"{getattr(exc, 'strerror', None) or exc}",
            )
    for target in targets:
        try:
            agents_spellings = _spellings(target)
            agents_disk = _disk_spellings(target)
        except Exception:
            return _reason(target, "sealed target path could not be resolved")
        # Layer 1+2: every spelling of one side against every spelling of the other.
        for work in work_spellings:
            for agents in agents_spellings:
                if _lexically_overlaps(work, agents):
                    return _reason(target, "path")
        # Layer 3: filesystem identity, both directions. Only an EXISTING node can
        # be an alias; a workspace the spawn is about to mkdir has no identity yet
        # and its lexical spellings above are the whole story.
        try:
            if not os.path.exists(raw_work):
                continue
            work_ids = {(s.st_dev, s.st_ino) for s in (os.stat(p) for p in work_disk)}
            for work_id in work_ids:
                for agents in agents_disk:
                    if os.path.exists(agents) and _identity_in_ancestor_chain(work_id, agents):
                        return _reason(target, "alias")
            for agents in agents_disk:
                if not os.path.exists(agents):
                    continue
                agents_stat = os.stat(agents)
                for work in work_disk:
                    if _identity_in_ancestor_chain((agents_stat.st_dev, agents_stat.st_ino), work):
                        return _reason(target, "alias")
        except OSError as exc:
            return _reason(
                target,
                "cannot verify: "
                f"{getattr(exc, 'filename', None) or raw_work}: "
                f"{getattr(exc, 'strerror', None) or exc}",
            )
    return None


def _spawns_kiro_cli(argv: list[str]) -> bool:
    """True when *argv* launches kiro-cli (by basename, the same convention
    as ``_resolve_kiro_bin``).

    Only the kiro-cli spawn may delegate isolation to kiro's internal
    sandbox — every other agent-influenced spawn (e.g. an MCP probe or a
    cron script) has no internal sandbox of its own and MUST keep Kiro Crew's
    wrap regardless of the kiro settings file.
    """
    return bool(argv) and Path(argv[0]).name == "kiro-cli"


def _delegate_to_kiro_internal_sandbox(
    argv: list[str],
    sandbox_level: str,
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
) -> tuple[list[str], str | None] | None:
    """Delegate an explicitly trusted kiro-cli spawn to its internal sandbox.

    This is NOT the forbidden silent unsandboxed fallback: the child still
    runs under kiro-cli's own sandbox. On macOS the delegation is config-driven
    mutual exclusion with Kiro Crew's seatbelt; on Windows it is restricted to a
    positive first-party Kiro backend classification because Kiro Crew has no
    native OS wrapper there. The decision is deterministic (never a reaction to
    a wrap failure), logged loudly once per process, and every delegated spawn
    is SEL-audited on an audit-or-deny basis. If the audit event cannot be
    written, ``None`` tells the caller to continue through the normal sandbox
    policy, which fail-closes on Windows.

    The POSIX env scrub is applied inline. Windows has no ``env -u`` launcher,
    so every production caller must pass :func:`scrub_agent_subprocess_env`'s
    result as the child environment; regression tests pin those call sites.

    Deliberately does NOT resolve the real kiro binary: the launcher shim is
    part of kiro's own sandbox mechanism on this path, so bypassing it here
    would defeat the delegated layer.
    """

    global _kiro_delegation_warned
    try:
        # circular import (pre-emptive, layering): sandbox.py is a low-level
        # leaf imported at module level by many modules including subprocess
        # entry points. A top-level dep on sel would invert the low-level ->
        # high-level layering; deferring to this rarely-taken path keeps
        # sandbox leaf-pure.
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="sandbox",
            agent="system",
            source="sandbox.wrap_argv",
            tool_name=_command_log_label(argv),
            tool_kind="subprocess",
            outcome="delegated",
            resources=(
                "Windows Kiro backend delegation: kiro internal sandbox owns "
                "this spawn; Kiro Crew has no native Windows sandbox backend"
                if sys.platform == "win32"
                else "macOS sandbox mutual exclusion: kiro internal sandbox on -> "
                "Kiro Crew seatbelt off for this kiro-cli spawn"
            ),
            # audit-or-deny: written synchronously; a filesystem failure
            # re-raises so an unaudited delegation can never proceed.
            critical=True,
        )
    except Exception:
        # Fail closed (security-controls): a security delegation that cannot
        # be audited does not happen. The caller continues through Kiro Crew's
        # normal policy: macOS gets its seatbelt; Windows, which has no native
        # backend, raises SandboxUnavailableError rather than run unaudited.
        logger.warning(
            "SEL audit failed for sandbox delegation — refusing unaudited "
            "delegation; falling back to Kiro Crew's sandbox policy",
            exc_info=True,
        )
        return None
    # SEL audit succeeded — delegation is actually proceeding. Only now
    # consume the warn-once flag (a SEL-failed attempt above fell back to
    # seatbelt and must not burn the warning for the first real delegation).
    if not _kiro_delegation_warned:
        _kiro_delegation_warned = True
        if sys.platform == "win32":
            logger.warning(
                "SECURITY: delegating this Windows kiro-cli spawn to kiro-cli's "
                "internal sandbox and skipping Kiro Crew's OS wrapper. Env scrubbing "
                "still applies."
            )
        else:
            # macOS delegation is decided by a settings file, so name it: the
            # operator who has to change this cannot find it from "delegating"
            # alone, and the symptom they arrive with is a denied read of a
            # path OUTSIDE the workspace, which looks like a macOS privacy
            # (TCC) problem and is not one.
            logger.warning(
                "SECURITY: delegating this macOS kiro-cli spawn to kiro-cli's "
                "internal sandbox and skipping Kiro Crew's OS wrapper (%s sets "
                '"%s": true). Env scrubbing still applies. kiro-cli owns file '
                "access for these spawns, so a path its own profile does not allow "
                'fails with "Operation not permitted" regardless of what macOS '
                "privacy settings grant; set that key to false to hand isolation "
                "back to Kiro Crew's profile.",
                _KIRO_INTERNAL_SETTINGS_PATH,
                _KIRO_INTERNAL_SANDBOX_KEY,
            )
    if sys.platform == "win32":
        return list(argv), None
    unset_args = _sandbox_env_unset_args(sandbox_level, strip_python_env, forward_ssh_auth_sock)
    if unset_args:
        return [_pinned_env_bin(), *unset_args, *argv], None
    return list(argv), None


def _command_log_label(argv: list[str]) -> str:
    """Return a fixed, non-sensitive executable class for diagnostics.

    ``wrap_argv`` is a generic boundary: later argv elements routinely contain
    user-controlled paths, URLs, and occasionally transport capabilities. Static
    analysis also correctly treats a list element as able to reach any other
    element. Never send a value taken from that container to a log or SEL event,
    even when the runtime expression selects ``argv[0]``. The fixed labels retain
    enough operational signal without exposing executable paths or arguments.
    """

    if not argv:
        return "unknown"
    name = argv[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if name.endswith(".exe"):
        name = name[:-4]
    if name == "git":
        return "git"
    if name in {"python", "python3", "pythonw", "pythonw3"}:
        return "python"
    if name in {"node", "npm", "npx"}:
        return "node"
    if name in {"kiro", "kiro-cli", "kirocrew"}:
        return "kiro"
    if name in {"bash", "sh", "zsh", "cmd", "powershell", "pwsh"}:
        return "shell"
    if name in {"env", "bwrap", "sandbox-exec", "systemd-run"}:
        return "sandbox-helper"
    return "other"
