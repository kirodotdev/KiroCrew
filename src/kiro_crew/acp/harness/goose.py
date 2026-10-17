"""goose, launched one process per session by ``AcpClient``.

goose serves ACP from its own binary (``goose acp``), so its executable comes off the
shared self-served ladder and there is no adapter package and no Node floor. Two
things make its launch its own: the builtin extension it is told to load beside
Crew's servers, and where its permission route comes from -- goose resolves
``GOOSE_MODE`` out of its own ENVIRONMENT, above its config file, so the mode is
seeded on the session's environment here and read back off the very response that
opens or restores the session (``AcpClient._verify_goose_routing``), not by a
read-back child of its own.

A Side Chat session (``side_read_only``) also gets a goose home of its own
(:func:`build_side_goose_root`), so goose's own ``permission.yaml`` cannot run a
tool without asking Crew first.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from kiro_crew import acp_tool_gate
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk.backends import ACP_BACKEND_GOOSE

__all__ = ["GooseLaunch", "build_side_goose_root", "goose_config_dir", "side_home_supported"]

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
        # passive reads ACP v1 cannot make it ask about. This harness re-exposes no
        # file from inside the mask, so no expose list is resolved for it.
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
