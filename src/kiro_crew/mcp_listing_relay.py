"""A stdio MCP server's first two answers, relayed from its gateway before the server boots.

An agent engine starts every MCP server of a session when the session starts and holds
the first prompt until each has listed its tools. Building that list in the server costs
its whole start-up first: the configuration, the platform, and for ``kirocrew-core`` the
live reads its descriptions carry. The gateway is one warm process that has all of that
loaded, so a server asks it: ONE request on the route the server already reads its tool
policy from (``GET /api/session-tool-policy``, same credential, same session), answered
with the server's ``initialize`` result and its filtered tool list, built by the same two
functions the server would have called (``mcp_shared_listing.initialize_result`` and the
declared ``list_tools`` behind ``mcp_shared_listing.within_policy``).

The server side lives here and runs before the configuration loader, the platform and the
audit log are imported, so everything this module imports is the standard library or a
leaf module, and nothing in it writes an audit record. Any outcome but a list (no session
identity or gateway in the environment, a refusal, a timeout, the gateway's "build
locally", a malformed answer) leaves the server on the order it has always started in,
where it builds both answers itself.

The gateway side is ``kiro_crew.mcp_listing_gateway``. The two meet
on the names and the two stamps below: the gateway answers with a list only for a server
that runs the code it runs and reads the data home and agents directory it reads.
"""

from __future__ import annotations

import collections
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import queue
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

#: Query parameter naming the server (its ``kirocrew`` command, as in
#: ``mcp_entry.EARLY_ANSWERS``) whose answers the caller asks for. A policy read
#: without it is answered exactly as before.
LISTING_PARAM = "listing"
#: Query parameters carrying :func:`code_stamp` and :func:`place_stamp` as the server
#: computes them.
CODE_PARAM = "code"
PLACE_PARAM = "place"
#: The key of the policy answer that holds the relayed answers, or the reason there
#: are none (:data:`LOCAL_KEY`).
LISTING_KEY = "listing"
#: Under :data:`LISTING_KEY`: the gateway's reason for leaving the list to the server.
LOCAL_KEY = "local"

#: Longest the server waits on the gateway, per socket wait. A warm gateway answers in
#: tens of milliseconds, and past roughly half a second the server would have built the
#: list itself sooner, so a wait this long only bounds a gateway that is not answering:
#: the server then starts as it always has, this much later.
RELAY_FETCH_TIMEOUT_SECS = 1.0

#: The fallback reason for a gateway that did not answer in time: the one outcome that
#: costs the session the wait on top of the server's own start-up.
_TIMED_OUT = "the gateway did not answer in time"

#: Longest the relay waits for the client's next frame once it holds the answers; the
#: same bound, for the same reason, as ``mcp_shared_listing.EARLY_FRAME_WAIT_SECS``.
RELAY_FRAME_WAIT_SECS = 1.0

#: The environment variable carrying this session's signed token, as
#: ``mcp_gateway.claim.STUB_SESSION_TOKEN_ENV`` names it. Repeated here because that
#: module loads the executor and transport graph; ``test_mcp_listing_relay`` holds the
#: two equal.
SESSION_TOKEN_ENV = "KIROCREW_STUB_SESSION_TOKEN"
#: The ``_meta`` key of a gateway-stamped per-call caller, as
#: ``mcp_caller.CALLER_META_KEY`` names it (same test, same reason).
CALLER_META_KEY = "kirocrew.caller"

#: The modules that build what the four servers list, relative to this package, and
#: the folders whose every module is one.
_LISTING_BUILDERS = (
    "mcp_core_tool_list.py",
    "mcp_cron_tool_list.py",
    "mcp_guide_tool_list.py",
    "mcp_mate_tool_list.py",
    "mcp_instructions.py",
    "mcp_shared_listing.py",
    "mcp_tool_titles.py",
    "mcp_tools/__init__.py",
)
_LISTING_BUILDER_DIRS = ("mcp_tools/catalog", "mcp_tools/control_parts", "mcp_tools/spawn_parts")
#: Every module of this package a builder imports by name: where a description's
#: text, names and limits come from. ``test_mcp_listing_relay.py`` derives this set
#: from the builders' import statements and fails on a row that is missing or left over.
#: shortcut: one import deep. That is exact for an installed package (an install or
#: upgrade rewrites every file) and misses a hand edit, under a running gateway, to a
#: module only these import; stamp deeper if that ever matters.
_LISTING_IMPORTS = (
    "agent_discovery.py",
    "agent_ipc_client.py",
    "agent_sdk/mcp_problems.py",
    "apps/__init__.py",
    "apps/app_agent_tools.py",
    "apps/manager.py",
    "autonudge.py",
    "autonudge_judge.py",
    "autonudge_judge_phrases.py",
    "browser_harness/__init__.py",
    "browser_harness/ops.py",
    "browser_harness/quick_ask.py",
    "conductor_patrol.py",
    "config/__init__.py",
    "config/loader.py",
    "constants.py",
    "context_management.py",
    "cron_service/model.py",
    "execution_context.py",
    "lesson_validation.py",
    "mcp_caller.py",
    "mcp_core.py",
    "mcp_core_parts/identity.py",
    "mcp_shared.py",
    "mcp_shared_policy.py",
    "mcp_shared_wire.py",
    "mcp_tools/_limits.py",
    "member_goals.py",
    "monitoring/limits.py",
    "monitoring/models.py",
    "monitoring/registry.py",
    "monitoring/targets.py",
    "platform/__init__.py",
    "platform_compat.py",
    "port_resolution.py",
    "resource_status.py",
    "security/__init__.py",
    "sel.py",
    "session_directive.py",
    "session_ledger.py",
    "slack/__init__.py",
    "slack/features.py",
    "spawn_label.py",
    "subagent.py",
    "subagent_read_collected.py",
    "subagent_standalone.py",
    "subagent_wait_reasons.py",
    "validation.py",
    "validation_parts/core_agent_schemas.py",
    "validation_parts/field_engine.py",
    "validation_parts/input_bounds.py",
    "validation_parts/wire_envelopes.py",
)
#: The package's own marker and this module, which is what the two sides agree by.
_STAMPED_BESIDES = ("__init__.py", "mcp_listing_relay.py")

_AS_A_TOOL_SERVER: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kirocrew_listing_as_a_tool_server", default=False
)


@contextlib.contextmanager
def as_a_tool_server() -> Iterator[None]:
    """Build tool descriptors, inside this block, as a tool server process builds them.

    The gateway builds a list for a server to relay, and a description may read a value
    only the gateway process holds (the sub-agent cap in force). Read there, the relayed
    list would say something the server's own list never says.
    """
    token = _AS_A_TOOL_SERVER.set(True)
    try:
        yield
    finally:
        _AS_A_TOOL_SERVER.reset(token)


def built_for_a_tool_server() -> bool:
    """Whether the descriptors being built now are for a tool server to relay."""
    return _AS_A_TOOL_SERVER.get()


def _digest(parts: list[str]) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


def listing_builders(package: str) -> list[str]:
    """The modules that build the lists, as paths relative to *package* with ``/``."""
    names = list(_LISTING_BUILDERS)
    for folder in _LISTING_BUILDER_DIRS:
        names.extend(
            f"{folder}/{entry}"
            for entry in os.listdir(os.path.join(package, folder))
            if entry.endswith(".py")
        )
    return names


def code_stamp() -> str:
    """Which listing code this process finds on disk, or ``""`` when it cannot tell.

    Size, modification time and inode of each module that builds a list and of each
    module those import. A server computes it as it starts; a gateway keeps the one it
    computed as it started, so a package replaced under a running gateway reads as
    different code and the server builds its own list.
    """
    package = os.path.dirname(os.path.realpath(__file__))
    try:
        names = {*listing_builders(package), *_LISTING_IMPORTS, *_STAMPED_BESIDES}
        parts = []
        for name in sorted(names):
            st = os.stat(os.path.join(package, os.path.normpath(name)))
            parts.append(f"{name}:{st.st_size}:{st.st_mtime_ns}:{st.st_ino}")
    except OSError:
        return ""
    return _digest(parts)


def place_stamp() -> str:
    """Which data home and agents directory this process reads, or ``""`` when unknown.

    Device and inode of each, so two processes agree only when they read the same two
    directories: a server placed on another host, or pointed at another agents
    directory, differs even where the paths are spelled alike. An agents directory
    that does not exist yet is one both sides can agree is absent.
    """
    from kiro_crew.config import paths

    parts = []
    try:
        for folder in (paths.config_dir(), paths.kiro_agents_dir()):
            try:
                st = os.stat(folder)
            except FileNotFoundError:
                parts.append("absent")
            else:
                parts.append(f"{st.st_dev}:{st.st_ino}")
    except OSError:
        return ""
    return _digest(parts)


class Listing(NamedTuple):
    """The two answers a gateway built for one server of one session."""

    initialize: dict[str, Any]
    tools: list[Any]


def _env_port() -> int | None:
    """The gateway port this process's environment names, or ``None``.

    The first two steps of ``port_resolution.resolve_client_port_src``, which is where
    a server's later policy read resolves its port: a server its gateway started has
    one of the two. The later steps read the configuration, so a process with neither
    is not asked to relay.
    """
    for name in ("KIROCREW_PORT", "KIROCREW_BOUND_PORT"):
        raw = os.environ.get(name)
        if raw:
            try:
                return int(raw)
            except ValueError:
                continue
    return None


def fetch_listing(
    command: str, *, timeout: float = RELAY_FETCH_TIMEOUT_SECS
) -> tuple[Listing | None, str]:
    """Ask the gateway for *command*'s answers: ``(listing, "")`` or ``(None, why not)``.

    One request, with the credential and the session identity the policy read sends.
    Only a process that carries all of them in its own environment asks: a pooled
    backend has no session of its own, and a server under an enforced harness holds a
    pickup in place of the credential.
    """
    from kiro_crew.agent_ipc_wire import PICKUP_ENV
    from kiro_crew.config.loader_locations import read_local_secret
    from kiro_crew.loopback_http import loopback_urlopen

    if os.environ.get(PICKUP_ENV):
        return None, "this server holds a pickup in place of the gateway credential"
    session_key = os.environ.get("KIROCREW_SESSION_KEY", "")
    token = os.environ.get(SESSION_TOKEN_ENV, "")
    if not session_key or not token:
        return None, "no session key and token in the environment"
    port = _env_port()
    if port is None:
        return None, "no gateway port in the environment"
    try:
        secret = read_local_secret(port, dial_host="127.0.0.1")
    except Exception:  # noqa: BLE001 - no credential is one of the ordinary outcomes
        secret = ""
    if not secret:
        return None, "no gateway credential"
    code, place = code_stamp(), place_stamp()
    if not code or not place:
        return None, "this process cannot name its code or its data home"
    query = urllib.parse.urlencode({LISTING_PARAM: command, CODE_PARAM: code, PLACE_PARAM: place})
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/session-tool-policy?{query}",
        headers={
            "X-Internal-Secret": secret,
            "X-Session-Key": session_key,
            "X-Session-Token": token,
        },
    )
    try:
        with loopback_urlopen(req, timeout=timeout) as resp:
            answer = json.loads(resp.read())
    except TimeoutError:
        return None, _TIMED_OUT
    except Exception as exc:  # noqa: BLE001 - every failure is the same fallback
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            return None, _TIMED_OUT
        status = getattr(exc, "code", None)
        return None, f"the gateway answered {status}" if status else type(exc).__name__
    listing = answer.get(LISTING_KEY) if isinstance(answer, dict) else None
    if not isinstance(listing, dict):
        return None, "the gateway answered no listing"
    initialize, tools = listing.get("initialize"), listing.get("tools")
    if isinstance(initialize, dict) and isinstance(tools, list):
        return Listing(initialize, tools), ""
    local = listing.get(LOCAL_KEY)
    return None, f"gateway: {local}" if isinstance(local, str) and local else "a malformed listing"


class RelayedFrames:
    """What :func:`relay_before_boot` read from stdin, for the server's own loop.

    The shape ``mcp_shared_listing.EarlyFrames`` has, which is what
    ``serve_before_handlers`` turns it into once that module may be imported.
    """

    def __init__(self) -> None:
        #: The ``params`` of the ``initialize`` answered here, or ``None``.
        self.init_params: dict[str, Any] | None = None
        #: Frames read but not answered, oldest first. ``None`` is the end of the stream.
        self.pending: collections.deque[dict[str, Any] | None] = collections.deque()
        #: The stdin pump's queue where the platform reads stdin through one.
        self.pump: queue.Queue[Any] | None = None
        #: Whether ``tools/list`` was answered here.
        self.listed = False


class _Outcome:
    """This process's one relay attempt: made at most once, reported at most once."""

    def __init__(self) -> None:
        self.made = False
        self.frames: RelayedFrames | None = None
        self.command = ""
        self.fallback = ""
        self.fetch_ms = 0.0
        self.reported = False


_OUTCOME = _Outcome()


def relay_before_boot(
    command: str, *, wait_secs: float = RELAY_FRAME_WAIT_SECS
) -> RelayedFrames | None:
    """Answer *command*'s ``initialize`` and ``tools/list`` with the gateway's answers.

    Returns the frames read, for ``serve_before_handlers`` to hand the loop, or ``None``
    when the gateway gave no list and nothing was read: the server then answers both
    itself, later, as it always has. At most one attempt per process; a second call
    returns the first one's result, so an entry that retries its imports may call again.
    """
    if _OUTCOME.made:
        return _OUTCOME.frames
    # Every import the answers need, before anything is read: a missing module then
    # leaves stdin untouched for the entry that heals it and retries.
    from kiro_crew import mcp_shared_wire, platform_compat
    from kiro_crew.validation_parts import field_engine, wire_envelopes

    _OUTCOME.made = True
    _OUTCOME.command = command
    started = time.perf_counter()
    try:
        listing, _OUTCOME.fallback = fetch_listing(command)
    except Exception as exc:  # noqa: BLE001 - never let the shortcut stop a server
        listing, _OUTCOME.fallback = None, type(exc).__name__
    _OUTCOME.fetch_ms = (time.perf_counter() - started) * 1000
    if listing is None:
        return None
    frames = RelayedFrames()
    _OUTCOME.frames = frames
    mcp_shared_wire.snapshot_stdout_fd()
    stdin = sys.stdin
    try:
        frames.pump = None if platform_compat.IS_POSIX else mcp_shared_wire._start_stdin_pump(stdin)
        while True:
            frame = mcp_shared_wire._frame_within(stdin, frames.pump, wait_secs)
            if isinstance(frame, mcp_shared_wire._NoFrame):
                break
            if isinstance(frame, mcp_shared_wire._Skipped):
                continue
            if frame is None:
                frames.pending.append(None)
                break
            try:
                method, req_id, params = wire_envelopes.validate_jsonrpc_request(frame)
            except field_engine.ValidationError:
                frames.pending.append(frame)
                break
            if not _answer_relayed(listing, frames, frame, method, req_id, params):
                break
    except (OSError, ValueError, TypeError):
        # A stdin this cannot wait on: nothing was answered that the loop will not
        # handle from the frames left for it.
        pass
    return frames


def _answer_relayed(
    listing: Listing,
    frames: RelayedFrames,
    frame: dict[str, Any],
    method: str,
    req_id: Any,
    params: dict[str, Any],
) -> bool:
    """Answer *frame* from *listing* if it is one of the two; True to keep reading.

    The control flow of ``mcp_shared_listing._answer_early``. A ``tools/list`` that
    names its caller is left for the loop: the gateway stamps a caller on a frame for a
    backend several sessions share, and the list in hand is this process's own.
    """
    from kiro_crew.mcp_shared_wire import respond

    if method == "initialize" and req_id is not None and frames.init_params is None:
        respond(req_id, listing.initialize)
        frames.init_params = params
        return True
    if method == "notifications/initialized" and frames.init_params is not None:
        return True
    meta = params.get("_meta")
    if (
        method == "tools/list"
        and req_id is not None
        and frames.init_params is not None
        and not (isinstance(meta, dict) and CALLER_META_KEY in meta)
    ):
        respond(req_id, {"tools": listing.tools})
        frames.listed = True
        return False
    frames.pending.append(frame)
    return False


def report_outcome() -> None:
    """Log this process's relay attempt once, after the server has set up its logging.

    One line each time the list was left to the server, so a host that always falls
    back can be seen: a warning for a gateway that did not answer in time, which costs
    the session the wait, and an informational line for every other reason.
    """
    if not _OUTCOME.made or _OUTCOME.reported:
        return
    _OUTCOME.reported = True
    if not _OUTCOME.fallback:
        logger.debug("%s: answers relayed in %.0f ms", _OUTCOME.command, _OUTCOME.fetch_ms)
        return
    logger.log(
        logging.WARNING if _OUTCOME.fallback == _TIMED_OUT else logging.INFO,
        "%s: tool list built by this server, not relayed (%s; %.0f ms)",
        _OUTCOME.command,
        _OUTCOME.fallback,
        _OUTCOME.fetch_ms,
    )
