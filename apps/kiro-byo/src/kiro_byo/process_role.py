"""Side-effect-free detection of which Kiro Crew process is running this code."""
from __future__ import annotations

import sys

_GATEWAY_MODULES = frozenset({"kiro_crew", "kiro_crew.__main__", "kiro_crew.cli"})


def is_crew_gateway_process(argv: list[str] | None = None) -> bool:
    """Return True only for the Kiro Crew Gateway interpreter itself.

    Kiro BYO's ``.pth`` hook runs in EVERY Python started from Crew's bundled
    interpreter: the Kiro BYO ACP child, MCP servers, cron scripts, ad-hoc
    diagnostics.  Only the Gateway (``python -m kiro_crew gateway ...``) serves
    ``/api/models``, so only it may claim that an integration is active.

    ``sys.orig_argv`` is preferred because ``sys.argv`` is still a ``-m``
    placeholder while ``site`` processes ``.pth`` files.
    """
    if argv is None:
        argv = list(getattr(sys, "orig_argv", None) or sys.argv or [])
    module = ""
    rest: list[str] = []
    for index, arg in enumerate(argv):
        if arg == "-m" and index + 1 < len(argv):
            module, rest = argv[index + 1], argv[index + 2 :]
            break
        if arg.startswith("-m") and len(arg) > 2:
            module, rest = arg[2:], argv[index + 1 :]
            break
    if module not in _GATEWAY_MODULES:
        return False
    # The subcommand is the first positional argument after the module.
    for arg in rest:
        if arg.startswith("-"):
            continue
        return arg == "gateway"
    return False
