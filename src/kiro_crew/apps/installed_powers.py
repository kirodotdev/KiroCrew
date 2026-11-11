"""Load installed Agent Plugins in place, the way an Agent Plugins client does.

This is the client half of the Agent Plugins 1.0 contract
(https://agent-plugins.org/client-implementers): discover plugins installed under
``~/.kiro/powers/installed/<name>/``, validate each ``plugin.json`` against the
locally-supported schema its ``$schema`` selects, and surface the two component
types Kiro Crew can consume -- the plugin's ``mcp.json`` servers and its
``skills/`` directory -- so a plugin installed once (by Kiro IDE) is available in
Kiro Crew too.

It is NOT the plugin CONVERTER (``plugin_import``). The converter copies a package
into a standalone app and, because conversion does not preserve the package root,
DROPS a server whose command is ``./``-relative. The defining feature of a
conformant client is the opposite: a ``./`` command is resolved AGAINST the plugin
root, ``cwd`` defaults to the root, and ``PLUGIN_ROOT`` / ``PLUGIN_DATA`` are
injected. So this loader projects servers in place rather than reusing the
converter.

How the projection reaches a session: Kiro Crew does not launch MCP servers
itself -- kiro-cli does, from the agent config this loader's output is merged into
(``agent_materialization.mcp_sources``). So "launch" here means emitting a server
spec whose command/args/env/cwd are already resolved, which kiro-cli then starts
under the ordinary MCP lifecycle. Skills are surfaced as their ``skills/`` root,
added to the agent's skill search path.

Trust: loading is OFF unless ``agent.load_installed_powers`` is set, because a
projected stdio server launches third-party code the operator installed into
``~/.kiro/powers`` -- the same trust decision as ``agent.apps_allow_third_party``.
Each server is still validated exactly as an imported plugin's is
(:func:`~kiro_crew.apps.manifest.mcp_url_kind`): a mistyped field, a
cleartext-remote url, a userinfo-bearing url, or any ``${VAR}`` the runtime would
expand from the operator's environment (beyond the two plugin variables this
loader expands itself) drops that one entry with a reason and leaves the rest.

Failure isolation follows the spec's narrow boundaries: one unreadable plugin, one
invalid ``mcp.json``, or one bad server entry is skipped and reported; it never
takes down a sibling plugin, a sibling server, or the agent-config rebuild.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.apps.manifest import mcp_url_kind
from kiro_crew.apps.plugin_import import (
    MAX_MCP_SERVERS,
    SCHEMA_NAMESPACE_PREFIX,
    PluginImportError,
    _bounded_sorted_entries,
    _read_json_object,
    resolve_declared_path,
)
from kiro_crew.config import config_dir
from kiro_crew.mcp_provenance import POWER_KEY
from kiro_crew.platform_compat import first_linked_ancestor, is_link_or_junction
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger("kiro_crew.apps.installed_powers")

#: Where Kiro IDE installs Agent Plugins. A FIXED ``~/.kiro`` location, NOT the
#: Kiro Crew data home: the whole point of the feature is to read the same tree
#: Kiro IDE writes. A module-level binding so a test can point it at a fixture.
INSTALLED_POWERS_DIR = Path.home() / ".kiro" / "powers" / "installed"

#: The manifest file name at the plugin root (Agent Plugins 1.0).
MANIFEST_FILENAME = "plugin.json"
#: The MCP configuration document at the plugin root.
MCP_FILENAME = "mcp.json"
#: A skill is an immediate child directory of ``skills/`` holding this file.
SKILL_ENTRY_FILENAME = "SKILL.md"
SKILLS_DIR = "skills"

#: The two plugin variables a client expands (Agent Plugins 1.0 MCP runtime).
#: Expanded by THIS client, in ``args`` / ``env`` values / ``cwd`` only -- never in
#: ``command`` (a ``./`` command is resolved against the root instead) or ``url``.
PLUGIN_ROOT_VAR = "PLUGIN_ROOT"
PLUGIN_DATA_VAR = "PLUGIN_DATA"

#: Every projected server key is ``power-<plugin slug>--<server slug>``. A slug
#: never holds two hyphens in a row (:func:`_slug` collapses them), so the double
#: hyphen splits the pair one way only: plugin ``foo`` + server ``bar-baz`` and
#: plugin ``foo-bar`` + server ``baz`` get different keys.
SERVER_KEY_PREFIX = "power-"
SERVER_KEY_SEPARATOR = "--"

#: Upper bound on plugin directories scanned in one pass. Installed plugins are
#: third-party input, so the enumeration is bounded like every other one in this
#: package; past it the rest are skipped and the truncation is reported.
MAX_POWERS = 500


@dataclass
class LoadedPower:
    """One installed plugin that loaded, and what it contributes."""

    slug: str
    root: Path
    #: Namespaced ``power-<slug>--<server>`` -> resolved agent-config server spec.
    servers: dict[str, dict] = field(default_factory=dict)
    #: The plugin's ``skills/`` root, when it holds at least one valid skill.
    skills_root: Path | None = None


@dataclass
class LoadReport:
    """The aggregate result of one load pass, for the rebuild and for diagnostics."""

    powers: list[LoadedPower] = field(default_factory=list)
    #: Human-readable, each naming the plugin or entry it concerns.
    warnings: list[str] = field(default_factory=list)

    def mcp_servers(self) -> dict[str, dict]:
        """Every loaded plugin's servers, flattened into one agent-config map."""
        merged: dict[str, dict] = {}
        for power in self.powers:
            merged.update(power.servers)
        return merged

    def skill_roots(self) -> list[str]:
        """Every loaded plugin's ``skills/`` root, as strings, sorted and deduped."""
        roots = {str(p.skills_root) for p in self.powers if p.skills_root is not None}
        return sorted(roots)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def loading_enabled() -> bool:
    """Whether ``agent.load_installed_powers`` is on. Fail-closed on any error.

    Read lazily (not imported at module load) for the same reason
    ``mcp_sources`` reads the gateway routing lazily: the config loader is heavy
    and pulls in subsystems this module must not close an import cycle with.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return bool(KiroCrewConfig.load().agent.load_installed_powers)
    except Exception:  # noqa: BLE001 -- an unreadable config loads nothing
        logger.debug("Could not read agent.load_installed_powers; treating as off", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def server_key(plugin_slug: str, server_slug: str) -> str:
    """The agent-config key for one plugin's server."""
    return f"{SERVER_KEY_PREFIX}{plugin_slug}{SERVER_KEY_SEPARATOR}{server_slug}"


def _slug(name: str) -> str:
    """Fold a name into one segment of a ``power-<slug>--<server>`` key.

    Lower-cased, every run of non-alphanumeric characters collapsed to one hyphen,
    trimmed. An empty result (a name of only punctuation) has no addressable
    namespace and the plugin is skipped by the caller.
    """
    lowered = "".join(ch if ch.isalnum() else "-" for ch in name.strip().lower())
    return "-".join(part for part in lowered.split("-") if part)


def _discover_power_dirs() -> tuple[list[Path], bool]:
    """Immediate child directories of :data:`INSTALLED_POWERS_DIR`, link-safe.

    Returns ``(dirs, complete)``; ``complete`` is False when either the listing or
    :data:`MAX_POWERS` cut the set short. A missing install root is valid absence
    (empty, complete), matching the spec's "a missing fixed location is not an
    error". A linked root, or one reached through a linked ancestor, is refused
    whole: resolving a junction on Windows can authenticate to a host the link
    chose, so the escape is refused before anything under it is read.
    """
    root = INSTALLED_POWERS_DIR
    try:
        if first_linked_ancestor(root) or is_link_or_junction(root):
            logger.warning("Not loading installed powers: %s is a link, not a directory", root)
            return [], True
        if not root.is_dir():
            return [], True
        entries, complete = _bounded_sorted_entries(root)
    except OSError as exc:
        logger.warning("Could not list installed powers under %s: %s", root, exc)
        return [], True
    dirs = [e for e in entries if not is_link_or_junction(e) and e.is_dir()]
    # The cap is a truncation too, so it clears ``complete`` like the listing bound.
    return dirs[:MAX_POWERS], complete and len(dirs) <= MAX_POWERS


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _schema_version(schema: object) -> str | None:
    """The version segment of an Agent Plugins ``$schema``, or None when unusable.

    ``https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`` -> ``"1.0.0"``.
    None for anything that is not a string under the published namespace, which is
    how the caller rejects a plugin with a missing or foreign ``$schema`` (the spec
    selects local rules from ``$schema`` and rejects when it cannot).
    """
    if not isinstance(schema, str) or not schema.startswith(SCHEMA_NAMESPACE_PREFIX):
        return None
    rest = schema[len(SCHEMA_NAMESPACE_PREFIX) :].strip("/")
    version = rest.split("/", 1)[0] if rest else ""
    return version or None


def _load_manifest(root: Path, report: LoadReport) -> tuple[str, str] | None:
    """Validate the root ``plugin.json``; return ``(name, schema_version)`` or None.

    Follows the spec's loading sequence: a root ``plugin.json`` must exist, parse
    as a bounded JSON object, carry a ``$schema`` under the published namespace
    (selecting local rules), and a string ``name``. A missing manifest is quiet
    absence (not this plugin's doing); any other failure is reported and rejects
    the plugin. Unknown top-level fields are left alone -- the spec makes them
    non-fatal, and this loader reads only the fields it consumes.
    """
    manifest_path = root / MANIFEST_FILENAME
    if is_link_or_junction(manifest_path) or not manifest_path.is_file():
        # Not a plugin, or a plugin whose manifest is a link (refused like the
        # root). Either way there is nothing to load and nothing to report: a
        # directory under the install root that is not a plugin is ordinary.
        return None
    try:
        data = _read_json_object(manifest_path, f"{root.name}/plugin.json")
    except PluginImportError as exc:
        report.warnings.append(f"{root.name}: plugin.json rejected ({exc.code}): {exc.message}")
        return None

    version = _schema_version(data.get("$schema"))
    if version is None:
        report.warnings.append(
            f"{root.name}: plugin.json has no $schema under {SCHEMA_NAMESPACE_PREFIX}; skipped"
        )
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        report.warnings.append(f"{root.name}: plugin.json has no string 'name'; skipped")
        return None
    extensions = data.get("extensions")
    if extensions is not None and not isinstance(extensions, dict):
        # Non-fatal per the spec: report and keep loading. Nothing here reads an
        # extension namespace, so the value is otherwise ignored.
        report.warnings.append(f"{name}: 'extensions' is not an object; ignored")
    return name.strip(), version


# ---------------------------------------------------------------------------
# Plugin variables
# ---------------------------------------------------------------------------


def _powers_data_dir(slug: str) -> Path:
    """The dedicated, persistent ``PLUGIN_DATA`` directory for *slug*.

    Keyed by slug under the Kiro Crew data home, so it is stable across plugin
    updates (the spec requires PLUGIN_DATA be preserved across updates) and never
    inside the plugin root the package controls.
    """
    return config_dir() / "powers-data" / slug


def _expand_plugin_vars(value: str, plugin_root: str, plugin_data: str) -> str:
    """Replace ``${PLUGIN_ROOT}`` / ``${PLUGIN_DATA}`` in *value*.

    Only these two placeholders, by literal substitution. Any OTHER ``${VAR}`` is
    left untouched so the downstream validator still sees it and rejects the entry
    -- the spec defines exactly these two plugin variables and no other.
    """
    return value.replace("${" + PLUGIN_ROOT_VAR + "}", plugin_root).replace(
        "${" + PLUGIN_DATA_VAR + "}", plugin_data
    )


def _expand_entry_vars(
    config: dict[str, Any], plugin_root: str, plugin_data: str
) -> dict[str, Any]:
    """A copy of *config* with the two plugin variables expanded in the spec's fields.

    Expansion is confined to ``args``, ``env`` VALUES and ``cwd`` (Agent Plugins
    1.0). ``command`` and ``url`` are deliberately left unexpanded: a ``./``
    command is resolved against the plugin root instead, and a url carrying a
    placeholder must stay a violation.
    """
    out = dict(config)
    cwd = out.get("cwd")
    if isinstance(cwd, str):
        out["cwd"] = _expand_plugin_vars(cwd, plugin_root, plugin_data)
    args = out.get("args")
    if isinstance(args, list):
        out["args"] = [
            _expand_plugin_vars(a, plugin_root, plugin_data) if isinstance(a, str) else a
            for a in args
        ]
    env = out.get("env")
    if isinstance(env, dict):
        out["env"] = {
            k: (_expand_plugin_vars(v, plugin_root, plugin_data) if isinstance(v, str) else v)
            for k, v in env.items()
        }
    return out


# ---------------------------------------------------------------------------
# MCP projection
# ---------------------------------------------------------------------------


def _lexically_inside(path: str, base: str) -> str | None:
    """*path* relative to *base* when it is *base* or under it, by text alone.

    No filesystem call: ``..`` is folded by :func:`os.path.normpath`, and a link
    below *base* is screened afterwards by :func:`_has_link_below`.
    """
    norm_path = os.path.normpath(path)
    norm_base = os.path.normpath(base)
    try:
        if os.path.commonpath([norm_path, norm_base]) != norm_base:
            return None
    except ValueError:  # different drives on Windows, or relative vs absolute
        return None
    return os.path.relpath(norm_path, norm_base)


def _has_link_below(base: str, relative: str) -> bool:
    """Whether any component of *relative* under *base* is a link or junction.

    Walked from *base* down, so each ``lstat`` runs on a path whose parents were
    already shown to be real directories. *base* itself is the loader's own
    directory (the plugin root or its ``PLUGIN_DATA``), never plugin input.
    """
    current = os.path.normpath(base)
    if relative in ("", os.curdir):
        return False
    for part in relative.split(os.sep):
        current = os.path.join(current, part)
        if is_link_or_junction(current):
            return True
    return False


def _resolve_cwd(cwd: str, root: Path, plugin_data: str) -> str | None:
    """The working directory a declared ``cwd`` names, or None when it is refused.

    A ``./`` path is resolved against the root like a command. An absolute path
    is what ``${PLUGIN_ROOT}`` / ``${PLUGIN_DATA}`` expand to, so it is accepted
    when, by its text alone, it lands inside the plugin root or that plugin's
    ``PLUGIN_DATA`` directory, and no component below that base is a link.
    """
    if not os.path.isabs(cwd):
        try:
            return str(resolve_declared_path(root, cwd))
        except (PluginImportError, ValueError):
            return None
    for base in (str(root), plugin_data):
        relative = _lexically_inside(cwd, base)
        if relative is None:
            continue
        if _has_link_below(base, relative):
            return None
        return os.path.normpath(cwd)
    return None


def _project_stdio(
    config: dict[str, Any],
    root: Path,
    plugin_root: str,
    plugin_data: str,
    warn: Any = None,
) -> dict[str, Any] | None:
    """Build the agent-config spec for a stdio server, or None to drop it.

    The command is resolved per the spec: a ``./`` command against the plugin root
    with containment enforced (reusing the converter's declared-path resolver), a
    bare command left for platform executable search, an absolute command left as
    is. ``cwd`` defaults to the plugin root. ``env`` carries the plugin's declared
    values with ``PLUGIN_ROOT`` and ``PLUGIN_DATA`` set LAST, so a plugin cannot
    override them.
    """
    command = config.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    resolved_command = command.strip()
    if resolved_command.startswith("./") or resolved_command.startswith(".\\"):
        # The resolver accepts only ``./``, so a Windows ``.\server.exe`` is
        # spelled the way it reads before containment is checked.
        relative = "./" + resolved_command[2:].replace("\\", "/")
        try:
            resolved_command = str(resolve_declared_path(root, relative))
        except (PluginImportError, ValueError):
            # Escapes the root, or cannot be a path at all (an embedded NUL).
            # Dropped by the caller with a reason; siblings still load.
            return None

    spec: dict[str, Any] = {"command": resolved_command}
    args = config.get("args")
    if isinstance(args, list):
        spec["args"] = list(args)

    cwd = config.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        resolved_cwd = _resolve_cwd(cwd.strip(), root, plugin_data)
        if resolved_cwd is None:
            # A declared cwd outside the root and PLUGIN_DATA falls back to the root.
            if warn is not None:
                warn("its cwd is outside the plugin root and PLUGIN_DATA; using the root")
            resolved_cwd = str(root)
        spec["cwd"] = resolved_cwd
    else:
        spec["cwd"] = str(root)

    env: dict[str, str] = {}
    declared_env = config.get("env")
    if isinstance(declared_env, dict):
        env.update({k: v for k, v in declared_env.items() if isinstance(v, str)})
    # PLUGIN_ROOT / PLUGIN_DATA last, so a declared env of the same name cannot win.
    env[PLUGIN_ROOT_VAR] = plugin_root
    env[PLUGIN_DATA_VAR] = plugin_data
    spec["env"] = env
    return spec


def _project_remote(config: dict[str, Any]) -> dict[str, Any] | None:
    """Build the agent-config spec for a Streamable HTTP server, or None to drop it."""
    url = config.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    spec: dict[str, Any] = {"url": url}
    headers = config.get("headers")
    if isinstance(headers, dict):
        spec["headers"] = {k: v for k, v in headers.items() if isinstance(v, str)}
    return spec


def _load_mcp_servers(
    root: Path, slug: str, plugin_version: str, report: LoadReport
) -> dict[str, dict]:
    """Validate the plugin's ``mcp.json`` and project its valid servers.

    Two-stage validation (Agent Plugins 1.0 MCP runtime): the top-level document
    first -- a JSON object under a matching ``$schema`` version with an
    ``mcpServers`` object -- then each entry independently, so one bad entry is
    skipped without disabling its siblings. A top-level failure disables MCP for
    the plugin. A missing ``mcp.json`` is valid absence.
    """
    mcp_path = root / MCP_FILENAME
    if is_link_or_junction(mcp_path) or not mcp_path.exists():
        return {}
    if not mcp_path.is_file():
        report.warnings.append(f"{slug}: mcp.json is not a regular file; MCP disabled")
        return {}
    try:
        document = _read_json_object(mcp_path, f"{slug}/mcp.json")
    except PluginImportError as exc:
        report.warnings.append(f"{slug}: mcp.json rejected ({exc.code}); MCP disabled")
        return {}

    mcp_version = _schema_version(document.get("$schema"))
    if mcp_version is None:
        report.warnings.append(
            f"{slug}: mcp.json has no $schema under {SCHEMA_NAMESPACE_PREFIX}; MCP disabled"
        )
        return {}
    if mcp_version != plugin_version:
        # The spec requires matching Agent Plugins versions across plugin.json and
        # mcp.json; a mismatch is a top-level failure, so MCP is disabled whole.
        report.warnings.append(
            f"{slug}: mcp.json version {mcp_version} != plugin.json {plugin_version}; MCP disabled"
        )
        return {}
    servers = document.get("mcpServers")
    if not isinstance(servers, dict):
        report.warnings.append(f"{slug}: mcp.json has no 'mcpServers' object; MCP disabled")
        return {}

    plugin_root = str(root)
    data_dir = _powers_data_dir(slug)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Best-effort: the directory is the server's to create on first write too,
        # and a projection that still names it is more useful than dropping every
        # stdio server because the parent is momentarily unwritable.
        logger.debug("Could not pre-create PLUGIN_DATA %s: %s", data_dir, exc)
    plugin_data = str(data_dir)

    projected: dict[str, dict] = {}
    inspected = 0
    for name, raw in servers.items():
        inspected += 1
        if inspected > MAX_MCP_SERVERS:
            report.warnings.append(f"{slug}: more than {MAX_MCP_SERVERS} servers; the rest dropped")
            break
        if not isinstance(name, str) or not name.strip():
            report.warnings.append(f"{slug}: a server with a non-string name was dropped")
            continue
        if not isinstance(raw, dict):
            report.warnings.append(f"{slug}: server {name!r} is not an object; dropped")
            continue
        # Expand the two plugin variables FIRST, then validate: a ``${PLUGIN_DATA}``
        # is resolved here, while any other ``${VAR}`` survives to be caught as an
        # operator-environment reference the client must not expand.
        expanded = _expand_entry_vars(raw, plugin_root, plugin_data)
        kind = mcp_url_kind(expanded)
        if kind == "invalid":
            report.warnings.append(
                f"{slug}: server {name!r} is not a valid MCP endpoint for in-place loading; dropped"
            )
            continue
        server_slug = _slug(name)
        if not server_slug:
            report.warnings.append(f"{slug}: server {name!r} has no usable name; dropped")
            continue
        key = server_key(slug, server_slug)
        if key in projected:
            # Two names that fold to one slug (``a_b`` and ``a-b``): keep the first.
            report.warnings.append(
                f"{slug}: server {name!r} folds to the same key as another server; dropped"
            )
            continue

        def _warn(reason: str, _name: str = name) -> None:
            report.warnings.append(f"{slug}: server {_name!r}: {reason}")

        if kind == "remote":
            spec = _project_remote(expanded)
        else:  # "" -> a well-typed stdio entry
            spec = _project_stdio(expanded, root, plugin_root, plugin_data, _warn)
        if spec is None:
            report.warnings.append(
                f"{slug}: server {name!r} declares no usable transport, or its "
                f"command escapes the plugin root; dropped"
            )
            continue
        # Stamp ownership on the entry itself, so the rebuild can tell this
        # loader's servers from an app's or a user's of the same name.
        spec[POWER_KEY] = True
        projected[key] = spec
    return projected


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def _skills_root(root: Path, slug: str, report: LoadReport) -> Path | None:
    """The plugin's ``skills/`` root when it holds at least one valid skill.

    A skill is an immediate child directory of ``skills/`` containing a regular
    ``SKILL.md`` (Agent Plugins 1.0, non-recursive). The root is returned only when
    one exists, so an empty or absent ``skills/`` adds nothing to the skill path. A
    linked ``skills/``, or one resolving onto a sensitive path, is refused.
    """
    skills = root / SKILLS_DIR
    if is_link_or_junction(skills) or not skills.exists():
        return None
    if not skills.is_dir():
        report.warnings.append(f"{slug}: skills/ is not a directory; skills disabled")
        return None
    if is_sensitive_path(str(skills.resolve())):
        report.warnings.append(f"{slug}: skills/ resolves onto a sensitive path; skills disabled")
        return None
    try:
        entries, _complete = _bounded_sorted_entries(skills)
    except OSError:
        return None
    for child in entries:
        if not child.is_dir():
            continue
        if (child / SKILL_ENTRY_FILENAME).is_file():
            return skills
    return None


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def load_installed_powers() -> LoadReport:
    """Discover and load every installed Agent Plugin. Empty when loading is off.

    Never raises: the agent-config rebuild and the skill scan both call this, and a
    broken plugin tree must not take either down. Each plugin is isolated -- an
    exception loading one is recorded and the rest continue.
    """
    report = LoadReport()
    if not loading_enabled():
        return report
    dirs, complete = _discover_power_dirs()
    if not complete:
        report.warnings.append(
            f"more than {MAX_POWERS} installed powers; only the first {len(dirs)} were loaded"
        )
    seen_slugs: set[str] = set()
    for power_dir in dirs:
        try:
            slug = _slug(power_dir.name)
            if not slug:
                report.warnings.append(f"{power_dir.name}: no usable slug; skipped")
                continue
            if slug in seen_slugs:
                # Two directories that fold to one slug (``Foo`` and ``foo``) would
                # share every key. Keep the first, in sorted order.
                report.warnings.append(
                    f"{power_dir.name}: folds to the same name as another plugin; skipped"
                )
                continue
            seen_slugs.add(slug)
            loaded = _load_manifest(power_dir, report)
            if loaded is None:
                continue
            _name, version = loaded
            power = LoadedPower(slug=slug, root=power_dir)
            power.servers = _load_mcp_servers(power_dir, slug, version, report)
            power.skills_root = _skills_root(power_dir, slug, report)
            if power.servers or power.skills_root is not None:
                report.powers.append(power)
        except Exception:  # noqa: BLE001 -- one bad plugin must not poison the rest
            logger.warning("Skipping installed power %s (load error)", power_dir, exc_info=True)
            continue
    # Every drop is logged, so an operator can find out why a plugin they
    # installed loads nothing. The help text for the flag promises a logged reason.
    for warning in report.warnings:
        logger.warning("Installed power: %s", warning)
    return report


def installed_power_mcp_servers() -> dict[str, dict]:
    """Every loaded plugin's MCP servers, namespaced ``power-<slug>--<server>``.

    The projection the agent-config rebuild merges. Empty when loading is off or
    nothing valid is installed.
    """
    return load_installed_powers().mcp_servers()


def installed_power_skill_roots() -> list[str]:
    """Every loaded plugin's ``skills/`` root, for the agent skill search path."""
    return load_installed_powers().skill_roots()
