"""Install Crew on a managed environment the way a user would, then its boot hook.

Every remote step is one of a closed list of scripts, each rendered here from checked
values and run over the ``crew-sbx-<id>`` alias (:mod:`.remote`):

1. :func:`install_script`: Crew's public one-liner (:data:`PUBLIC_URL`, which picks the
   Python itself) with ``KIROCREW_HOME`` on ``/projects``, so the install survives a
   restart, then the launcher :data:`.remote.WRAPPER` that points at it;
2. :func:`.machine.config_script`: machine mode on the box's Crew;
3. :func:`hook_write_script`: the boot hook :data:`HOOK`, root-owned 0755;
4. :data:`HOOK_RUN`: run the hook once, now.

The hook clears the image's credential variables (:data:`ALWAYS_CLEARED` plus the
configured ones) before it starts anything, and starts the gateway only when the box's
Crew answers on machine mode (:mod:`.machine`). No kiro-cli is used.
"""

from __future__ import annotations

import base64
import shlex
from typing import Iterable

from kiro_crew.managed_env import machine
from kiro_crew.managed_env.remote import CREW_BIN, GATEWAY_PORT, ROOT, WRAPPER, XDG_DATA_HOME
from kiro_crew.managed_env.settings import ENV_NAME_RE

PUBLIC_URL = "https://download.crew.kiro.dev/cli.sh"
CREW_HOME = f"{ROOT}/.crew/home"
#: The one-liner's venv is ``${KIROCREW_HOME}-venv``.
CREW_VENV = f"{CREW_HOME}-venv"
#: Where pipx puts its venvs and links when the image has pipx: on /projects too.
PIPX_HOME = f"{ROOT}/.crew/pipx"
PIPX_BIN = f"{ROOT}/.crew/pipx-bin"
SANDBOX_DIR = f"{ROOT}/.sandbox"
HOOK_NAME = "50-kirocrew-gateway"
HOOK = f"{SANDBOX_DIR}/boot.d/{HOOK_NAME}"
#: The image supervises ``run/<name>.pid`` only for a hook of that name in ``boot.d``.
PIDFILE = f"{SANDBOX_DIR}/run/{HOOK_NAME}.pid"
LOG = f"{SANDBOX_DIR}/gateway.log"
#: Pidfile line 2: the gateway is alive only while its cmdline carries this.
GATEWAY_TOKEN = f"kirocrew gateway --no-open --port {GATEWAY_PORT}"
#: Cloud credentials an image may export, always cleared before Crew starts.
ALWAYS_CLEARED = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
)

INSTALL_DONE = "install: done"
INSTALL_ALREADY = "install: already"
HOOK_WRITTEN = "hook: written"
#: The hook's exit codes when it starts nothing.
NOT_MACHINE_EXIT = 4
NEEDS_SIGNIN_EXIT = 3
LAUNCHER_FAILED_EXIT = 5


class InstallError(ValueError):
    """A value that would make an unsafe script."""


def cleared_names(extra: Iterable[str] = ()) -> tuple[str, ...]:
    """The names the hook clears: :data:`ALWAYS_CLEARED`, then *extra*, each once."""
    out = list(ALWAYS_CLEARED)
    for name in extra:
        if not ENV_NAME_RE.fullmatch(name):
            raise InstallError(f"not an environment variable name: {name!r}")
        if name not in out:
            out.append(name)
    return tuple(out)


def launcher_text() -> str:
    """The launcher on /projects: whichever of the installer's two installs exists.

    The installer uses pipx when the image has it, else a managed venv beside the data
    home. Both are pointed at /projects (:data:`PIPX_HOME`, :data:`CREW_VENV`), so either
    survives a restart; the launcher runs the one that is there. A managed venv is tried
    first through its ``-current`` link, which an in-app update repoints, so a resumed
    environment starts the updated release.
    """
    return f"""#!/bin/sh
export KIROCREW_HOME={CREW_HOME}
export PIPX_HOME={shlex.quote(PIPX_HOME)}
export PIPX_BIN_DIR={shlex.quote(PIPX_BIN)}
for bin in {CREW_VENV}-current/bin/kirocrew {CREW_VENV}/bin/kirocrew {PIPX_HOME}/venvs/kirocrew/bin/kirocrew; do
  if [ -x "$bin" ]; then exec "$bin" "$@"; fi
done
echo "kirocrew: no install under {ROOT}/.crew" >&2
exit 127
"""


def install_script() -> str:
    """The public one-liner into ``/projects``, then the launcher; idempotent."""
    encoded = base64.b64encode(launcher_text().encode()).decode()
    return f"""set -e
if [ -x {WRAPPER} ] && {WRAPPER} --version >/dev/null 2>&1; then echo {shlex.quote(INSTALL_ALREADY)}; exit 0; fi
mkdir -p {CREW_BIN}
curl -fsSL {PUBLIC_URL} | KIROCREW_HOME={CREW_HOME} PIPX_HOME={PIPX_HOME} PIPX_BIN_DIR={PIPX_BIN} sh -s
printf %s {encoded} | base64 -d > {WRAPPER}.tmp
chmod 0755 {WRAPPER}.tmp
mv -f {WRAPPER}.tmp {WRAPPER}
{WRAPPER} --version >/dev/null
echo {shlex.quote(INSTALL_DONE)}
"""


def hook_text(clear: Iterable[str] = ()) -> str:
    """The boot hook: idempotent, returns at once; the gateway leaves its process group."""
    names = " ".join(cleared_names(clear))
    return f"""#!/bin/sh
# {HOOK_NAME}: start the Kiro Crew gateway from {ROOT}.
unset {names}
export XDG_DATA_HOME={XDG_DATA_HOME}
export PATH={CREW_BIN}:$PATH
TOKEN='{GATEWAY_TOKEN}'
mkdir -p {SANDBOX_DIR}/run
if [ -r {PIDFILE} ]; then
  pid="$(sed -n 1p {PIDFILE})"
  case "$pid" in
    ''|*[!0-9]*) ;;
    *) if [ -r "/proc/$pid/cmdline" ] && tr '\\000' ' ' <"/proc/$pid/cmdline" | grep -qF "$TOKEN"; then
         echo "{HOOK_NAME}: alive"; exit 0
       fi ;;
  esac
fi
{WRAPPER} --version </dev/null >/dev/null 2>&1 || {{ echo "{HOOK_NAME}: launcher_failed"; exit {LAUNCHER_FAILED_EXIT}; }}
{machine.ROUTE_CHECK} || {{ echo "{HOOK_NAME}: not_machine"; exit {NOT_MACHINE_EXIT}; }}
{machine.WHOAMI_CHECK} || {{ echo "{HOOK_NAME}: needs_signin"; exit {NEEDS_SIGNIN_EXIT}; }}
cd {ROOT} || exit 1
setsid nohup {WRAPPER} gateway --no-open --port {GATEWAY_PORT} </dev/null >>{LOG} 2>&1 &
printf '%s\\n%s\\n' "$!" "$TOKEN" >{PIDFILE}.tmp && mv -f {PIDFILE}.tmp {PIDFILE}
echo "{HOOK_NAME}: started"
"""


def hook_write_script(clear: Iterable[str] = ()) -> str:
    """Write :func:`hook_text` to :data:`HOOK` through a temp file, root-owned 0755."""
    encoded = base64.b64encode(hook_text(clear).encode()).decode()
    return f"""set -e
mkdir -p {SANDBOX_DIR}/boot.d
printf %s {encoded} | base64 -d > {HOOK}.tmp
chmod 0755 {HOOK}.tmp
mv -f {HOOK}.tmp {HOOK}
echo {shlex.quote(HOOK_WRITTEN)}
"""


HOOK_RUN = f"{HOOK} </dev/null\n"


def hook_state(returncode: int, stdout: str) -> str:
    """``started`` / ``alive`` / ``not_machine`` / ``needs_signin`` / ``failed``."""
    words = {"started", "alive", "not_machine", "needs_signin", "launcher_failed"}
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(f"{HOOK_NAME}: "):
            word = line.split(": ", 1)[1].strip()
            if word in words:
                return "failed" if word == "launcher_failed" else word
    return "failed"
