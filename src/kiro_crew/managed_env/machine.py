"""The seam to machine mode: what a new managed environment's Crew is told to run on.

A managed environment's Crew runs Crew's own engine on the environment's machine token,
not on a person's sign-in. That needs machine mode (the ``agent.kas_*`` keys) in the
Crew release the environment installs. Everything this lane asks of machine mode is in
this module, and nothing else in the lane names an ``agent.kas_*`` key:

* :func:`config_script`: the box's ``kirocrew config set`` calls that turn it on;
* :data:`ROUTE_CHECK` and :data:`WHOAMI_CHECK`: the box's own answer that it is on.

The box's Crew also needs the ``crew_client`` preview on, because machine mode is read
only while it is.
"""

from __future__ import annotations

import base64
import json
import shlex
from typing import Any

from kiro_crew.managed_env.remote import WRAPPER
from kiro_crew.managed_env.settings import Cell


def shipped(agent_config: Any = None) -> bool:
    """Whether this Crew release has machine mode (every key :func:`config_args` writes).

    A managed environment installs Crew's public release, so a release without machine
    mode could only create an environment whose install then fails. Until it ships the
    lane offers no launch row. *agent_config* is the config class to read, for tests.
    """
    import dataclasses

    if agent_config is None:
        from kiro_crew.config.sections_agent import AgentConfig as agent_config

    names = {f.name for f in dataclasses.fields(agent_config)}
    return all(name in names for name in _AGENT_FIELDS)


_AGENT_FIELDS = (
    "kas_auth",
    "kas_bundle",
    "kas_endpoint",
    "kas_control_plane_endpoint",
    "kas_region",
    "kas_token_path",
    "kas_machine_env_drop",
)

#: The config script's last line when every ``config set`` exited 0.
CONFIG_DONE = "machine-config: done"
#: ``kirocrew kas route`` answers this when Crew's own engine owns the sign-in.
ROUTE_KAS_BUNDLE = "kas-bundle"
#: ``kas whoami --format json``'s ``accountType`` for a machine token.
MACHINE_ACCOUNT_TYPE = "machine"
#: The class the box's Crew reports for a missing, empty or unreadable token file.
TOKEN_MISSING = "machine_token_missing"


def config_args(cell: Cell) -> list[tuple[str, str]]:
    """The ``config set`` pairs, in order. ``agent.kas_auth machine`` goes last, so a
    config written halfway stays on the person's sign-in path, which machine mode's
    absence refuses at once on the box."""
    args = [
        ("dashboard.feature_previews.crew_client", "true"),
        ("agent.kas_bundle", "true"),
        ("agent.kas_endpoint", cell.endpoint),
        ("agent.kas_control_plane_endpoint", cell.control_plane_endpoint),
        ("agent.kas_region", cell.region),
        ("agent.kas_token_path", cell.token_path),
    ]
    if cell.env_drop:
        # Base64, so a shell on the way cannot strip the JSON's quotes.
        drop = base64.b64encode(json.dumps(list(cell.env_drop)).encode()).decode()
        args.append(("agent.kas_machine_env_drop", f"--value-base64={drop}"))
    args.append(("agent.kas_auth", "machine"))
    return args


def config_script(cell: Cell) -> str:
    """Crew's own ``config set``, one call per key, through the launcher on /projects.
    It ends in :data:`CONFIG_DONE` only when every call exited 0."""
    calls = [
        f"{WRAPPER} config set {shlex.quote(key)} {shlex.quote(value)} </dev/null"
        for key, value in config_args(cell)
    ]
    return " && ".join([*calls, f"echo {shlex.quote(CONFIG_DONE)}"]) + "\n"


#: The boot hook's two gates, in shell: Crew's own bundle and a machine sign-in.
ROUTE_CHECK = f'[ "$({WRAPPER} kas route </dev/null 2>/dev/null)" = {ROUTE_KAS_BUNDLE} ]'
WHOAMI_CHECK = (
    f'machine_identity="$({WRAPPER} kas whoami --format json </dev/null 2>/dev/null)"'
    ' && printf \'%s\\n\' "$machine_identity" | grep -q \'"accountType" *: *"machine"\''
)
#: The sign-in step's read: the box's own account, as JSON on the last line.
WHOAMI_SCRIPT = f"timeout 60 {WRAPPER} kas whoami --format json </dev/null\n"


def account_type(stdout: str) -> str:
    """``accountType`` from the last JSON line of a ``kas whoami --format json``."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("accountType"), str):
            return data["accountType"]
    return ""
