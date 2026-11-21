"""Install Crew on a managed environment the way a user would, then the boot hook.

Every remote step is one ``ssh <alias> <command>`` as root (no key; the alias is the
``Host crew-sbx-*`` block of :mod:`.ssh_config`). The commands are a closed list, rendered
by :func:`commands_text`; a test pins that the runner never sees a command outside it.

**The install** is Crew's public one-liner (``cli.sh`` from :data:`PUBLIC_URL`), run in the
lane's own env (:func:`install_env_script`): a Python found by real path the way the
one-liner's own ladder looks (3.12, then 3.13 as a last resort), each candidate checked by
running it; a fixed PATH with no pipx (a pipx there would take the install off /projects);
``KIROCREW_HOME=/projects/.crew/home``, so the venv lands on ``/projects`` and survives a
restart. After the one-liner exits 0 it writes the launcher ``/projects/.crew/bin/kirocrew``,
which the hook and the token mint run. "Installed" means that launcher runs.

**Machine mode** (:func:`machine_config_command`): Crew's own ``config set``, one call per
key, writes the engine values from ``instances.managed_env_*``, so the box's Crew runs on
the environment's machine token and asks for no sign-in.

**The boot hook** is ``/projects/.sandbox/boot.d/50-kirocrew-gateway``, root-owned 0755
whatever the ssh umask, gateway started from ``/projects``. It clears the image's credential
variables (:func:`hook_unset_env`) before it starts anything, and starts the gateway only
when Crew's own engine owns the sign-in (``kas route`` answers ``kas-bundle``) and
``kas whoami`` answers. No kiro-cli is used. Its pidfile is ``run/<hook name>.pid``: the image
supervises a pidfile only when a hook of that name is in ``boot.d``.

**A lone ssh exit 255.** The environment's ssh server can send all of a command's output and
then exit status 255 with no message, when its reaper reaps the session's shell before the
session's own wait does. So the install check is judged by its done line, whatever the rc,
and the idempotent setup commands are tried up to :data:`LONE_255_TRIES` times while the
answer is a lone 255: rc 255 and no line but the command's own. Any other line (the alias's
ProxyCommand prints a ``mint-and-attach:`` line on every failure) is reported at once, so a
failed mint never costs another session. ``install-public`` is never tried again.
"""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

from kiro_crew.managed_env.settings import MachineCell

ROOT = "/projects"
SANDBOX_DIR = f"{ROOT}/.sandbox"
BOOT_D = f"{SANDBOX_DIR}/boot.d"
HOOK_NAME = "50-kirocrew-gateway"
HOOK = f"{BOOT_D}/{HOOK_NAME}"
HOOK_TMP = f"{BOOT_D}/.{HOOK_NAME}.tmp"
XDG_DATA = f"{ROOT}/.xdg-data"
#: The image supervises ``run/<name>.pid`` only when a hook named ``<name>`` is in ``boot.d``,
#: so the name comes from the hook's.
PIDFILE = f"{SANDBOX_DIR}/run/{HOOK_NAME}.pid"
BOOT_LOG = f"{SANDBOX_DIR}/boot.log"  # the image's boot runner writes it
LOG = f"{SANDBOX_DIR}/gateway.log"
GATEWAY_PORT = 5476  # the core's default dashboard/gateway port
#: Pidfile line 2: the image reads the gateway as alive iff its cmdline carries this.
GATEWAY_TOKEN = f"kirocrew gateway --no-open --port {GATEWAY_PORT}"

#: Crew's own place on /projects: bin dir, data home, the one-liner's venv
#: (``${KIROCREW_HOME}-venv``) and the stable link the update engine keeps pointing at it.
CREW_DIR = f"{ROOT}/.crew"
CREW_BIN = f"{CREW_DIR}/bin"
CREW_HOME = f"{CREW_DIR}/home"
CREW_VENV = f"{CREW_HOME}-venv"
CREW_VENV_CURRENT = f"{CREW_VENV}-current"
#: The launcher on /projects: the hook starts the gateway with it and the Remote Crew row's
#: ``remote_bin`` names it, so the token mint runs it too.
WRAPPER = f"{CREW_BIN}/kirocrew"
WRAPPER_CHECK_SECS = 20  # bound on a ``--version`` read of the launcher

#: Where a user-level install puts its launchers (cli.sh: ~/.local/bin).
PATH_PREFIX = "/root/.local/bin:/usr/local/bin"

#: Crew's public installer.
PUBLIC_URL = "https://download.crew.kiro.dev/cli.sh"

#: The one-liner's PATH after Crew's bin dir: a fixed list, nothing from the image PATH, so no
#: pyenv shim and no ``/usr/local/bin/pipx``. cli.sh takes its pipx branch whenever
#: ``command -v pipx`` answers, and that branch ignores ``KIROCREW_HOME``.
INSTALL_BASE_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
#: The image's own pipx: read for the record only (``image_pipx=``), never run for the install.
IMAGE_PIPX = "/usr/local/bin/pipx"
#: The installer is fetched to this file first, so a failed download has its own exit code.
CLI_SH_FILE = f"{CREW_DIR}/.cli.sh"
#: The one-liner's own interpreter ladder (3.12, then 3.13 as a last resort) as real paths,
#: in order: (python_source, path, minor). A ``*`` is a patch number; the newest is tried first.
PY_RUNGS = (
    ("pyenv", "/root/.pyenv/versions/3.12.*/bin/python3.12", "3.12"),
    ("system", "/usr/bin/python3.12", "3.12"),
    ("pyenv", "/root/.pyenv/versions/3.13.*/bin/python3.13", "3.13"),
    ("system", "/usr/bin/python3.13", "3.13"),
)
#: The names cli.sh's ladder tries on its PATH, in its order.
CLI_LADDER = ("python3.12", "python3.13", "python3")
#: The one-liner's managed Python (no rung passed) downloads from these.
MANAGED_NEEDS = "github.com,python-build-standalone"

#: Why an install could not go on. Stable words: the engine maps each to its own text.
WHY_PIPX_ON_BASE_PATH = "pipx_on_base_path"
WHY_KAS_UNAVAILABLE = "kas_unavailable"  # cli.sh exit 3: this host cannot run the KAS engine
WHY_FETCH_FAILED = "fetch_failed"  # the installer's download failed (a proxy 403, no route)
WHY_INSTALL_FAILED = "install_failed"
WHY_NOT_KAS_BUNDLE = "not_kas_bundle"  # Crew installed, but its engine is not on the bundle route

#: ``kas route``'s answer when Crew's own engine owns the sign-in.
ROUTE_KAS_BUNDLE = "kas-bundle"
HAVE_KAS_BUNDLE = "kas-bundle"  # the probe's ``have:`` word for that route
KAS_UNAVAILABLE_EXIT = 3  # cli.sh's exit when Crew installed but the KAS engine can't run here

PATH_A = "A"

NEEDS_SIGNIN_EXIT = 3
NOT_KAS_BUNDLE_EXIT = 4  # the launcher's engine is not on the bundle route: nothing to start
LAUNCHER_FAILED_EXIT = 5  # Crew's launcher does not run: nothing to start

#: ssh's own failure status, which the box's ssh server also sends when its reaper wins.
SSH_EXIT = 255
#: A lone 255 on an idempotent setup command: tries in all, and the seconds between them.
LONE_255_TRIES = 3
LONE_255_DELAY = 3.0

# ---- the closed command list -------------------------------------------------------------

CMD_PROBE = "probe"
CMD_INSTALL_PUBLIC = "install-public"
CMD_HOOK_DIRS = "hook-dirs"
CMD_HOOK_WRITE = "hook-write"
CMD_HOOK_RUN = "hook-run"
CMD_GATEWAY_PROBE = "gateway-probe"
CMD_VERIFY_KILL = "verify-kill-gateway"
CMD_VERIFY_BOOT_LOG = "verify-boot-log"
CMD_MACHINE_CONFIG = "machine-config"
CMD_CREW_ALIVE = "crew-alive"

#: ``machine-config``'s last line: every ``config set`` before it exited 0.
MACHINE_CONFIG_DONE = "machine-config=done"

_q = shlex.quote

COMMANDS_A: dict[str, tuple[str, str]] = {
    CMD_PROBE: (
        "what is already on the box: whether Crew's launcher on /projects runs "
        "(`have:crew-wrapper`), and whether its `kas route` answers kas-bundle "
        "(`have:kas-bundle`: Crew's own engine owns the sign-in)",
        f"timeout {WRAPPER_CHECK_SECS} {WRAPPER} --version </dev/null >/dev/null 2>&1 && echo have:crew-wrapper; "
        f'r="$(timeout {WRAPPER_CHECK_SECS} {WRAPPER} kas route </dev/null 2>/dev/null)" '
        f'&& [ "$r" = {ROUTE_KAS_BUNDLE} ] && echo have:{HAVE_KAS_BUNDLE}; '
        "echo probe=done",
    ),
    CMD_INSTALL_PUBLIC: (
        "Crew's public one-liner in the lane's own env (script on stdin: Python by real path, "
        "no pipx, install on /projects, then the launcher)",
        "sh -s",
    ),
    CMD_HOOK_DIRS: (
        "create the hook dirs and the data dir; root-owned 0755 whatever the ssh umask",
        f"mkdir -p {BOOT_D} {XDG_DATA} && chown root:root {SANDBOX_DIR} {BOOT_D} {XDG_DATA} "
        f"&& chmod 0755 {SANDBOX_DIR} {BOOT_D}",
    ),
    CMD_HOOK_WRITE: (
        "write the boot hook from stdin: root-owned, 0755, then moved into place",
        f"cat >{HOOK_TMP} && chown root:root {HOOK_TMP} && chmod 0755 {HOOK_TMP} && mv -f {HOOK_TMP} {HOOK}",
    ),
    CMD_HOOK_RUN: (
        "run the hook now (exit 3 = needs sign-in, gateway not started)",
        HOOK,
    ),
    CMD_GATEWAY_PROBE: (
        f"is the gateway listening on 127.0.0.1:{GATEWAY_PORT}",
        f"bash -c 'exec 3<>/dev/tcp/127.0.0.1/{GATEWAY_PORT}' 2>/dev/null && echo gateway=up || echo gateway=down",
    ),
}

#: Verification only, never run by the install: two reads of the image's supervision
#: (kill the gateway the pidfile names, then read the boot runner's log).
COMMANDS_C: dict[str, tuple[str, str]] = {
    CMD_VERIFY_KILL: (
        "kill the gateway the pidfile names, only if its cmdline carries the token "
        "(the image's boot runner must then restart the hook)",
        f'pid="$(sed -n 1p {PIDFILE})"; case "$pid" in \'\'|*[!0-9]*) echo pid=none; exit 1 ;; esac; '
        f"tr '\\000' ' ' </proc/\"$pid\"/cmdline | grep -qF '{GATEWAY_TOKEN}' && kill \"$pid\" "
        '&& echo "killed=$pid"',
    ),
    CMD_VERIFY_BOOT_LOG: (
        "read the end of the boot runner's log",
        f"tail -n 40 {BOOT_LOG}",
    ),
}


#: ``crew-alive``'s two answers. ``crew=up`` only when Crew's own gateway runs: the pid
#: on the pidfile's line 1, that pid's cmdline carrying :data:`GATEWAY_TOKEN` (the hook's own
#: alive check), then the port answers. Any other listener on the port reads ``crew=down``.
CREW_UP = "crew=up"
CREW_DOWN = "crew=down"


def crew_alive_command(
    sandbox: str = SANDBOX_DIR, proc: str = "/proc", port: int = GATEWAY_PORT
) -> str:
    """``crew-alive``'s one fixed text. ``sandbox``, ``proc`` and ``port`` stand for the box's own
    and are only for tests; the shipped text has none."""
    pidfile = f"{sandbox}/run/{HOOK_NAME}.pid"
    return (
        f'pid="$(sed -n 1p {pidfile} 2>/dev/null)"; '
        f"case \"$pid\" in ''|*[!0-9]*) echo {CREW_DOWN}; exit 0 ;; esac; "
        f"tr '\\000' ' ' <{proc}/\"$pid\"/cmdline 2>/dev/null | grep -qF '{GATEWAY_TOKEN}' "
        f"|| {{ echo {CREW_DOWN}; exit 0; }}; "
        f"bash -c 'exec 3<>/dev/tcp/127.0.0.1/{port}' 2>/dev/null "
        f"&& echo {CREW_UP} || echo {CREW_DOWN}"
    )


#: Section E: one read on a managed environment Crew has no record of, on the user's Set up
#: Crew or after their Resume. Read-only; no hook, no install, no write.
COMMANDS_E: dict[str, tuple[str, str]] = {
    CMD_CREW_ALIVE: (
        "read only: is Crew's own gateway running here: the pid on the pidfile's line 1, its "
        "cmdline carrying the gateway token (the hook's alive check), then the port",
        crew_alive_command(),
    ),
}


def wrapper_script(prefix: str = "") -> str:
    """``/projects/.crew/bin/kirocrew``: Crew's data home on /projects, Crew's bin dir first on
    PATH, then the venv's launcher through the stable link the update engine repoints.
    ``prefix`` stands for ``/`` and is only for tests; the shipped text has none."""
    p = prefix.rstrip("/")
    return "\n".join(
        [
            "#!/bin/sh",
            f"# kirocrew on {ROOT} (written by kiro_crew.managed_env.install): the hook and the token",
            f"# mint run this, so Crew's data home and venv stay on {ROOT} and survive a restart.",
            f"export KIROCREW_HOME={_q(p + CREW_HOME)}",
            f"export XDG_DATA_HOME={_q(p + XDG_DATA)}",
            f'PATH={_q(p + CREW_BIN)}:"$PATH"',
            "export PATH",
            f'exec {_q(p + CREW_VENV_CURRENT)}/bin/kirocrew "$@"',
            "",
        ]
    )


def install_env_script(prefix: str = "", fetch: str = "", openssl: str = "") -> str:
    """The stdin of ``install-public``: the one-liner in the lane's own env, then the launcher.

    Prints ``python_source=`` (pyenv|system|managed), ``python=``, ``pipx=``, ``image_pipx=``,
    ``place=`` and ``openssl=`` (the one on PATH, the image's linked into Crew's bin dir, or
    ``none``) before the one-liner, and ``install_rc=`` and ``wrapper=`` after it; a stop
    before the one-liner prints ``stop=<reason>`` instead (``pipx_on_base_path``,
    ``ladder_mismatch``). The one-liner is downloaded to :data:`CLI_SH_FILE` and then run, so a
    failed download prints ``stop=fetch_failed`` and ``fetch_rc=<the fetch's exit>``, never
    ``install_rc=0``. Every check and ``--version`` read takes ``</dev/null``, since the script
    itself is read from stdin.

    ``openssl`` is ``instances.managed_env_box_openssl``: an image openssl to link when none is
    on the fixed PATH (cli.sh checks its signed manifest with it); empty links none.
    ``prefix`` (a dir standing for ``/``) and ``fetch`` (the command that prints the installer)
    are only for tests.
    """
    p = prefix.rstrip("/")
    fetch = fetch or f"curl -fsSL {PUBLIC_URL}"
    home = _q(p + CREW_HOME)
    image_pipx, cli_file = _q(p + IMAGE_PIPX), _q(p + CLI_SH_FILE)
    minors = sorted({m for _, _, m in PY_RUNGS})
    out = [
        "# install-public (kiro_crew.managed_env.install): the public one-liner with a Python found",
        "# by real path, no pipx on PATH, and the install on /projects; then the launcher there.",
        *_crew_dir_lines(p),
        *_python_rung_lines(p),
    ]
    out.append("# The choice, by its minor version, where the one-liner's ladder looks first.")
    for m in minors:
        link = _q(f"{p}{CREW_BIN}/python{m}")
        out.append(
            f'if [ "$minor" = {m} ]; then ln -sfn "$py" {link} || {{ echo stop=link_failed; exit 20; }}; '
            f"elif [ -L {link} ]; then rm -f {link}; fi"
        )
    out += [
        "# cli.sh checks its signed manifest with the openssl on this PATH.",
        'ossl="$(command -v openssl 2>/dev/null || true)"',
    ]
    if openssl.startswith("/"):
        image_openssl, openssl_link = _q(p + openssl), _q(f"{p}{CREW_BIN}/openssl")
        out += [
            "# With none there, link the configured image openssl (it must run); never its dir.",
            'if [ -z "$ossl" ]; then',
            f"  if [ -x {image_openssl} ] "
            f"&& timeout 5 {image_openssl} version </dev/null >/dev/null 2>&1; then",
            f"    ln -sfn {image_openssl} {openssl_link} || {{ echo stop=link_failed; exit 20; }}",
            f"    ossl={image_openssl}",
            "  fi",
            "fi",
        ]
    out += [
        '[ -n "$ossl" ] || ossl=none',
        'echo "python_source=$src"',
        'echo "python=${py:-none}"',
        'pipx_at="$(command -v pipx 2>/dev/null || true)"',
        'echo "pipx=${pipx_at:-absent}"',
        f"if [ -x {image_pipx} ]; then",
        f"  if timeout 5 {image_pipx} --version </dev/null >/dev/null 2>&1; then image_pipx=works; "
        "else image_pipx=broken; fi",
        "else",
        "  image_pipx=absent",
        "fi",
        'echo "image_pipx=$image_pipx"',
        f"echo place={home}",
        'echo "openssl=$ossl"',
        "# Any pipx on this PATH, working or not, would take the one-liner off /projects.",
        'if [ -n "$pipx_at" ]; then echo stop=pipx_on_base_path; exit 21; fi',
        "# The installer is downloaded to a file first: a failed download is then its own exit",
        "# (the fetch's), never the exit of an sh that read an empty script.",
        "_kc_install() {",
        f"  rm -f {cli_file}",
        f"  {fetch} >{cli_file} || {{ frc=$?; rm -f {cli_file}; echo stop=fetch_failed; "
        'echo "fetch_rc=$frc"; exit 25; }',
        f'  sh {cli_file} "$@" </dev/null',
        "}",
        "# The one-liner's own ladder on this PATH must land on the link.",
        'if [ "$src" != managed ]; then',
        '  first=""',
        f"  for n in {' '.join(CLI_LADDER)}; do",
        '    if command -v "$n" >/dev/null 2>&1 && timeout 5 "$n" -c '
        "'import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)' </dev/null >/dev/null 2>&1; then",
        '      first="$(command -v "$n")"; break',
        "    fi",
        "  done",
        '  echo "ladder=${first:-none}"',
        f'  if [ "$first" != {_q(p + CREW_BIN)}/python"$minor" ]; then echo stop=ladder_mismatch; exit 22; fi',
        "  _kc_install --system-python",
        "else",
        "  _kc_install",
        "fi",
        "rc=$?",
        f"rm -f {cli_file}",
        'echo "install_rc=$rc"',
        f"# 0, or {KAS_UNAVAILABLE_EXIT}: Crew is installed (3: this host cannot run the KAS engine).",
        'case "$rc" in 0|3) ;; *) exit 23 ;; esac',
        *_launcher_lines(prefix),
        "",
    ]
    return "\n".join(out)


def _crew_dir_lines(p: str) -> list[str]:
    """The install script's first lines: a fixed PATH (Crew's bin dir, then the base dirs, nothing
    from the image PATH), ``KIROCREW_HOME`` on /projects, and Crew's bin dir, root-owned 0755.
    ``p`` stands for ``/`` (tests only)."""
    crew_dir, crew_bin, home = _q(p + CREW_DIR), _q(p + CREW_BIN), _q(p + CREW_HOME)
    path = ":".join([p + CREW_BIN] + [p + d for d in INSTALL_BASE_PATH])
    return [
        f"export PATH={_q(path)}",
        f"export KIROCREW_HOME={home}",
        f"mkdir -p {crew_bin} && chown root:root {crew_dir} {crew_bin} "
        f"&& chmod 0755 {crew_dir} {crew_bin} || {{ echo stop=crew_dir_failed; exit 20; }}",
    ]


def _python_rung_lines(p: str) -> list[str]:
    """The Python choice by real path (:data:`PY_RUNGS`, in order, each checked by running it): it
    sets ``src`` (``managed`` when no rung runs), ``py`` and ``minor``."""
    out = [
        "# A candidate counts only if it runs: >= 3.12 with venv and ensurepip (without them the",
        "# one-liner itself switches to its managed Python), bounded, no PYTHONPATH/PYTHONHOME.",
        "_kc_usable() {",
        '  [ -x "$1" ] || return 1',
        '  (unset PYTHONPATH PYTHONHOME; exec timeout 5 "$1" -c '
        "'import sys, venv, ensurepip; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)') "
        "</dev/null >/dev/null 2>&1",
        "}",
        "# The newest patch of $1<patch>$2 that runs, as globbed (an image link is not resolved).",
        "_kc_newest() {",
        '  for c in "$1"*"$2"; do',
        '    v="${c#"$1"}"; v="${v%"$2"}"',
        "    case \"$v\" in ''|*[!0-9]*) continue ;; esac",
        '    echo "$v $c"',
        "  done | sort -rn | while read -r v c; do",
        '    if _kc_usable "$c"; then echo "$c"; break; fi',
        "  done",
        "}",
        'src=managed; py=""; minor=""',
    ]
    for i, (source, pattern, minor) in enumerate(PY_RUNGS):
        kw = "if" if i == 0 else "elif"
        if "*" in pattern:
            head, tail = pattern.split("*", 1)
            out.append(f'{kw} c="$(_kc_newest {_q(p + head)} {_q(tail)})"; [ -n "$c" ]; then')
            out.append(f'  src={source}; py="$c"; minor={minor}')
        else:
            out.append(f"{kw} _kc_usable {_q(p + pattern)}; then")
            out.append(f"  src={source}; py={_q(p + pattern)}; minor={minor}")
    out.append("fi")
    return out


def _launcher_lines(prefix: str) -> list[str]:
    """The install script's last lines: the launcher on /projects (:func:`wrapper_script`), checked
    by running it before it is moved into place, so a failed install never leaves a launcher that
    does not run. Prints ``wrapper=ok`` or ``wrapper=failed`` (exit 24)."""
    p = prefix.rstrip("/")
    wrapper, wrapper_tmp = _q(p + WRAPPER), _q(f"{p}{CREW_BIN}/.kirocrew.tmp")
    return [
        "# The launcher is checked before it is moved into place (its exec path is absolute), so a",
        "# failed install never leaves a launcher that does not run.",
        f"cat >{wrapper_tmp} <<'KC_WRAPPER'",
        wrapper_script(prefix).rstrip("\n"),
        "KC_WRAPPER",
        f"if chown root:root {wrapper_tmp} && chmod 0755 {wrapper_tmp} "
        f"&& timeout {WRAPPER_CHECK_SECS} {wrapper_tmp} --version </dev/null >/dev/null 2>&1 "
        f"&& mv -f {wrapper_tmp} {wrapper}; then",
        "  echo wrapper=ok",
        "else",
        f"  rm -f {wrapper_tmp}; echo wrapper=failed; exit 24",
        "fi",
    ]


#: Registry mode on the box's Crew: the gateway's first session is declared
#: registry-governed, so its managed MCP servers are not waited for.
REGISTRY_MODE_KEY = "agent.mcp_registry_mode"


@dataclass(frozen=True)
class MachineConfig:
    """What ``machine-config`` writes: the engine cell, the box's token file and variable,
    the image's own engine variables to drop, and registry mode."""

    cell: MachineCell
    token_path: str
    token_env: str = ""
    env_drop: tuple[str, ...] = ()
    registry_mode: bool = False


def machine_config_args(mc: MachineConfig) -> list[tuple[str, str]]:
    """The ``config set`` arguments ``machine-config`` runs, in order: ``agent.kas_bundle true``
    first (machine mode needs the bundle route), then the cell's three keys and the token file's
    path, the token variable's name and the drop list when set, then (with registry mode)
    ``agent.mcp_registry_mode true``, and ``agent.kas_auth machine`` last, so a config written
    halfway stays in callback mode.

    The drop list is a JSON array, so it goes as ``--value-base64=<its Base64>``: the command runs
    through ssh and the box's shell, and a shell that strips the quotes would turn ``["a","b"]``
    into ``[a,b]``. Every other value is one shell word as it is."""
    args = [
        ("agent.kas_bundle", "true"),
        ("agent.kas_endpoint", mc.cell.endpoint),
        ("agent.kas_control_plane_endpoint", mc.cell.control_plane_endpoint),
        ("agent.kas_region", mc.cell.region),
        ("agent.kas_token_path", mc.token_path),
    ]
    if mc.token_env:
        args.append(("agent.kas_machine_token_env", mc.token_env))
    if mc.env_drop:
        drop = json.dumps(list(mc.env_drop)).encode()
        args.append(
            ("agent.kas_machine_env_drop", "--value-base64=" + base64.b64encode(drop).decode())
        )
    if mc.registry_mode:
        args.append((REGISTRY_MODE_KEY, "true"))
    args.append(("agent.kas_auth", "machine"))
    return args


def machine_config_command(mc: MachineConfig, *, root: str = ROOT) -> str:
    """``machine-config``: Crew's own ``kirocrew config set <key> <value>``, one call per key
    (:func:`machine_config_args`), through the launcher on /projects, so it writes
    ``config.json`` in Crew's home there (never ``--local``). It ends in
    :data:`MACHINE_CONFIG_DONE` only when every call exited 0. ``root`` is only for tests."""
    launcher = root + WRAPPER[len(ROOT) :]
    calls = [
        f"{launcher} config set {_q(key)} {_q(value)} </dev/null"
        for key, value in machine_config_args(mc)
    ]
    return " && ".join([*calls, f"echo {MACHINE_CONFIG_DONE}"])


#: The credential variables the hook always clears before it runs anything: the container's
#: AWS role credentials and the core's two other spellings of that pair. The image's own
#: credential names come from ``instances.managed_env_box_unset_env`` and the token variable
#: from ``instances.managed_env_box_token_env`` (:func:`hook_unset_env`). The image's boot pass
#: starts the hook with the image server's own environment; without this, the gateway, its
#: engine and Crew's agents and their shell commands would inherit those credentials.
HOOK_UNSET_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
)


def _env_name(name: str) -> bool:
    return bool(name) and (name[0].isalpha() or name[0] == "_") and name.replace("_", "").isalnum()


def hook_unset_env(extra: Iterable[str] = ()) -> tuple[str, ...]:
    """:data:`HOOK_UNSET_ENV` plus *extra* (the configured names), each a valid shell name,
    without repeats. An invalid name is refused rather than dropped, so a typo never leaves a
    credential set."""
    out = list(HOOK_UNSET_ENV)
    for name in extra:
        if not _env_name(name):
            raise InstallError("bad_env_name", f"not an environment variable name: {name!r}")
        if name not in out:
            out.append(name)
    return tuple(out)


def hook_script(root: str = ROOT, unset: Iterable[str] = ()) -> str:
    """``50-kirocrew-gateway``: idempotent, returns at once, the gateway leaves this process group.
    Its first command is ``unset`` of :func:`hook_unset_env` (*unset* adds the configured names),
    before ``ROOT=`` and before anything it starts. ``unset`` of a name that is not set is not an
    error, so the ``register`` run over ssh takes the same line.

    Pidfile ``run/50-kirocrew-gateway.pid`` (named for the hook, so the image supervises it):
    line 1 = pid, line 2 = cmdline token (a pid alone is never trusted: pids are reused). The
    launcher must run first (exit 5 otherwise). Then the sign-in gate: the launcher's
    ``kas route`` must answer ``kas-bundle`` (exit 4 otherwise) and ``kirocrew kas whoami`` must
    answer (exit 3 = needs_signin). No kiro-cli is used. ``root`` is only for tests that run the
    hook under a temp dir; the shipped hook is always ``/projects``."""
    names = hook_unset_env(unset)
    sandbox, xdg = f"{root}/.sandbox", f"{root}/.xdg-data"
    launcher = root + WRAPPER[len(ROOT) :]
    ask = f"timeout {WRAPPER_CHECK_SECS} {launcher}"
    return f"""#!/bin/sh
# {HOOK_NAME}: start the Kiro Crew gateway from {ROOT} (written by kiro_crew.managed_env.install).
# Idempotent; exit 3 = needs_signin, exit 4 = not_kas_bundle, exit 5 = launcher_failed
# (gateway not started).
unset {" ".join(names)}
ROOT={root}
TOKEN='{GATEWAY_TOKEN}'
PIDFILE={sandbox}/run/{HOOK_NAME}.pid
LOG={sandbox}/gateway.log
export KIRO_CREW_SANDBOX_ROOT={root}
export XDG_DATA_HOME={xdg}
export PATH={root}{CREW_BIN[len(ROOT):]}:{PATH_PREFIX}:$PATH
mkdir -p "$(dirname "$PIDFILE")"
if [ -r "$PIDFILE" ]; then
  pid="$(sed -n 1p "$PIDFILE")"
  case "$pid" in
    ''|*[!0-9]*) ;;
    *) if [ -r "/proc/$pid/cmdline" ] && tr '\\000' ' ' <"/proc/$pid/cmdline" | grep -qF "$TOKEN"; then
         echo "{HOOK_NAME}: gateway alive (pid $pid)"
         exit 0
       fi ;;
  esac
fi
if ! {ask} --version </dev/null >/dev/null 2>&1; then
  echo "{HOOK_NAME}: launcher_failed ({launcher} does not run), gateway not started"
  exit {LAUNCHER_FAILED_EXIT}
fi
route="$({ask} kas route </dev/null 2>/dev/null)" || route=""
if [ "$route" != {ROUTE_KAS_BUNDLE} ]; then
  echo "{HOOK_NAME}: not_kas_bundle (Crew's engine is not on the bundle route), gateway not started"
  exit {NOT_KAS_BUNDLE_EXIT}
fi
if ! {ask} kas whoami </dev/null >/dev/null 2>&1; then
  echo "{HOOK_NAME}: needs_signin (kirocrew kas whoami failed), gateway not started"
  exit {NEEDS_SIGNIN_EXIT}
fi
cd "$ROOT" || exit 1
setsid nohup {launcher} gateway --no-open --port {GATEWAY_PORT} </dev/null >>"$LOG" 2>&1 &
pid=$!
printf '%s\\n%s\\n' "$pid" "$TOKEN" >"$PIDFILE.tmp" && mv -f "$PIDFILE.tmp" "$PIDFILE"
echo "{HOOK_NAME}: started gateway pid $pid"
"""


#: Placeholder values for :func:`commands_text`: the shipped text names no real endpoint.
_DOC_MACHINE = MachineConfig(
    cell=MachineCell("https://<runtime endpoint>", "https://<control plane endpoint>", "<region>"),
    token_path="<token file>",
    token_env="<token variable>",
    env_drop=("<image variable>",),
    registry_mode=True,
)


def commands_text() -> str:
    """Every command the lane runs on a managed environment, by section: A (the install and the
    hook), C (verification reads only), D (machine mode, with placeholders for the configured
    values) and E (the read on a box with no record)."""
    out = ["# Install commands run over the ssh alias (root, no key)", ""]
    out += [
        "The dashboard's launch issues section A and D itself: `probe`, `install-public`, `probe`, "
        "`machine-config`, `hook-dirs`, `hook-write` in its provision step, after the managed "
        "environment is RUNNING; `hook-run` and `gateway-probe` in its connect step.",
        "",
    ]
    out += ["## A. Install and boot hook", ""]
    for cid, (why, cmd) in COMMANDS_A.items():
        out += [f"- `{cid}`: {why}", "", "```", cmd, "```", ""]
    out += [
        "Install-env script (stdin of `install-public`); it writes the launcher only after the "
        f"one-liner exits 0 or {KAS_UNAVAILABLE_EXIT}:",
        "",
        "```",
        install_env_script().rstrip("\n"),
        "```",
        "",
    ]
    out += [
        f"The launcher it writes, `{WRAPPER}` (root, 0755):",
        "",
        "```",
        wrapper_script().rstrip("\n"),
        "```",
        "",
    ]
    out += [
        "Boot hook (stdin of `hook-write`), with no configured credential names:",
        "",
        "```",
        hook_script().rstrip("\n"),
        "```",
        "",
    ]
    out += ["## C. Verification only, not part of the install", ""]
    for cid, (why, cmd) in COMMANDS_C.items():
        out += [f"- `{cid}`: {why}", "", "```", cmd, "```", ""]
    out += [
        "## D. Machine mode",
        "",
        f"- `{CMD_MACHINE_CONFIG}`: Crew's own `config set`, one call per key, through the launcher "
        "on /projects; `agent.kas_auth machine` last. The values come from "
        "`instances.managed_env_*`; shown here with placeholders.",
        "",
        "```",
        machine_config_command(_DOC_MACHINE),
        "```",
        "",
    ]
    out += [
        "## E. On a managed environment Crew has no record of (Set up Crew, or after Resume)",
        "",
        "One read-only command, once per click (or once after the Resume reads RUNNING), through a "
        "one-shot ssh ticket. It prints one line, "
        f"`{CREW_UP}` or `{CREW_DOWN}`, and exits 0; any other result is a failed read. "
        "Nothing else runs on such an environment: no hook, no install, no write.",
        "",
    ]
    for cid, (why, cmd) in COMMANDS_E.items():
        out += [f"- `{cid}`: {why}", "", "```", cmd, "```", ""]
    return "\n".join(out)


# ---- runner ------------------------------------------------------------------------------


class InstallError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class RunResult:
    rc: int
    out: str = ""


class Runner(Protocol):
    def run(self, host: str, command: str, stdin: bytes | None = None) -> RunResult: ...


class SshRunner:
    """Plain ``ssh <alias> <command>``; the alias carries User root and the ProxyCommand."""

    def __init__(self, timeout: float = 900.0):
        self.timeout = timeout

    def run(self, host: str, command: str, stdin: bytes | None = None) -> RunResult:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host, command],
            input=stdin if stdin is not None else b"",
            capture_output=True,
            timeout=self.timeout,
        )
        return RunResult(p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace"))


# ---- results -----------------------------------------------------------------------------


@dataclass
class InstallResult:
    path: str  # "A"
    status: str  # installed | already | blocked | failed
    why: str = ""  # a WHY_* word when it could not go on
    detail: str = ""

    def line(self) -> str:
        parts = [f"path={self.path}", f"status={self.status}"]
        if self.why:
            parts.append(f"why={self.why}")
        if self.detail:
            parts.append(self.detail)
        return " ".join(parts)


@dataclass
class HookResult:
    path: str
    state: str  # started | alive | needs_signin | not_kas_bundle | failed
    gateway: str = ""  # up | down | ""
    detail: str = ""

    def line(self) -> str:
        parts = [f"path={self.path}", f"hook={self.state}"]
        if self.gateway:
            parts.append(f"gateway={self.gateway}")
        if self.detail:
            parts.append(self.detail)
        return " ".join(parts)


@dataclass
class Outcome:
    install: InstallResult
    hook: HookResult | None = None
    lines: list[str] = field(default_factory=list)


# ---- steps -------------------------------------------------------------------------------


def _cmd(cid: str) -> str:
    return COMMANDS_A[cid][1]


#: The install-env script's lines before the one-liner (read from its leading block) and after it.
STEP_FACTS = ("python_source", "python", "pipx", "image_pipx", "place", "openssl")
_STEP_PRE = frozenset(STEP_FACTS + ("ladder", "stop"))
_STEP_POST = frozenset({"install_rc", "wrapper", "fetch_rc"})
#: A stop the step names that is a box condition, not a failed command: reported as blocked.
_STEP_BLOCKED = {"pipx_on_base_path": WHY_PIPX_ON_BASE_PATH, "ladder_mismatch": WHY_INSTALL_FAILED}


def parse_step(out: str) -> dict[str, str]:
    """The install-env script's ``key=value`` lines. The ones it prints before the one-liner are
    read only from the leading block (the one-liner's output follows them); ``install_rc`` and
    ``wrapper`` are the last ones printed."""
    d: dict[str, str] = {}
    lines = out.splitlines()
    i = 0
    while i < len(lines):
        k, sep, v = lines[i].partition("=")
        if not sep or k not in _STEP_PRE:
            break
        d.setdefault(k, v.strip())
        i += 1
    for ln in lines[i:]:
        k, sep, v = ln.partition("=")
        if sep and k in _STEP_POST:
            d[k] = v.strip()
    return d


def _tail(out: str, n: int = 5, limit: int = 400) -> str:
    keep = [ln.strip() for ln in out.splitlines() if ln.strip()][-n:]
    return "tail=" + " | ".join(keep)[-limit:]


def _install_public(
    runner: Runner, host: str, openssl: str = ""
) -> InstallResult | tuple[str, str]:
    """Run ``install-public``; a result to report as is, or (facts, why) when Crew is installed."""
    r = runner.run(host, _cmd(CMD_INSTALL_PUBLIC), install_env_script(openssl=openssl).encode())
    d = parse_step(r.out)
    facts = " ".join(f"{k}={d[k]}" for k in STEP_FACTS if k in d)
    if d.get("python_source") == "managed":
        facts += f" managed_needs={MANAGED_NEEDS}"
    stop = d.get("stop")
    if stop in _STEP_BLOCKED:
        extra = f" ladder={d['ladder']}" if "ladder" in d else ""
        return InstallResult(
            PATH_A, "blocked", _STEP_BLOCKED[stop], f"reason={stop}{extra} {facts}".rstrip()
        )
    if stop == "fetch_failed":
        return InstallResult(
            PATH_A,
            "failed",
            WHY_FETCH_FAILED,
            f"reason=fetch_failed fetch_rc={d.get('fetch_rc', '?')} {facts} " f"{_tail(r.out)}",
        )
    rc_s = d.get("install_rc", "")
    if stop or not rc_s.isdigit():
        why = f"reason={stop}" if stop else f"install-public exit {r.rc}"
        return InstallResult(PATH_A, "failed", WHY_INSTALL_FAILED, f"{why} {facts} {_tail(r.out)}")
    irc = int(rc_s)
    if irc not in (0, KAS_UNAVAILABLE_EXIT):
        return InstallResult(
            PATH_A,
            "failed",
            WHY_INSTALL_FAILED,
            f"install command exit {irc} {facts} {_tail(r.out)}",
        )
    if d.get("wrapper") != "ok":
        return InstallResult(
            PATH_A,
            "failed",
            WHY_INSTALL_FAILED,
            f"install exited {irc} but {WRAPPER} does not run {facts} {_tail(r.out)}",
        )
    if irc == KAS_UNAVAILABLE_EXIT:
        # Crew stays installed; only the KAS engine is missing on this host.
        return f"{facts} kas=unavailable", WHY_KAS_UNAVAILABLE
    return facts, ""


#: The install check's last line: every ``have:`` line is printed before it, on the same stdout.
CHECK_DONE = "probe=done"


def _no_own_lines(line: str) -> bool:
    return False  # ``hook-dirs`` and ``hook-write`` print nothing of their own


def _have_line(line: str) -> bool:
    return line.startswith("have:")


def _hook_line(line: str) -> bool:
    return line.startswith(f"{HOOK_NAME}: ")


def _lone_255(r: RunResult, own: Callable[[str], bool]) -> bool:
    """rc 255 and every non-blank line is one the command prints itself (``own``): ssh lost the
    status after the command ran. Any other line is a connect that failed with a reason."""
    return r.rc == SSH_EXIT and all(own(ln.strip()) for ln in r.out.splitlines() if ln.strip())


def _run_setup(
    runner: Runner,
    host: str,
    cid: str,
    stdin: bytes | None = None,
    *,
    own: Callable[[str], bool] = _no_own_lines,
    sleep: Callable[[float], None] = time.sleep,
    command: str | None = None,
) -> tuple[RunResult, int]:
    """Run an idempotent setup command, and again while its answer is a lone 255: at most
    :data:`LONE_255_TRIES` tries in all, :data:`LONE_255_DELAY` s apart, the same ``stdin`` bytes
    each time. ``command`` is the text for an id whose text is built per call
    (``machine-config``); otherwise the id's own. Returns the last result and the tries."""
    text = command if command is not None else _cmd(cid)
    tries = 0
    while True:
        tries += 1
        r = runner.run(host, text, stdin)
        if not _lone_255(r, own) or tries >= LONE_255_TRIES:
            return r, tries
        sleep(LONE_255_DELAY)


def probe(runner: Runner, host: str, *, sleep: Callable[[float], None] = time.sleep) -> set[str]:
    """The install check: what the box has (its ``have:`` lines). It is judged by its done line
    (:data:`CHECK_DONE`), whatever the rc; a lone 255 before that line is tried again."""
    r, tries = _run_setup(runner, host, CMD_PROBE, own=_have_line, sleep=sleep)
    if not any(ln.strip() == CHECK_DONE for ln in r.out.splitlines()):
        if _lone_255(r, _have_line):
            raise InstallError(
                "probe_failed",
                f"the box did not answer the install check (ssh exit {SSH_EXIT}, {tries} tries)",
            )
        raise InstallError("probe_failed", "the box did not answer the probe")
    return {ln[5:].strip() for ln in r.out.splitlines() if ln.startswith("have:")}


def install_path_a(
    runner: Runner,
    host: str,
    *,
    openssl: str = "",
    sleep: Callable[[float], None] = time.sleep,
) -> InstallResult:
    """Run Crew's public install once; a second call finds the launcher and does nothing.

    "Installed" means Crew's launcher on /projects runs (``have:crew-wrapper``) and its
    engine is on the bundle route (``have:kas-bundle``): Crew's own engine is the box's one
    sign-in, so a box whose launcher answers any other route is ``blocked``."""
    have = probe(runner, host, sleep=sleep)
    if "crew-wrapper" in have and HAVE_KAS_BUNDLE in have:
        return InstallResult(PATH_A, "already")
    facts, why = "", ""
    if "crew-wrapper" not in have:
        step = _install_public(runner, host, openssl)
        if isinstance(step, InstallResult):
            return step
        facts, why = step
    have = probe(runner, host, sleep=sleep)
    if "crew-wrapper" not in have:
        return InstallResult(
            PATH_A,
            "failed",
            WHY_INSTALL_FAILED,
            f"install exited 0 but {WRAPPER} does not run {facts}".rstrip(),
        )
    if HAVE_KAS_BUNDLE not in have:
        kas = f" kas_why={why}" if why else ""
        return InstallResult(
            PATH_A,
            "blocked",
            WHY_NOT_KAS_BUNDLE,
            f"kirocrew installed; its engine is not on the bundle route {facts}{kas}".rstrip(),
        )
    return InstallResult(PATH_A, "installed", why, f"route={ROUTE_KAS_BUNDLE} {facts}".rstrip())


def _machine_config_line(line: str) -> bool:
    """A line ``machine-config`` prints before its done line: core's ``config set`` success line
    for one of its keys."""
    return line.startswith("✅ agent.")


def machine_config(
    runner: Runner,
    host: str,
    mc: MachineConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Run ``machine-config`` once (:func:`machine_config_command`). It is judged by its done
    line, whatever the rc. Every ``config set`` writes the same value again, so a lone 255 before
    the done line is tried again; any other answer raises ``machine_config_failed`` with the box's
    last lines (Crew's own refusal names the key, e.g. a release without machine mode)."""
    command = machine_config_command(mc)
    r, tries = _run_setup(
        runner, host, CMD_MACHINE_CONFIG, own=_machine_config_line, sleep=sleep, command=command
    )
    if not any(ln.strip() == MACHINE_CONFIG_DONE for ln in r.out.splitlines()):
        raise InstallError(
            "machine_config_failed",
            f"{CMD_MACHINE_CONFIG} exit {r.rc} ({tries} tries) {_tail(r.out)}",
        )


def write_hook(
    runner: Runner,
    host: str,
    *,
    unset: Iterable[str] = (),
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """``hook-dirs``, then ``hook-write`` with the hook on stdin. Both print nothing, so a lone 255
    on either is tried again; ``hook-write`` gets the same bytes and moves them into place."""
    body = hook_script(unset=unset).encode()
    r, _ = _run_setup(runner, host, CMD_HOOK_DIRS, sleep=sleep)
    if r.rc != 0:
        raise InstallError("hook_dirs_failed", f"hook dirs exit {r.rc}")
    r, _ = _run_setup(runner, host, CMD_HOOK_WRITE, body, sleep=sleep)
    if r.rc != 0:
        raise InstallError("hook_write_failed", f"hook write exit {r.rc}")


def run_hook(
    runner: Runner,
    host: str,
    path: str = PATH_A,
    *,
    attempts: int = 6,
    delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> HookResult:
    """Run the hook, then probe the gateway; it may be down right after RUNNING, so a miss retries.
    The hook is idempotent, so a lone 255 (only the hook's own lines) runs it again."""
    r, _ = _run_setup(runner, host, CMD_HOOK_RUN, own=_hook_line, sleep=sleep)
    if r.rc == NEEDS_SIGNIN_EXIT:
        return HookResult(path, "needs_signin")
    if r.rc == NOT_KAS_BUNDLE_EXIT:
        return HookResult(path, "not_kas_bundle", detail=f"why={WHY_NOT_KAS_BUNDLE}")
    if r.rc == LAUNCHER_FAILED_EXIT:
        return HookResult(path, "failed", detail="launcher_failed: Crew's launcher does not run")
    if r.rc != 0:
        return HookResult(path, "failed", detail=f"hook exit {r.rc}")
    state = "alive" if "gateway alive" in r.out else "started"
    for i in range(attempts):
        g = runner.run(host, _cmd(CMD_GATEWAY_PROBE))
        if "gateway=up" in g.out:
            return HookResult(path, state, "up")
        if i + 1 < attempts:
            sleep(delay)
    return HookResult(path, state, "down", f"no answer after {attempts} probes")
