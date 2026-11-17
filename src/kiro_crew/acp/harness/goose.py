"""goose, launched one process per session by ``AcpClient``.

goose serves ACP from its own binary (``goose acp``), so its executable comes off the
shared self-served ladder and there is no adapter package and no Node floor. Two
things make its launch its own: the builtin extension it is told to load beside
Crew's servers, and where its permission route comes from -- goose resolves
``GOOSE_MODE`` out of its own ENVIRONMENT, above its config file, so the mode is
seeded on the session's environment here and read back off the very response that
opens or restores the session (``GooseAcpClient._verify_goose_routing``), not by a
read-back child of its own.

A Side Chat session (``side_read_only``) also gets a goose home of its own
(:func:`build_side_goose_root`), so goose's own ``permission.yaml`` cannot run a
tool without asking Crew first.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from kiro_crew import acp_tool_gate
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness._common import MembershipHarness
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    ProcessAdapter,
    ReclaimPolicy,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.transport_errors import AcpError, AcpToolGateUnroutable
from kiro_crew.acp.types import METHOD_SESSION_CLOSE, METHOD_SESSION_UPDATE, UPDATE_CURRENT_MODE
from kiro_crew.agent_sdk.backends import ACP_BACKEND_GOOSE

__all__ = [
    "GooseHarness",
    "GooseLaunch",
    "build_side_goose_root",
    "goose_config_dir",
    "mode_update_issue",
    "refuse_routing",
    "session_mode_issue",
    "side_home_supported",
]

# The per-process host was the client's code, so it logs under the client's name.
logger = logging.getLogger("kiro_crew.acp.client")

# The channel Crew's permission routing travels down on this harness: goose resolves
# its mode from a PLAIN ENVIRONMENT VARIABLE, above its own config file, so the seed
# needs neither a file nor a JSON document. Verified on this harness by resolving a
# config that declares ``GOOSE_MODE: auto`` and reading ``approve`` back off the
# session.
_ENV_GOOSE_MODE = "GOOSE_MODE"
# The builtin extension Crew asks goose to load. goose REPLACES its configured
# extensions with the client's ``mcpServers`` array, so a session handed Crew's
# servers and nothing else carries no shell and no file tools at all. Naming it on
# the command line restores those alongside Crew's own, and they route through the
# same permission mode as everything else.
_GOOSE_BUILTIN_ARG = "--with-builtin"
_GOOSE_BUILTIN_DEVELOPER = "developer"
# goose's auto-approving mode (``auto``) is never named here: the seed is the one
# place a mode value enters the child and it carries the required mode, so the auto
# mode has no path onto the wire by construction. ``session/set_mode`` would accept
# it, which is why no constant for it exists to be passed.

# goose roots its config, data and state directories at this variable when it is an
# absolute path (``Paths::path_root``), above the XDG directories.
_ENV_GOOSE_PATH_ROOT = "GOOSE_PATH_ROOT"
# The Side Chat home's directory name inside the session's own scratch window.
_SIDE_ROOT_DIRNAME = "goose-side-root"
# Config-directory entries a Side Chat home does not link. ``permission.yaml`` is
# goose's per-tool pre-approval store: in its ``approve`` mode a tool listed under
# ``user.always_allow`` runs without a permission request, so Crew's gate never sees
# it (measured on goose 1.50.1). ``config.yaml`` is written instead, without its
# ``extensions`` block, so no user extension starts in a side session.
_SIDE_ROOT_UNLINKED = frozenset(
    {"permission.yaml", "permission.yaml.lock", "config.yaml", "config.yaml.lock"}
)
_SIDE_CONFIG_DROPPED_KEYS = ("extensions",)


def side_home_supported() -> bool:
    """Whether a goose Side Chat session can be given a goose home of its own here.

    POSIX only. On Windows goose keeps its secrets under ``%APPDATA%``, outside
    the credential leaf the sensitive-path deny names for it, and linking the
    user's config entries needs a symlink privilege; a goose side turn there keeps
    running ``REJECT_ALL``.
    """
    return os.name != "nt"


def goose_config_dir(env: Mapping[str, str]) -> Path:
    """The config directory goose resolves under *env* on POSIX (``Paths::config_dir``).

    ``GOOSE_PATH_ROOT`` first, when absolute; otherwise the XDG layout goose uses:
    ``$XDG_CONFIG_HOME/goose`` when that is absolute, else ``~/.config/goose``.
    """
    root = env.get(_ENV_GOOSE_PATH_ROOT, "")
    if root and os.path.isabs(root):
        return Path(root) / "config"
    xdg = env.get("XDG_CONFIG_HOME", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / "goose"
    return Path(env.get("HOME") or str(Path.home())) / ".config" / "goose"


def build_side_goose_root(source_config_dir: Path, parent: Path) -> Path:
    """Build a fresh goose home for one Side Chat session under *parent*.

    The new ``config`` directory links every entry of the user's own config
    directory (provider secrets, OAuth tokens, custom providers) except
    ``permission.yaml``, and carries a copy of ``config.yaml`` without its
    ``extensions`` block. ``data`` and ``state`` start empty. Any earlier home at
    the same path is removed first, so one spawn never inherits another's grants.

    Raises :class:`AcpError` when the home cannot be built or ``config.yaml``
    cannot be read as a mapping: the session is then refused rather than started
    with an unknown configuration. Blocking; run it off the loop.
    """
    try:
        return _build_side_goose_root(source_config_dir, parent)
    except OSError as exc:
        raise AcpError(
            f"a separate goose home for this Side Chat session could not be built ({exc}); "
            "the side turn is refused."
        ) from None


def _build_side_goose_root(source_config_dir: Path, parent: Path) -> Path:
    root = parent / _SIDE_ROOT_DIRNAME
    if root.is_symlink() or root.is_file():
        root.unlink()
    elif root.exists():
        shutil.rmtree(root)
    config_dir = root / "config"
    config_dir.mkdir(parents=True, mode=0o700)
    if source_config_dir.is_dir():
        for entry in sorted(source_config_dir.iterdir()):
            # Case-folded: on case-insensitive storage ``Permission.yaml`` is the
            # file goose opens as ``permission.yaml``.
            if entry.name.casefold() in _SIDE_ROOT_UNLINKED:
                continue
            os.symlink(entry, config_dir / entry.name)
    source_config = source_config_dir / "config.yaml"
    if source_config.is_file():
        try:
            data = yaml.safe_load(source_config.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise AcpError(
                f"goose's config.yaml could not be read for a Side Chat session ({exc}); "
                "the side turn runs no tools until it parses."
            ) from None
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise AcpError(
                "goose's config.yaml is not a mapping, so a Side Chat session cannot be "
                "given a copy of it; the side turn runs no tools until it is."
            )
        for key in _SIDE_CONFIG_DROPPED_KEYS:
            data.pop(key, None)
        fd = os.open(config_dir / "config.yaml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
    return root


class GooseLaunch(ProcessAdapter):
    """goose: a self-served binary whose mode is seeded on the session's environment."""

    backend = ACP_BACKEND_GOOSE

    def __init__(self) -> None:
        super().__init__()
        # Set by resolve_spawn; read by prepare_spawn_env for the same spawn.
        self._side_scratch: Callable[[], Path | None] | None = None

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the binary, warm the session's MCP array, then mask and seed."""
        session = ctx.session
        assert session is not None, "goose is launched for one session"
        _goose_bin, argv, spawn_label, stderr_label = await launch_mod.resolve_self_served_launch(
            self.backend
        )
        # The builtin extension travels on the ARGV rather than in the session array,
        # because it is not one of Crew's servers: it is the harness's own shell and
        # file tools, which this harness drops when a client supplies
        # ``mcpServers``. Appended AFTER the shared resolution, so the label stays
        # the harness plus its ACP subcommand and does not grow a builtin an
        # operator did not name.
        argv = [*argv, _GOOSE_BUILTIN_ARG, _GOOSE_BUILTIN_DEVELOPER]
        # Translate the agent spec into this session's MCP array HERE, for the
        # reason the claude and opencode adapters do it in their own launches: the
        # translation reads disk, and doing it at the shared session/new call site
        # would put an executor hop and a new failure mode on EVERY backend's
        # construction path, kiro-cli included (harness-parity H13).
        await session._prepare_session_mcp()
        # The same refuse-then-mask preflight the other enforced hosts run, keyed on
        # the same routing question rather than on this harness's identity: it is
        # ENFORCED, so the OS credential mask is the compensating control for the
        # passive reads ACP v1 cannot make it ask about.
        hidden = await launch_mod._run_preflight_bounded(
            launch_mod._sandbox_preflight, self.backend, ctx.sandbox_mode
        )
        # The routing seed. There is no read-back child here and no wrapped second
        # spawn: this harness reports the mode it resolved in the ``modes`` block of
        # the very response that opens or restores the session, so the read-back
        # rides the session's own connection. What travels here is only the seed,
        # written onto the session's own environment overlay so every child the
        # session starts carries it.
        session._extra_env = {
            **session._extra_env,
            _ENV_GOOSE_MODE: acp_tool_gate.permission_setting_for(self.backend)[1],
        }
        if getattr(session, "_side_read_only", False):
            # The session's scratch window is allocated after this plan, so the
            # home is built in prepare_spawn_env; this reads the window then.
            self._side_scratch = lambda: getattr(session, "_scratch_dir", None)
        return SpawnPlan(
            argv=argv,
            spawn_label=spawn_label,
            stderr_label=stderr_label,
            extra_hidden_dirs=hidden,
            extra_expose_files=acp_tool_gate.adapter_expose_files(self.backend, hidden),
        )

    async def prepare_spawn_env(
        self,
        env: dict[str, str],
        *,
        to_thread: Callable[..., Awaitable[Any]],
    ) -> None:
        """On a Side Chat session, point goose at a home of its own.

        Built inside the session's private scratch window, from the config
        directory goose would otherwise read under this same environment. FAIL
        CLOSED: a side session with no scratch window is refused, never started on
        the user's own home, where ``permission.yaml`` can pre-approve a tool.
        """
        self.apply_spawn_env(env)
        if self._side_scratch is None:
            return
        scratch = self._side_scratch()
        if not side_home_supported():
            raise AcpError(
                "a goose Side Chat session cannot be given a separate goose home on "
                "this platform; the side turn is refused."
            )
        if scratch is None:
            raise AcpError(
                "a goose Side Chat session needs its own scratch directory for a "
                "separate goose home, and none was allocated; the side turn is refused."
            )
        root = await to_thread(build_side_goose_root, goose_config_dir(env), Path(scratch))
        env[_ENV_GOOSE_PATH_ROOT] = str(root)

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Nothing beyond the seed, which reaches the child through the session's overlay."""


# ── The permission mode goose reports, read on both drivers ──


def session_mode_issue(resp: object) -> str:
    """What is wrong with the mode a ``session/new`` or ``session/load`` answer reports.

    ``""`` when goose reports the required mode. A missing ``modes`` block is an
    issue too: goose always sends one, so its absence means the answer is not the
    shape this read was verified against, and no claim can be made from it.
    """
    from kiro_crew.acp._dispatch import parse_session_modes
    from kiro_crew.acp.client_permissions import _scrub_observed

    _ids, current_mode, advertised = parse_session_modes(resp if isinstance(resp, dict) else {})
    observed = current_mode if advertised else ""
    return acp_tool_gate.seeded_setting_issue(ACP_BACKEND_GOOSE, str(_scrub_observed(observed)))


def mode_update_issue(update: object) -> tuple[str, str]:
    """``(issue, mode)`` for a ``session/update`` that reports a mode move.

    ``("", "")`` for every update that is not a ``current_mode_update``, and for one
    that names the required mode (goose re-emits the current mode on load). *mode*
    is the reported id, scrubbed, for the refusal's log line and audit row.
    """
    from kiro_crew.acp.client_permissions import _scrub_observed

    if not isinstance(update, dict) or update.get("sessionUpdate") != UPDATE_CURRENT_MODE:
        return "", ""
    mode_id = update.get("currentModeId")
    observed = str(_scrub_observed(mode_id)) if isinstance(mode_id, str) else ""
    return acp_tool_gate.seeded_setting_issue(ACP_BACKEND_GOOSE, observed), observed


def refuse_routing(issue: str) -> None:
    """Raise the tool gate's refusal for *issue*, as the ACP-layer type."""
    try:
        acp_tool_gate.enforce_runtime_routing(
            ACP_BACKEND_GOOSE,
            issue,
            remedy=acp_tool_gate.remediation_for(ACP_BACKEND_GOOSE),
        )
    except acp_tool_gate.ToolGateUnroutable as exc:
        raise AcpToolGateUnroutable(str(exc)) from None


class _GooseSession:
    """What the shared runtime keeps per goose session.

    *placed* is the server names its last ``session/new`` or ``session/load`` put on
    the array, ``None`` until one went out.
    """

    __slots__ = ("placed",)

    def __init__(self) -> None:
        self.placed: frozenset[str] | None = None


class GooseHarness(MembershipHarness):
    """goose as one ``goose acp`` process that several sessions share (``AcpRuntime``).

    Registered so the shared runtime can drive it and so it can be tested, and not
    yet a member of ``ACP_BACKENDS_ACP_RUNTIME``: no session takes this path until
    the routing change names it, and a process-per-session client still launches
    goose through :class:`GooseLaunch`.

    goose 1.57.0 keeps one agent per session in one process and runs their turns in
    parallel; ``session/close`` drops one session and keeps its record, which a
    later ``session/load`` restores on the same process.

    What differs from :class:`GooseLaunch`, and why:

    * the ``GOOSE_MODE`` seed is the child's environment, so it is the same for every
      session on the process; the mode each session RESOLVED is read back per
      session off its own ``session/new`` / ``session/load`` answer
      (:meth:`after_session_start`), and a later move is caught per session
      (:meth:`session_update_issue`). A refusal closes that session alone.
    * a Side Chat session is not served here: its own goose home is the child's
      ``GOOSE_PATH_ROOT``, a process-wide setting, so it keeps a process of its own.
    """

    backend = ACP_BACKEND_GOOSE

    per_session_state = True

    # ── Seam 1: spawn ──

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """The resolved ``goose acp`` argv with the developer builtin, and its mask.

        The same binary resolution and builtin as :class:`GooseLaunch`. The mask is
        resolved here, and a refusal aborts the spawn: goose is an enforced harness,
        so an empty mask is a missing control.
        """
        from kiro_crew.acp.session_handle import AcpRuntimeError

        try:
            _bin, argv, spawn_label, stderr_label = await launch_mod.resolve_self_served_launch(
                self.backend
            )
        except AcpError as exc:
            raise AcpRuntimeError(str(exc)) from exc
        # The same refuse-then-mask preflight ``GooseLaunch`` runs, for goose's own
        # backend: its credential mask and the files it hands back read-only.
        hidden = await launch_mod._run_preflight_bounded(
            launch_mod._sandbox_preflight, self.backend, ctx.sandbox_mode
        )
        return SpawnPlan(
            argv=[*argv, _GOOSE_BUILTIN_ARG, _GOOSE_BUILTIN_DEVELOPER],
            spawn_label=spawn_label,
            stderr_label=stderr_label,
            extra_hidden_dirs=hidden,
            extra_expose_files=acp_tool_gate.adapter_expose_files(self.backend, hidden),
        )

    def apply_spawn_env(
        self,
        env: dict[str, str],
        *,
        spawned_binary: str | None = None,
        cli_owned_auth: bool = False,
    ) -> None:
        """Seed the required mode; leave the goose home the operator chose.

        ``GOOSE_MODE`` outranks goose's config file, so an operator's ``auto`` there
        cannot defeat it. An operator's own ``GOOSE_PATH_ROOT`` is kept, so the child
        reads the same goose home the per-chat client and the placement's account era
        (``goose_config_dir``) read. Crew sets that variable only for a Side Chat, and
        a Side Chat never reaches this process (:meth:`prepare_session`).
        """
        from kiro_crew.config import loader as loader_mod

        loader_mod.strip_kiro_cli_api_key(env)
        env[_ENV_GOOSE_MODE] = acp_tool_gate.permission_setting_for(self.backend)[1]

    @property
    def verifies_agent_activation(self) -> bool:
        """No: goose takes no ``--agent``, and its modes are permission modes."""
        return False

    # ── Seam 2: initialize ──

    @property
    def protocol_version(self) -> Any:
        from kiro_crew.agent_sdk.backends import launch_for

        return launch_for(self.backend).protocol_version

    @property
    def client_capabilities(self) -> dict[str, Any]:
        """What ``AcpClient`` declares to goose, unchanged."""
        from kiro_crew.acp.client_native_children import client_capabilities_for

        return client_capabilities_for(self.backend)

    # ── Seam 3: session/new and session/load ──

    async def session_extras(self, agent: str, **_kwargs: Any) -> SessionExtras:
        """Empty: goose has no custom-agent channel. Its per-session payload is the
        mirrored array."""
        return SessionExtras()

    def session_mcp_servers(
        self,
        requested: list[dict[str, Any]],
        *,
        agent_capabilities: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """The mirrored array as built: goose mounts the stdio shape Crew sends."""
        return requested

    @property
    def wants_session_file_on_load(self) -> bool:
        """No: goose finds the session from its id."""
        return False

    async def prepare_session(
        self,
        *,
        work_dir: str | Path,
        agent: str | None,
        model: str,
        session_key: str,
        channel_id: str,
        memory_mode: str,
        options: Mapping[str, Any] | None = None,
    ) -> Any:
        """A marker for this session, so the runtime checks it after it starts.

        A Side Chat session is refused here: its own goose home is the child's
        ``GOOSE_PATH_ROOT``, which a shared process cannot carry per session, and
        on the user's home goose's ``permission.yaml`` could pre-approve a tool.
        The placement refuses such a chat before it gets here.
        """
        if options and options.get("side_read_only"):
            raise AcpToolGateUnroutable(
                "a goose Side Chat session needs a goose home of its own, which a "
                "shared goose process cannot give it; it runs on a process of its own."
            )
        return _GooseSession()

    def session_meta(
        self,
        state: Any,
        mcp_servers: list[dict[str, Any]],
        *,
        zero_tools: bool = False,
        stub_token: str = "",
    ) -> dict[str, Any] | None:
        """No envelope; record the servers this request places on the session.

        They are the session's whole server universe beside goose's own builtin
        extension, which :meth:`permission_refusal` holds a trusted identity to.
        """
        if isinstance(state, _GooseSession):
            state.placed = frozenset(
                element["name"]
                for element in mcp_servers
                if isinstance(element, dict) and isinstance(element.get("name"), str)
            )
        return None

    def permission_refusal(
        self, state: Any, event: Any, *, unclassified: bool
    ) -> tuple[str, str] | None:
        """The two refusals goose's ``_meta.goose.toolCall`` identity channel earns.

        The same two ``AcpClient`` runs for goose: a call no channel classified (no
        ``kind``, no identity) cannot be told from a served tool, so no gate can be
        applied to it; and a trusted identity naming a server this session never
        placed and that is not goose's own extension is a drifted builtin that would
        otherwise pass as MCP-served and skip the command tier.
        """
        from kiro_crew.acp._dispatch import (
            REFUSAL_FOREIGN_SERVER,
            REFUSAL_UNCLASSIFIED_CALL,
            foreign_server_refused,
            unclassified_call_refused,
        )

        if unclassified_call_refused(event, frame_unclassified=unclassified):
            return REFUSAL_UNCLASSIFIED_CALL, "tool__unclassified"
        placed = getattr(state, "placed", None)
        if placed is not None and foreign_server_refused(event, placed):
            tool = getattr(event, "tool_name", "") or "unknown"
            return REFUSAL_FOREIGN_SERVER, f"mcp__{event.mcp_server_name}__{tool}"
        return None

    async def after_session_start(
        self,
        state: Any,
        session_id: str,
        resp: dict[str, Any],
        *,
        send_request: Callable[[str, dict[str, Any]], Awaitable[Any]],
        close_session: Callable[[], Awaitable[None]],
        agent_version: str = "",
    ) -> None:
        """Read the mode this session resolved back off its own answer.

        A restored session carries the mode it was last left in, which the seed
        does not re-apply, so the load answer is read as the new one is. A session
        not in the required mode is closed on its own and refused; the process and
        every other session on it keep running.
        """
        issue = session_mode_issue(resp)
        if not issue:
            return
        logger.error(
            "goose routing: session %s started in a mode that is not the required "
            "one; closing it",
            session_id,
        )
        await close_session()
        refuse_routing(issue)

    def session_update_issue(self, state: Any, update: dict[str, Any]) -> str:
        """The refusal a ``current_mode_update`` off the required mode earns."""
        issue, _mode = mode_update_issue(update)
        return issue

    # ── Seam 4: inbound requests the host answers ──

    @property
    def host_answered_methods(self) -> tuple[str, ...]:
        """None: goose asks Crew for no connection-level callback."""
        return ()

    async def answer_request(self, method: str) -> dict[str, Any]:
        """Never called: :attr:`host_answered_methods` is empty."""
        raise NotImplementedError(f"goose harness answers no inbound request, including {method!r}")

    # ── Seam 5: notification aliases ──

    @property
    def notification_aliases(self) -> NotificationAliases:
        """Plain ACP ``session/update`` only."""
        return NotificationAliases(session_update=(METHOD_SESSION_UPDATE,))

    # ── Seam 6: teardown ──

    @property
    def teardown(self) -> TeardownPolicy:
        """``session/close``, awaited: it ends one session and keeps its record.

        goose 1.57.0's ``on_close_session`` cancels that session's run, drops it from
        the session table and leaves its record in goose's session store; a later
        ``session/load`` on the same process restores it.
        """
        return TeardownPolicy(method=METHOD_SESSION_CLOSE, notification=False)

    # ── Seam 9: reclaim ──

    def reclaim_policy(self, *, max_age_secs: float, max_rss_mb: float) -> ReclaimPolicy:
        """The operator's thresholds: goose's sessions live inside its one process."""
        return ReclaimPolicy(max_age_secs=max_age_secs, max_rss_mb=max_rss_mb)
