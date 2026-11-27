"""The packaged fake ACP backend, asking for approval of a command the tool gate holds.

The packaged backend's ``[[GATED]]`` turn asks about ``echo hello-from-fake``, which the
gateway's tool gate allows without asking, so no approval ever waits and the Owner
approval routes have nothing to read or answer. This wrapper changes two things and
nothing else: the command text in the tool call the backend reports, and how long it
waits for the answer. Like the packaged backend it runs no command: it reports a tool
call, asks, waits for the answer and reports the call as completed or failed.

It reaches into two private names of the backend. If either is gone the wrapper stops
at import with the name, so a renamed internal fails every turn loudly instead of
leaving the packaged command in place.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Any

from kiro_crew.testing import fake_acp_backend as engine
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

#: Names the command the tool call reports. Set by the launcher, not by a prompt.
COMMAND_ENV = "KIROCREW_CONTRACT_GATED_COMMAND"
#: The title the packaged backend gives its one tool call.
_TOOL_TITLE = "fake demo tool"
#: Twice the longest wait a contract case makes, so the case's own bound ends first.
WAIT_SECS = 2 * FALLBACK_TIMEOUT_SECS


def launcher(directory: Path, command: str) -> Path:
    """Write the agent binary a gateway spawns: this module, reporting *command*.

    POSIX only: the one CI step that runs the contract module is Linux only.
    """
    path = directory / "gated-fake-backend"
    path.write_text(
        "#!/bin/sh\n"
        f"{COMMAND_ENV}={shlex.quote(command)} "
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        '"$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _install(command: str) -> None:
    update = getattr(engine, "_update", None)
    wait = getattr(engine, "PERMISSION_WAIT_SECS", None)
    if not callable(update) or not isinstance(wait, float):
        raise SystemExit(
            "crew_api_contract_gated_engine: fake_acp_backend no longer has _update and "
            "PERMISSION_WAIT_SECS; the wrapper must follow the rename"
        )

    def update_with_command(session_id: str, payload: dict[str, Any]) -> None:
        raw = payload.get("rawInput")
        if payload.get("title") == _TOOL_TITLE and isinstance(raw, dict) and "command" in raw:
            payload = {**payload, "rawInput": {**raw, "command": command}}
        update(session_id, payload)

    engine._update = update_with_command  # type: ignore[attr-defined]
    engine.PERMISSION_WAIT_SECS = WAIT_SECS


if __name__ == "__main__":
    wanted = os.environ.get(COMMAND_ENV, "")
    if not wanted:
        raise SystemExit(f"crew_api_contract_gated_engine: {COMMAND_ENV} is not set")
    _install(wanted)
    sys.exit(engine.main())
