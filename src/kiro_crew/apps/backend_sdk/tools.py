"""Where the host's tools are: the gateway's own lookups for the binaries it trusts.

Each name answers with a path, or ``None``, from the lookup the gateway runs for its
own children, looked up at call time so a lookup tightened later reaches the app with
the wheel. They run in the caller's process and read only what that process can
already read (a few ``stat`` calls on fixed directories, the caller's own ``PATH`` and
home directory), so none of them adds reach; they only stop an app from running a
tool that a planted ``PATH`` entry chose.
"""

from __future__ import annotations

from kiro_crew import env, platform_compat

__all__ = [
    "agent_template_host_values",
    "nvm_version_bin_dirs",
    "trusted_git_bin",
    "trusted_system_bin",
    "uv_executable",
]


def trusted_git_bin() -> str | None:
    """The ``git`` executable from the fixed system directories, or ``None`` (level 18).

    ``kiro_crew.platform_compat.trusted_git_bin``: ``trusted_system_bin("git")``, plus
    Git for Windows' fixed install roots. ``PATH`` is never read to choose it, so a
    shim planted on ``PATH`` is not what runs. Treat ``None`` as "do not run git":
    falling back to a bare ``"git"`` restores the hazard the lookup exists to close.
    """
    found = platform_compat.trusted_git_bin()
    return found if isinstance(found, str) and found else None


def trusted_system_bin(name: str) -> str | None:
    """*name* resolved from the fixed system directories only, or ``None`` (level 18).

    ``kiro_crew.platform_compat.trusted_system_bin``: the directories the gateway
    trusts for the tools it runs for OS introspection (``lsof``, ``osascript`` and the
    like), never ``PATH``. A miss is logged once per name, naming where ``PATH`` holds
    the tool, and is ``None``: treat it as "unavailable".
    """
    found = platform_compat.trusted_system_bin(name)
    return found if isinstance(found, str) and found else None


def uv_executable() -> str | None:
    """The absolute path of a usable ``uv``, or ``None`` (level 18).

    ``kiro_crew.env.resolve_uv``: the ``uv`` wheel's own locator first (a gateway
    started as a service has a minimal ``PATH`` without the venv's scripts directory),
    then an absolute ``PATH`` hit; a hit through a relative ``PATH`` entry is skipped.
    It is the ``uv`` the gateway renders into an agent template's ``{UV_BIN}``. Never
    raises.
    """
    found = env.resolve_uv()
    return found if isinstance(found, str) and found else None


def nvm_version_bin_dirs() -> tuple[str, ...]:
    """Every existing nvm version ``bin`` directory, newest first per root (level 18).

    ``kiro_crew.env.nvm_version_bin_dirs``: the nvm share of the gateway's own Node
    search, across ``~/.nvm`` and the XDG config root, for an app that keeps its own
    Node search list.
    """
    return tuple(env.nvm_version_bin_dirs())


def agent_template_host_values() -> dict[str, str]:
    """The values the gateway renders for the host placeholders that name no app (level 18).

    ``kiro_crew.apps.agent_template_values.host_values``, the same function the
    gateway's agent-template renderer calls: ``{"{UV_BIN}": ..., "{HOST_PATH}": ...,
    "{PATH_SEP}": ...}``, keyed by the placeholder as a template spells it. A value
    the host cannot compute is left out, as the renderer leaves it out. ``{APP_ROOT}``
    and ``{APP_DATA}`` are not in it: they name one app's directories, which the app
    already holds (its own checkout, ``ctx.data_dir``). Computed in the calling
    process, from its environment, which for a spawned backend carries the gateway's
    ``PATH``.
    """
    # Imported here: the module loads the app manager, which a spawned backend asking
    # for one binary does not need.
    from kiro_crew.apps import agent_template_values

    return dict(agent_template_values.host_values())
