"""Registering an app's MCP servers, including resolving each stdio server's command.

``resolve_stdio_command`` and the interpreter and launch-target rules it applies, the
``kirocrew`` host-CLI pin, the live backend port an HTTP entry is rewritten to, the
warnings for a command that cannot spawn or a secret reference that cannot resolve,
``_register_mcp_servers`` / ``_deregister_mcp_servers``, and the re-registration and
scrub passes a backend start or stop drives, including the legacy shared ``mcp.json``.

Part of the ``kiro_crew.apps.bridges`` family: callers and tests reach every name here
as ``kiro_crew.apps.bridges.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from kiro_crew import platform_compat
from kiro_crew.apps import deps_boot as _deps_boot_module
from kiro_crew.apps.bridges_agents import _is_within, _register_agents
from kiro_crew.apps.bridges_mcp_config import (
    _health_reconcile_guard,
    _mcp_lock,
    _read_mcp_json_unlocked,
    _write_mcp_json_unlocked,
)
from kiro_crew.apps.bridges_sources import _registration_denied, _registration_source
from kiro_crew.apps.execution import shipped_builtin_app_root
from kiro_crew.apps.interpreter import (
    app_deps_dir,
    path_command_is_abi_matched,
    resolve_app_python,
    venv_provided_command,
)
from kiro_crew.apps.manager import app_dir
from kiro_crew.apps.manifest import AppManifest, has_stdio_mcp_server, is_module_style_entry_point
from kiro_crew.atomic_write import atomic_write
from kiro_crew.env import emit_env
from kiro_crew.executors import maintenance_executor
from kiro_crew.platform.governance import strip_ungoverned_auto_approve
from kiro_crew.security import is_sensitive_path
from kiro_crew.user_json import loads_mcp_config
from kiro_crew.zip_vet import ZipInventoryRejected, vet_zip_inventory

logger = logging.getLogger("kiro_crew.apps.bridges")


#: Absolute path of the stdlib-only launch shim, for interpreters whose
#: flags (-S/-E/-I) make the ``-m kiro_crew.apps.deps_boot`` spelling
#: unimportable.
_DEPS_BOOT_PATH = Path(os.path.abspath(_deps_boot_module.__file__))


#: The host CLI name a builtin app may declare as its MCP ``command``.
_HOST_CLI_COMMAND = "kirocrew"


def _pin_host_cli_command(app_name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Rewrite ``command: "kirocrew"`` to THIS gateway's interpreter.

    A builtin app's MCP server is the gateway's own code — but declaring the host
    CLI by name makes kiro-cli spawn it via a PATH lookup, which is wrong in two
    ways that both fail silently:

    * the launcher on PATH may belong to a DIFFERENT Kiro Crew install, so the
      server reads a different data home than the gateway that registered it and
      reports the app as not installed;
    * that launcher may simply be broken (a stale wrapper whose interpreter is
      gone), in which case the server never starts and its tools never register —
      the agent just has no such tools, with no error surfaced anywhere.

    Pinning ``sys.executable -m kiro_crew`` plus PYTHONPATH and KIROCREW_HOME makes
    the spawned server provably the same code and the same data home as the
    gateway. Only applied to the host CLI name; an app shipping its own binary is
    left alone.
    """
    if cfg.get("command") != _HOST_CLI_COMMAND:
        return cfg

    pkg_parent = str(Path(kiro_crew_file()).parent.parent)
    env = dict(cfg.get("env") or {})
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{pkg_parent}{os.pathsep}{existing}" if existing else pkg_parent
    env.setdefault("KIROCREW_HOME", str(app_dir(app_name).parent.parent))
    # ``-P``: the MCP host spawns this server with a cwd Kiro Crew does not
    # choose; keeping it off sys.path is the same guarantee the launchers give.
    argv = platform_compat.isolated_python_argv(
        "-P",
        "-m",
        "kiro_crew",
        *list(cfg.get("args") or []),
        force_isolation=True,
    )
    cfg["command"] = argv[0]
    cfg["args"] = argv[1:]
    cfg["env"] = env
    return cfg


def kiro_crew_file() -> str:
    """Path to the running ``kiro_crew`` package's ``__init__``."""
    import kiro_crew

    return str(kiro_crew.__file__)


# The pre-fix location. Still scrubbed on deregister so an upgrade removes
# entries an older build leaked into the shared file.
_LEGACY_SHARED_MCP_PATH = Path.home() / ".kiro" / "settings" / "mcp.json"


def _resolve_live_mcp_url(app_name: str, url: str, live_port: int | None = None) -> str:
    """Rewrite a manifest HTTP MCP url's port to the backend's ACTUALLY-allocated port.

    Gateway-managed backends declare ``backend.port:"auto"`` and get a free port at
    spawn time (``backend_runtime/ports.py:_find_free_port`` — 9100 if free, else 9101, …). The
    manifest's ``mcpServers.<name>.url`` carries an illustrative fixed port (e.g.
    ``http://localhost:9100/mcp``). Registering that verbatim is a latent bug: whenever
    the backend lands on a different port, the registered MCP server points at the wrong
    one and every agent tool call to the app silently fails. Here we substitute the live
    port (preserving scheme/host/path) so the registration always matches the running
    backend. Non-HTTP transports and apps with no resolvable port are passed through
    unchanged.

    ``live_port`` may be passed explicitly by a caller that knows the just-allocated
    port (the boot/enable path, where the backend isn't marked *healthy* yet so the
    tracked-port lookup would still return None). When omitted we fall back to the
    health-gated ``get_app_backend_port``.
    """
    if not url or not url.startswith("http"):
        return url
    try:
        if live_port is None:
            # circular import: backend_runtime/registration.py imports from bridges
            # (reregister_app_mcp_servers in its health gate), so bridges can't import
            # backend at module load — defer it.
            from kiro_crew.apps.backend import get_app_backend_port

            live_port = get_app_backend_port(app_name)
        if not live_port:
            return url  # backend not running yet — keep the manifest default
        p = urlparse(url)
        if p.port == live_port:
            return url  # already correct
        host = p.hostname or "127.0.0.1"
        netloc = f"{host}:{live_port}"
        return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
    except Exception:  # noqa: BLE001 — registration must never crash on URL rewrite
        return url


def _live_port_for(app_name: str, live_port: int | None) -> int | None:
    """The backend's actually-allocated port, or None if it isn't running yet.

    ``live_port`` (passed by the boot/enable path that just spawned the backend) wins;
    otherwise fall back to the health-gated tracked-port lookup. Never raises — a failure
    to resolve is treated as "not live" so registration can fail safe."""
    if live_port:
        return live_port
    try:
        # circular import: backend.py imports from bridges in its boot path — defer.
        from kiro_crew.apps.backend import get_app_backend_port

        return get_app_backend_port(app_name)
    except Exception:  # noqa: BLE001 — registration must never crash on a port lookup
        return None


#: Bare python launchers an app manifest may name. Each is substituted with a resolved
#: absolute interpreter - the RUNNING interpreter when the gateway provisioned the app's
#: deps dir (its wheels are ABI-bound to that interpreter), else the app's own venv
#: python when a version-matched one exists, else the RUNNING interpreter, which is the
#: only one guaranteed to import ``kiro_crew``.
_BARE_PYTHON = frozenset({"python", "python3", "py"})


def _resolve_app_launcher_arg(args: list, app_root: Path) -> list:
    """Anchor an app's OWN relative ``.py`` launcher to its install root.

    Returns a new list with the launch target replaced by its real absolute
    path, or ``args`` itself (the same object) when nothing is rewritten.

    WHY: an app whose package is staged (``apps/<id>/src/...``) and not
    pip-installed cannot start by ``-m its.package``: the package is not
    importable, and without a ``requirements.txt`` nothing is put on
    ``PYTHONPATH``. Such an app ships a launcher that registers its package
    from its own ``__file__``. A manifest can name that launcher only relative
    to the app root, and kiro-cli spawns these servers under the session cwd,
    so a relative operand never reaches the staged file. This is the one place
    that knows the install root.

    Only the launch TARGET (``_py_target_index`` over already-normalized args)
    is read, and only when it is a relative path ending in ``.py`` that
    resolves (links followed) to an existing file inside the real app root.
    Every other shape is left exactly as authored: ``-m``/``-c``, interpreter
    options, arguments after the script, an absolute path, and a relative path
    that is not a file under the app root.
    """
    ti = _py_target_index(args)
    if ti is None:
        return args
    target = args[ti]
    if not isinstance(target, str) or not target.endswith(".py") or os.path.isabs(target):
        return args
    try:
        real_root = os.path.realpath(app_root)
        real_target = os.path.realpath(os.path.join(real_root, target))
        if not _is_within(Path(real_target), Path(real_root)):
            return args
        if not os.path.isfile(real_target):
            return args
    except (OSError, ValueError):
        return args
    return [*args[:ti], real_target, *args[ti + 1 :]]


def resolve_stdio_command(cfg: dict, app_root: Path | None = None) -> dict:
    """Resolve a bare stdio MCP ``command`` to an absolute path, venv first.

    A manifest that hard-codes ``python3`` does not launch on a native Windows install (a venv
    there ships ``python.exe`` and no ``python3.exe``), so the spawn fails with ENOENT and the
    app's MCP tools go silently missing. Plain ``python`` is not a fix either: on Windows
    ``shutil.which("python")`` can resolve a 0-byte Microsoft-Store reparse point — the case
    ``platform_compat._is_windows_store_python_stub`` exists for — and kiro-cli strips env when
    spawning MCP subprocesses, so a PATH lookup can land on an interpreter that cannot import
    ``kiro_crew`` at all. ``mcp_gateway/rewriter_spec.py`` bakes ``sys.executable`` into its stub
    entry for exactly that reason; this is the same decision for app manifests.

    With ``app_root``, the resolution matches what the app's BACKEND launcher already does
    (see :mod:`kiro_crew.apps.interpreter`): the gateway's ``sys.executable`` whenever the
    gateway has provisioned the app's deps dir (``pip install --target`` - those wheels are
    ABI-bound to that interpreter), else the app's own venv interpreter when a
    version-matched one exists, else ``sys.executable`` - and expose the provisioned deps
    dir through ``PYTHONPATH`` exactly as the backend spawn env does, EXCEPT to a server
    launching a ``kiro_crew`` module, which must never see an app-supplied ``kiro_crew``
    copy. The two spawn paths share one policy on purpose; a second divergent copy is the
    defect this shape removes.

    The rewrite rule, precisely: a BARE name (no path separator) is touched when it is a
    known python launcher OR the app's venv or deps dir provides that exact binary (a pip
    console script - invisible to PATH because neither layout is ever activated). ONE
    path-carrying shape is also rewritten: an absolute script whose shebang names an
    ABI-matched interpreter, which is relaunched through deps_boot under that interpreter
    so its provisioned deps resolve. Every other path-carrying command, a bare PATH
    dependency the app does not provide (``node``, ``npx``, ``docker``), and an HTTP entry
    (no ``command``) was chosen deliberately and is left untouched. Getting this predicate
    wrong breaks working apps, so the boundary is pinned by tests on both sides.
    """
    command = cfg.get("command")
    if not isinstance(command, str):
        return cfg
    name = command.strip()

    _launcher_injected_pythonpath = False

    def _isolate_selected_python() -> None:
        args = list(cfg.get("args") or [])
        force_isolation = _launcher_injected_pythonpath or str(_DEPS_BOOT_PATH) in args
        argv = platform_compat.isolated_python_argv(
            *args,
            executable=str(cfg["command"]),
            force_isolation=force_isolation,
        )
        cfg["command"] = argv[0]
        cfg["args"] = argv[1:]

    # Bound on every path: the shim arms below consult it even when the
    # deps-exposure block does not run (e.g. a gateway-module server).
    _deps_stamp_ok = False
    if app_root is not None and not _targets_gateway_module(cfg):
        # Expose the provisioned deps dir the same way the backend spawn env
        # does: a --target install carries no interpreter, so PYTHONPATH is the
        # only bridge - a deps-provided console script's shebang is
        # sys.executable and imports its own package from here, and a
        # python-launcher server imports the app's requirements from here.
        # Prepended so the app's pinned requirements win over a manifest's own
        # PYTHONPATH. Inert for non-Python commands, and skipped entirely when
        # the app has no provisioned deps dir. Runs BEFORE the path-command
        # early return below: a path-based command (./bin/server,
        # .venv/bin/python) keeps its spelling - with ONE exception, an
        # absolute script whose shebang names an ABI-matched interpreter,
        # which is relaunched through deps_boot under that interpreter - and
        # it still needs the app's provisioned deps on import. NEVER injected into a server that
        # launches a kiro_crew module: an app that pip-pins its own kiro_crew
        # copy would otherwise shadow the gateway's code with a foreign
        # version on the gateway's own interpreter - the same reason
        # _targets_gateway_module pins sys.executable below. Registration can
        # precede the first backend spawn (which provisions the dir), so the
        # path is emitted whenever provisioning is EXPECTED (the app ships a
        # requirements.txt) even before the dir exists: a missing PYTHONPATH
        # entry is inert to Python. And ONLY while the requirements.txt is
        # still declared: an update that removes it must not leave a stale
        # preserved tree injecting removed dependency code.
        # ABI gate for PATH-carrying commands: the deps tree is built by the
        # GATEWAY's pip, so its wheels are ABI-bound to the gateway's
        # interpreter. Bare names are safe (their resolution below is the
        # gateway interpreter, a version-probed venv python, or a
        # deps/venv-provided script whose shebang is one of those two), but
        # a path-pinned command is arbitrary - a manifest naming a foreign
        # python (3.11 against 3.12-built wheels) would import mismatched
        # binary wheels and die. A path command gets the deps PYTHONPATH
        # only on a POSITIVE match: it resolves to the gateway interpreter
        # itself, or to the app venv's python when that venv passed the
        # version probe. Everything else keeps its env untouched - exactly
        # the pre-deps status quo for that server.
        deps_dir = app_deps_dir(app_root)
        _carries_path = bool(
            os.sep in name
            or (os.altsep is not None and os.altsep in name)
            or os.path.splitdrive(name)[0]
        )
        _abi_matched_path = _carries_path and path_command_is_abi_matched(app_root, name)
        # Deferred import (bridges cannot import backend at module load).
        from kiro_crew.apps.backend import _deps_tree_stamp_current

        # Stamp gate, same as the spawn path: a failed reprovision after a
        # Python upgrade leaves an old-ABI tree on disk, and injecting it
        # here would kill the MCP server at import instead of letting it
        # start without deps and surface the provisioning error.
        _deps_stamp_ok = (app_root / "requirements.txt").is_file() and _deps_tree_stamp_current(
            app_root, app_root / "requirements.txt"
        )
        # A PATH command that is not itself an interpreter can still be a
        # PYTHON SCRIPT: an app shipping an absolute-path launcher whose
        # shebang names an ABI-matched interpreter (the gateway executable
        # or the probed venv python) imports its provisioned deps exactly
        # like a bare-python launch - refusing it strands the server with
        # ModuleNotFoundError. Verified through the shebang, not the name.
        _abi_shebang_interp = ""
        if _carries_path and not _abi_matched_path and _deps_stamp_ok and os.path.isabs(name):
            _cand_interp = _python_shebang_interpreter(name)
            if _cand_interp and path_command_is_abi_matched(app_root, _cand_interp):
                _abi_shebang_interp = _cand_interp
        if _deps_stamp_ok and (not _carries_path or _abi_matched_path or _abi_shebang_interp):
            env = dict(cfg.get("env") or {})
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{deps_dir}{os.pathsep}{existing}" if existing else str(deps_dir)
            cfg["env"] = env
            # Positive provenance: manifest-authored PYTHONPATH never sets this bit.
            _launcher_injected_pythonpath = True
        if _abi_shebang_interp and _deps_stamp_ok:
            # Launch through deps_boot (same shape as the deps-dir console
            # script arm): the verified shebang interpreter runs the shim,
            # the script becomes the shim's target, so .pth processing and
            # editable installs work. Shim XOR PYTHONPATH, as everywhere.
            cfg["command"] = _abi_shebang_interp
            cfg["args"] = [
                str(_DEPS_BOOT_PATH),
                str(app_deps_dir(app_root)),
                name,
                *(cfg.get("args") or []),
            ]
            _strip_deps_pythonpath(cfg, app_deps_dir(app_root))
            logger.debug(
                "stdio MCP path command %r: ABI-matched shebang %s - shimmed via deps_boot",
                name,
                _abi_shebang_interp,
            )
            _isolate_selected_python()
            return cfg
        if _abi_matched_path and _deps_stamp_ok:
            # An ABI-MATCHED path python (the gateway executable by path, or
            # the probed app venv python) is still a python launch: raw
            # PYTHONPATH would skip .pth hooks and an editable dependency
            # dies at import. Same shim walk as the bare-python branch - the
            # command itself is deliberately never rewritten.
            _args = _normalize_attached_m(list(cfg.get("args") or []))
            _ti = _py_target_index(_args)
            if _ti is not None:
                # ALWAYS the absolute-path spelling here: an ABI-matched
                # path python can be the app's own venv interpreter, which
                # has no system-site-packages and cannot import kiro_crew -
                # `-m kiro_crew.apps.deps_boot` would die at launch. The
                # stdlib-only shim by path runs under ANY interpreter (the
                # same reasoning the isolation-flag arm relies on).
                cfg["args"] = [
                    *_args[:_ti],
                    str(_DEPS_BOOT_PATH),
                    str(deps_dir),
                    *_args[_ti:],
                ]
                _strip_deps_pythonpath(cfg, deps_dir)
    if (
        not name
        or os.sep in name
        or (os.altsep is not None and os.altsep in name)
        or os.path.splitdrive(name)[0]
    ):
        # Carries a path (or, on Windows, a drive qualifier like ``D:foo``,
        # which pathlib would treat as a new anchor and silently discard the
        # venv prefix in the join below) — deliberate, never rewritten. An
        # interpreter path keeps that exact executable but still receives the
        # shared user-site isolation flag.
        if name == sys.executable or (
            app_root is not None and path_command_is_abi_matched(app_root, name)
        ):
            _isolate_selected_python()
        return cfg
    # ``python.exe`` / ``python3.exe`` are ordinary Windows spellings of the
    # same launchers; normalise the suffix so they get the interpreter policy
    # (venv-first, sys.executable fallback) instead of the console-script probe.
    base = name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if base in _BARE_PYTHON:
        if _targets_gateway_module(cfg):
            # The server runs Kiro Crew's OWN code (``-m kiro_crew...``). App
            # venvs are created WITHOUT --system-site-packages, so kiro_crew is
            # not importable there and the venv interpreter would die on
            # import; and even a venv that pip-installed its own kiro_crew is a
            # version-skewed foreign copy the gateway must not execute. Gateway
            # code runs provably under the gateway's interpreter — the same
            # decision _pin_host_cli_command makes for the host CLI.
            cfg["command"] = sys.executable
        else:
            cfg["command"] = resolve_app_python(app_root)
            # Provisioned-deps launch shim (same reasoning as the backend
            # spawn): PYTHONPATH never processes .pth files, so a python
            # launcher with provisioned (or expected) deps routes through
            # deps_boot, which site.addsitedir()s the deps dir first. Only
            # when the resolved interpreter is the GATEWAY's own (deps pin
            # sys.executable; the shim is gateway code and must not be
            # imported by a foreign interpreter). Interpreter options are
            # WALKED, not guessed: the shim triple is inserted at the target
            # token (script, -m, -c, or the operand after --), so `-u
            # server.py` keeps -u consumed by the interpreter and server.py
            # shimmed; attached -mMODULE/-cCODE spellings are normalized
            # first. A shape with no resolvable target falls back to the
            # PYTHONPATH transport.
            _args = _normalize_attached_m(list(cfg.get("args") or []))
            if app_root is not None:
                # Anchor the app's own relative .py launcher to its install
                # root BEFORE the shim below, so deps_boot (or the plain
                # launch) receives the absolute target whatever the deps
                # state. Written back only when it rewrote something.
                _anchored = _resolve_app_launcher_arg(_args, app_root)
                if _anchored is not _args:
                    _args = _anchored
                    cfg["args"] = list(_anchored)
            if (
                app_root is not None
                and cfg["command"] == sys.executable
                # Stamp-gated like every deps exposure: shimming routes the
                # server through the deps tree, and a stale-ABI tree must
                # not be selected any more than it may be PYTHONPATH-injected.
                and _deps_stamp_ok
            ):
                _ti = _py_target_index(_args)
                if _ti is not None:
                    # ALWAYS the absolute-path spelling: MCP servers start
                    # under the SESSION's cwd (an untrusted project), and a
                    # project shipping kiro_crew/apps/deps_boot.py would
                    # shadow the -m spelling through sys.path[0] - project
                    # code running as the "shim". The stdlib-only path
                    # spelling has no import to shadow, and it is also what
                    # -S/-E/-I (which kill -m outright) require.
                    cfg["args"] = [
                        *_args[:_ti],
                        str(_DEPS_BOOT_PATH),
                        str(app_deps_dir(app_root)),
                        *_args[_ti:],
                    ]
                    _strip_deps_pythonpath(cfg, app_deps_dir(app_root))
            # The one remaining silent-death path: a script-entry server that
            # imports gateway-env packages flips to the venv interpreter the
            # moment a venv materialises and dies on import with no warning
            # (the rewritten path exists). Make the chosen interpreter
            # greppable so that diagnosis starts from a log line.
            logger.debug("stdio MCP command %r resolved to interpreter %s", name, cfg["command"])
    elif app_root is not None:
        venv_cmd = venv_provided_command(app_root, name)
        if venv_cmd is not None:
            cfg["command"] = venv_cmd
            deps_dir = app_deps_dir(app_root)
            _shim_script = venv_cmd
            if venv_cmd.lower().endswith(".exe"):
                # pip's WINDOWS launcher: a native .exe with no shebang. The
                # classic launcher pair ships a companion `<name>-script.py`
                # beside it - that companion is the python entry and shims
                # like any other script. An embedded-script .exe (no
                # companion) stays a direct launch: it re-execs python
                # itself and cannot be runpy'd.
                _companion = Path(venv_cmd).with_name(Path(venv_cmd).stem + "-script.py")
                if _companion.is_file():
                    _shim_script = str(_companion)
                elif zipfile.is_zipfile(venv_cmd) and _zip_has_main(venv_cmd):
                    # EMBEDDED launcher (no companion): the console script
                    # rides inside the exe as an appended ZIP with a
                    # __main__.py stub; deps_boot's exe arm extracts and
                    # dispatches that stub after addsitedir. A ZIP-bearing
                    # exe WITHOUT the stub (a self-extractor, an installer,
                    # any PE with an appended archive) is not a launcher --
                    # wrapping it would make deps_boot's archive read raise
                    # and the server fail to start, so it falls through to
                    # the direct-launch arm below.
                    _shim_script = venv_cmd
                else:
                    # A native exe a wheel shipped as data - not a launcher
                    # at all; shimming it would hand a binary to the ZIP
                    # reader. Direct launch, exactly as before the shim.
                    _shim_script = ""
            elif not _has_python_shebang(venv_cmd):
                _shim_script = ""
            if deps_dir in Path(venv_cmd).parents and _deps_stamp_ok and _shim_script:
                # A deps-dir console script is pip-generated - a Python
                # script whose shebang is the gateway interpreter, a Windows
                # launcher pair, or an embedded-ZIP exe. Run direct, its
                # editable/.pth-dependent imports die (PYTHONPATH never
                # processes .pth), so route it through deps_boot: script and
                # companion forms as script targets, embedded exes through
                # the shim's exe arm. Only artifacts that ARE python-backed
                # (shebang sniff / launcher shapes) - a package can ship
                # arbitrary bin artifacts, and runpy on a shell script would
                # break a working launch. Shim XOR PYTHONPATH, as
                # everywhere.
                cfg["command"] = sys.executable
                # absolute-path spelling for the same session-cwd shadowing
                # reason as the bare-python branch
                cfg["args"] = [
                    str(_DEPS_BOOT_PATH),
                    str(deps_dir),
                    _shim_script,
                    *(cfg.get("args") or []),
                ]
                _strip_deps_pythonpath(cfg, deps_dir)
    if base in _BARE_PYTHON or cfg.get("command") == sys.executable:
        _isolate_selected_python()
    return cfg


def _strip_deps_pythonpath(cfg: dict, deps_dir: Path) -> None:
    """Drop the deps dir from a shimmed server's PYTHONPATH, in place.

    Shim XOR PYTHONPATH: ``-m kiro_crew.apps.deps_boot`` resolves kiro_crew
    through sys.path, so a deps-provided kiro_crew copy on PYTHONPATH would
    SHADOW the gateway's shim - app code running as the "shim" on the
    gateway's own interpreter. addsitedir supplies the deps only after the
    trusted shim has imported; a manifest's own PYTHONPATH entries pass
    through untouched.
    """
    env = dict(cfg.get("env") or {})
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and p != str(deps_dir)]
    if parts:
        env["PYTHONPATH"] = os.pathsep.join(parts)
    else:
        env.pop("PYTHONPATH", None)
    if env:
        cfg["env"] = env
    else:
        cfg.pop("env", None)


def _zip_has_main(path: str) -> bool:
    """True when *path* is a ZIP whose archive carries a ``__main__.py``.

    That member is what makes a ZIP-bearing executable a Python launcher
    deps_boot's exe arm can dispatch; without it the wrap is guaranteed to
    fail at start. Any read error reads as "not a launcher". Reads are
    gated on the sensitive-path check like every other content sniff here.
    """
    if is_sensitive_path(path):
        return False
    try:
        # Vet the declared inventory BEFORE constructing ZipFile: the
        # central directory is app-controlled and ZipFile loads it whole
        # into the gateway's memory - a crafted launcher-shaped exe must
        # exhaust a cap, not the gateway. Real console-script launchers
        # carry a handful of members.
        try:
            vet_zip_inventory(path, max_members=2048)
        except ZipInventoryRejected:
            return False
        with zipfile.ZipFile(path) as zf:
            return "__main__.py" in zf.namelist()
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return False


def _python_shebang_interpreter(script: str) -> str | None:
    """The ABSOLUTE interpreter path a script's shebang names, or None.

    Only the bare direct spelling (``#!/abs/path/to/python``, no arguments)
    is returned. An ``env`` form resolves through PATH at spawn time and
    cannot be ABI-verified ahead of it. A shebang that carries arguments
    (``#!<python> -I``) is never a rewrite candidate: the kernel passes
    everything after the interpreter as ONE token with its own splitting
    rules, and flags like ``-I``/``-E``/``-s`` set the interpreter's
    isolation posture - a rewrite that drops or re-splits them silently
    weakens the isolation the script asked for, where the kernel's own
    shebang launch keeps every flag exactly as written. Such scripts stay
    on the direct launch.

    The read is gated: ``script`` is a manifest-author-controlled absolute
    path, and the gateway must not open a protected location outside the
    sandbox on an app's say-so - the same sensitive-path gate every other
    file-access surface consults. Any refusal or read failure reads as
    None - the launch stays exactly what it was.
    """
    if is_sensitive_path(script):
        return None
    try:
        with open(script, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return None
    first = head.split(b"\n", 1)[0]
    if not first.startswith(b"#!"):
        return None
    try:
        parts = first[2:].decode("utf-8", "strict").strip().split()
    except UnicodeDecodeError:
        return None
    if len(parts) != 1:
        return None
    interp = parts[0]
    if os.path.basename(interp) == "env" or not os.path.isabs(interp):
        return None
    return interp


def _has_python_shebang(script: str) -> bool:
    """True when ``script`` opens with a ``#!`` line naming a python.

    pip-generated console scripts do (their shebang is the interpreter that
    ran pip); native binaries and foreign-language scripts do not. Any read
    failure reads as "not python" and the launch falls back to direct
    execution. The read carries the same sensitive-path gate as every
    shebang sniff: the callers pass app-dir-confined paths today, and the
    gate keeps that true against any future caller handing this a
    manifest-verbatim path.
    """
    if is_sensitive_path(script):
        return False
    try:
        with open(script, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return False
    lines = head.split(b"\n")
    first = lines[0]
    if not first.startswith(b"#!"):
        return False
    if b"python" in first.lower():
        return True
    # pip (distlib) emits a shell TRAMPOLINE when the interpreter path is
    # too long or contains spaces: a /bin/sh shebang whose SECOND line is
    # exactly the polyglot re-exec - a triple-quote-fenced `exec <python>
    # "$0" "$@"`. Match that structure, not token presence: a dependency's
    # ordinary shell wrapper that merely MENTIONS python somewhere must not
    # be handed to runpy as python source.
    if b"sh" not in first or len(lines) < 2:
        return False
    second = lines[1]
    q3 = b"\x27\x27\x27"  # three single quotes
    return (
        second.startswith(q3 + b"exec\x27 ")
        and b'"$0" "$@"' in second
        and b"python" in second.lower()
    )


#: CPython interpreter options that consume the NEXT argv element as their
#: value. Everything else the interpreter accepts is either a self-contained
#: flag (-s, -u, -O, ...) or attaches its value in the same token (-Wignore,
#: -Xdev). Kept as an explicit table so the scanner's skip logic is checkable
#: against `python --help` rather than inferred per finding.
_PY_OPTS_WITH_SEPARATE_VALUE = frozenset({"-X", "-W", "--check-hash-based-pycs"})


def _normalize_attached_m(args: list) -> list:
    """Split attached ``-mMODULE`` / ``-cCODE`` spellings into separate form.

    CPython treats ``-mserver`` and ``-m server`` identically (same for
    ``-c``); the shim walker only takes over the separate forms, so the
    attached spellings would silently fall back to the PYTHONPATH transport
    and skip ``.pth`` processing. Walks the same option-prefix branch table;
    returns the args unchanged when there is nothing to normalize.
    """
    out: list = []
    i = 0
    while i < len(args):
        arg = args[i]
        if not isinstance(arg, str):
            return args
        if arg in ("-m", "-c") or not arg.startswith("-"):
            return [*out, *args[i:]]
        if arg == "--":
            # `python -- server.py` is `python server.py` whenever the
            # operand does not itself start with a dash - drop the
            # separator so the walker shims the script. An operand that DOES
            # start with a dash needs the `--` and stays unshimmable.
            if (
                i + 1 < len(args)
                and isinstance(args[i + 1], str)
                and not args[i + 1].startswith("-")
            ):
                return [*out, *args[i + 1 :]]
            return [*out, *args[i:]]
        if arg.startswith(("-m", "-c")) and len(arg) > 2:
            return [*out, arg[:2], arg[2:], *args[i + 1 :]]
        out.append(arg)
        if arg in _PY_OPTS_WITH_SEPARATE_VALUE:
            if i + 1 < len(args):
                out.append(args[i + 1])
            i += 2
            continue
        i += 1
    return out


def _py_target_index(args: list) -> int | None:
    """Index of the first token CPython treats as the LAUNCH TARGET.

    Walks the interpreter-option prefix with the same branch table as
    :func:`_targets_gateway_module`, returning the index of the script
    operand or a separate ``-m`` - the insertion point for the deps_boot
    shim triple, so interpreter options stay consumed by the interpreter.
    Separate ``-m`` and ``-c`` ARE targets (deps_boot has arms for both),
    and attached spellings are split by the normalizer before this walk.
    ``None`` only for shapes with no resolvable target (a surviving ``--``
    guarding a dash-led operand, non-string tokens, or an option prefix
    with no target): those keep the PYTHONPATH transport.
    """
    i = 0
    while i < len(args):
        arg = args[i]
        if not isinstance(arg, str):
            return None
        if arg in ("-m", "-c"):
            return i if i + 1 < len(args) else None
        if arg == "--" or (arg.startswith(("-m", "-c")) and len(arg) > 2):
            # A surviving `--` means the normalizer could not remove it (the
            # operand starts with a dash): inserting the shim after `--`
            # would be read as a FILENAME, not an option - unshimmable.
            return None
        if arg == "-x" or (arg.startswith("-") and not arg.startswith("--") and "x" in arg[1:]):
            # -x tells CPython to SKIP THE FIRST LINE of the script it
            # launches. Inserting the shim makes deps_boot that script, and
            # its first line opens the module docstring - the interpreter
            # dies with SyntaxError. The flag is legacy (non-Python first
            # lines) and cannot be honored through a shim: unshimmable, the
            # PYTHONPATH transport keeps serving. Clustered spellings
            # (-xu) are caught too - a missed one would be the same crash.
            return None
        if not arg.startswith("-"):
            return i
        if arg in _PY_OPTS_WITH_SEPARATE_VALUE:
            i += 2
            continue
        i += 1
    return None


def _targets_gateway_module(cfg: dict) -> bool:
    """True when a python stdio entry launches a ``kiro_crew``-owned module.

    Scans only the INTERPRETER-OPTION prefix of ``args``, mirroring CPython's
    own argv parsing. The full branch table (each row is pinned by a test):

    - ``-m`` (separate): the next element is the module — answer on it.
    - ``-mMODULE`` (attached): CPython accepts the attached spelling too.
    - ``-c`` / ``-cPROGRAM`` / ``--``: option parsing ends; nothing after is
      an interpreter option, and no module launch is involved.
    - a non-dash token: the script operand — everything after belongs to the
      SCRIPT (``python3 server.py -m kiro_crew.mode``), never to CPython.
    - ``-X``/``-W``/``--check-hash-based-pycs`` (separate form): consume TWO
      tokens, so their value is never mistaken for the script operand.
    - any other dash token: a value-less flag or an attached-value option —
      consume one token.

    Out of scope, conservatively: single-letter clustering that ends in ``m``
    (``-sm kiro_crew``) reads here as an unknown flag followed by an operand
    and answers False — the venv-first default applies. No manifest uses that
    spelling; documenting the boundary beats guessing at getopt semantics.
    """
    args = cfg.get("args")
    if not isinstance(args, list):
        return False
    i = 0
    while i < len(args):
        arg = args[i]
        if not isinstance(arg, str):
            return False
        if arg == "-m":
            if i + 1 < len(args):
                module = args[i + 1]
                return isinstance(module, str) and (
                    module == "kiro_crew" or module.startswith("kiro_crew.")
                )
            return False
        if arg.startswith("-m") and len(arg) > 2:
            module = arg[2:]
            return module == "kiro_crew" or module.startswith("kiro_crew.")
        if arg == "--" or arg.startswith("-c") or not arg.startswith("-"):
            return False
        if arg in _PY_OPTS_WITH_SEPARATE_VALUE:
            i += 2
            continue
        i += 1
    return False


def _warn_unresolvable_stdio_command(app_name: str, server_name: str, cfg: dict) -> None:
    """Surface a stdio server whose command cannot spawn, instead of silent tool absence.

    The failure mode this closes: kiro-cli spawns each registered server itself and reports
    nothing when the spawn fails — the app's tools simply never appear. Emit one warning at
    registration time naming the app, the server, and the command, so the operator has a log
    line to find. Never raises: one bad server must not take down the whole registration
    pass, and the entry is still written.

    Deliberately checks only a command that CARRIES a path (one ``stat``). A bare name is
    not probed: PATH at spawn time is not the gateway's PATH (kiro-cli strips env), a
    legitimate binary may be installed later (``onEnable``) or live in app-local trees like
    ``node_modules/.bin``, and walking PATH with ``shutil.which`` would multiply the stats.

    Even the single stat can block in the kernel on a dead network mount, so the caller
    (:func:`_schedule_unresolvable_warning`) runs this OFF the event loop whenever one is
    running — the diagnostic is advisory and gates nothing, so it never needs to hold up
    registration.
    """
    command = cfg.get("command")
    if not isinstance(command, str) or not command.strip():
        return
    name = command.strip()
    has_sep = (
        os.sep in name
        or (os.altsep is not None and os.altsep in name)
        or bool(os.path.splitdrive(name)[0])
    )
    if not has_sep:
        return
    if not platform_compat.is_executable_file(name):
        logger.warning(
            "App %s: stdio MCP server %r declares command %r which resolves to no "
            "existing executable — the server will likely fail to spawn and its tools "
            "will be silently unavailable",
            app_name,
            server_name,
            command,
        )


def _warn_unresolved_secret_refs(app_name: str, server_name: str, env: dict) -> None:
    """Warn when an app's stdio MCP server declares a ``secret://`` env value.

    An app-registered stdio server is written into the agent config, and when
    kiro-cli spawns it directly the child gets the literal reference text: the
    credential silently never arrives. How an installed app may receive a vault
    secret at all is an open trust decision, so this neither resolves the value
    nor points at a route that would: it states the gap, gates nothing, and
    leaves the entry exactly as declared.

    Names only the env-var KEYS, never the secret name after the scheme -- the
    same sink rule :mod:`kiro_crew.mcp_gateway.secret_uri` follows, because a
    manifest-authored name can carry control characters. Never raises.
    """
    # Lazy: the resolver module pulls in the vault, which registration never needs.
    from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX

    keys = sorted(
        str(key)
        for key, value in env.items()
        if isinstance(value, str) and value.startswith(SECRET_URI_PREFIX)
    )
    if not keys:
        return
    logger.warning(
        "App %s: stdio MCP server %r sets env %s to a secret:// reference; when "
        "kiro-cli spawns the server directly it receives the literal reference "
        "text, not a vault value. How an app may receive a vault secret is "
        "undecided (https://github.com/kirodotdev/KiroCrew/issues/10641)",
        app_name,
        server_name,
        ", ".join(repr(k) for k in keys),
    )


def _schedule_unresolvable_warning(app_name: str, server_name: str, cfg: dict) -> None:
    """Run the unresolvable-command probe without ever blocking the event loop.

    ``_register_mcp_servers`` is a sync function reachable from async dashboard
    handlers, so its thread MAY be the loop thread: a ``stat`` on a dead
    network-mounted path there would freeze every task until the stall watchdog
    kills the gateway. When a loop is running, hand the probe (with a snapshot
    of the entry — registration mutates ``cfg`` after this point) to the
    maintenance pool; the log line is the only output, so fire-and-forget is
    the whole contract. With no loop (CLI, tests, worker threads), probe inline.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _warn_unresolvable_stdio_command(app_name, server_name, cfg)
        return
    loop.run_in_executor(
        maintenance_executor(),
        _warn_unresolvable_stdio_command,
        app_name,
        server_name,
        dict(cfg),
    )


def _maybe_provision_backendless_deps(app_name: str, manifest: "AppManifest") -> None:
    """Provision requirements.txt for an app whose backend spawn never runs pip.

    That covers an app with NO backend entry point, and also an ADOPTED
    file-entry backend (an externally-managed instance the gateway never
    spawns, so the spawn-path provisioning never fires for it).

    Provisioning normally runs in the backend spawn - but an app can ship
    only stdio MCP servers, and with no backend start nothing else ever runs
    pip: the shim/PYTHONPATH transports the resolution below emits would
    reference a forever-empty deps tree and the server would die on its
    first import. Stamp-gated (repeat registrations with unchanged
    requirements do no network work), and gated on the backend entry point
    being ABSENT: when one exists, the backend spawn owns provisioning - a
    module-style builtin entry point in particular must never have an
    app-dir requirements.txt provisioned (trust boundary; see
    provision_app_deps). Failure is logged by the provisioner and
    registration proceeds: the spawn-time import error points back at the
    provisioning log line, matching the backend spawn's own behavior.
    """
    # Provenance gate, not a manifest gate: a shipped BUILTIN's trust story
    # is the same one the backend spawn's entry gate enforces - builtin code
    # is trusted package code, its declared dependencies live in the
    # package's own pyproject, and an agent-planted requirements.txt in the
    # (writable) app dir must never have pip execute its build hooks under
    # the gateway without the third-party grant. shipped_builtin_app_root is
    # the authoritative provenance check (immutable package composition, not
    # mutable installed.json fields).
    if shipped_builtin_app_root(app_name) is not None:
        return
    root = app_dir(app_name)
    entry_point = manifest.backend.entryPoint
    if entry_point:
        # Registration provisions for FILE-entry apps too, not only
        # backend-less ones: a healthy fixed-port backend gets ADOPTED
        # (never spawned this session), so the spawn-path provisioning
        # never ran and a dependency-backed stdio server would reference an
        # empty deps tree. The stamp gate and the per-app flock make the
        # overlap with a real spawn cheap and safe. A MODULE-style entry
        # (trusted package code, the backend spawn's own trust gate) still
        # never provisions app-dir requirements -- decided by the same
        # shared predicate the backend spawn and the install-time desktop
        # gate answer from.
        if is_module_style_entry_point(entry_point, root):
            return
    if not os.path.lexists(root / "requirements.txt"):
        # True ABSENCE only: is_file() would also answer False for a
        # DANGLING requirements.txt symlink, silently skipping the
        # provisioner - a present-but-unreadable entry must fall through to
        # provision_app_deps, whose failure epilogue surfaces it (ERROR log
        # + SEL event) instead of this fast path eating it.
        return
    if not has_stdio_mcp_server(manifest):
        return
    # Deferred import: bridges is imported during backend's boot path, so it
    # cannot import backend at module load (same pattern as the other
    # backend imports in this module).
    from kiro_crew.apps.backend import provision_app_deps

    provision_app_deps(app_name, root)


def _register_mcp_servers(
    app_name: str, manifest: AppManifest, live_port: int | None = None
) -> list[str]:
    """Register app-provided MCP servers into Kiro Crew's agent config.

    Uses ``{app_name}:{server_name}`` namespace to avoid collisions. HTTP MCP urls have
    their port rewritten to the backend's live allocated port (see
    :func:`_resolve_live_mcp_url`) so a ``backend.port:"auto"`` app whose backend landed
    on a non-default port is still reachable by agents. ``live_port`` lets the boot/enable
    path pass the just-allocated port directly (health not yet confirmed).

    FAIL-SAFE for ``backend.port:"auto"`` HTTP servers (regression fix):
    a manifest's ``mcpServers.<name>.url`` carries an ILLUSTRATIVE
    fixed port (e.g. ``:9100``). If we wrote that verbatim while the backend is NOT
    running (app disabled / down / registered before the port is known), the entry is a
    reachable-LOOKING but dead URL. kiro-cli connects to every server in the agent config on each request; a connect failure surfaces as a "transient
    HTTP 5xx / backend hiccup", gets retried 3× by the transient-retry path, then shown as
    a hard error — breaking ALL kiro requests, not just this app's. (An alternate ACP
    backend reads a different config file, so it was unaffected — the asymmetry in the
    report.)
    So: a GATEWAY-MANAGED backend's HTTP server (``backend.entryPoint`` set) with NO
    resolvable LIVE port is NOT written at all (and any stale entry for it is scrubbed) —
    never a dead URL the kiro binary might still dial whether or not it honours a
    ``disabled`` flag. The boot/enable path calls :func:`reregister_app_mcp_servers` with
    the real ``live_port`` once the backend is up, which writes the entry with the
    correct, reachable port.

    A SELF-MANAGED app (empty ``backend.entryPoint``) is the exception: the gateway
    launches no backend for it, so its ``url`` is an AUTHORITATIVE fixed endpoint, not an
    illustrative port awaiting resolution. It never gets a live registration, so its url
    is preserved rather than scrubbed — mirroring :func:`_collect_app_mcp_servers`
    (``agent_materialization/mcp_sources.py``), the other writer of this config. The two
    writers must agree, or one scrubs an entry the other immediately writes back.

    stdio/command servers (no ``url``) are always registered — they have no port to be dead.
    """
    if not manifest.mcpServers:
        return []
    resolved_port = _live_port_for(app_name, live_port)
    _maybe_provision_backendless_deps(app_name, manifest)
    registered: list[str] = []
    skipped: list[str] = []
    # Reconcile guard OUTSIDE _mcp_lock: that order is fixed everywhere, so a health
    # transition and a lifecycle registration can never deadlock against each other.
    with _health_reconcile_guard(), _mcp_lock():
        mcp_data = _read_mcp_json_unlocked(strict=True)
        servers = mcp_data.setdefault("mcpServers", {})
        for server_name, server_config in manifest.mcpServers.items():
            namespaced = f"{app_name}:{server_name}"
            cfg = dict(server_config) if isinstance(server_config, dict) else server_config
            if isinstance(cfg, dict):
                cfg = _pin_host_cli_command(app_name, cfg)
            is_http = isinstance(cfg, dict) and bool(cfg.get("url"))
            if is_http and not resolved_port and manifest.backend.entryPoint:
                # GATEWAY-MANAGED backend (backend.entryPoint set) with no live port: the
                # manifest's url carries only an ILLUSTRATIVE fixed port, so writing it
                # verbatim is a dead default-port URL that breaks every kiro session. Skip
                # it AND scrub any stale entry so a prior (now-dead) registration can't keep
                # poisoning the provider path. The boot/enable path re-registers with the
                # live port once the backend is up.
                #
                # A SELF-MANAGED app (empty backend.entryPoint) is the opposite case: it has
                # no backend the gateway launches, so its url is an AUTHORITATIVE fixed
                # endpoint that never gets a live registration — preserve it instead of
                # dropping it. This mirrors ``_collect_app_mcp_servers`` in
                # ``agent_materialization/mcp_sources.py`` (the other writer of this config),
                # which keeps a self-managed url for exactly this reason. The two writers
                # must agree, or registration scrubs the entry and the next rebuild writes
                # it straight back.
                servers.pop(namespaced, None)
                skipped.append(namespaced)
                continue
            if is_http:
                # _resolve_live_mcp_url is a no-op without a live port, so a self-managed
                # app's authoritative url is written through unchanged here.
                cfg["url"] = _resolve_live_mcp_url(app_name, cfg["url"], live_port=resolved_port)
                if resolved_port:
                    cfg.pop("disabled", None)  # backend is live — ensure enabled
            else:
                # A stdio entry: resolve a bare interpreter to an absolute one — the
                # app's venv python when present, else the running interpreter (see
                # `resolve_stdio_command`) — and surface an unresolvable command
                # instead of letting the tools go silently missing.
                if isinstance(cfg, dict):
                    cfg = resolve_stdio_command(cfg, app_root=app_dir(app_name))
                    _schedule_unresolvable_warning(app_name, server_name, cfg)
                    # This file is consumed by kiro-cli, which applies a declared
                    # env per key — an app manifest naming a PATH fragment would
                    # hand its server that fragment as the WHOLE PATH. Emit
                    # through the shared normalization point (env.emit_env).
                    env = cfg.get("env")
                    if isinstance(env, dict):
                        _warn_unresolved_secret_refs(app_name, server_name, env)
                        cfg = {**cfg, "env": emit_env(env)}
            servers[namespaced] = cfg
            registered.append(namespaced)
        # LAST governance pass before this map hits disk. This file IS read by
        # kiro-cli, and an `autoApprove` on an entry here auto-approves that
        # server locally with NO permission request — so a manifest that ships
        # `autoApprove` on a governed server would bypass the PreToolUse gate and
        # the ceiling's denial, the same second route the agent-config writers
        # already close. Strip a governed grant here too; the tools stay, they
        # just go through the gate. Idempotent and a no-op on an ungoverned host.
        # This app's own entries are named explicitly rather than left to the
        # name-shape test: the rest of this map is the owner's own file, whose
        # `autoApprove` the opt-in exists to honour, so the two cannot share one
        # verdict. Every key carrying this app's prefix counts, including a stale
        # one from an earlier registration.
        mcp_data["mcpServers"] = dict(
            strip_ungoverned_auto_approve(
                servers,
                third_party={k for k in servers if k.startswith(f"{app_name}:")},
            )
        )
        _write_mcp_json_unlocked(mcp_data)
    logger.info(
        "Registered %d MCP server(s) for app %s (live_port=%s); skipped %d HTTP server(s) "
        "with no live backend: %s",
        len(registered),
        app_name,
        resolved_port,
        len(skipped),
        skipped or "none",
    )
    return registered


def registered_app_mcp_servers() -> dict[str, Any]:
    """Return the app MCP servers as CURRENTLY registered on disk.

    This is the live map :func:`_register_mcp_servers` writes: an auto-port app's
    HTTP ``url`` already rewritten to the backend's live allocated port, and any
    HTTP server with no live backend already skipped (never a dead-port URL). The
    agent rebuild reads THIS in preference to the manifest so that a resolved
    auto-port survives the rebuild — copying the manifest's illustrative port back
    over a live one is exactly the regression this exists to prevent. Best-effort:
    a missing or unreadable file yields an empty map, and the caller falls back to
    the manifest for stdio servers (and skips live-portless HTTP ones).
    """
    try:
        with _mcp_lock():
            data = _read_mcp_json_unlocked()
    except Exception:  # noqa: BLE001 — never let a rebuild fail on this read
        return {}
    servers = data.get("mcpServers", {})
    return dict(servers) if isinstance(servers, dict) else {}


def reregister_app_mcp_servers(
    app_name: str, live_port: int | None = None, io_failures: list[str] | None = None
) -> list[str]:
    """Re-register admitted MCP servers after an app backend has started.

    HTTP URLs are rewritten to the live allocated port. Shipped definitions are
    sourced from their immutable package manifest; denied apps have any stale
    global entries scrubbed instead of being made reachable.
    """
    manifest, app_root = _registration_source(app_name)
    if _registration_denied(
        app_name,
        action="mcp_register",
        app_root=app_root,
    ):
        _deregister_mcp_servers(app_name)
        return []
    if not manifest:
        # UNREADABLE, not merely empty: nothing was registered, so reporting success
        # would let the caller record a registration that never happened and leave a
        # healthy backend with no MCP entry and nothing to retry it.
        if io_failures is not None:
            io_failures.append(f"{app_name}: manifest unreadable")
        return []
    if not manifest.mcpServers:
        return []
    registered = _register_mcp_servers(app_name, manifest, live_port=live_port)
    # Refresh the app's AGENTS after the live server lands. register_app runs
    # _register_agents AFTER _register_mcp_servers precisely because agents COPY
    # the registered spec into their own config; this live-port path (health
    # confirmed, backend just came up) wrote only the global map, so without this
    # the app's agent kept the pre-live spec (or none) and could not reach its own
    # declared MCP tools until some unrelated rebuild happened to run.
    if registered:
        try:
            _register_agents(app_name, manifest, app_root, io_failures=io_failures)
        except Exception:  # noqa: BLE001 — reported via io_failures, never raised on
            logger.warning(
                "Could not refresh agents for app %s after live MCP registration", app_name
            )
            if io_failures is not None:
                io_failures.append(f"{app_name}:<all agents>")
    return registered


def scrub_backend_mcp_url(app_name: str, unreconciled: list[str] | None = None) -> list[str]:
    """Remove an app's backend-dependent MCP entry, keeping the servers that need no port.

    Registering with no live port is the existing path for this: it pops each HTTP entry
    and keeps stdio/command ones, which kiro-cli launches itself and which have no port
    to be dead. Returns the servers that were kept.

    Falls back to removing EVERY entry for the app when its manifest cannot be resolved
    or declares no servers. That case cannot distinguish a backend-dependent server from
    an independent one, and the dead url must not survive on the strength of not knowing
    — the failure this whole gate exists to prevent.

    The fallback never touches the app's materialized AGENT files. See the comment on
    that branch: their deletion is unrecoverable, and only ``deregister_app`` owns it.

    ``unreconciled`` collects a reason when the entry could not be brought into a
    consistent state — today, an UNREADABLE manifest. Keeping the agents there is right,
    but it leaves them naming the server just removed and nothing else revisits them, so
    the caller must treat it as unlanded and retry rather than record it as done. A
    manifest that simply declares no servers is fully reconciled: there is nothing stale
    to correct.
    """
    manifest, app_root = _registration_source(app_name)
    if not manifest and unreconciled is not None:
        # Unreadable, not merely empty: the agents are kept (see below) and therefore
        # still name the removed server, and `refresh_app_agents` gives up on the same
        # condition — so nothing here can finish the job. Report it so the watch retries
        # once the manifest is readable again.
        unreconciled.append(f"{app_name}: manifest unreadable")
    if not manifest or not manifest.mcpServers:
        # Scrub the entry, but NEVER the materialized agents. Deleting them is
        # unrecoverable — it takes the user-owned fields `_preserve_user_agent_edits`
        # carries across every refresh — while the thing it would prevent, an agent
        # naming a server that is gone, costs failed tool calls until the next
        # successful refresh rewrites it. An unreadable manifest is also frequently
        # TRANSIENT, so destroying data over it trades a temporary fault for a permanent
        # one. Uninstall removes these files through `deregister_app`, which is the path
        # that legitimately owns their deletion.
        removed = _deregister_mcp_servers(app_name)
        if removed:
            logger.warning(
                "Scrubbed %d MCP server(s) for app %s: its manifest declares none or "
                "could not be read. Its materialized agents are KEPT and may still name "
                "the removed server until a refresh with a readable manifest rewrites "
                "them.",
                removed,
                app_name,
            )
        return []
    # `_register_mcp_servers` directly, NOT `reregister_app_mcp_servers`: the latter also
    # calls `_register_agents`, which would re-materialize this app's agent configs here
    # — before the caller's enablement check runs — making a disabled app's agents
    # dispatchable in the gap. The scrub only needs the mcp.json half; the agent refresh
    # is the caller's, and it is gated.
    #
    # The admission gate that `reregister_app_mcp_servers` applies is kept explicitly: a
    # denied app gets a FULL removal rather than the selective keep-stdio treatment,
    # because nothing of a denied app should stay reachable.
    if _registration_denied(app_name, action="mcp_register", app_root=app_root):
        _deregister_mcp_servers(app_name)
        return []
    return _register_mcp_servers(app_name, manifest, live_port=None)


def _scrub_legacy_shared_mcp(app_name: str) -> int:
    """Remove an app's entries from the pre-fix shared ~/.kiro/settings/mcp.json.

    Older builds registered app MCP servers into the shared file, where they were
    visible to Kiro IDE and every other kiro-cli agent. Upgrading doesn't retroact
    on what is already on disk, so deregister scrubs both locations — otherwise a
    stale entry keeps leaking (and, for an HTTP server whose backend is gone,
    keeps pointing at a dead port). Best-effort: never raises.
    """
    prefix = f"{app_name}:"
    try:
        if not _LEGACY_SHARED_MCP_PATH.is_file():
            return 0
        # Hold the shared file's own lock across read+remove+write: this file can
        # be written concurrently (Kiro IDE, another kiro-cli agent, another
        # Kiro Crew process), and an unlocked stale read-modify-write here would
        # clobber a server another writer added between our read and our atomic
        # rename. It is a DIFFERENT sidecar than _mcp_lock's default (that guards
        # kirocrew.json), so pass the legacy path explicitly.
        with _mcp_lock(target=_LEGACY_SHARED_MCP_PATH):
            data = loads_mcp_config(_LEGACY_SHARED_MCP_PATH.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {})
            stale = [k for k in servers if k.startswith(prefix)]
            if not stale:
                return 0
            for k in stale:
                del servers[k]
            atomic_write(_LEGACY_SHARED_MCP_PATH, json.dumps(data, indent=2) + "\n")
        logger.info("Scrubbed %d legacy shared MCP entry(ies) for app %s", len(stale), app_name)
        return len(stale)
    except Exception as exc:  # noqa: BLE001 — cleanup must never block deregistration
        logger.warning("Failed to scrub legacy shared MCP entries for %s: %s", app_name, exc)
        return 0


def _deregister_mcp_servers(app_name: str) -> int:
    """Remove an app's MCP servers from the agent config (and the legacy shared file)."""
    prefix = f"{app_name}:"
    with _health_reconcile_guard(), _mcp_lock():
        mcp_data = _read_mcp_json_unlocked(strict=True)
        servers = mcp_data.get("mcpServers", {})
        to_remove = [k for k in servers if k.startswith(prefix)]
        for k in to_remove:
            del servers[k]
        if to_remove:
            _write_mcp_json_unlocked(mcp_data)
    # NOT scrubbed here: the legacy shared ~/.kiro/settings/mcp.json is held by
    # OTHER processes (Kiro IDE, other kiro-cli agents), so its cross-process
    # flock can block indefinitely. deregister_app() runs synchronously on the
    # gateway event loop (dashboard disable/update/uninstall), and a stall here
    # would freeze all chat and heartbeat tasks. The scrub is idempotent and is
    # performed at boot by reconcile_enabled_app_resources(), which the gateway
    # already runs off-loop via run_in_executor — so the migration still lands,
    # just not on this hot path.
    if to_remove:
        logger.info("Deregistered %d MCP server(s) for app %s", len(to_remove), app_name)
    return len(to_remove)
