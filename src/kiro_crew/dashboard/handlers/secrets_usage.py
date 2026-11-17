"""Where each vault item is used: ``GET /api/secrets/usage``.

Lists, per stored item, the places that would resolve it today, so the
Settings → Secrets pane can tell a live item from a stale one. A place is
named by its kind and its own name (and the env-var or setting key that
holds the reference); no value and no file content is ever returned.

The places are exactly the readers that resolve a vault reference:

- ``mcp_server``: a ``secret://`` value in an MCP server's ``env`` in an agent
  spec under ``~/.kiro/agents``, resolved by the MCP gateway at spawn
  (``mcp_gateway.secret_uri.resolve_secret_uris``).
- ``env_file``: a ``secret://`` value on a credential key of the data-home
  ``.env`` (``config.loader_credentials.resolve_env_credentials``).
- ``cron_job``: a scheduled job's approved secret grant (``secret_env``),
  resolved when the job runs (``cron_script_grants._resolve_secret_env``).
- ``setting``: ``agent.deepseek_env``, ``agent.strands_env`` and
  ``tunnel.relay_lease_credential`` in ``config.json``.
- ``connection`` / ``mcp_server`` (no key): the client and token entries
  Crew-owned OAuth reads by name for a Connections card or an owner-added
  remote server (:func:`_scan_crew_oauth`).
- ``integration``, ``approved_api``, ``sprites_profile``: the other features
  that read an entry by name (:mod:`.secrets_usage_readers`).

The scan covers these places only, so an item absent from the answer has no
use found here, which is not proof that nothing reads it.

Each place is matched the way its own resolver reads it, so an item is listed
under a place only when that place would read that item.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import stat
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from kiro_crew.config.loader import config_dir
from kiro_crew.dashboard.handlers.secrets import _owner_only, _secrets_vault_preview_on
from kiro_crew.secrets import SecretVault
from kiro_crew.secrets.items import parse_reference

logger = logging.getLogger(__name__)

KIND_MCP_SERVER = "mcp_server"
KIND_ENV_FILE = "env_file"
KIND_CRON_JOB = "cron_job"
KIND_SETTING = "setting"
KIND_CONNECTION = "connection"

Place = dict[str, str]


def referenced_item(ref: str, stored: frozenset[str], *, fields: bool) -> Optional[str]:
    """The stored item a ``secret://`` value names, or ``None``.

    *ref* is the text after ``secret://``. Mirrors ``resolve_secret_uris``: an
    entry stored under the whole text wins, else (only where the reader takes a
    ``#FIELD``) the name before the first ``#``. A reader that does not take a
    field (``.env``, the relay credential) looks the whole text up.
    """
    if ref in stored:
        return ref
    if fields:
        name, field = parse_reference(ref)
        if field is not None and name in stored:
            return name
    return None


def _present(path: Path, is_kind: Callable[[int], bool]) -> bool:
    """Whether *path* exists as that kind; only a missing path counts as absent.

    ``Path.is_file`` and ``is_dir`` answer ``False`` on any stat error, which would
    turn an unreadable place into "no use found". Any other error raises, so the
    caller marks the kind incomplete.
    """
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    return is_kind(mode)


def _redacted(text: str) -> str:
    """*text* as the Schedule board shows agent-written strings (``cron_shared``)."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


def _add(usage: dict[str, list[Place]], item: Optional[str], place: Place) -> None:
    if item is None:
        return
    # A place's name and key come from files and stores the agent can write (a
    # spec, a job's grant), so both are redacted before they leave the gateway.
    place = {**place, "name": _redacted(place["name"]), "key": _redacted(place["key"])}
    rows = usage.setdefault(item, [])
    if place not in rows:
        rows.append(place)


def _env_refs(env: object) -> Iterable[tuple[str, str]]:
    """``(key, text after secret://)`` for each reference value in an env mapping."""
    from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX

    if not isinstance(env, Mapping):
        return []
    return [
        (str(key), value[len(SECRET_URI_PREFIX) :])
        for key, value in env.items()
        if isinstance(value, str) and value.startswith(SECRET_URI_PREFIX)
    ]


def _add_server_refs(
    usage: dict[str, list[Place]], stored: frozenset[str], servers: object
) -> None:
    if not isinstance(servers, dict):
        return
    for server, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        for key, ref in _env_refs(entry.get("env")):
            _add(
                usage,
                referenced_item(ref, stored, fields=True),
                {"kind": KIND_MCP_SERVER, "name": str(server), "key": key},
            )


def _scan_mcp_servers(usage: dict[str, list[Place]], stored: frozenset[str]) -> None:
    """MCP server env references the gateway resolves. BLOCKING.

    The sources the overlay rewriter reads: every agent spec under
    ``~/.kiro/agents``, each crewmate file ``list_mate_sources`` takes, and the
    global ``~/.kiro/settings/mcp.json`` whose servers the rewriter stubs into
    every agent. A listing or file that fails
    to read raises, so the scan is incomplete rather than "no use found"; a
    file that is not a spec is skipped, as the rewriter skips it.
    """
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.agent_discovery import read_agent_spec_strict
    from kiro_crew.agent_spec_format import iter_agent_spec_files_strict
    from kiro_crew.config.paths import mate_agents_dir
    from kiro_crew.mcp_gateway.mate_overlays import list_mate_sources
    from kiro_crew.user_json import loads_user_json

    def read_specs(paths: Iterable[Path]) -> None:
        for path in paths:
            try:
                spec = read_agent_spec_strict(path, operation="secrets_usage", source="dashboard")
            except FileNotFoundError:
                continue  # removed between the listing and the read
            except ValueError:
                continue  # not a spec, and re-reading will not change that
            if isinstance(spec, dict):
                _add_server_refs(usage, stored, spec.get("mcpServers"))

    agents_dir = kiro_agents_dir_path()
    if _present(agents_dir, stat.S_ISDIR):
        read_specs(iter_agent_spec_files_strict(agents_dir))
    # The crewmate files the rewriter stubs for each mate's workers, chosen by
    # its own listing; one it could not decide leaves the scan incomplete.
    mates = list_mate_sources(mate_agents_dir())
    if not mates.complete:
        raise OSError("the crewmate agent folder could not be listed")
    read_specs(mates.files)
    settings = agents_dir.parent / "settings" / "mcp.json"
    if _present(settings, stat.S_ISREG):
        loaded = loads_user_json(settings.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            _add_server_refs(usage, stored, loaded.get("mcpServers"))


def _scan_env_file(usage: dict[str, list[Place]], stored: frozenset[str]) -> None:
    """Credential-key references in the data-home ``.env``. BLOCKING.

    Read with the loader's own reader and line parser; a key the credential
    resolver does not resolve is not a use. A missing file, and a UTF-16/32
    file the credential readers treat as unset, hold no reference.
    """
    from kiro_crew.config.loader_credentials import (
        EnvFileWideEncodingError,
        _is_env_credential_key,
        env_path,
        parse_env_lines,
        read_env_text,
    )

    path = env_path()
    try:
        text = read_env_text(path)
    except (FileNotFoundError, EnvFileWideEncodingError):
        return
    for key, ref in _env_refs(parse_env_lines(text)):
        if _is_env_credential_key(key):
            _add(
                usage,
                referenced_item(ref, stored, fields=False),
                {"kind": KIND_ENV_FILE, "name": path.name, "key": key},
            )


def _load_config() -> Any:
    """The settings as loaded now; raises when the load fell back to defaults. BLOCKING.

    ``KiroCrewConfig.load`` degrades instead of raising, so a config file it could
    not parse (or a section it discarded) would read as "set to nothing" and the
    places it holds as unused. Raising makes the caller mark those kinds incomplete.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    config = KiroCrewConfig.load()
    if (
        getattr(config, "_base_unreadable", False)
        or getattr(config, "_overlay_unreadable", False)
        or getattr(config, "degraded_sections", frozenset())
    ):
        raise OSError("the settings could not be read in full")
    return config


def _scan_settings(usage: dict[str, list[Place]], stored: frozenset[str]) -> None:
    """References in the ``config.json`` settings that resolve one. BLOCKING."""
    from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX
    from kiro_crew.relay_client.lease import credential_reference_problem

    config = _load_config()
    for setting, env in (
        ("agent.deepseek_env", config.agent.deepseek_env),
        ("agent.strands_env", config.agent.strands_env),
    ):
        for key, ref in _env_refs(env):
            _add(
                usage,
                referenced_item(ref, stored, fields=True),
                {"kind": KIND_SETTING, "name": setting, "key": key},
            )
    # As the relay reads it: the setting trimmed (``relay_client.provider``),
    # then a well-formed ``secret://NAME`` (``lease.credential_reference_problem``).
    relay = config.tunnel.relay_lease_credential
    relay = relay.strip() if isinstance(relay, str) else ""
    if relay.startswith(SECRET_URI_PREFIX) and credential_reference_problem(relay) is None:
        _add(
            usage,
            referenced_item(relay[len(SECRET_URI_PREFIX) :], stored, fields=False),
            {"kind": KIND_SETTING, "name": "tunnel.relay_lease_credential", "key": ""},
        )


def _scan_cron_jobs(usage: dict[str, list[Place]], stored: frozenset[str], jobs: list[Any]) -> None:
    """Approved secret grants of scheduled jobs; a pending request is not a use."""
    for job in jobs:
        grant = getattr(job, "secret_env", None)
        if not isinstance(grant, Mapping):
            continue
        for key, item in grant.items():
            if isinstance(item, str) and item in stored:
                _add(usage, item, {"kind": KIND_CRON_JOB, "name": str(job.name), "key": str(key)})


def _scan_crew_oauth(usage: dict[str, list[Place]], stored: frozenset[str]) -> None:
    """Entries Crew-owned OAuth reads by name, for each card that exists now. BLOCKING.

    A Connections card (every registry slug Crew's sign-in serves) and an
    owner-added remote MCP server (``connections.owner_mcp_servers``) each hold a
    client entry and a token pair under names computed from the card's key.
    The names are computed with the module's own helpers and compared with the
    stored names; no entry is read. A card that is gone lists nothing, so an
    entry it left behind reads as unused, which it is.
    """
    from kiro_crew.config.loader import read_config_for_update
    from kiro_crew.connections import crew_oauth, crew_oauth_dcr
    from kiro_crew.connections.owner_servers import (
        CONFIG_ROOT_KEY,
        CONFIG_SERVERS_KEY,
        recorded_url,
    )
    from kiro_crew.connections.registry import get_provider

    def add(key: str, place: Place) -> None:
        for name in (crew_oauth_dcr.client_entry_name(key), crew_oauth.token_entry_name(key)):
            if name in stored:
                _add(usage, name, place)

    for slug in sorted(crew_oauth.SUPPORTED_SLUGS):
        provider = get_provider(slug)
        if provider is not None:
            add(slug, {"kind": KIND_CONNECTION, "name": str(provider["name"]), "key": ""})
    config = read_config_for_update()
    root = config.get(CONFIG_ROOT_KEY) if isinstance(config, dict) else None
    servers = root.get(CONFIG_SERVERS_KEY) if isinstance(root, dict) else None
    for name in sorted(servers) if isinstance(servers, dict) else ():
        url = recorded_url(config, name)
        if isinstance(name, str) and url is not None:
            add(
                crew_oauth.custom_key(name, url), {"kind": KIND_MCP_SERVER, "name": name, "key": ""}
            )


def _scan_files(stored: frozenset[str]) -> tuple[dict[str, list[Place]], list[str]]:
    """Every file-backed place, and the kinds that could not be read. BLOCKING."""
    usage: dict[str, list[Place]] = {}
    incomplete: list[str] = []
    for kind, scan in (
        (KIND_MCP_SERVER, _scan_mcp_servers),
        (KIND_ENV_FILE, _scan_env_file),
        (KIND_SETTING, _scan_settings),
        (KIND_CONNECTION, _scan_crew_oauth),
    ):
        try:
            scan(usage, stored)
        except Exception:  # noqa: BLE001 - one unreadable place must not hide the rest
            logger.warning("Usage scan: could not read %s places", kind, exc_info=True)
            incomplete.append(kind)
    _scan_named_readers(usage, stored, incomplete)
    return usage, incomplete


def _scan_named_readers(
    usage: dict[str, list[Place]], stored: frozenset[str], incomplete: list[str]
) -> None:
    """The integrations that read an entry by name (:mod:`.secrets_usage_readers`). BLOCKING."""
    from kiro_crew.config.paths import data_home
    from kiro_crew.dashboard.handlers import secrets_usage_readers as readers

    def add(name: str, place: Place) -> None:
        _add(usage, name, place)

    def run(kind: str, scan: Callable[[], None]) -> None:
        try:
            scan()
        except Exception:  # noqa: BLE001 - one unreadable place must not hide the rest
            logger.warning("Usage scan: could not read %s places", kind, exc_info=True)
            if kind not in incomplete:
                incomplete.append(kind)

    run(
        readers.KIND_APPROVED_API,
        lambda: readers.scan_approved_apis(Path(config_dir()), stored, add),
    )
    run(
        readers.KIND_SPRITES_PROFILE,
        lambda: readers.scan_sprites_resources(Path(data_home()), stored, add),
    )
    try:
        config = _load_config()
    except Exception:  # noqa: BLE001 - every config-backed reader is then unknown
        logger.warning("Usage scan: could not read config", exc_info=True)
        incomplete.extend(
            kind
            for kind in (readers.KIND_INTEGRATION, readers.KIND_SPRITES_PROFILE)
            if kind not in incomplete
        )
        return
    scans: tuple[tuple[str, Callable[[Any, frozenset[str], readers.Add], None]], ...] = (
        (readers.KIND_INTEGRATION, readers.scan_bitbucket_server),
        (readers.KIND_INTEGRATION, readers.scan_linear),
        (readers.KIND_INTEGRATION, readers.scan_decisions),
        (readers.KIND_INTEGRATION, readers.scan_otlp),
        (readers.KIND_SPRITES_PROFILE, readers.scan_sprites),
    )
    for kind, scan in scans:
        run(kind, functools.partial(scan, config, stored, add))


async def api_secrets_usage(request: web.Request) -> web.Response:
    """GET /api/secrets/usage — where each stored item is referenced.

    ``{"usage": {item: [{"kind", "name", "key"}]}, "incomplete": [kind]}``. An
    item missing from ``usage`` is used nowhere that was scanned; a kind in
    ``incomplete`` could not be scanned, so "used nowhere" is not known.

    With the ``secrets_vault`` preview off, nothing is read and the answer is
    the one this path gave before the route existed: ``DELETE
    /api/secrets/{name}`` matches it, so aiohttp answers 405 allowing DELETE.
    """
    if not await asyncio.to_thread(_secrets_vault_preview_on):
        raise web.HTTPMethodNotAllowed(request.method, ["DELETE"])
    denied = await _owner_only(request, "secrets_usage")
    if denied is not None:
        return denied
    vault = SecretVault(Path(config_dir()))
    try:
        stored = frozenset(await asyncio.to_thread(vault.list_names))
    except (OSError, ValueError) as exc:
        logger.warning("Vault listing failed: %s", exc)
        return web.json_response(
            {"error": "The vault could not be read.", "code": "vault_unreadable"}, status=500
        )
    usage, incomplete = await asyncio.to_thread(_scan_files, stored)
    try:
        jobs = await request.app["state"].crons.list_jobs_async(include_disabled=True)
        _scan_cron_jobs(usage, stored, jobs)
    except Exception:  # noqa: BLE001 - same rule as the file scans
        logger.warning("Usage scan: could not read cron_job places", exc_info=True)
        incomplete.append(KIND_CRON_JOB)
    for rows in usage.values():
        rows.sort(key=lambda row: (row["kind"], row["name"], row["key"]))
    return web.json_response({"usage": usage, "incomplete": sorted(incomplete)})
