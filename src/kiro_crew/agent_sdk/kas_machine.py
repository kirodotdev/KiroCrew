"""KAS machine mode: Crew's own engine reads a host-issued token file itself.

``agent.kas_auth`` says who gives the engine on Crew's own bundle
(``agent.kas_bundle``) its credential:

- ``acp-callback``, the default: the engine asks Crew over ACP and Crew answers
  from its own sign-in.
- ``machine``: a host that issues the agent a machine token in a file starts the
  engine with ``--auth=machine --token-path=<file>``. The engine opens and re-reads
  that file in its own process. Crew never opens, reads, copies or hashes it: it
  checks the path with ``access(2)`` and ``stat(2)`` alone
  (:func:`token_file_present`), and no Crew text carries a token value.

Machine mode is honoured only while the ``crew_client`` feature preview is on
(``edition_locks.preview_on``, so an LTS build reads it as off); off,
``agent.kas_auth`` reads as callback mode whatever it says.

It has no default service and no default token file. A missing or non-https
endpoint or a missing region refuses the spawn (``machine_cell_missing``) instead of
letting the engine choose its own default host, and an unset or unusable token path
refuses it too (``machine_token_missing``). It runs on ``agent.kas_bundle`` only,
because kiro-cli's relay has no machine mode (``machine_route_not_bundle``).

The settings a spawn ran under travel on its spawn plan to the runtime
(``SpawnPlan.machine_auth`` -> ``AcpRuntime._kas_machine_auth``), so a live session
is judged by the token file its process reads, not by the current configuration.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from kiro_crew.config.sections_agent_fields import KAS_AUTH_MACHINE

logger = logging.getLogger(__name__)

#: ``auth.login.endpoints.CREW_CLIENT_PREVIEW``, spelled here so a spawn-path read
#: does not load ``kiro_crew.auth``.
CREW_CLIENT_PREVIEW = "crew_client"

MACHINE_CELL_MISSING = "machine_cell_missing"
MACHINE_TOKEN_MISSING = "machine_token_missing"
MACHINE_ROUTE_NOT_BUNDLE = "machine_route_not_bundle"

#: Why a machine-mode spawn is refused off ``agent.kas_bundle``.
RELAY_REFUSAL = (
    f"{MACHINE_ROUTE_NOT_BUNDLE}: machine mode runs only on Kiro Crew's own engine "
    "bundle; set agent.kas_bundle to true"
)

#: The engine's own classes for a refused machine credential, as its errors name
#: them (``data.errorType``, or the text).
KAS_REFUSAL_CLASSES = (
    "ModelRegistryAccessDeniedError",
    # Not ModelRegistryUnavailableError: the engine sends it for a failure other
    # than a refused credential, retried as a server error.
    "ModelRegistryUnauthenticatedError",
    "TokenInvalidError",
    "TokenExpiredError",
)

#: What the model catalog is recorded under in machine mode, where Crew's vault holds
#: no identity to key it by.
MACHINE_CATALOG_IDENTITY_PREFIX = "machine:"


@dataclass(frozen=True)
class MachineAuth:
    """The machine-mode settings one spawn runs under, as configured."""

    token_path: str
    endpoint: str
    control_plane_endpoint: str
    region: str
    env_drop: tuple[str, ...]

    @property
    def catalog_identity(self) -> str:
        """The model catalog's key: the token file and the cell it is sent to, so a
        new endpoint or region never reads the previous cell's models."""
        cell = (self.token_path, self.endpoint, self.control_plane_endpoint, self.region)
        return MACHINE_CATALOG_IDENTITY_PREFIX + json.dumps(cell)


def runs_machine_auth(runtime: object) -> bool:
    """Whether *runtime*'s process was started under machine mode
    (``AcpRuntime._kas_machine_auth``). Reads only in-memory state."""
    return isinstance(getattr(runtime, "_kas_machine_auth", None), MachineAuth)


class MachineAuthRefused(Exception):
    """A machine-mode spawn that cannot start. ``refusal`` is the class name."""

    def __init__(self, refusal: str, detail: str) -> None:
        super().__init__(f"{refusal}: {detail}")
        self.refusal = refusal


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def machine_auth(agent: Any) -> MachineAuth | None:
    """The machine settings of a loaded ``agent`` section, or None in callback mode.

    Pure: it does not read the preview (:func:`machine_auth_in` does).
    """
    if getattr(agent, "kas_auth", "") != KAS_AUTH_MACHINE:
        return None
    drop = getattr(agent, "kas_machine_env_drop", None)
    names = tuple(_text(n) for n in drop if _text(n)) if isinstance(drop, list) else ()
    return MachineAuth(
        token_path=_text(getattr(agent, "kas_token_path", "")),
        endpoint=_text(getattr(agent, "kas_endpoint", "")),
        control_plane_endpoint=_text(getattr(agent, "kas_control_plane_endpoint", "")),
        region=_text(getattr(agent, "kas_region", "")),
        env_drop=names,
    )


def machine_auth_in(cfg: Any) -> MachineAuth | None:
    """Machine mode as *cfg* states it: the KAS backend, the preview on, ``machine``."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.edition_locks import preview_on

    agent = getattr(cfg, "agent", None)
    if getattr(agent, "acp_backend", "") == ACP_BACKEND_KAS and preview_on(
        CREW_CLIENT_PREVIEW, cfg
    ):
        return machine_auth(agent)
    return None


def configured_machine_auth() -> MachineAuth | None:
    """Machine mode as this host's ``config.json`` states it (:func:`machine_auth_in`).

    Read from the file, as the spawn reads it. Blocking; call off the loop. Never
    raises: a config that cannot be read is callback mode.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.edition_locks import preview_on

        # The preview first, from the live configuration: while it is off no file is
        # loaded, so every caller costs what it did on main.
        if not preview_on(CREW_CLIENT_PREVIEW):
            return None
        return machine_auth_in(KiroCrewConfig.load())
    except Exception:  # noqa: BLE001 - an unreadable config keeps callback mode
        logger.debug("KAS machine mode: config unreadable, callback mode", exc_info=True)
        return None


#: :func:`fenced_token_path`'s last read of ``config.json`` and ``config.local.json``
#: where no watcher runs (an MCP server, a hook child): each file's
#: ``(path, mtime_ns, size)`` and the token path they named. The file gate asks per
#: path, so a read is reused until either file changes.
_fence_config: tuple[tuple[tuple[str, int, int], ...], str] | None = None


def _fence_from(agent: Any, previews_on: bool) -> str:
    """The token path to fence: ``agent.kas_auth`` machine with the preview on.

    Not :func:`machine_auth_in`: the fence does not ask the backend, so it never
    misses a file a config that leaves ``agent.acp_backend`` at its default names.
    A wider fence only refuses one more path to agent file tools.
    """
    if not previews_on:
        return ""
    machine = machine_auth(agent)
    return machine.token_path if machine is not None else ""


def _fence_from_file() -> str:
    """:func:`_fence_from` over ``config.json`` read as JSON, never through the loader
    (which can write the file). Reused while the file's stat is unchanged."""
    global _fence_config
    from types import SimpleNamespace

    from kiro_crew.config.loader_locations import (
        config_local_path,
        config_path,
        read_config_text,
    )
    from kiro_crew.config.resolution import _deep_merge
    from kiro_crew.edition_locks import feature_previews

    # config.local.json overlays config.json, as the loader merges them.
    paths = (config_path(), config_local_path())
    stats: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            info = os.stat(path)
        except FileNotFoundError:
            stats.append((str(path), -1, -1))
        else:
            stats.append((str(path), info.st_mtime_ns, info.st_size))
    key = tuple(stats)
    cached = _fence_config
    if cached is not None and cached[0] == key:
        return cached[1]
    data: dict[str, Any] = {}
    try:
        for path, (_, mtime, _) in zip(paths, stats):
            if mtime >= 0:
                doc = json.loads(read_config_text(path))
                data = _deep_merge(data, doc) if isinstance(doc, dict) else data
    except (OSError, ValueError):
        # A file mid-edit or malformed keeps the last fence read: only a config that
        # reads cleanly with machine mode off lifts it.
        logger.debug("KAS machine mode: config unreadable; keeping the last fence")
        return cached[1] if cached is not None else ""
    agent = data.get("agent") if isinstance(data, dict) else None
    dashboard = data.get("dashboard") if isinstance(data, dict) else None
    previews = feature_previews(
        dashboard.get("feature_previews") if isinstance(dashboard, dict) else None
    )
    on = isinstance(previews, Mapping) and previews.get(CREW_CLIENT_PREVIEW) is True
    token = _fence_from(SimpleNamespace(**agent) if isinstance(agent, dict) else None, on)
    _fence_config = (key, token)
    return token


def fenced_token_path() -> str:
    """The configured machine token file, while machine mode is on, else ``""``.

    The agent file gate (``security.paths.sensitive_path_refusal``) fences it like a
    credential: only the engine reads it, and it opens the file itself, outside every
    agent file tool. Read from the live configuration, or where no watcher runs from
    ``config.json`` as JSON (:func:`_fence_from_file`). Never raises.
    """
    try:
        from kiro_crew.config import live
        from kiro_crew.edition_locks import preview_on

        cfg = live.snapshot()
        if cfg is None:
            return _fence_from_file()
        return _fence_from(getattr(cfg, "agent", None), preview_on(CREW_CLIENT_PREVIEW, cfg))
    except Exception:  # noqa: BLE001 - unreadable config is callback mode: nothing to fence
        logger.debug("KAS machine mode: config unreadable for the file gate", exc_info=True)
        return ""


def _is_https_url(value: str) -> bool:
    return value.lower().startswith("https://") and len(value) > len("https://")


def check_cell(machine: MachineAuth) -> None:
    """Raise ``machine_cell_missing`` unless both endpoints are https and a region is set."""
    for key, value in (
        ("agent.kas_endpoint", machine.endpoint),
        ("agent.kas_control_plane_endpoint", machine.control_plane_endpoint),
    ):
        if not _is_https_url(value):
            raise MachineAuthRefused(MACHINE_CELL_MISSING, f"{key} is not an https:// url")
    if not machine.region:
        raise MachineAuthRefused(MACHINE_CELL_MISSING, "agent.kas_region is not set")


def token_file_present(path: str) -> bool:
    """Whether *path* is a readable, non-empty regular file, by access(2) and stat(2).

    The file is never opened: the engine is the only process that reads it.
    """
    if not path or not os.path.isabs(path):
        return False
    try:
        if not os.access(path, os.R_OK):
            return False
        info = os.stat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > 0


def check_spawnable(machine: MachineAuth) -> None:
    """Raise the refusal a machine-mode spawn meets, if any. Blocking (stats a file)."""
    check_cell(machine)
    if not token_file_present(machine.token_path):
        raise MachineAuthRefused(
            MACHINE_TOKEN_MISSING,
            f"no readable, non-empty machine token file at {machine.token_path or '<unset>'}",
        )


def spawn_refusal(machine: MachineAuth) -> str:
    """The text :func:`check_spawnable` refuses with, or ``""``. Blocking; never raises."""
    try:
        check_spawnable(machine)
    except MachineAuthRefused as exc:
        return str(exc)
    return ""


def with_engine_args(argv: list[str], machine: MachineAuth) -> list[str]:
    """The bundle *argv* with Crew's callback auth argument replaced by machine mode's."""
    from kiro_crew.acp.kas_transport import KAS_BUNDLE_AUTH_ARG

    if KAS_BUNDLE_AUTH_ARG not in argv:
        raise ValueError("the bundle argv carries no auth argument to replace")
    at = argv.index(KAS_BUNDLE_AUTH_ARG)
    args = [
        "--auth=machine",
        f"--token-path={machine.token_path}",
        f"--endpoint={machine.endpoint}",
        f"--control-plane-endpoint={machine.control_plane_endpoint}",
        f"--region={machine.region}",
    ]
    return [*argv[:at], *args, *argv[at + 1 :]]


def drop_engine_env(env: MutableMapping[str, str], machine: MachineAuth) -> None:
    """Remove every ``kas_machine_env_drop`` name from the engine's *env*.

    Matched as the platform matches env names (``platform_compat.env_key_allowed``):
    case-insensitive on Windows, where inherited keys arrive upper-cased, exact on
    POSIX. The caller also strips Crew's own API key.
    """
    from kiro_crew import platform_compat

    if not machine.env_drop:
        return
    for key in [key for key in env if platform_compat.env_key_allowed(key, machine.env_drop)]:
        del env[key]


def refusal_class(text: str, data: object = None) -> str:
    """The engine class a refusal names: ``data.errorType`` first, then the text."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if isinstance(data, dict) and data.get("errorType") in KAS_REFUSAL_CLASSES:
        return str(data["errorType"])
    for name in KAS_REFUSAL_CLASSES:
        if name in text:
            return name
    return ""


#: A machine-mode start the engine refused with no class of its own named.
REFUSED_TEXT = (
    "Kiro refused the machine token. Check the file in agent.kas_token_path on this host; "
    "signing in to Kiro Crew does not change it."
)


def start_refusal(text: str, code: object) -> str:
    """``Kiro refused the machine token: <class> (<code>)``, or ``""`` when the error
    names no machine-credential class."""
    refusal = refusal_class(text)
    if not refusal:
        return ""
    shown = str(code) if isinstance(code, int) and not isinstance(code, bool) else "no code"
    return f"Kiro refused the machine token: {refusal} ({shown})"


def raise_turn_error(
    runtime: object, error: object, available_models: Any, *, backend: str
) -> None:
    """``transport_errors._raise_acp_error`` for a session on *runtime*.

    On a process started under machine mode (:func:`runs_machine_auth`) a rejected
    credential is the host token's: a personal sign-in cannot repair it. It is raised
    as that, terminal (a retry re-sends the same token) and with none of the sign-in
    tags, so no sign-in card is offered. Every other error raises as it did.
    """
    from kiro_crew.acp.client import AcpError, _raise_acp_error
    from kiro_crew.acp.error_frames import raw_error_detail

    try:
        _raise_acp_error(error, available_models, backend=backend)
    except AcpError as err:
        if not runs_machine_auth(runtime):
            raise
        frame = error if isinstance(error, dict) else {}
        raw = f"{frame.get('data', '')} {frame.get('message', '')}"
        text = start_refusal(raw, frame.get("code"))
        sign_in = bool(getattr(err, "sign_in_needed", False) or getattr(err, "auth_error_code", ""))
        if not text and (sign_in or refusal_class(raw, frame.get("data"))):
            text = REFUSED_TEXT
        if not text:
            raise
        refused = AcpError(text, transient=False, user_worded=True)
        refused.raw_detail = raw_error_detail(error)
        # No cause chain: the sign-in tags on *err* must not reach any reader.
        raise refused from None


def spawn_selects_bundle() -> bool:
    """``agent.kas_bundle`` through the reader ``KasHarness.resolve_spawn`` uses
    (``acp/harness/kas.py`` ``_bundle_route_selected``), for callers outside the ACP
    layer, so the turn gate and the spawn cannot drift. Blocking; may raise."""
    from kiro_crew.acp.harness.kas import _bundle_route_selected

    return _bundle_route_selected()


def person_vault_fenced() -> bool:
    """Whether the usage poll must leave Crew's person vault alone (``crew_client`` on).

    True while the stored sign-in is on an edition's own start URL, or in machine
    mode, where the vault holds no account to read usage for. Blocking; never raises.
    """
    from kiro_crew.auth.login.endpoints import stored_sign_in_registered

    return stored_sign_in_registered() or configured_machine_auth() is not None
