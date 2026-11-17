"""Slim start for the stdio MCP servers: skip importing :mod:`kiro_crew.cli`.

Importing the whole CLI before dispatch would pull aiohttp, boto3/botocore,
awscrt and slack_sdk, none of which a stdio MCP server uses, and every agent
session runs several of these servers. This entry runs the same start-up steps
``cli.main`` runs for an ``mcp-*`` command, then the server, without that import.

It only takes the common case: the bare ``kirocrew mcp-<name>`` argv the
gateway launches, a valid (or unset) ``KIROCREW_PORT``, and a stderr that is not
the gateway log. Anything else returns ``False`` and the caller runs the full
CLI, which owns every other shape (``--help``, ``-v``, a bad port, a detached
process).

A server in :data:`EARLY_ANSWERS` answers ``initialize`` and ``tools/list`` here,
before its tool handlers are imported: an agent engine starts every MCP server of
a session at session start and holds the first prompt until each has listed its
tools, so everything loaded before that answer is paid on every session start. It
first asks its gateway for both answers, before it loads its configuration or the
platform (``kiro_crew.mcp_listing_relay``), and relays them. With no such answers
it builds both itself once the platform has booted (``kiro_crew.mcp_shared_listing``).
"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Sequence
from typing import Any, Callable

# command -> (module, entry function). Same targets ``cli.main`` dispatches to.
SLIM_SERVERS: dict[str, tuple[str, str]] = {
    "mcp-core": ("kiro_crew.mcp_core", "run_mcp_core_server"),
    "mcp-cron": ("kiro_crew.mcp_cron", "run_mcp_server"),
    "mcp-guide": ("kiro_crew.mcp_guide", "run_mcp_server"),
    "mcp-mate": ("kiro_crew.mcp_mate", "run_mcp_server"),
    "mcp-panel": ("kiro_crew.mcp_panel", "run_mcp_server"),
    "mcp-dashboard": ("kiro_crew.mcp_dashboard", "run_mcp_server"),
    # The same dispatch in cli.main, and the same light import (no aiohttp,
    # boto3 or slack_sdk; test_mcp_slim_entry checks each module).
    "mcp-work": ("kiro_crew.mcp_work", "run_mcp_server"),
    "mcp-computer": ("kiro_crew.mcp_computer", "run_mcp_server"),
    "mcp-crew-log": ("kiro_crew.mcp_crew_log", "run_mcp_server"),
    "mcp-debug": ("kiro_crew.mcp_debug", "run_mcp_server"),
    "mcp-secrets": ("kiro_crew.mcp_secrets", "run_mcp_server"),
}

#: command -> (module, attribute) of the server's ``StdioServer`` declaration, for the
#: servers that answer ``initialize`` and ``tools/list`` before their handlers load.
#: The module must import no tool handler; ``test_mcp_listing_import_budget`` holds
#: what each one loads by the time it has listed.
EARLY_ANSWERS: dict[str, tuple[str, str]] = {
    "mcp-core": ("kiro_crew.mcp_core_tool_list", "SERVER"),
    "mcp-cron": ("kiro_crew.mcp_cron_tool_list", "SERVER"),
    "mcp-guide": ("kiro_crew.mcp_guide_tool_list", "SERVER"),
    "mcp-mate": ("kiro_crew.mcp_mate_tool_list", "SERVER"),
}


def lists_before_handlers(argv: Sequence[str]) -> bool:
    """Whether the server *argv* starts lists its tools before it has loaded its handlers.

    *argv* is what follows the command, as a spec's ``args`` holds it. Such a server's
    ``tools/list`` answer does not show that its handlers load: a caller that needs to
    know the server can run a tool (the discovery probe) asks for one more answer, which
    only its dispatch loop gives.
    """
    return bool(argv) and argv[-1] in EARLY_ANSWERS


def _relay_before_boot(command: str) -> Any:
    """Answer the client's first two requests with the gateway's answers, if it has them.

    For a server that declares itself, after the root-run refusal and before anything
    heavier is imported. A server whose stderr is the gateway log is left alone: the
    full CLI owns that shape and this entry is about to hand it over. Returns the
    frames read (``mcp_listing_relay.RelayedFrames``), or ``None`` when the gateway
    gave no answers, which leaves every later step as it was.

    A gateway answers only where the list it builds is the one this server would
    build. It does not on a platform that composes an overlay, so a server that fails
    closed on its platform still ends before it has answered anything.
    """
    if command not in EARLY_ANSWERS:
        return None
    from kiro_crew.config import paths as config_paths

    if _stderr_is(config_paths.config_dir() / "gateway.log"):
        return None
    from kiro_crew.mcp_listing_relay import relay_before_boot

    return relay_before_boot(command)


def _answer_before_handlers(command: str, relayed: Any = None) -> None:
    """Answer the client's first two requests now, for a server that declares itself.

    Runs last of the start-up steps, so a server that builds its own answers and may
    not serve (a platform that failed to compose) has ended before it answered
    anything. Only the handler import comes after it: a failure there ends the process
    once the client already holds the tool list, and the client then sees a server
    that listed its tools and exited.

    *relayed* is what :func:`_relay_before_boot` returned. With it the answers are
    already out and nothing more is read here; its frames are handed to the loop.
    """
    declared = EARLY_ANSWERS.get(command)
    if declared is None:
        return
    from kiro_crew.mcp_shared_listing import serve_before_handlers

    module, attribute = declared
    serve_before_handlers(getattr(importlib.import_module(module), attribute), relayed=relayed)


def _port_env_ok() -> bool:
    raw = os.environ.get("KIROCREW_PORT")
    if raw is None:
        return True
    try:
        return 1 <= int(raw) <= 65535
    except ValueError:
        return False


def _stderr_is(path: os.PathLike[str] | str) -> bool:
    try:
        a = os.fstat(2)
        b = os.stat(path)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _setup_logging(log_dir: os.PathLike[str] | str) -> None:
    """The ``cli._setup_cli_logging`` shape for a short-lived, foreground ``mcp-*``."""
    from pathlib import Path

    from kiro_crew.cli_log_files import _AppendOnlyLogFileHandler
    from kiro_crew.config import KiroCrewConfig

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    level = logging.WARNING
    try:
        level = getattr(logging, KiroCrewConfig.load().agent.log_level.upper(), logging.WARNING)
    except Exception:
        pass  # config missing or corrupt: keep WARNING
    kc_logger = logging.getLogger("kiro_crew")
    kc_logger.setLevel(level)
    log_file = Path(log_dir) / "gateway.log"
    if any(
        isinstance(h, logging.FileHandler) and Path(h.baseFilename) == log_file.absolute()
        for h in kc_logger.handlers
    ):
        return  # a heal-and-retry runs this twice: one handler, not two
    try:
        fh = _AppendOnlyLogFileHandler(log_file, encoding="utf-8")
    except OSError as exc:
        kc_logger.warning(
            "persistent log file %s unavailable (%s); continuing with console logging only",
            log_file,
            exc,
        )
        return
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [PID %(process)d]: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    kc_logger.addHandler(fh)


def slim_server(argv: list[str]) -> Callable[[], None] | None:
    """Run the start-up steps for the MCP server *argv* names and return its
    entry function, imported but not yet called; or return ``None``.

    ``None`` means no server was imported and nothing irreversible happened:
    the caller runs the full CLI, which repeats only idempotent steps. Every
    import happens here, before the server starts, so a caller can heal a
    ``ModuleNotFoundError`` and retry without a half-served stdin.
    """
    if len(argv) != 1 or argv[0] not in SLIM_SERVERS:
        return None
    if not _port_env_ok():
        return None  # the full CLI prints the port error

    from kiro_crew import platform_compat
    from kiro_crew._llama_lib_path import drop_inherited_lib_path
    from kiro_crew._ssl_compat import _ensure_ssl_certs

    # Module-scope steps of kiro_crew.cli, in its order.
    _ensure_ssl_certs()
    drop_inherited_lib_path()
    # cli.main, in its order, for an ``mcp-*`` command.
    platform_compat.ensure_utf8_console()
    from kiro_crew import sandbox_launch_mark

    sandbox_launch_mark.note()  # the governance tier reads it after the pop below
    os.environ.pop("KIROCREW_SANDBOX_ACTIVE", None)
    os.environ.pop("KIROCREW_SANDBOX_LEVEL", None)
    # A root run aimed at a person's data home is refused before anything can
    # write into it, project detection included; the full CLI prints the refusal.
    from kiro_crew.config import paths as config_paths
    from kiro_crew.data_home_owner import root_run_refusal

    if root_run_refusal(config_paths.peek_data_home(), argv[0], argv):
        return None
    relayed = _relay_before_boot(argv[0])
    # The configuration loader, which the relay above must not wait for.
    from kiro_crew.config import config_dir, ensure_data_home

    if not os.environ.get("KIROCREW_PROJECT_DIR"):
        from kiro_crew.project_dir import detect_project_dir

        detected = detect_project_dir(lambda: config_dir() / "project_dir")
        if detected:
            os.environ["KIROCREW_PROJECT_DIR"] = detected
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
        os.environ.setdefault(var, "1")
    from kiro_crew.platform.tree_liveness import hold_running_tree_lock

    hold_running_tree_lock()  # idempotent, so a fallback below may repeat it
    ensure_data_home()
    if _stderr_is(config_dir() / "gateway.log"):
        return None  # detached: the full CLI owns that logging shape
    from kiro_crew.config.validation import prime_extension_config_keys

    prime_extension_config_keys()
    _setup_logging(config_dir())
    if argv[0] in EARLY_ANSWERS:
        from kiro_crew.mcp_listing_relay import report_outcome

        report_outcome()

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.platform import PlatformCompositionError, boot_platform

    try:
        boot_platform(KiroCrewConfig.load())
    except PlatformCompositionError:
        # Fail closed, exactly like the CLI: no MCP server without its overlay.
        raise
    except Exception:
        logging.getLogger("kiro_crew").debug("platform boot deferred", exc_info=True)

    _answer_before_handlers(argv[0], relayed)
    module, func = SLIM_SERVERS[argv[0]]
    server: Callable[[], None] = getattr(importlib.import_module(module), func)
    return server


def run_if_slim(argv: list[str]) -> bool:
    """Run the MCP server *argv* names and return ``True``, or return ``False``
    (see :func:`slim_server`) so the caller runs the full CLI."""
    server = slim_server(argv)
    if server is None:
        return False
    server()
    return True
