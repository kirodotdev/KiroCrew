"""Data-home and workspace locations derived from ``config_dir()`` at call time.

The pure primitives live in the leaf :mod:`kiro_crew.config.paths`. An owner of the
:mod:`kiro_crew.config.loader` facade; it never imports the facade.
"""

from __future__ import annotations

import json
import logging
import os
import stat as _stat
from pathlib import Path

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.config.paths import (
    _WORKSPACE_DIR_NAME,
    OUTBOX_DIR_NAME,
    _default_workspace_base,
    _safe_dir_name,
    config_dir,
    config_package_dir,
)
from kiro_crew.config.sections import WorkspaceConfig
from kiro_crew.user_json import strip_utf8_bom

logger = logging.getLogger("kiro_crew.config.loader")


def _workspace_dir_file() -> Path:
    """Return the path to the saved workspace_dir file, respecting KIROCREW_HOME."""
    return config_dir() / "workspace_dir"


def normalize_workspace_path(raw: str) -> Path:
    """Drop ONE symmetric outer quote pair (keeping its inside verbatim), expand ``~``."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        raw = text[1:-1]
    try:
        return Path(raw).expanduser()
    except RuntimeError:  # ``~unknown-user``: stays relative, so callers fall back
        return Path(raw)


def _resolve_workspace_root(root: Path, *, create: bool = True) -> Path:
    """Realpath-normalize a workspace root, by default after ensuring it exists.

    On hosts with a symlinked ``$HOME``/workspace path (e.g. ``/home/<u> ->
    /local/home/<u>``, ``/home/<u>/workplace -> /workplace/<u>``) the symlink-form
    root and its resolved form name the same directory via different strings. The
    per-session work_dir built from this root is passed as the spawn cwd and
    persisted as ``cwd`` in session_map.json. If the stored cwd is the symlink form
    while the transcript is written under the resolved form, cold resume misses and
    silently falls back to a fresh session.

    Normalizing here, at the single source, makes the SAME resolved path flow into
    spawn cwd and the persisted session_map cwd so write and resume always agree.
    This mirrors the existing ``os.path.realpath`` in ``default_project_dir``.

    ``create=False`` is for a read-only caller (the doctor) that must not leave a
    workspace tree behind on a host where no gateway ever ran; realpath of a
    missing path resolves the components that do exist.
    """
    if not root.is_absolute():
        # A relative root would be created under whatever CWD this process has.
        logger.warning("workspace root %r is not absolute; using the default", str(root))
        root = _default_workspace_base() / _WORKSPACE_DIR_NAME
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return Path(os.path.realpath(str(root)))


def workspace_root(*, create: bool = True) -> Path:
    """Return the top-level workspace root for LLM sessions and tasks.

    Resolution order:
    1. ``KIROCREW_WORKSPACE`` env var (no subdirectory appended)
    2. Saved path in ``config_dir()/workspace_dir`` (written by ``kirocrew setup``)
    3. Platform default with ``kirocrew-workspace`` subdirectory

    Values are unquoted and ``~``-expanded; a non-absolute root is replaced by (3).
    The chosen root is realpath-normalized (see ``_resolve_workspace_root``) so
    sessions resume correctly on hosts with a symlinked home/workspace path. It is
    created unless ``create=False``, which only resolves the configured path.
    """
    override = os.environ.get("KIROCREW_WORKSPACE")
    if override:
        return _resolve_workspace_root(normalize_workspace_path(override), create=create)
    if _workspace_dir_file().is_file():
        try:
            saved = _workspace_dir_file().read_text(encoding="utf-8").strip()
            if saved:
                return _resolve_workspace_root(normalize_workspace_path(saved), create=create)
        except OSError:
            pass
    base = _default_workspace_base()
    return _resolve_workspace_root(base / _WORKSPACE_DIR_NAME, create=create)


def _session_work_dir(session_key: str | None) -> Path:
    """Return a per-session subdirectory under workspace_root()."""
    root = workspace_root()
    if session_key:
        return root / _safe_dir_name(session_key)
    return root / "_default"


def outbox_dir() -> Path:
    """Return the outbox directory for agent-to-user file delivery."""
    d = workspace_root() / OUTBOX_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def read_config_text(path: Path) -> str:
    """The text of ``config.json`` / ``config.local.json``, without a leading BOM.

    Both files are hand-edited, and an editor that saves "UTF-8 with BOM" puts a
    U+FEFF in front that ``json.loads`` refuses. The mark is dropped by
    :func:`kiro_crew.user_json.strip_utf8_bom`, the one place that already does it
    for the other user-owned JSON files. Writers keep emitting plain UTF-8, so a
    locked write removes the mark. Every in-tree reader of the live files comes
    through here (``test_config_json_bom.test_every_config_reader_tolerates_a_bom``
    pins it); readers of COPIES of them -- the pod seed and export bundles -- and
    ``agent._load_json`` strip the same mark with
    :func:`~kiro_crew.user_json.strip_utf8_bom` directly.
    """
    return strip_utf8_bom(path.read_text(encoding="utf-8"))


def overlay_pins(*key_path: str) -> bool:
    """Whether ``config.local.json`` sets the key at *key_path*.

    That overlay deep-merges OVER ``config.json``, so a Settings switch that
    writes the BASE file snaps back to the overlay's value after a successful
    write. A surface reporting this can say why instead of looking broken.

    Best-effort: an unreadable, non-UTF-8 or malformed overlay reports "not
    pinned" rather than raising, matching the loader itself (it warns and marks
    the file degraded). The effective value a caller reports is authoritative
    either way. One helper rather than one per key: the shadowing mechanism is
    the overlay, not the key.
    """
    try:
        path = config_local_path()
        if not path.is_file():
            return False
        # ValueError covers JSONDecodeError AND the UnicodeDecodeError a file
        # saved in a non-UTF-8 code page raises.
        node: object = json.loads(read_config_text(path))
    except (OSError, ValueError):
        return False
    for key in key_path[:-1]:
        if not isinstance(node, dict):
            return False
        node = node.get(key)
    return isinstance(node, dict) and key_path[-1] in node


def config_local_path() -> Path:
    """Return path to config.local.json — an overlay whose keys win over config.json."""
    return config_dir() / "config.local.json"


def denied_commands_path() -> Path:
    """Return path to denied_commands.json — the denied-command opt-out state.

    This is a KEYSTONE trust-root file (on ``security._SENSITIVE_HOME_DIRS``):
    it holds ``{disable_all, disabled_ids, user_added}``, the user's opt-out from
    the built-in deny ceiling. It lives OUTSIDE the agent-readable
    ``config.json`` precisely so an auto-approved/YOLO agent shell cannot write
    it (via any shell trick) and disable its own deny ceiling. Only the operator
    edits it out-of-band — through the dashboard ``/api/security/…`` endpoints,
    which do not route through the agent tool gate. Respects ``KIROCREW_HOME``.
    """
    return config_dir() / "denied_commands.json"


def registry_trust_path() -> Path:
    """Return path to registry_trust.json — operator grants of ``owner`` trust to
    hand-configured app registries.

    This is a KEYSTONE trust-root file (on ``security._SENSITIVE_HOME_DIRS``),
    for the same reason as :func:`denied_commands_path`: a grant lets the
    registries it names have their apps cloned with the machine's git identity,
    so it must be a decision only the operator can make. ``config.json`` is
    agent-writable (any shell form), which is exactly why the registry rows there
    can never carry the tier themselves; the grant lives here, out of the agent's
    reach, and the operator edits it through the dashboard
    ``/api/security/trusted-registries`` endpoints. Holds
    ``{"version": 1, "owner_trusted": ["<credential-free repo url>", ...]}``.
    Respects ``KIROCREW_HOME``.
    """
    return config_dir() / "registry_trust.json"


def computer_use_state_path() -> Path:
    """Return path to computer_use.json — the computer-use primary enable.

    Same KEYSTONE reasoning as :func:`denied_commands_path`, and the leaf is on
    ``security._CREW_SECRET_LEAVES`` for the same reason: enabling computer use
    grants full desktop observation plus input synthesis into the operator's real
    applications, which is a security ceiling, not a preference. Keeping it out
    of the agent-readable ``config.json`` is what makes it un-flippable by a
    prompt-injected agent — ``is_sensitive_path`` blocks the tool path and the
    OS sandbox mounts the keystone read-only for the agent's shell.

    Holds ``{enabled, allowed_apps, extra_denied_apps}``; every read fails soft
    to DISABLED (see ``computer_use.enable_state``). The only writer is the
    dashboard ``/api/computer-use/config`` PUT, which does not route through the
    agent tool gate. Respects ``KIROCREW_HOME``.

    Note the deliberate asymmetry with the ``computer_use`` section of
    ``config.json``: that section carries display/limit knobs ONLY and has no
    ``enabled`` field, precisely so there is exactly one place the feature can be
    turned on and it is not one the agent can reach.
    """
    return config_dir() / "computer_use.json"


def oauth_endpoints_path() -> Path:
    """Return path to oauth_endpoints.json — the operator OAuth-endpoint extension.

    Same KEYSTONE reasoning as :func:`denied_commands_path` and
    :func:`computer_use_state_path`, and the leaf is on
    ``security._CREW_SECRET_LEAVES`` for the same reason: each listed endpoint
    widens the banner-only OAuth entropy carve-out (``security.py``'s
    ``_OAUTH_AUTHORIZATION_ENDPOINTS``), so an agent that could write this file
    could exempt an attacker-controlled host from the exfiltration heuristics —
    it is a trust boundary, not a preference. ``is_sensitive_path`` blocks the
    tool path and the OS sandbox mounts the keystone read-only for the shell.

    Holds ``{"additional_authorization_endpoints": [{"host": …, "path": …}]}``;
    every read fails soft to an EMPTY extension set (see
    ``security._load_operator_oauth_endpoints``). There is no dashboard writer:
    the operator hand-edits the file out-of-band. Respects ``KIROCREW_HOME``.
    """
    return config_dir() / "oauth_endpoints.json"


def aws_consent_path() -> Path:
    """Return path to aws_service_consent.json — paid-AWS-service consent.

    Same KEYSTONE reasoning as :func:`computer_use_state_path`, and the leaf is
    on ``security._CREW_SECRET_LEAVES`` for the same reason: a recorded consent
    to call a PAID AWS service is an authorization, not a preference. Storing it
    in ``config.json`` would leave it writable by any auto-approved agent shell,
    so a prompt-injected agent could mint the grant and consent, on the
    operator's behalf, to spending the operator's money in an account it picked.
    ``is_sensitive_path`` blocks the tool path and the OS sandbox mounts the
    keystone read-only for the shell.

    Holds ``{"<service>": {profile, region, account, arn, granted_at}}``; every
    read fails soft to NO CONSENT (see ``aws_consent.read_grant``). The writer is
    the authenticated dashboard ``/api/aws/consent`` handler, which opens the path
    directly rather than through this gate. Respects ``KIROCREW_HOME``.
    """
    return config_dir() / "aws_service_consent.json"


def decisions_consent_path() -> Path:
    """Return path to decisions_consent.json — consent to send conversation state to Jev.

    Same KEYSTONE reasoning as :func:`aws_consent_path`, and the leaf is on
    ``security._CREW_SECRET_LEAVES`` for the same reason: enabling the decision
    seam sends message text and skill descriptions to an external, paid provider,
    which is an egress authorization, not a preference. Kept out of the
    agent-writable ``config.json`` so a prompt-injected shell cannot flip it and
    have the live config watcher start the egress; ``is_sensitive_path`` blocks
    the tool path and the OS sandbox mounts the keystone read-only for the shell.

    Holds ``{"enabled": bool, "endpoint": str}`` -- the switch and the provider
    address the owner consented to; every read fails soft to NOT CONSENTED (see
    ``decisions.consent``). The only writer is the authenticated, browser-only
    dashboard ``PUT /api/decisions/consent`` handler, which opens the path
    directly rather than through this gate. Respects ``KIROCREW_HOME``.

    The ``decisions`` section of ``config.json`` deliberately carries NO
    ``enabled`` field: sampling share and provider knobs only, so there is exactly
    one place the seam can be switched on and it is not one the agent can reach.
    """
    return config_dir() / "decisions_consent.json"


def file_delivery_consent_path() -> Path:
    """Return path to file_delivery_consent.json -- flagged-file delivery consent.

    Same KEYSTONE reasoning as :func:`aws_consent_path`, and the leaf is on
    ``security._CREW_SECRET_LEAVES`` for the same reason: a recorded consent to
    deliver a file the credential scanner flagged is an authorization, not a
    preference. Storing it in ``config.json`` would leave it writable by any
    auto-approved agent shell, so a prompt-injected agent could mint the grant and
    consent, on the owner's behalf, to shipping the owner's secrets -- the exact
    shape ``CredentialPolicy.exempt_exact_hosts`` refuses when it says such a set
    is "NEVER sourced from ``config.json``". ``is_sensitive_path`` blocks the tool
    path and the OS sandbox mounts the keystone read-only for the shell.

    Holds ``{"<destination_class>": {destination_class, granted_at}}``; every read
    fails soft to NO CONSENT (see ``file_delivery_consent.read_grant``). The only
    writer is the authenticated, OWNER-gated dashboard
    ``/api/file-delivery/consent`` handler, which opens the path directly rather
    than through this gate. There is deliberately NO CLI verb -- a terminal
    command that records a grant on request is a grant an automated caller can
    take. Respects ``KIROCREW_HOME``.
    """
    return config_dir() / "file_delivery_consent.json"


def credential_redaction_path() -> Path:
    """Return path to credential_redaction.json -- the credential-redaction switch.

    Same KEYSTONE reasoning as :func:`file_delivery_consent_path`, and the leaf
    is on ``security._CREW_SECRET_LEAVES`` for the same reason: turning the
    credential scrubber OFF is an authorization, not a preference. Stored in the
    agent-readable ``config.json`` it would be writable by any auto-approved agent
    shell, so a prompt-injected agent could switch off the very pass that keeps
    the secrets it can read out of the owner's dashboard file viewer (the one
    surface the switch governs). ``is_sensitive_path`` blocks the tool path and
    the OS sandbox mounts the keystone read-only for the shell.

    Holds ``{"enabled": bool, "changed_at": str}``; a missing, unreadable or
    malformed file reads as ENABLED (see ``security.redaction_switch``), so the
    fail direction is always "keep redacting". The only writer is the
    authenticated, OWNER-gated dashboard ``/api/security/credential-redaction``
    handler. Respects ``KIROCREW_HOME``.
    """
    return config_dir() / "credential_redaction.json"


def ssh_auth_sock_consent_path() -> Path:
    """Return path to ssh_auth_sock_consent.json -- the SSH-agent forward consent.

    Same KEYSTONE reasoning as :func:`aws_consent_path` and
    :func:`file_delivery_consent_path`, and the leaf is on
    ``security._CREW_SECRET_LEAVES`` for the same reason: keeping
    ``SSH_AUTH_SOCK`` in the agent subprocess environment grants USE of the
    operator's ssh-agent keys -- signing, git-over-SSH, and any authentication
    the socket reaches -- for the lifetime of the session. That is an
    authorization, not a preference. Stored in the agent-readable ``config.json``
    it would be writable by any auto-approved agent shell, so a prompt-injected
    agent could flip its own forwarding on and a subagent it spawns would then
    authenticate as the operator with keys the sandbox exists to keep out of its
    reach. ``is_sensitive_path`` blocks the tool path and the OS sandbox mounts
    the keystone read-only for the agent's shell, so the consent is
    un-flippable from inside the sandbox.

    Holds ``{"enabled": bool, "granted_at": str}``; every read fails soft to
    DISABLED (see ``ssh_auth_sock_consent.is_granted``). The only writer is the
    authenticated, OWNER-gated dashboard handler, which opens the path directly
    rather than through this gate. There is deliberately NO CLI verb -- a
    terminal command that records the grant on request is a grant an automated
    caller can take. Respects ``KIROCREW_HOME``.
    """
    return config_dir() / "ssh_auth_sock_consent.json"


def read_local_secret(port: int, dial_host: str | None = None) -> str:
    """Read the internal-API credential for the gateway on *port*.

    Single home for the secret read that callers (cron scripts, MCP tool bridges,
    CLI) need to authenticate to the gateway's internal API. Returns empty string
    when no credential can be read.

    Resolution depends on whether the caller can name the ADDRESS it dials:

    * With *dial_host* -- the loopback host the caller is about to send the
      credential to (``127.0.0.1``, ``localhost``, ``::1``) -- the credential is
      resolved per LISTENER: :func:`run_marker.read_listener_secret` returns the
      value published under every loopback family that host reaches. When it
      returns a value, that value is used. When it returns nothing, the answer
      turns on WHY:

      - The gateway published NO listener entry for this port at all
        (:func:`run_marker.has_listener_entries` is false) -- an older gateway,
        or one that could not name its bound address. There is no OTHER
        listener's credential on this port to confuse it with, so this falls back
        to the port-keyed read and then the shared file, exactly as an
        address-less caller does. This is the pre-per-listener world, not the
        desync, and withholding the credential here would stop an ordinary
        install from authenticating at all.
      - Listener entries EXIST but none covers the family *dial_host* reaches.
        A gateway bound some address other than the one being dialled, so the
        port-keyed read could hand back a DIFFERENT listener's credential -- the
        exact desync this issue closes. This FAILS CLOSED (returns ``""``): no
        fallback, because the fallback is the bug.

    * Without *dial_host* -- a caller that structurally cannot name an address --
      resolution is per LISTENER for the port only: ``run/gateway-<port>.secret``,
      then the shared ``.local_secret``. This is the pre-existing behaviour, kept
      for the callers section 12.1 names as resolving a port and nothing finer.

    Every reader that constructs a loopback URL a line or two away from this call
    SHOULD pass that URL's host as *dial_host*, so the credential is paired to the
    listener it will actually reach. *port* stays REQUIRED regardless: the
    credential is a function of the dial target, so inferring the target here
    would let a caller dial one gateway while authenticating for another -- the
    exact desync, reintroduced one call site at a time and invisible at the call
    site.
    """
    # Function-local: port_resolution imports this module, so a module-level
    # import would be circular.
    from kiro_crew.instances import run_marker

    if dial_host:
        try:
            listener = run_marker.read_listener_secret(int(port), dial_host)
            if listener:
                return listener
            # An empty listener read is a REFUSAL unless the gateway PROVABLY
            # published no listener entries at all -- only then does this port
            # predate the per-listener publish and the port-keyed read below is
            # safe and required. ``has_listener_entries`` is three-valued: a
            # proven-empty ``False`` re-opens the fallback; ``True`` (entries
            # exist, none covered the family) and ``None`` (could not enumerate,
            # so absence is unproven) both FAIL CLOSED here -- an unreadable
            # ``run/`` must not silently downgrade to the shared credential.
            if run_marker.has_listener_entries(int(port)) is not False:
                return ""
        except Exception:
            return ""
    try:
        per_port = run_marker.read_secret(int(port))
    except Exception:
        per_port = ""
    if per_port:
        return per_port
    try:
        return (config_dir() / ".local_secret").read_text().strip()
    except OSError:
        return ""


class WorkspaceDirUnusable(Exception):
    """A workspace ``dir`` cannot be materialized as a directory.

    ``code`` is the stable token both writers map onto their own error contract
    (the dashboard's 409 body ``code``; the CLI's exit-1 message):
    ``workspace_dir_not_a_directory`` when the path exists as something that is
    not a directory, ``workspace_dir_uncreatable`` for everything else (a missing
    parent, a parent swapped for a link, a permission error).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _PinnedCreateRefusal(Exception):
    """pinned_fs's refusal for the workspace create, mapped by the caller."""


def materialize_workspace_dir(validated: Path, *, leaf: Path, display: str) -> None:
    """Make *validated* exist as a directory: adopt one that is there, create one that is not.

    One writer-side rule shared by the dashboard handler and the CLI: a published
    workspace must name a usable directory for provider cwd and project documents.

    *validated* is the path AS THE CALLER'S VALIDATION RESOLVED IT (``Path.resolve()``
    at validation time). This function never resolves it again: resolving here would
    follow whatever a parent component points at by now, which is the exact swap the
    pinned create exists to refuse (see :func:`pinned_fs.pin_parent`).

    An existing DIRECTORY is adopted untouched (pointing a new workspace at a folder
    the owner already keeps is the normal case for an absolute ``dir``). Anything
    else that exists at the name is refused, as is a missing PARENT: only the final
    component is ever created, so a mistyped nested ``dir`` fails here instead of
    committing an entry that breaks an unrelated member later.

    Adoption AND creation are judged through the PINNED parent where the platform
    can pin: every parent component is opened with ``O_NOFOLLOW`` relative to the
    previous one, and the final name is inspected or made relative to that
    descriptor. A component swapped for a link after validation is therefore
    refused rather than followed. That is the discipline every other write under a
    validated path in this product keeps (:mod:`kiro_crew.pinned_fs`). Where nothing
    can be pinned (Windows: no ``dir_fd``, no ``O_NOFOLLOW``), both checks are by
    name, which is that module's documented limit, not a silent substitute.

    Not a rollback site: the directory this makes is left in place when the
    caller's config write later fails. It is reachable only through the entry
    written in the same locked section, and a concurrent create can already have
    adopted it, so deleting it is the unsafe option.

    *leaf* is the same path BEFORE resolution. Resolving follows a link at the
    final name, so *validated* alone never shows one: the entry the caller
    registers is the unresolved spelling, and a link or junction there is
    refused on every platform. It is normalized first, so a ``..`` through a
    missing component cannot make the probe miss the name resolution lands on.
    """
    if platform_compat.is_link_or_junction(os.path.normpath(leaf)):
        raise WorkspaceDirUnusable(
            "workspace_dir_not_a_directory",
            f"'{display}' is a link, not a directory; choose another dir or remove it first",
        )
    if pinned_fs.supports_pinned_walk():
        try:
            parent_fd = pinned_fs.pin_parent(
                str(validated.parent),
                what=f"workspace directory {display!r}",
                refusal=_PinnedCreateRefusal,
            )
            try:
                st = pinned_fs.stat_at(parent_fd, validated.name)
                if st is None:
                    try:
                        os.mkdir(validated.name, dir_fd=parent_fd)
                        return
                    except FileExistsError:
                        st = pinned_fs.stat_at(parent_fd, validated.name)
                if st is not None and _stat.S_ISDIR(st.st_mode):
                    return
                raise WorkspaceDirUnusable(
                    "workspace_dir_not_a_directory",
                    f"'{display}' exists and is not a directory; "
                    "choose another dir or remove it first",
                )
            finally:
                os.close(parent_fd)
        except _PinnedCreateRefusal as exc:
            raise WorkspaceDirUnusable(
                "workspace_dir_uncreatable", f"Directory '{display}' could not be created: {exc}"
            ) from exc
        except OSError as exc:
            raise WorkspaceDirUnusable(
                "workspace_dir_uncreatable",
                f"Directory '{display}' could not be created: {exc.strerror or exc}",
            ) from exc

    # By name here, so a link at the name must be screened out explicitly: a
    # Windows junction answers is_dir() True and is_symlink() False, and the
    # pinned arm above refuses every link through its lstat.
    if not platform_compat.is_link_or_junction(validated) and validated.is_dir():
        return
    try:
        os.mkdir(validated)
    except FileExistsError as exc:
        # EEXIST is the filesystem itself saying something is at the name: a
        # racer's directory is the state this create wanted, while a file, a
        # socket, a dangling link, a symlink or a junction cannot serve as a workspace, and
        # registering one writes back exactly the unusable entry this prevents.
        if not platform_compat.is_link_or_junction(validated) and validated.is_dir():
            return
        raise WorkspaceDirUnusable(
            "workspace_dir_not_a_directory",
            f"'{display}' exists and is not a directory; choose another dir or remove it first",
        ) from exc
    except OSError as exc:
        raise WorkspaceDirUnusable(
            "workspace_dir_uncreatable",
            f"Directory '{display}' could not be created: {exc.strerror or exc}",
        ) from exc


def workspace_dir_from_entry(entry: WorkspaceConfig | None) -> Path:
    """The directory a ``workspaces`` entry names, by the ONE placement rule.

    ``entry.dir`` may be absolute (anywhere on the host) or relative to the
    data home; an absent entry or an empty ``dir`` is the base workspace
    directory under ``config_dir()``. :func:`workspace_dir_for` applies this
    after its own config load; a caller that already holds a
    :class:`KiroCrewConfig` snapshot (the folder-steering memory-store fence)
    applies it directly to that snapshot's entries so every workspace it fences
    comes from the same load -- a second load per name could observe a
    different document (a concurrent write, a transient read failure) and
    silently fall back to the base directory for a workspace the first load
    had placed elsewhere.
    """
    dirname = entry.dir if entry is not None and entry.dir else WorkspaceConfig().dir
    p = Path(dirname).expanduser()
    if p.is_absolute():
        return p
    return config_dir() / dirname


def resolve_agent_config_path() -> Path:
    """Return defaults.json, preferring project-dir override for development.

    All modules that need the agent config path should call this instead
    of reimplementing the resolution chain.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR")
    if proj:
        p = Path(proj) / "agents" / "defaults.json"
        if p.exists():
            return p
    return config_package_dir() / "defaults.json"
