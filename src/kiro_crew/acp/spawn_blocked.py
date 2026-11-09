"""The session-start refusal for a Windows host whose policy will not run the agent binary.

AppLocker, an EDR product or group policy can stop the agent executable from
starting. ``CreateProcess`` then fails with ``ERROR_ACCESS_DENIED`` (5) or
``ERROR_ACCESS_DISABLED_BY_POLICY`` (1260), and a respawn hits the same wall.
The spawn still makes main's one retry first, since a self-update window can
also answer access denied; a refusal that survives it is raised as
:class:`AcpSpawnBlocked`. Only an administrator can lift it, so the chat says
that once, naming the file, instead of a generic transport error.
"""

from __future__ import annotations

from kiro_crew.acp.session_handle_runtime_contract import AcpRuntimeError

#: ``winerror`` values a host policy answers a blocked spawn with.
_BLOCKED_WINERRORS = frozenset({5, 1260})


def is_windows_spawn_blocked(exc: BaseException) -> bool:
    """True when *exc* is a Windows spawn refusal a host policy gave.

    The ``winerror`` code alone decides: 5 and 1260 match. Any other code, and
    an ``OSError`` with no ``winerror`` (every POSIX spawn), does not.
    """
    if not isinstance(exc, OSError):
        return False
    return getattr(exc, "winerror", None) in _BLOCKED_WINERRORS


def spawn_blocked_message(binary: str) -> str:
    """The person-facing sentence for a blocked spawn of *binary*."""
    target = f"the agent program ({binary})" if binary else "the agent program"
    return (
        f"Windows would not start {target}. AppLocker, an EDR product or group "
        "policy on this computer likely blocked it. Ask your IT administrator to "
        "allow that file, then start a new chat."
    )


class AcpSpawnBlocked(AcpRuntimeError):
    """Windows refused to start the agent binary, so no process exists.

    Not ``transient``: a respawn meets the same policy. ``windows_spawn_blocked``
    is the structural tag the chat reads to word the card in the reader's
    language, and ``binary`` is the path it names.
    """

    transient = False
    windows_spawn_blocked = True

    def __init__(self, binary: str) -> None:
        super().__init__(spawn_blocked_message(binary))
        self.binary = binary
