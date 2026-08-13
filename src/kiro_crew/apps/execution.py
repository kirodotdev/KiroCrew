"""Central execution boundary for App Kit apps.

App admission and governance decide which apps may be installed or activated.
This module answers the separate runtime question: whether executable code from
an admitted app may run in the gateway's trust domain.  Keeping that decision in
one place prevents Python hooks, backend processes, and manifest shell commands
from drifting to different defaults.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_BUILTINS_DIR = (Path(__file__).resolve().parent / "builtins").resolve()
_ALLOW_ALL_SETTING_PATH = "agent.apps_allow_third_party"
_TRUST_SETTING_PATH = "agent.apps_trusted"

# App names admissible as a per-app trust grant. Deliberately the same shape the
# dashboard's app routes accept, so a grant can only ever name a real app: no
# wildcard, no separator, no traversal, no empty string. The length cap is a DoS
# bound, not a naming rule: this set is rebuilt on EVERY execution decision (each
# hook load, backend spawn, bridge registration and boot-reconcile iteration), so
# an unbounded entry — or an unbounded list — turns a hand-edited config into a
# per-decision cost. Real app names are kebab-case and short.
_MAX_GRANT_NAME_LEN = 128
_MAX_GRANT_ENTRIES = 512
APP_NAME_RE = re.compile(rf"[a-z0-9][a-z0-9_-]{{0,{_MAX_GRANT_NAME_LEN - 1}}}")


def _builtin_manifest_sources() -> tuple[Path, ...]:
    """Return resolved builtin-manifest roots from core and the active edition.

    The platform seam is imported lazily because ``apps.manager`` imports this
    module while the platform graph is still being composed. A missing or
    unreadable edition source is omitted (fail-closed for that source); it can
    never widen provenance to a mutable installed-app directory.
    """
    sources: list[Path] = [_BUILTINS_DIR]
    try:
        from kiro_crew.platform import current_context

        sources.extend(
            Path(source)
            for source in current_context().apps_loader.manifest_sources()
        )
    except Exception:  # noqa: BLE001 - unavailable composition must not admit code
        logger.debug("edition builtin manifest sources unavailable", exc_info=True)

    roots: list[Path] = []
    seen: set[Path] = set()
    for source in sources:
        try:
            root = source.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if not root.is_dir() or root in seen:
            continue
        seen.add(root)
        roots.append(root)
    return tuple(roots)


def shipped_builtin_app_root(app_name: str) -> Path | None:
    """Return the immutable package directory that ships ``app_name``.

    The package ``app.json`` is authoritative. Mutable installed metadata is
    deliberately ignored, and a directory without a valid shipped manifest is
    not a builtin even when its name resembles one.
    """
    for source in _builtin_manifest_sources():
        try:
            entries = sorted(source.iterdir())
        except OSError:
            continue

        for entry in entries:
            try:
                root = entry.resolve(strict=True)
                if not root.is_dir() or not root.is_relative_to(source):
                    continue
                manifest_path = (root / "app.json").resolve(strict=True)
                if not manifest_path.is_file() or not manifest_path.is_relative_to(root):
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(manifest, dict) and manifest.get("name") == app_name:
                return root
    return None


def builtin_app_names() -> frozenset[str]:
    """First-party app names: shipped-manifest provenance AND a builtin-owned install.

    A name qualifies only when BOTH hold:

    1. A shipped ``app.json`` declares it — the SAME manifest sources as
       :func:`shipped_builtin_app_root` (core ``builtins/`` plus any
       edition/companion source via the platform ``apps_loader``). This says the
       app *could* be first party.
    2. Its active ``installed.json`` record is builtin-owned
       (:func:`manager.builtin_owns_installed`). This proves the builtin
       actually occupies the slot rather than a user-installed app that shares
       the name and made the builtin registration *stand down*
       (see :func:`manager.register_builtin_apps`). A shadowing third-party app,
       or a missing/unreadable record, is excluded.

    ``installed.json`` is consulted ONLY to REMOVE trust, never to widen it: a
    name absent from every shipped manifest can never enter this set regardless
    of installed metadata, so a mutable record cannot forge first-party
    provenance. The gate's sole consumer therefore auto-approves an app's calls
    to its own MCP server only when that app is genuinely the shipped builtin —
    not a user app that merely shadows a builtin name.

    Does filesystem I/O (dir scan + manifest reads + one installed-record read
    per candidate); callers warm it ONCE off the event loop. A source that fails
    to read is skipped, and an unreadable installed record drops just that name
    (fail-closed) — neither ever widens provenance.
    """
    candidates: set[str] = set()
    for source in _builtin_manifest_sources():
        try:
            entries = sorted(source.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                root = entry.resolve(strict=True)
                if not root.is_dir() or not root.is_relative_to(source):
                    continue
                manifest_path = (root / "app.json").resolve(strict=True)
                if not manifest_path.is_file() or not manifest_path.is_relative_to(root):
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(manifest, dict):
                name = manifest.get("name")
                if isinstance(name, str) and name:
                    candidates.add(name)

    # Narrow shipped-manifest candidates to those whose ACTIVE install is
    # builtin-owned, so a user app shadowing a builtin's name is NOT trusted as
    # first party. Deferred import: manager imports this module at load time, so
    # the reverse edge must resolve lazily (manager is fully imported by the
    # time this runs, off the event loop, at gateway boot).
    from kiro_crew.apps.manager import builtin_owns_installed

    return frozenset(name for name in candidates if builtin_owns_installed(name))


def builtin_app_mcp_servers() -> frozenset[str]:
    """Canonical ``<app>:<server>`` names DECLARED in shipped builtin manifests.

    For each first-party manifest (the SAME sources as :func:`builtin_app_names`)
    whose active install is builtin-owned, emit ``f"{name}:{server}"`` for every
    key in its ``mcpServers``. This is the IMMUTABLE set of app-own MCP servers:
    the gate's app-own-server auto-approve requires membership here so it trusts
    only a server the app's SHIPPED manifest declares — never an arbitrary
    ``<app>:``-prefixed entry that landed in the mutable global MCP config (which
    ``bridges._own_mcp_servers`` injects into the agent by prefix). Narrowed to
    builtin-owned installs exactly like :func:`builtin_app_names`, so a shadowing
    user app cannot contribute declared servers.

    Enumerates its own manifest loop (mirroring :func:`builtin_app_names`) rather
    than sharing state with it, to keep that security-sensitive function
    untouched. Filesystem I/O; callers warm it ONCE off the event loop. A source
    or manifest that fails to read is skipped (fail-closed), never widening the
    trusted set.
    """
    # Deferred import: manager imports this module at load time (see
    # builtin_app_names), so the reverse edge resolves lazily.
    from kiro_crew.apps.manager import builtin_owns_installed

    servers: set[str] = set()
    owned: dict[str, bool] = {}
    for source in _builtin_manifest_sources():
        try:
            entries = sorted(source.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                root = entry.resolve(strict=True)
                if not root.is_dir() or not root.is_relative_to(source):
                    continue
                manifest_path = (root / "app.json").resolve(strict=True)
                if not manifest_path.is_file() or not manifest_path.is_relative_to(root):
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            name = manifest.get("name")
            mcp_servers = manifest.get("mcpServers")
            if not (isinstance(name, str) and name and isinstance(mcp_servers, dict)):
                continue
            if name not in owned:
                owned[name] = builtin_owns_installed(name)
            if not owned[name]:
                continue
            for server_name in mcp_servers:
                if isinstance(server_name, str) and server_name:
                    servers.add(f"{name}:{server_name}")
    return frozenset(servers)


def builtin_app_agents() -> dict[str, str]:
    """Map each agent DECLARED by a shipped builtin manifest to its owning app.

    For every first-party manifest (the SAME sources and builtin-owned-install
    narrowing as :func:`builtin_app_names`), read each entry of its ``agents``
    list and map that agent's declared ``name`` to the app that ships it. Both
    the bare name and the ``f"{app}--{agent}"`` link form (the filename
    ``bridges._safe_link_name`` registers it under) are emitted, because a chat
    slot may carry either spelling depending on which surface bound it; both are
    derived from the same IMMUTABLE manifest and identify the same app agent.

    This exists so the PreToolUse gate can recover an app identity for a slot
    that has none. ``Slot._app`` is set from the request's AUTHENTICATED app
    scope, so a builtin whose UI is not an app iframe — e.g. an Electron window
    authenticating with the dashboard session cookie — creates its slot with an
    empty ``_app`` and its calls to its OWN MCP server never satisfy the
    app-own-server auto-approve. Deriving the owner from the agent restores that
    intra-app UX without trusting anything the client sent: the mapping comes
    only from shipped manifests, exactly like :func:`builtin_app_mcp_servers`.

    An agent name declared by MORE than one app is dropped entirely rather than
    resolved arbitrarily — ambiguous provenance must not grant either app's
    identity (fail-closed). Filesystem I/O (manifest + one read per declared
    agent file); callers warm it ONCE off the event loop. A source, manifest, or
    agent file that fails to read is skipped, never widening the mapping.
    """
    # Deferred import: manager imports this module at load time (see
    # builtin_app_names), so the reverse edge resolves lazily.
    from kiro_crew.apps.manager import builtin_owns_installed

    mapping: dict[str, str] = {}
    ambiguous: set[str] = set()
    owned: dict[str, bool] = {}
    for source in _builtin_manifest_sources():
        try:
            entries = sorted(source.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                root = entry.resolve(strict=True)
                if not root.is_dir() or not root.is_relative_to(source):
                    continue
                manifest_path = (root / "app.json").resolve(strict=True)
                if not manifest_path.is_file() or not manifest_path.is_relative_to(root):
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            name = manifest.get("name")
            agents = manifest.get("agents")
            if not (isinstance(name, str) and name and isinstance(agents, list)):
                continue
            if name not in owned:
                owned[name] = builtin_owns_installed(name)
            if not owned[name]:
                continue
            for rel in agents:
                if not isinstance(rel, str) or not rel:
                    continue
                try:
                    # Confine the declared path to the app root: a manifest is
                    # immutable shipped data, but resolving before the
                    # containment check keeps a symlinked entry from reading an
                    # agent file outside the app it claims to ship.
                    agent_path = (root / rel).resolve(strict=True)
                    if not agent_path.is_file() or not agent_path.is_relative_to(root):
                        continue
                    agent_doc = json.loads(agent_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(agent_doc, dict):
                    continue
                agent_name = agent_doc.get("name")
                if not (isinstance(agent_name, str) and agent_name):
                    continue
                for key in (agent_name, f"{name}--{agent_name}"):
                    if mapping.get(key, name) != name:
                        ambiguous.add(key)
                    mapping[key] = name
    for key in ambiguous:
        mapping.pop(key, None)
    return mapping


def shipped_builtin_module_path(app_name: str, module_name: str) -> Path | None:
    """Resolve a ``python -m`` target only when it belongs to ``app_name``'s package."""
    root = shipped_builtin_app_root(app_name)
    if root is None:
        return None
    module_parts = module_name.split(".")
    if not module_parts or not all(part.isidentifier() for part in module_parts):
        return None

    # Reconstruct the dotted module from each ancestor of the immutable app
    # root. This supports both core modules (``kiro_crew.apps.builtins.*``)
    # and edition package namespaces without importing an attacker-selected
    # parent package merely to call ``find_spec``.
    for base in root.parents:
        target = base.joinpath(*module_parts)
        for candidate in (target.with_suffix(".py"), target / "__main__.py"):
            try:
                resolved = candidate.resolve(strict=True)
                if resolved.is_file() and resolved.is_relative_to(root):
                    return resolved
            except (OSError, ValueError):
                continue
    return None


def is_builtin_app(
    *, app_root: Path | None = None, app_name: str | None = None
) -> bool:
    """Return whether immutable package provenance covers the executed path.

    Path-only callers must point inside one of the composed shipped-manifest
    roots. When an app name is available, its shipped manifest must declare
    that exact name and the executable path must resolve inside that app's
    package directory. Mutable ``installed.json`` fields are never provenance.
    """
    if app_root is None:
        return False
    try:
        resolved = app_root.resolve(strict=True)
    except (OSError, ValueError):
        return False
    if app_name is None:
        return any(
            resolved.is_relative_to(source)
            for source in _builtin_manifest_sources()
        )
    shipped_root = shipped_builtin_app_root(app_name)
    return shipped_root is not None and resolved.is_relative_to(shipped_root)


def third_party_execution_allowed() -> bool:
    """Return the operator's explicit third-party execution decision.

    Absence, malformed values, and config-load failures all deny.  The strict
    identity check intentionally rejects truthy values such as ``1`` or
    ``"true"``; only a validated JSON boolean ``true`` is an admission.
    Environment variables are not consulted, so a child/app-controlled env value
    cannot widen this process-level trust boundary.
    """
    try:
        # Deferred to avoid importing the full config graph while it imports apps.
        from kiro_crew.config.loader import KiroCrewConfig

        value = getattr(KiroCrewConfig.load().agent, "apps_allow_third_party", False)
        return value is True
    except Exception as exc:  # noqa: BLE001 - unreadable policy must fail closed
        logger.error(
            "%s: config load failed (%s); refusing third-party app execution",
            _ALLOW_ALL_SETTING_PATH,
            exc,
        )
        return False


def trusted_app_names() -> frozenset[str]:
    """App names the operator granted third-party execution ONE AT A TIME.

    The narrow counterpart to :func:`third_party_execution_allowed`: a name here
    admits exactly that app's code and says nothing about any other app, so a
    user who wants one registry app does not thereby authorise every future one.

    Every failure mode yields the EMPTY set (fail closed): an unreadable config,
    a non-list value, and a non-string member all deny rather than widening.
    Entries are matched literally against the app's manifest name — no globbing,
    no path semantics — and an entry that is not a well-formed app name is
    dropped, so ``"*"``, ``"../x"`` and ``""`` can never admit anything. Trusting
    every app is deliberately NOT expressible here; that is what the explicit
    ``agent.apps_allow_third_party`` boolean is for.
    """
    try:
        # Deferred for the same reason as third_party_execution_allowed().
        from kiro_crew.config.loader import KiroCrewConfig

        raw = getattr(KiroCrewConfig.load().agent, "apps_trusted", [])
    except Exception as exc:  # noqa: BLE001 - unreadable policy must fail closed
        logger.error(
            "%s: config load failed (%s); refusing per-app trust grants",
            _TRUST_SETTING_PATH,
            exc,
        )
        return frozenset()
    if not isinstance(raw, list):
        logger.error("%s: not a JSON array; ignoring every grant", _TRUST_SETTING_PATH)
        return frozenset()
    if len(raw) > _MAX_GRANT_ENTRIES:
        # Bound the per-decision cost of a pathological config rather than
        # denying outright: the operator's real grants are at the front of an
        # append-ordered list, so truncating preserves them while capping work.
        logger.error(
            "%s: %d entries exceeds the %d cap; considering only the first %d",
            _TRUST_SETTING_PATH,
            len(raw),
            _MAX_GRANT_ENTRIES,
            _MAX_GRANT_ENTRIES,
        )
        raw = raw[:_MAX_GRANT_ENTRIES]
    return frozenset(
        entry
        for entry in raw
        if isinstance(entry, str) and APP_NAME_RE.fullmatch(entry)
    )


def app_execution_denied(
    app_name: str,
    *,
    action: str,
    app_root: Path | None = None,
    caller: str = "gateway",
) -> str | None:
    """Return a denial reason when an app execution surface must not run.

    Shipped package code is exempt only when ``app_root`` resolves inside the
    immutable builtin package registered for ``app_name``.  Every other target
    requires EITHER a per-app grant in ``agent.apps_trusted`` (the narrow form,
    admitting this app alone) OR ``agent.apps_allow_third_party`` set to the JSON
    boolean ``true`` (the blanket form).  Allowed and denied decisions are
    audited best-effort, but audit unavailability never changes the execution
    decision.
    """
    builtin = is_builtin_app(app_name=app_name, app_root=app_root)
    granted = not builtin and app_name in trusted_app_names()
    if builtin:
        provenance = "provenance=shipped_builtin"
    elif granted:
        provenance = "provenance=trusted_grant"
    else:
        provenance = "provenance=unverified"
    if builtin or granted or third_party_execution_allowed():
        try:
            sel().log_api_access(
                caller=caller,
                operation="app_execution_admission",
                outcome="allowed",
                resources=f"app={app_name!r} action={action!r} {provenance}",
            )
        except Exception:  # noqa: BLE001 - admission must survive audit unavailability
            logger.debug("app execution admission audit failed", exc_info=True)
        return None

    reason = (
        "third-party app execution is disabled; trust this app alone by adding "
        f"{app_name!r} to {_TRUST_SETTING_PATH}, or set {_ALLOW_ALL_SETTING_PATH}=true to allow every "
        "third-party app's Python, backend, and manifest shell code"
    )
    try:
        sel().log_api_access(
            caller=caller,
            operation="app_execution_admission",
            outcome="denied",
            resources=f"app={app_name!r} action={action!r} {provenance}",
            error=reason,
        )
    except Exception:  # noqa: BLE001 - denial must survive audit unavailability
        logger.debug("app execution denial audit failed", exc_info=True)
    return reason
