"""KAS machine mode: Crew's own engine reads a host-issued token file itself.

``agent.kas_auth`` says who gives the engine on Crew's own bundle
(``agent.kas_bundle``) its credential:

- ``acp-callback``, the default: the engine asks Crew over ACP
  (``_kiro/auth/getAccessToken``) and Crew answers from its own sign-in.
- ``machine``: a host that issues the agent a machine token in a file starts the
  engine with ``--auth=machine --token-path=<file>``. The engine opens and re-reads
  that file inside its own process. Crew never opens, reads, copies or hashes it:
  it checks the path with ``access(2)`` and ``stat(2)`` alone
  (:func:`token_file_present`), and no Crew text carries a token value.

Machine mode is honoured only while the ``crew_client`` feature preview is on
(``kiro_crew.crew_client_preview``); off, ``agent.kas_auth`` reads as callback mode
whatever it says. It plugs into the engine-credential hook
(``agent_sdk/kas_engine_auth.py``) as Crew's built-in provider, so the spawn, the
environment and the refusal wording follow that hook.

Machine mode has no default service and no default token file.
``agent.kas_endpoint``, ``agent.kas_control_plane_endpoint`` and
``agent.kas_region`` name the cell; a missing or non-https value refuses the spawn
(``machine_cell_missing``) instead of letting the engine choose its own default
host, and an unset or unusable token path refuses it too
(``machine_token_missing``). It runs on the bundle route only, because kiro-cli's
relay has no machine mode (``machine_route_not_bundle``).
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

from kiro_crew.config.sections_agent_fields import KAS_AUTH_MACHINE

logger = logging.getLogger(__name__)

MACHINE_CELL_MISSING = "machine_cell_missing"
MACHINE_TOKEN_MISSING = "machine_token_missing"
MACHINE_ROUTE_NOT_BUNDLE = "machine_route_not_bundle"

#: The engine's own classes for a refused machine credential, as its frames name
#: them (``data.errorType``, or the prose). The text sites report these by name.
KAS_REFUSAL_CLASSES = (
    "ModelRegistryAccessDeniedError",
    "ModelRegistryUnavailableError",
    "ModelRegistryUnauthenticatedError",
    "TokenInvalidError",
    "TokenExpiredError",
)

#: What the model catalog is recorded under in machine mode, where Crew's vault is
#: empty and has no identity to key it by.
MACHINE_CATALOG_IDENTITY_PREFIX = "machine:"


@dataclass(frozen=True)
class MachineAuth:
    """The machine-mode settings one spawn runs under, as configured."""

    token_path: str
    endpoint: str
    control_plane_endpoint: str
    region: str
    token_env: str
    env_drop: tuple[str, ...]


class MachineAuthRefused(Exception):
    """A machine-mode spawn that cannot start. ``refusal`` is the class name."""

    def __init__(self, refusal: str, detail: str) -> None:
        super().__init__(f"{refusal}: {detail}")
        self.refusal = refusal


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def machine_auth(agent: Any) -> MachineAuth | None:
    """The machine settings of a loaded ``agent`` section, or None in callback mode.

    Pure: it does not read the preview. :func:`configured_machine_auth` does.
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
        token_env=_text(getattr(agent, "kas_machine_token_env", "")),
        env_drop=names,
    )


def machine_auth_in(cfg: Any) -> MachineAuth | None:
    """Machine mode as *cfg* states it: the KAS backend, the preview on, ``machine``."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.crew_client_preview import preview_on

    agent = getattr(cfg, "agent", None)
    if getattr(agent, "acp_backend", "") == ACP_BACKEND_KAS and preview_on(cfg):
        return machine_auth(agent)
    return None


def configured_machine_auth() -> MachineAuth | None:
    """Machine mode as this host's config states it (:func:`machine_auth_in`).

    Blocking (reads config); call off the loop. Never raises: a config that cannot
    be read is callback mode, the mode every existing install runs.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.crew_client_preview import preview_on

        # The preview first, from the live configuration: while it is off nothing
        # else is loaded, so every caller costs what it did on main.
        if not preview_on():
            return None
        return machine_auth_in(KiroCrewConfig.load())
    except Exception:  # noqa: BLE001 - an unreadable config keeps callback mode
        logger.debug("KAS machine mode: config unreadable, callback mode", exc_info=True)
        return None


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


def engine_auth_args(machine: MachineAuth) -> list[str]:
    """The engine's auth arguments in machine mode, in its own flag spelling."""
    return [
        "--auth=machine",
        f"--token-path={machine.token_path}",
        f"--endpoint={machine.endpoint}",
        f"--control-plane-endpoint={machine.control_plane_endpoint}",
        f"--region={machine.region}",
    ]


def drop_engine_env(env: MutableMapping[str, str], machine: MachineAuth) -> None:
    """Remove the token variable and every ``kas_machine_env_drop`` name from *env*."""
    for name in (machine.token_env, *machine.env_drop):
        if name:
            env.pop(name, None)


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


def refusal_text(refusal: str, code: object) -> str:
    """``Kiro refused the machine token: <class> (<code>)``."""
    shown = str(code) if isinstance(code, int) and not isinstance(code, bool) else "no code"
    return f"Kiro refused the machine token: {refusal} ({shown})"


@dataclass(frozen=True)
class MachineEngineAuth:
    """Machine mode as an :class:`~kiro_crew.agent_sdk.kas_engine_auth.EngineAuth`."""

    machine: MachineAuth
    relay_refusal: str = (
        f"{MACHINE_ROUTE_NOT_BUNDLE}: machine mode runs only on Kiro Crew's own engine "
        "bundle; set agent.kas_bundle to true"
    )

    @property
    def catalog_identity(self) -> str:
        """The model catalog's key: the token file's path, as the vault holds no identity."""
        return MACHINE_CATALOG_IDENTITY_PREFIX + self.machine.token_path

    def preflight(self) -> None:
        from kiro_crew.agent_sdk.kas_engine_auth import EngineAuthRefused

        try:
            check_spawnable(self.machine)
        except MachineAuthRefused as exc:
            raise EngineAuthRefused(str(exc)) from exc

    def engine_args(self) -> list[str]:
        return engine_auth_args(self.machine)

    def scrub_env(self, env: MutableMapping[str, str]) -> None:
        drop_engine_env(env, self.machine)

    def start_refusal(self, text: str, code: object) -> str:
        """Only an error naming one of :data:`KAS_REFUSAL_CLASSES` answers."""
        refusal = refusal_class(text)
        return refusal_text(refusal, code) if refusal else ""


def builtin_engine_auth() -> MachineEngineAuth | None:
    """Crew's own engine-credential provider: machine mode, when configured."""
    machine = configured_machine_auth()
    return MachineEngineAuth(machine) if machine is not None else None
