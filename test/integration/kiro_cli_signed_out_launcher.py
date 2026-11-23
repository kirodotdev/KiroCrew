#!/usr/bin/env python3
"""A kiro-cli stand-in that offers the KAS relay while kiro-cli is signed out.

``KIROCREW_KIRO_BIN`` points at this file in
``test_knowledge_mcp_ingest_on_kas_signed_out.py``. It routes on the subcommand and,
for ``acp``, on the ENGINE VALUE it is started with:

* ``acp --help`` is answered here with help text naming ``--agent-engine`` and
  both engines, so the boot floor's ``kas_engine_verdict`` reads KAS as served.
  The fake backend's own help (``Usage: kiro-cli acp [OPTIONS]``) names neither,
  and other boots rely on that text staying as it is.
* ``acp --agent-engine v3 ...`` is the KAS relay: the fake backend is exec'd with
  the same argv, so it serves the session over stdio.
* ``acp --agent-engine v2 ...`` and ``acp`` with no engine flag are kiro-cli's
  own engine, which needs kiro-cli's own sign-in: they print kiro-cli's
  signed-out text and exit non-zero.
* ``whoami`` answers signed out.
* ``--version``, ``chat --list-models`` and anything else pass through to the
  fake backend.

Every ``acp`` invocation but ``acp --help`` is appended to the file
:data:`LAUNCH_LOG_ENV` names, one JSON object per line carrying the time, the
argv and the route taken, so the test can read which engine each spawn asked for.
Stdlib only, no shell, POSIX only (``os.execv``), like the integration lane.
"""

from __future__ import annotations

import json
import os
import sys
import time

#: Names the JSON-lines file each ``acp`` start is recorded in. Unset: nothing is
#: recorded.
LAUNCH_LOG_ENV = "KIROCREW_TEST_KIRO_LAUNCH_LOG"
#: The engine flag and the KAS engine, as ``acp.kas_transport`` spells them
#: (copied so this file stays stdlib-only; the test pins them equal).
ENGINE_FLAG = "--agent-engine"
KAS_ENGINE = "v3"
#: What ``acp --help`` prints: the two lines ``kas_engine_gap`` and
#: ``kiro_cli.chat_agent_engines`` read, in the clap shape kiro-cli prints.
ACP_HELP = (
    "Usage: kiro-cli acp [OPTIONS]\n"
    "\n"
    "Options:\n"
    "      --agent-engine <AGENT_ENGINE>  Agent engine to serve "
    "[possible values: v2, v3]\n"
    "      --auth-method <AUTH_METHOD>    Who resolves credentials "
    "[possible values: cli, client]\n"
    "  -h, --help                         Print help\n"
)
#: kiro-cli's answer to a start that needs its own sign-in.
SIGNED_OUT_TEXT = "error: You are not logged in, please log in with kiro-cli login"
ROUTE_KAS_RELAY = "kas_relay"
ROUTE_SIGNED_OUT = "signed_out"
#: The fake backend of the checkout this file sits in, run by path like the
#: harness's own ``KIROCREW_KIRO_BIN`` (``fake_acp_backend.__file__``).
FAKE_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src",
    "kiro_crew",
    "testing",
    "fake_acp_backend.py",
)


def engine_of(args: list[str]) -> str | None:
    """The value of ``--agent-engine`` in *args*, or ``None``."""
    for index, arg in enumerate(args):
        if arg == ENGINE_FLAG and index + 1 < len(args):
            return args[index + 1]
    return None


def _record(args: list[str], route: str) -> None:
    path = os.environ.get(LAUNCH_LOG_ENV, "")
    if not path:
        return
    line = json.dumps({"at": time.time(), "argv": args, "route": route})
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _run_fake(args: list[str]) -> None:
    """Become the fake backend with *args*; never returns."""
    os.execv(sys.executable, [sys.executable, FAKE_BACKEND, *args])


def main() -> int:
    args = sys.argv[1:]
    if args == ["acp", "--help"]:
        sys.stdout.write(ACP_HELP)
        return 0
    if args[:1] == ["whoami"]:
        sys.stderr.write(SIGNED_OUT_TEXT + "\n")
        return 1
    if args[:1] == ["acp"]:
        if engine_of(args) == KAS_ENGINE:
            _record(args, ROUTE_KAS_RELAY)
            _run_fake(args)
        _record(args, ROUTE_SIGNED_OUT)
        sys.stderr.write(SIGNED_OUT_TEXT + "\n")
        return 1
    _run_fake(args)
    return 0  # pragma: no cover - execv does not return


if __name__ == "__main__":
    sys.exit(main())
