"""The Strands harness, launched one process per session by ``AcpClient``.

Kiro Crew runs the official Strands CLI (``@strands-agents/cli``) in its Python-project
ACP mode: ``strands --acp-server --agent <project>``. Four things are this harness's
own, each measured on the verified release (``STRANDS_VERIFIED_VERSION``):

* **The project is Crew's gate module.** In this mode the CLI sends
  ``session/request_permission`` only for a Strands interrupt, and nothing in the CLI
  raises one for a tool call. So ``--agent`` names a sealed copy of
  ``agent_sdk/gate_extensions/strands/kiro_crew_strands_gate.py``, which makes every
  Strands ``Agent`` built in the process -- the bundled default, or the operator's own
  project named by ``agent.strands_project`` -- raise one interrupt per tool call, in
  Kiro Crew's gate envelope. Before the session's own process starts, a read-back boot
  of the same argv proves the module loaded from Crew's file under this session's
  nonce (``Routing.VERIFIED_GATE_EXTENSION`` with ``Readback.LOAD_MARKER``), the way the
  DeepSeek Harness is proved. The CLI's default ``--acp-server`` profile, which asks
  for nothing, is never started.
* **MCP servers travel on the launch, not on the session.** The CLI refuses a
  non-empty ``session/new`` ``mcpServers`` array in project mode ("ACP MCP servers
  cannot be added to an authored source agent"), so the session's projected array is
  written to an owner-only ``--mcp-config`` file, read when the CLI starts and removed
  once ``initialize`` answers, and the wire array stays empty. The CLI's own MCP and
  skill discovery (``~/.kiro/settings/mcp.json``, ``~/.claude.json`` and other tools'
  files) is switched off for the child.
* **Provider keys come from Crew's vault** (``agent.strands_env``), validated against
  the provider names the CLI reads, and the gate withholds them from every shell and
  code child the agent runs.
* **The model is a launch argument.** The CLI takes ``--model`` at start and offers no
  ACP model channel in this mode, so ``auto`` launches with the project's own default.
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import os
import shutil
import stat
import subprocess as subprocess_mod
import tempfile
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable

from kiro_crew import acp_tool_gate, agent_scratch, agent_sdk, model_scope, platform_compat
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp.harness import pi_node
from kiro_crew.acp.harness.base import ProcessAdapter, ProcessSession, SpawnContext, SpawnPlan
from kiro_crew.acp.harness.copilot import _config_directory
from kiro_crew.acp.harness.strands_keys import (
    KEYS_DIR_PREFIX,
    KEYS_FILE_ENV,
    KEYS_FILE_NAME,
    remove_keys_dir,
    write_keys_file,
)
from kiro_crew.acp.mcp_startable import prune_unstartable
from kiro_crew.acp.transport_errors import AcpError, AcpToolGateUnroutable
from kiro_crew.acp.transport_framing import write_frames_blocking
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_STRANDS,
    STRANDS_NODE_FLOOR,
    STRANDS_PYTHON_REQUIREMENT,
    STRANDS_VERIFIED_VERSION,
)
from kiro_crew.config.paths import config_dir
from kiro_crew.config.sections import AGENT_ENV_NAME_GRAMMAR, DEFAULT_MODEL
from kiro_crew.config.sections_agent_env import STRANDS_PROVIDER_CHOICES
from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX, resolve_secret_uris
from kiro_crew.sandbox import (
    agent_env_scrub_prefixes,
    scrub_agent_subprocess_env,
    wrap_argv,
    wrap_argv_async,
)

__all__ = [
    "STRANDS_ENV_NAMES",
    "STRANDS_GATE_SHA256",
    "StrandsLaunch",
    "strands_gate_module_path",
    "strands_installed_version",
    "strands_mcp_config",
    "strands_project_dir",
    "strands_python",
    "strands_python_env_issue",
]

#: The SHA-256 of the shipped gate module, LF line endings. A copy with other bytes is
#: never loaded.
STRANDS_GATE_SHA256 = "c98689ad57d75534087b337fce62791940175164ebcdd5f177bcc518fa0f35f6"

#: What the gate module names itself in its load marker; the routing table's probe.
STRANDS_GATE_NAME = "kiro-crew-strands-gate"

# The gate module reads these and removes them from its own ``os.environ`` on import.
_ENV_GATE_SESSION = "KIROCREW_STRANDS_GATE_SESSION"
_ENV_GATE_MARKER = "KIROCREW_STRANDS_GATE_MARKER"
_ENV_PROJECT = "KIROCREW_STRANDS_PROJECT"
_ENV_STATE_DIR = "KIROCREW_STRANDS_STATE_DIR"
_ENV_WITHHELD = "KIROCREW_STRANDS_WITHHELD"

#: The CLI's own switches for its discovery of other tools' configuration
#: (``tui/workspace/trust.js`` and ``tui/skills.js`` in the verified release). Off for
#: every Crew child: MCP servers come from Crew's ``--mcp-config`` file alone.
STRANDS_DISCOVERY_OFF: dict[str, str] = {
    "STRANDS_CLI_MCP_DISCOVERY": "off",
    "STRANDS_CLI_SKILL_DISCOVERY": "off",
}

#: The provider settings ``agent.strands_env`` may map: the CLI's
#: ``PROVIDER_ENVIRONMENT_KEYS`` (``tui/provider/environment.js``), minus the two that
#: name FILES (``AWS_CONFIG_FILE``, ``AWS_SHARED_CREDENTIALS_FILE``), which would point
#: the agent at a credential store, and minus ``AWS_ACCESS_KEY_ID`` /
#: ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN``, which Kiro Crew's agent
#: environment scrub removes from every agent child (Bedrock travels as
#: ``AWS_BEARER_TOKEN_BEDROCK``), and minus ``AWS_PROFILE``, which names a profile in
#: ``~/.aws`` that the sandbox masks from the child.
STRANDS_ENV_NAMES = frozenset(
    {
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OLLAMA_HOST",
        "OLLAMA_MODEL",
        "LITELLM_BASE_URL",
        "LITELLM_API_KEY",
        "LITELLM_MODEL",
    }
)

#: The CLI's ``PROVIDER_CREDENTIAL_KEYS``. Withheld from the agent's shells and code
#: runs whether or not ``agent.strands_env`` maps them, because the CLI copies keys
#: from its own config into the process environment it hands the project.
STRANDS_CREDENTIAL_NAMES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "LITELLM_API_KEY",
)

#: The npm package the CLI ships as, whose manifest names the installed release.
_STRANDS_NPM_PACKAGE = "@strands-agents/cli"
_STRANDS_MANIFEST_SEARCH_DEPTH = 3

#: Where the launch MCP config files go: the masked auth-store staging directory the
#: Copilot CLI launch file uses, under a prefix of their own.
_MCP_CONFIG_PREFIX = "strands-mcp-"
_STALE_CONFIG_SECS = 600.0

#: How long the read-back boot may take to load the project and publish its marker.
#: Loading a Python project imports the Strands SDK; a cold import measured a few
#: seconds, and a project of the operator's may import more.
_READBACK_TIMEOUT_S = 90.0
_READBACK_POLL_S = 0.1
_READBACK_EXIT_S = 5.0
_MARKER_MAX_BYTES = 64 * 1024
_CANARY_PREFIX = "kirocrew-strands-canary-"

#: The two requests the read-back sends: the handshake, then one session, which is
#: what makes the CLI start the project's Python worker and load the gate.
_READBACK_INITIALIZE_ID = 1
_READBACK_SESSION_ID = 2


def strands_gate_module_path() -> str:
    """The absolute path of the gate module Kiro Crew ships for the Strands harness."""
    return str(
        Path(agent_sdk.__file__).resolve().parent
        / "gate_extensions"
        / "strands"
        / "kiro_crew_strands_gate.py"
    )


def strands_project_dir() -> str:
    """Create the owner-only directory the Strands CLI is pointed at, or refuse.

    ``<crew home>/pi-gate/strands``: inside the gate artifact leaf, so every sandbox
    tier exposes it read-only to this harness's child and fences it from the agent's
    own file tools. It holds the sealed gate module as ``agent.py`` and the Python
    environment the operator creates once as ``.venv`` (the CLI runs a project with
    ``<project>/.venv`` when it exists). Blocking.
    """
    parent = pi_mod._pi_gate_artifact_dir()
    expected = os.path.join(parent, "strands")
    try:
        os.makedirs(expected, mode=0o700, exist_ok=True)
        info = os.lstat(expected)
        if not stat.S_ISDIR(info.st_mode) or platform_compat.is_link_or_junction(expected):
            raise OSError("path is not a real directory")
        if not platform_compat.IS_WINDOWS:
            os.chmod(expected, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
    except OSError as exc:
        raise AcpToolGateUnroutable(
            f"{acp_tool_gate.label_for(ACP_BACKEND_STRANDS)} runs Kiro Crew's gate "
            "module from its own artifact directory, but that directory could not be "
            f"created or secured ({expected}: {exc}). Fix the permissions or free space "
            "under the Kiro Crew config directory to select this harness."
        ) from exc
    return expected


def _seal_strands_gate() -> str:
    """Verify the shipped gate module and return the sealed ``agent.py`` to load. Blocking."""
    return pi_mod._seal_gate_extension(
        strands_gate_module_path(),
        STRANDS_GATE_SHA256,
        artifact_dir=strands_project_dir(),
        sealed_name="agent.py",
        stage_prefix="kirocrew_strands_gate_",
        label="Strands gate module",
    )


def strands_python(project_dir: str) -> str:
    """The interpreter the CLI runs the project with: ``<project>/.venv``'s own."""
    if platform_compat.IS_WINDOWS:
        return os.path.join(project_dir, ".venv", "Scripts", "python.exe")
    return os.path.join(project_dir, ".venv", "bin", "python")


def _site_packages(project_dir: str) -> list[Path]:
    venv = Path(project_dir, ".venv")
    if platform_compat.IS_WINDOWS:
        return [venv / "Lib" / "site-packages"]
    return sorted(venv.glob("lib/python3*/site-packages"))


def strands_python_setup_command(project_dir: str) -> str:
    """The commands that create the project's Python environment, for this OS."""
    venv = os.path.join(project_dir, ".venv")
    python = strands_python(project_dir)
    create = "py -3 -m venv" if platform_compat.IS_WINDOWS else "python3 -m venv"
    joiner = "; " if platform_compat.IS_WINDOWS else " && "
    return f'{create} "{venv}"{joiner}{_run_quoted(python)} -m pip install "{STRANDS_PYTHON_REQUIREMENT}"'


def _run_quoted(program: str) -> str:
    """*program* quoted so the operator's shell RUNS it: PowerShell reads a bare quoted
    path as a string expression, so on Windows it takes the ``&`` call operator."""
    return f'& "{program}"' if platform_compat.IS_WINDOWS else f'"{program}"'


def strands_python_env_issue(project_dir: str) -> str:
    """Why the Python environment the CLI would run the project with is not ready, or ``""``.

    File reads only, so the install probe can poll it: the interpreter exists, and the
    ``strands_harness`` package the CLI's worker imports is installed beside it.
    """
    python = strands_python(project_dir)
    if not os.path.isfile(python):
        return (
            "the Strands harness needs a Python environment (3.10 or newer) at "
            f"{os.path.join(project_dir, '.venv')}, and there is none. Create it with: "
            f"{strands_python_setup_command(project_dir)}"
        )
    if not any((site / "strands_harness").is_dir() for site in _site_packages(project_dir)):
        return (
            f"the Python environment at {os.path.join(project_dir, '.venv')} does not "
            f"have {STRANDS_PYTHON_REQUIREMENT} installed. Install it with: "
            f'{_run_quoted(python)} -m pip install "{STRANDS_PYTHON_REQUIREMENT}"'
        )
    return ""


def strands_installed_version(strands_bin: str) -> str:
    """The release the npm install *strands_bin* runs, or ``""`` when unreadable.

    Read from ``@strands-agents/cli``'s own ``package.json``, never by running it. On
    POSIX the bin link resolves into the package; on Windows the bin is a
    ``strands.cmd`` shim in the npm prefix, with the package under that directory's
    ``node_modules``. Blocking (reads files).
    """
    try:
        here = Path(os.path.realpath(strands_bin)).parent
    except (OSError, ValueError):
        return ""
    roots = [here / "node_modules"]
    if here.name == ".bin":
        roots.append(here.parent)
    candidates = [root / _STRANDS_NPM_PACKAGE / "package.json" for root in roots]
    candidates += [
        directory / "package.json"
        for directory in [here, *here.parents][:_STRANDS_MANIFEST_SEARCH_DEPTH]
    ]
    for manifest in candidates:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name") == _STRANDS_NPM_PACKAGE:
            version = data.get("version")
            return version.strip() if isinstance(version, str) else ""
    return ""


def _strands_version_issue(strands_bin: str) -> str:
    """Why this install must not serve a session, or ``""``. Blocking."""
    found = strands_installed_version(strands_bin)
    if found and found != STRANDS_VERIFIED_VERSION:
        return (
            f"Kiro Crew's Strands gate was verified on Strands CLI {STRANDS_VERIFIED_VERSION} "
            f"and this machine has {found}. Install the verified release with "
            f"'npm i -g {_STRANDS_NPM_PACKAGE}@{STRANDS_VERIFIED_VERSION}', then start a new chat."
        )
    return ""


# ── agent.strands_env: provider settings from Crew's vault ──

#: Names Kiro Crew sets on this child itself, which a mapping must not overwrite.
_STRANDS_ENV_RESERVED_PREFIXES = ("KIROCREW_", "STRANDS_")


def _validate_strands_env_mapping(mapping: dict[str, str]) -> None:
    """Refuse every ``agent.strands_env`` entry this harness would not honour.

    Raises :exc:`ValueError` naming ONLY the env-var key, never the vault name or the
    value, so the message is safe on the log and in the chat error card. The rules
    of ``agent.deepseek_env``, with an allowlist where that map had a scrub class: a
    name the CLI does not read for a provider is refused rather than injected, so a
    mapping cannot set ``NODE_OPTIONS`` or a ``STRANDS_CLI_*`` switch on the child.
    """
    scrub_prefixes = agent_env_scrub_prefixes()
    for key, value in mapping.items():
        if not value.startswith(SECRET_URI_PREFIX):
            raise ValueError(
                f"agent.strands_env entry {key!r} holds a literal value. This mapping "
                f"takes a '{SECRET_URI_PREFIX}<vault name>' reference only, so a provider "
                "key is never stored in config.json. Save the key under Settings > "
                f"Secrets, then map it as '{SECRET_URI_PREFIX}<vault name>'."
            )
        if not AGENT_ENV_NAME_GRAMMAR.fullmatch(key) or key.startswith(
            _STRANDS_ENV_RESERVED_PREFIXES
        ):
            raise ValueError(
                f"agent.strands_env entry {key!r} is not a provider setting Kiro Crew "
                "hands the Strands harness. Use one of: " + ", ".join(sorted(STRANDS_ENV_NAMES))
            )
        if key not in STRANDS_ENV_NAMES:
            raise ValueError(
                f"agent.strands_env entry {key!r} is not one of the provider settings the "
                "Strands CLI reads. Use one of: " + ", ".join(sorted(STRANDS_ENV_NAMES))
            )
        if any(key.startswith(prefix) for prefix in scrub_prefixes):
            raise ValueError(
                f"agent.strands_env entry {key!r} matches a name prefix Kiro Crew's own "
                "agent environment scrub removes before the child starts, so the "
                "harness would start without it."
            )


def _strands_env_mapping() -> dict[str, str]:
    from kiro_crew.config.loader import KiroCrewConfig

    return dict(KiroCrewConfig.load().agent.strands_env)


def _strands_vault_env_names() -> tuple[str, ...]:
    """The validated ``agent.strands_env`` names, sorted; the vault is not opened. Blocking."""
    mapping = _strands_env_mapping()
    if not mapping:
        return ()
    _validate_strands_env_mapping(mapping)
    return tuple(sorted(mapping))


def _strands_vault_env() -> dict[str, str]:
    """``agent.strands_env`` validated and resolved from the vault. Blocking.

    Every failure is a :exc:`ValueError` (a refused name, a malformed reference, a
    secret absent from the vault), which the caller turns into a refused session.
    """
    mapping = _strands_env_mapping()
    if not mapping:
        return {}
    _validate_strands_env_mapping(mapping)
    resolved, _secret_keys = resolve_secret_uris(
        mapping, Path(config_dir()), subject="agent.strands_env"
    )
    return resolved


def _strands_user_project() -> str:
    """The operator's project from ``agent.strands_project``, absolute, or ``""``.

    Raises :exc:`ValueError` when the setting names nothing loadable, so the session
    is refused with the path rather than started on the bundled project instead.
    Blocking (reads config and stats the path).
    """
    from kiro_crew.config.loader import KiroCrewConfig

    raw = KiroCrewConfig.load().agent.strands_project
    if not raw:
        return ""
    target = Path(raw).expanduser()
    entry = target / "agent.py" if target.is_dir() else target
    if not entry.is_file() or entry.suffix != ".py":
        raise ValueError(
            f"agent.strands_project names {raw!r}, which is not a Strands Python project. "
            "Point it at the project's agent.py, or at a folder holding one."
        )
    return str(entry.resolve())


#: The provider prefixes the pinned CLI's ``--model`` accepts
#: (``provider/<model>``, ``tui/provider/environment.js`` in 0.2.3).
STRANDS_PROVIDERS = frozenset(STRANDS_PROVIDER_CHOICES)


def strands_configured_model(provider: str, model: str) -> str:
    """The ``--model`` value ``agent.strands_provider`` + ``agent.strands_model`` name.

    ``"auto"`` (or an empty model) is ``""``: no ``--model`` flag, so the provider's
    CLI default runs. Any other model is ``<provider>/<model>``, written once when the
    model already carries this provider's prefix. An empty provider is the
    setting's default, ``bedrock``. Raises :exc:`ValueError` naming the setting for a
    provider the CLI does not know, so the session is refused rather than started on
    a model the operator did not choose.
    """
    provider = provider.strip().lower() or STRANDS_PROVIDER_CHOICES[0]
    model = model.strip()
    if provider not in STRANDS_PROVIDERS:
        raise ValueError(
            f"agent.strands_provider is {provider!r}, which the Strands CLI does not "
            "know. Use one of: " + ", ".join(STRANDS_PROVIDER_CHOICES) + "."
        )
    if not model or model == DEFAULT_MODEL:
        return ""
    if model.startswith(f"{provider}/"):
        return model
    return f"{provider}/{model}"


def _strands_configured_model() -> str:
    """:func:`strands_configured_model` over the live config. Blocking (reads config)."""
    from kiro_crew.config.loader import KiroCrewConfig

    agent = KiroCrewConfig.load().agent
    return strands_configured_model(agent.strands_provider, agent.strands_model)


def _state_dir(home: Path) -> str:
    """Where the CLI stores this harness's sessions: under its own ``~/.strands/cli``.

    Never the session's working directory, which may be the operator's repository.
    """
    return str(home / ".strands" / "cli" / "kirocrew-sessions")


# ── the launch MCP config ──


def strands_mcp_config(elements: list[Any]) -> dict[str, Any] | None:
    """The CLI's ``mcpServers`` document for the session's projected ACP array.

    *elements* are ``acp_server_element`` shapes. A stdio element becomes
    ``{command, args, env}``; an ``http`` or ``sse`` one ``{url, transport, headers}``.
    Each server has ``continueOnError`` so one server that cannot start costs that
    server, as on the wire. ``None`` when nothing is left.
    """
    servers: dict[str, Any] = {}
    for element in elements:
        if not isinstance(element, dict):
            continue
        name = element.get("name")
        if not isinstance(name, str) or not name:
            continue
        url = element.get("url")
        if isinstance(url, str) and url:
            headers = {
                pair["name"]: str(pair.get("value", ""))
                for pair in element.get("headers") or []
                if isinstance(pair, dict) and isinstance(pair.get("name"), str)
            }
            transport = "sse" if element.get("type") == "sse" else "streamable-http"
            servers[name] = {"url": url, "transport": transport, "headers": headers}
            continue
        command = element.get("command")
        if not isinstance(command, str) or not command:
            continue
        args = element.get("args")
        env = {
            pair["name"]: str(pair.get("value", ""))
            for pair in element.get("env") or []
            if isinstance(pair, dict) and isinstance(pair.get("name"), str)
        }
        servers[name] = {
            "command": command,
            "args": [str(arg) for arg in args] if isinstance(args, list) else [],
            "env": env,
            "continueOnError": True,
        }
    return {"mcpServers": servers} if servers else None


def _sweep_stale_configs(directory: str) -> None:
    now = time.time()
    own = os.getpid()
    for name in os.listdir(directory):
        if not name.startswith(_MCP_CONFIG_PREFIX):
            continue
        pid_text = name[len(_MCP_CONFIG_PREFIX) :].split("-", 1)[0]
        full = os.path.join(directory, name)
        try:
            pid = int(pid_text)
            mtime = os.lstat(full).st_mtime
        except (ValueError, OSError):
            continue
        dead = pid != own and not platform_compat.pid_exists(pid)
        if dead or (pid == own and now - mtime > _STALE_CONFIG_SECS):
            with suppress(OSError):
                os.remove(full)


def write_strands_mcp_config(document: dict[str, Any]) -> str:
    """Write *document* to a fresh owner-only file and return its path. Blocking.

    The file carries a session credential (the elements' ``env``), which is why it is
    a file in the masked staging directory and not inline JSON on the argv.
    """
    directory = _config_directory()
    _sweep_stale_configs(directory)
    fd, path = tempfile.mkstemp(
        prefix=f"{_MCP_CONFIG_PREFIX}{os.getpid()}-", suffix=".json", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
    except BaseException:
        discard_strands_mcp_config(path)
        raise
    return path


def discard_strands_mcp_config(path: str) -> None:
    """Remove a launch config; one already gone is the same outcome. Blocking."""
    if path:
        with suppress(FileNotFoundError):
            os.remove(path)


def strands_model_args(model: str | None) -> list[str]:
    """``["--model", <id>]`` for a model the CLI should run, or ``[]`` for its default.

    ``auto`` (and no model) leaves the project's own default. A pin another harness's
    catalog owns -- a Kiro model picked before switching here -- is left out rather
    than handed to a CLI that cannot run it.
    """
    if not model or model == DEFAULT_MODEL:
        return []
    provider, sep, rest = model.partition("/")
    # A ``<provider>/<model>`` id naming a Strands provider is Strands' own spelling,
    # whatever another harness's cached catalog also lists: kept as picked.
    if sep and rest and provider in STRANDS_PROVIDERS:
        return ["--model", model]
    if model_scope.foreign_namespaces(model, "strands"):
        return []
    return ["--model", model]


def strands_launch_model_args(chat_model: str | None, configured: Callable[[], str]) -> list[str]:
    """The ``--model`` arguments a Strands session launches with. Blocking.

    A model picked for this chat wins. Only when there is none is *configured* (the
    ``agent.strands_provider`` / ``agent.strands_model`` reader) called, so a setting
    the chat does not use can never refuse it; its :exc:`ValueError` propagates.
    Neither means ``[]``: the project's own default.
    """
    picked = strands_model_args(chat_model)
    if picked:
        return picked
    fallback = configured()
    return ["--model", fallback] if fallback else []


def strands_launched_model(model_args: list[str]) -> str:
    """The model a Strands launch passes as ``--model``, or ``""`` when it passes none."""
    return model_args[1] if len(model_args) == 2 and model_args[0] == "--model" else ""


# ── the read-back ──


def _strands_node_choice(
    session: ProcessSession, strands_bin: str, wrap: pi_node.ProbeWrap
) -> pi_node.PiNodeChoice:
    """How to run *strands_bin* so the Node under it meets the CLI's floor. Blocking.

    Searched on the PATH the CLI child inherits: the session's environment with the
    augmentation the read-back's environment gets, in the directory the CLI starts
    in, each ``node --version`` under *wrap* -- exactly as pi's launch measures its
    own (``pi._pi_node_choice``).
    """
    from kiro_crew.acp.client import augmented_path

    env = {**os.environ, **session._agent_env_for_spawn(), **session._extra_env}
    return pi_node.choose_pi_node(
        strands_bin,
        STRANDS_NODE_FLOOR,
        augmented_path(env.get("PATH", "")),
        cwd=session._spawn_work_dir,
        wrap=wrap,
        label="The Strands CLI",
    )


#: What :func:`_sweep_keys_dirs` can answer besides ``""`` (proven clear). Codes, not
#: text: the refusal is worded by its caller, so nothing the sweep read reaches it.
_SWEEP_UNLISTABLE = "unlistable"
_SWEEP_KEY_LEFT = "key-left"


def _sweep_keys_dirs(directories: tuple[str, ...]) -> str:
    """Remove key files an earlier spawn of this session tree left behind. Blocking.

    The gate removes its file on load, so one is left only when a spawn died before
    the gate ran; a respawn that joins the same scratch tree must not hand its shells
    that file. Returns ``""`` once every window is PROVEN clear, else
    :data:`_SWEEP_UNLISTABLE` for a window that cannot be listed or
    :data:`_SWEEP_KEY_LEFT` for a key file still there (or not inspectable) after
    removal; either refuses the spawn rather than starting a child that could read it. A gone window
    is clear. A planted link with the prefix is never followed or removed: it is not
    a directory Kiro Crew wrote, so it holds none of its keys.
    """
    for directory in directories:
        if not directory:
            continue
        try:
            entries = os.listdir(directory)
        except FileNotFoundError:
            continue
        except OSError:
            return _SWEEP_UNLISTABLE
        # Only the directories found here are verified: one another spawn of the tree
        # writes after this listing is its own live file, not an old one.
        swept = [
            os.path.join(directory, entry) for entry in entries if entry.startswith(KEYS_DIR_PREFIX)
        ]
        for candidate in swept:
            remove_keys_dir(candidate)
        if any(_key_file_left(candidate) for candidate in swept):
            return _SWEEP_KEY_LEFT
    return ""


def _key_file_left(candidate: str) -> bool:
    """Whether *candidate* may still hold a key file (``False`` only when it provably does not).

    The key is only ever in ``<dir>/keys.json``, so that file is what is checked. Gone
    is proven by FileNotFoundError alone: any other error refuses. A prefixed link is
    never a directory Kiro Crew wrote, so it holds none of its keys and is skipped.
    """
    try:
        info = os.lstat(candidate)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not stat.S_ISDIR(info.st_mode):
        return False
    try:
        os.lstat(os.path.join(candidate, KEYS_FILE_NAME))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _read_marker(marker_path: str) -> object:
    """Read the child-written marker without following, blocking, or growing memory."""
    try:
        fd = platform_compat.open_file_no_reparse(marker_path, nonblocking=True)
    except OSError:
        return None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or not 0 <= metadata.st_size <= _MARKER_MAX_BYTES:
            return None
        payload = os.read(fd, metadata.st_size + 1)
        if len(payload) != metadata.st_size:
            return None
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return json.loads(payload)
    except (ValueError, RecursionError):
        return None


def _readback_requests(cwd: str) -> list[dict[str, Any]]:
    """The handshake and one ``session/new`` the read-back sends."""
    return [
        {
            "jsonrpc": "2.0",
            "id": _READBACK_INITIALIZE_ID,
            "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": _READBACK_SESSION_ID,
            "method": "session/new",
            "params": {"cwd": cwd, "mcpServers": []},
        },
    ]


def remedy_for_strands() -> str:
    return acp_tool_gate.remediation_for(ACP_BACKEND_STRANDS)


def _verify_strands_gate(
    session: ProcessSession,
    argv: list[str],
    env: dict[str, str],
    sealed_module: str,
    marker_path: str,
    nonce: str,
    *,
    child_scrub_names: tuple[str, ...],
) -> tuple[str, str]:
    """Boot the CLI once on the session's argv and read the gate's load marker back.

    The child gets the marker path and the nonce, plus a canary under each name of
    *child_scrub_names* so the gate can prove it withholds them from its shells. It
    answers ``initialize``, then ``session/new`` starts the project worker, which
    loads the gate and publishes the marker. stdin is closed once the marker exists,
    which ends the CLI. Returns ``("", "")`` only for this session's own marker, from
    Crew's sealed module, with every canary withheld. Blocking.
    """
    with suppress(OSError):
        os.unlink(marker_path)
    env = dict(env)
    env[_ENV_GATE_MARKER] = marker_path
    env[_ENV_GATE_SESSION] = nonce
    env[_ENV_WITHHELD] = ":".join(child_scrub_names)
    # The canaries take the session's own route, the one-time key file, so the
    # marker proves that route: loaded, gone from every launch environment, and
    # withheld from the gate's shells.
    for name in child_scrub_names:
        env.pop(name, None)
    if child_scrub_names:
        try:
            env[KEYS_FILE_ENV] = write_keys_file(
                os.path.dirname(marker_path),
                {name: f"{_CANARY_PREFIX}{nonce}" for name in child_scrub_names},
            )
        except OSError as exc:
            return f"the gate's key file could not be written ({exc})", remedy_for_strands()
    remedy = remedy_for_strands()
    requests = _readback_requests(session._spawn_work_dir or os.getcwd())
    try:
        # A dry run of the very write below, before any child exists: a request too
        # large for the blocking pipe (a very long workspace path) is refused here
        # instead of failing once the probe is running.
        write_frames_blocking(io.BytesIO(), requests)
    except ValueError as exc:
        return (
            f"the gate's read-back requests do not fit the probe's pipe ({exc}); start "
            "the chat in a workspace with a shorter path",
            remedy,
        )
    deadline = time.monotonic() + _READBACK_TIMEOUT_S
    try:
        process = subprocess_mod.Popen(
            argv,
            cwd=session._spawn_work_dir or None,
            env=env,
            stdin=subprocess_mod.PIPE,
            stdout=subprocess_mod.DEVNULL,
            stderr=subprocess_mod.DEVNULL,
        )
    except (OSError, subprocess_mod.SubprocessError) as exc:
        return f"the gate's load marker could not be read back ({exc})", remedy
    published = False
    try:
        if process.stdin is not None:
            write_frames_blocking(process.stdin, requests)
        while time.monotonic() < deadline:
            if os.path.lexists(marker_path):
                published = True
                break
            if process.poll() is not None:
                break
            time.sleep(_READBACK_POLL_S)
        with suppress(OSError):
            if process.stdin is not None:
                process.stdin.close()
        try:
            process.wait(timeout=_READBACK_EXIT_S if published else 0)
        except subprocess_mod.TimeoutExpired:
            process.kill()
            with suppress(subprocess_mod.SubprocessError, OSError):
                process.wait(timeout=_READBACK_EXIT_S)
    except (OSError, subprocess_mod.SubprocessError, ValueError) as exc:
        return f"the gate's load marker could not be read back ({exc})", remedy
    finally:
        # Whatever ended the read-back, the probe child never outlives it.
        if process.poll() is None:
            with suppress(OSError):
                if process.stdin is not None:
                    process.stdin.close()
            with suppress(OSError, subprocess_mod.SubprocessError):
                process.kill()
            with suppress(OSError, subprocess_mod.SubprocessError):
                process.wait(timeout=_READBACK_EXIT_S)
    if not published and not os.path.lexists(marker_path):
        return (
            "the Strands project did not load Kiro Crew's gate within "
            f"{_READBACK_TIMEOUT_S:.0f}s of starting the harness (the project failed to "
            "load, or its agent could not be gated), so its tools would not ask",
            remedy,
        )
    marker = _read_marker(marker_path)
    issue = acp_tool_gate.gate_marker_issue(
        ACP_BACKEND_STRANDS,
        marker,
        pi_mod._same_file_spelling(sealed_module),
        nonce,
        child_scrub_names=child_scrub_names,
    )
    return (issue, remedy) if issue else ("", "")


def _refuse(issue: str, remedy: str = "") -> None:
    """Refuse the session through the routing enforcement, typed for the ACP layer."""
    try:
        acp_tool_gate.enforce_runtime_routing(
            ACP_BACKEND_STRANDS,
            issue,
            remedy=remedy or acp_tool_gate.remediation_for(ACP_BACKEND_STRANDS),
        )
    except acp_tool_gate.ToolGateUnroutable as exc:
        raise AcpToolGateUnroutable(str(exc)) from None
    # Unreachable while this harness is enforced; the fail-closed floor if it ever is not.
    raise AcpToolGateUnroutable(issue)


class StrandsLaunch(ProcessAdapter):
    """The Strands CLI in Python-project mode, on Crew's gate module, verified by a boot."""

    backend = ACP_BACKEND_STRANDS

    def __init__(self) -> None:
        self._nonce = ""
        self._project = ""
        self._state_dir = ""
        self._session: ProcessSession | None = None

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the CLI, check its Node, seal the gate, write MCP, then prove the gate."""
        session = ctx.session
        assert session is not None, "the Strands harness is launched for one session"
        self._session = session
        strands_bin, argv, spawn_label, stderr_label = await launch_mod.resolve_self_served_launch(
            self.backend
        )
        version_issue = await asyncio.to_thread(_strands_version_issue, strands_bin)
        if version_issue:
            raise AcpError(version_issue, transient=False, user_worded=True)
        try:
            project_dir = await asyncio.to_thread(strands_project_dir)
            sealed_module = await asyncio.to_thread(_seal_strands_gate)
        except pi_mod.PiGateExtensionTampered as exc:
            _refuse(str(exc))
            raise  # pragma: no cover - _refuse always raises
        python_issue = await asyncio.to_thread(strands_python_env_issue, project_dir)
        if python_issue:
            raise AcpError(
                f"{python_issue}, then start a new chat.", transient=False, user_worded=True
            )
        try:
            self._project = await asyncio.to_thread(_strands_user_project)
            vault_env_names = await asyncio.to_thread(_strands_vault_env_names)
        except ValueError as exc:
            raise AcpError(str(exc), transient=False, user_worded=True) from None
        self._state_dir = _state_dir(ctx.home)
        await asyncio.to_thread(os.makedirs, self._state_dir, 0o700, True)
        try:
            model_args = await asyncio.to_thread(
                strands_launch_model_args, ctx.model or session._model, _strands_configured_model
            )
        except ValueError as exc:
            raise AcpError(str(exc), transient=False, user_worded=True) from None
        argv = [*argv, "--agent", sealed_module, *model_args]
        # What the session reports as its model is what it launched with: a pin
        # this harness left out (another harness's) never ran, so it is not kept.
        session._model = strands_launched_model(model_args) or DEFAULT_MODEL
        # The session's projected array, translated into the launch file. The wire
        # array stays empty: the CLI refuses a non-empty one in project mode.
        await session._prepare_session_mcp()
        kept, _pruned = await asyncio.to_thread(prune_unstartable, session._session_mcp_servers())
        document = strands_mcp_config(kept)
        # What the session report and the unresolved-ref guard judge: the wire array
        # stays empty for this harness, so they would otherwise call every spec
        # server unconfigured while the launch file mounts it.
        session._launch_mcp_servers = list(kept)
        await session._discard_copilot_mcp_config()
        hidden = await launch_mod._run_preflight_bounded(
            launch_mod._sandbox_preflight, self.backend, ctx.sandbox_mode
        )
        expose = acp_tool_gate.adapter_expose_files(self.backend, hidden)
        # The CLI is a Node script whose shebang finds ``node`` on the child's PATH;
        # below its ``engines`` floor it dies at load with nothing on the wire. The
        # same choice pi's launch makes, measured the way pi measures it: on the PATH
        # the child inherits (the session's environment, augmented as the read-back's
        # is), in the directory the CLI starts in (a version-manager shim picks its
        # Node by directory), with each ``node --version`` confined in the session's
        # sandbox. A Node that meets the floor beside the install or later on PATH
        # runs the script by path, otherwise a refusal names the Node found and the
        # floor. Windows' ``strands.cmd`` picks its own.
        choice = await asyncio.to_thread(
            _strands_node_choice,
            session,
            strands_bin,
            pi_mod._pi_probe_wrap(ctx.sandbox_mode, hidden, expose),
        )
        if choice.issue:
            raise AcpError(choice.issue, transient=False, user_worded=True)
        if choice.argv:
            argv = [*choice.argv, *argv[1:]]
        self._nonce = uuid.uuid4().hex
        # The envelope nonce: a permission frame is read as the gate's envelope only
        # under it, and the completed-unasked tripwire keys on it.
        session._pi_gate_nonce = self._nonce
        spawn_label = " ".join([spawn_label, "--agent", sealed_module])
        stderr_label = spawn_label
        await self._prove_gate(session, ctx, argv, sealed_module, vault_env_names, hidden, expose)
        if document is not None:
            try:
                path = await asyncio.to_thread(write_strands_mcp_config, document)
            except OSError as exc:
                raise AcpError(
                    "Kiro Crew could not write the MCP server list this Strands session "
                    f"starts with ({exc}). Fix the permissions or free space under the "
                    "Kiro Crew config directory and start a new chat.",
                    transient=False,
                    user_worded=True,
                ) from exc
            session._copilot_mcp_config_path = path
            argv = [*argv, "--mcp-config", path]
        return SpawnPlan(
            argv=argv,
            spawn_label=spawn_label,
            stderr_label=stderr_label,
            extra_hidden_dirs=hidden,
            extra_expose_files=(
                (*expose, session._copilot_mcp_config_path)
                if session._copilot_mcp_config_path
                else expose
            ),
        )

    async def _prove_gate(
        self,
        session: ProcessSession,
        ctx: SpawnContext,
        argv: list[str],
        sealed_module: str,
        vault_env_names: tuple[str, ...],
        hidden: tuple[str, ...],
        expose: tuple[str, ...],
    ) -> None:
        """The read-back boot, in a throwaway private window, sandboxed like the session."""
        from kiro_crew.acp.client import _resolve_spawn_env, augmented_path

        try:
            probe_dir = await asyncio.to_thread(
                agent_scratch.allocate_scratch,
                f"{session._session_key or 'session'}-strands-probe",
            )
        except (OSError, agent_scratch.ScratchBoundaryError):
            _refuse(
                "the gate's load marker has nowhere to be written: this session got no "
                "private scratch directory"
            )
        marker = os.path.join(str(probe_dir), f"kirocrew_strands_gate_{self._nonce}.marker.json")

        def build_env() -> dict[str, str]:
            # Off the loop: the first agent-env read this spawn reads config, and
            # _resolve_spawn_env repairs credential sockets with stat/glob calls.
            built = scrub_agent_subprocess_env(
                _resolve_spawn_env(
                    {**os.environ, **session._agent_env_for_spawn(), **session._extra_env},
                    kiro_api_key=False,
                )
            )
            built["PATH"] = augmented_path(built.get("PATH", ""))
            return built

        env = await asyncio.to_thread(build_env)
        env.update(STRANDS_DISCOVERY_OFF)
        # The probe's sessions go in its own window, never the operator's store.
        env[_ENV_STATE_DIR] = os.path.join(str(probe_dir), "sessions")
        if self._project:
            env[_ENV_PROJECT] = self._project
        cleanup: str | None = None
        try:
            probe_argv, cleanup = await wrap_argv_async(
                argv,
                mode=ctx.sandbox_mode,
                strip_python_env=True,
                extra_hidden_dirs=hidden,
                extra_private_dirs=(str(probe_dir),),
                extra_expose_files=expose,
                _prepare=wrap_argv,
            )
            issue, remedy = await asyncio.to_thread(
                functools.partial(
                    _verify_strands_gate,
                    session,
                    probe_argv,
                    env,
                    sealed_module,
                    marker,
                    self._nonce,
                    child_scrub_names=vault_env_names,
                )
            )
        finally:
            if cleanup:
                await asyncio.to_thread(launch_mod._unlink_readback_launcher, cleanup)
            await asyncio.to_thread(shutil.rmtree, probe_dir, ignore_errors=True)
        if issue:
            _refuse(issue, remedy)

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Discovery off, the nonce, the state directory, the project and the withheld names.

        No marker path: the one marker is the read-back's, already judged.
        """
        env.update(STRANDS_DISCOVERY_OFF)
        if self._nonce:
            env[_ENV_GATE_SESSION] = self._nonce
        if self._state_dir:
            env[_ENV_STATE_DIR] = self._state_dir
        if self._project:
            env[_ENV_PROJECT] = self._project

    async def prepare_spawn_env(
        self,
        env: dict[str, str],
        *,
        to_thread: Callable[..., Awaitable[Any]],
    ) -> None:
        """The launch environment, then the provider settings from Crew's own vault.

        The settings never enter ``env``: a launch environment stays readable from
        ``/proc/<pid>/environ`` for the process's whole life, by every shell its
        descendants run. They go to an owner-only one-time file in this session's
        private scratch window, which the gate module reads and removes before any
        tool can run (``acp/harness/strands_keys.py``). No private window means
        nowhere safe to put them, so the session is refused.
        """
        self.apply_spawn_env(env)
        try:
            vault_env = await to_thread(_strands_vault_env)
        except ValueError as exc:
            _refuse(str(exc))
        withheld = sorted({*vault_env, *STRANDS_CREDENTIAL_NAMES})
        for name in withheld:
            env.pop(name, None)
        env[_ENV_WITHHELD] = ":".join(withheld)
        try:
            scratch = getattr(self._session, "_scratch_dir", None)
            shared = getattr(self._session, "_shared_scratch", None)
            # Every spawn, keys or none: a respawn re-exposes the previous window, so
            # a file an earlier spawn left (it died before the gate loaded it) must be
            # gone before this child starts, even when the mapping is now empty.
            windows = (str(scratch) if scratch else "", str(shared) if shared else "")
            left = await to_thread(_sweep_keys_dirs, windows)
            if left:
                # Built from constants and the session's own paths: the sweep's
                # answer only selects which sentence, never supplies text.
                where = " and ".join(w for w in windows if w)
                cause = (
                    "cannot be checked for old provider key files"
                    if left == _SWEEP_UNLISTABLE
                    else "still holds an old provider key file Kiro Crew could not remove"
                )
                _refuse(
                    f"a scratch window of this session ({where}) {cause}, so a child "
                    f"started now could read old keys; delete its {KEYS_DIR_PREFIX}* "
                    "directories and start a new chat"
                )
            if vault_env:
                if scratch is None:
                    _refuse(
                        "the provider keys in agent.strands_env have nowhere private to "
                        "go: this session got no private scratch directory, and a key in "
                        "the launch environment would be readable by the agent's shells"
                    )
                env[KEYS_FILE_ENV] = await to_thread(write_keys_file, str(scratch), vault_env)
        finally:
            # The resolver's dict is the last holder of the plaintext in this process.
            vault_env.clear()
