"""The placeholders any app's agent template may name, and the values the gateway computes.

An app's agent config ships inside the app, so it can only name paths that exist when
the app is packaged. Where the data home put the app, its data directory, the ``uv``
this host resolves and the ``PATH`` its tools are found on are known only on the host,
so a template names them as placeholders and ``bridges_agents._render_shipped_agent``
writes the rendered copy under the data home.

The set is fixed and names no app. Every value is computed here, in the gateway
process, from the data home, the gateway's own environment and the installed Python
package. None is read from a file, so an agent that can write the app's tree or data
cannot choose one. Rendering grants nothing: the app could already spell any of these
strings literally, and the rendered config goes through the same MCP policy, tool
ceiling and auto-approve strip as one that names no placeholder.

A value that cannot be computed is left out, and a template that names it is then not
registered (fail closed). Owning spec: app-kit-platform.md § 34, "Agent-template
placeholders".
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from kiro_crew import soffice_discovery
from kiro_crew.apps.manager import app_dir
from kiro_crew.env import resolve_uv

#: The app's registration root: the directory its manifest and agent files are read
#: from (``bridges_sources._registration_source``). For an installed app it is the
#: install directory under the data home.
APP_ROOT = "{APP_ROOT}"
#: The app's data directory, ``<data home>/apps/<name>/data``: the path
#: ``manager_records.app_data_dir`` names and the hooks receive as ``ctx.data_dir``.
APP_DATA = "{APP_DATA}"
#: The absolute ``uv`` this host resolves (``kiro_crew.env.resolve_uv``). Left out when
#: there is none.
UV_BIN = "{UV_BIN}"
#: The gateway's ``PATH``, plus the install directory of a host tool the gateway itself
#: finds off ``PATH`` (LibreOffice's fixed Windows roots,
#: ``soffice_discovery.windows_install_path``). Left out when both are empty, so a
#: template that appends ``{PATH_SEP}<dir>`` never renders a leading empty element,
#: which on POSIX means the current directory.
HOST_PATH = "{HOST_PATH}"
#: ``os.pathsep`` on this host, so one template can extend ``{HOST_PATH}`` on every OS.
PATH_SEP = "{PATH_SEP}"

#: Every host placeholder, in the order the spec lists them.
HOST_PLACEHOLDERS: tuple[str, ...] = (APP_ROOT, APP_DATA, UV_BIN, HOST_PATH, PATH_SEP)


def _host_path() -> str:
    """The value of ``{HOST_PATH}``, or ``""`` when there is nothing to put in it."""
    entries = []
    inherited = os.environ.get("PATH", "")
    if inherited:
        entries.append(inherited)
    if shutil.which(soffice_discovery.SOFFICE) is None:
        found = soffice_discovery.windows_install_path()
        if found:
            entries.append(os.path.dirname(found))
    return os.pathsep.join(entries)


def host_placeholder_values(app_name: str, app_root: Path) -> dict[str, str]:
    """``{"{NAME}": value}`` for every host placeholder this host can compute.

    *app_root* is the root the app's agents register from. ``{APP_DATA}`` is derived
    without creating the directory: rendering a template is not a reason to create a
    data dir, and an installed app already has one.
    """
    values = {
        APP_ROOT: str(app_root),
        APP_DATA: str(app_dir(app_name) / "data"),
        PATH_SEP: os.pathsep,
    }
    uv_bin = resolve_uv()
    if uv_bin:
        values[UV_BIN] = uv_bin
    host_path = _host_path()
    if host_path:
        values[HOST_PATH] = host_path
    return values
