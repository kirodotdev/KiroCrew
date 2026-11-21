"""Whether the running gateway of this data home was started with ``--headless``.

The foreground restart repeats that flag. It reads the lock holder's own argv and
parses the part after ``gateway`` with the gateway command's real arguments
(``cli.add_gateway_arguments``), so an abbreviation such as ``--headl`` counts and
the text ``--headless`` inside another argument's value does not. Whenever a
gateway of this home is alive and its arguments cannot be established, the answer
is ``None`` and the restart refuses.
"""

import argparse
from collections.abc import Sequence
from typing import NoReturn

from kiro_crew import platform_compat
from kiro_crew.cli_server_peer import _recorded_gateway_pid
from kiro_crew.config.paths import config_dir
from kiro_crew.gateway_lock import LockProbeError, lock_holder
from kiro_crew.port_resolution import _is_kirocrew_process


class _Unparsed(Exception):
    """The arguments do not parse as a gateway start."""


class _GatewayArgs(argparse.ArgumentParser):
    """The gateway command's parser, raising where argparse would print and exit."""

    def error(self, message: str) -> NoReturn:
        raise _Unparsed(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        raise _Unparsed(message or "")

    def print_help(self, file: object = None) -> None:
        raise _Unparsed("help")


def gateway_parser() -> argparse.ArgumentParser:
    """A parser holding exactly the arguments ``main()`` gives ``gateway``."""
    from kiro_crew.cli import add_gateway_arguments

    parser = _GatewayArgs(prog="kirocrew gateway")
    add_gateway_arguments(parser)
    return parser


def argv_headless(argv: Sequence[str]) -> bool | None:
    """``True`` headless, ``False`` ordinary, ``None`` when it cannot be told.

    The first ``gateway`` element is the subcommand: every launch shape puts only
    the program, interpreter options and top-level options before it. An argv with
    no ``gateway`` element is not a gateway start, so it is ``False``: there is no
    flag to keep, and the restart goes on as it does without this read.

    Arguments this build's parser does not know are ignored (``parse_known_args``):
    a gateway started by another build of Kiro Crew may carry a flag this one
    lacks, and no unknown element can stand for ``--headless``. An ambiguous
    abbreviation or a help flag is ``None``. On Windows the argv is a split of one
    joined command line, so a quote in an argument after ``gateway`` makes where a
    value ends untrustworthy, and that is ``None`` too; Linux and macOS read the
    exact vector, where a quote is an ordinary character.
    """
    if "gateway" not in argv:
        return False
    rest = list(argv[argv.index("gateway") + 1 :])
    if platform_compat.IS_WINDOWS and any('"' in arg for arg in rest):
        return None
    try:
        known, _unknown = gateway_parser().parse_known_args(rest)
    except _Unparsed:
        return None
    return bool(known.headless)


def _our_listener_or_unknown(port: int) -> bool:
    """True when a Kiro Crew process listens on *port*, or that cannot be looked up."""
    if not platform_compat.listening_pid_tool_available():
        return True
    return any(_is_kirocrew_process(pid) for pid in platform_compat.find_listening_pids(port))


def live_gateway_headless(port: int) -> bool | None:
    """Decide the restart's mode from this home's live gateway, before anything stops.

    The holder is the one ``gateway.lock`` names. When the probe reports the lock
    held but cannot name its pid (a Windows mandatory lock hides the stamp), the
    holder is the pid of the gateway's own run record for *port*
    (``_recorded_gateway_pid``: ``run/gateway-<port>.pid`` whose start token still
    matches that live pid), and with no fresh record the answer is ``None``. When
    the probe cannot tell whether the lock is held at all, the answer is ``None`` if
    a Kiro Crew process listens on *port* (or the listener lookup is unavailable),
    and ``False`` otherwise.

    ``False``: no live gateway, or one started without ``--headless``; the restart
    then does what it does without this read. ``None``: a live gateway whose argv
    cannot be read or parsed. Read once: no retry count exists for this probe.
    """
    try:
        holder = lock_holder(config_dir())
        pid = holder.pid if holder.alive else None
    except LockProbeError as exc:
        if not exc.held:
            return None if _our_listener_or_unknown(port) else False
        pid = _recorded_gateway_pid(port)
        if pid is None:
            return None
    if pid is None:
        return False
    argv = platform_compat.process_argv(pid)
    return None if argv is None else argv_headless(argv)
