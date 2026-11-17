"""Optional channel SDKs the desktop app installs on first use.

The desktop app's backend is a code-signed bundle: nothing may be written inside
it, and every app update replaces it whole. So an optional SDK is not shipped in
the bundle and not pip-installed into it. The owner presses Install in Settings,
and the gateway installs the SDK's pinned wheels into the data home::

    <data home>/extras/<extra>/<python tag>/

Outside the bundle, so the signature is untouched and the files survive updates.
The directory joins the END of ``sys.path``, so a bundled module always wins over
a same-named one here.

Integrity: ``pip --require-hashes`` checks every wheel against the lock shipped in
the package (``sdk_extras_locks/<extra>.txt``), with ``--no-deps`` so nothing but
the locked files is fetched. The ``extras`` leaf is READONLY inside every agent
sandbox and fenced from the agent's file tools (``sandbox_leaves``,
``security.paths_lists``), so the agent cannot change code the gateway later
imports. The installer runs in the gateway, outside the sandbox.

The python tag carries the interpreter version and the machine, because a wheel
with native code (``pycryptodome``) only loads in the interpreter it was built for.
A new app with a different Python finds no install for its tag and offers Install
again, rather than importing a library built for another interpreter.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

#: The crew-home leaf. Spelled identically in ``sandbox_leaves``,
#: ``sandbox_precreate`` and ``security.paths_lists``; ``test_sdk_extras`` pins them.
EXTRAS_LEAF = "extras"

#: The hashed locks, beside this module.
LOCKS_DIR = Path(__file__).with_name("sdk_extras_locks")

#: Written last, so a directory without it is an install that did not finish.
MARKER_FILE = "kirocrew-extra.json"

#: The extras this installer may lay down. Each has a lock in ``LOCKS_DIR``.
INSTALLABLE: frozenset[str] = frozenset({"feishu"})

#: A cold download of the locked wheels over a slow link.
_PIP_TIMEOUT_SECS = 10 * 60.0

#: The tail of pip's output an error carries; the reason is at the end.
ERROR_DETAIL_CAP = 2000

#: ``pip config list`` only reads files; a pip that hangs here means no config.
_PIP_CONFIG_TIMEOUT_SECS = 15.0

#: The gateway's own variables that name the user's index, passed to the first attempt.
_PIP_INDEX_ENV: tuple[str, ...] = (
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_TRUSTED_HOST",
    "PIP_CERT",
    "PIP_CLIENT_CERT",
)

#: The same settings as pip config keys, read when ``PIP_INDEX_URL`` is unset.
_PIP_CONFIG_KEYS: dict[str, str] = {
    "index-url": "PIP_INDEX_URL",
    "extra-index-url": "PIP_EXTRA_INDEX_URL",
    "trusted-host": "PIP_TRUSTED_HOST",
    "cert": "PIP_CERT",
    "client-cert": "PIP_CLIENT_CERT",
}

#: A URL inside pip's output, up to the first space, comma, quote or bracket.
_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>()\[\],]+")


#: One install at a time in this process. The Settings button and the start-up
#: install for an already enabled channel can both run; the second waits and then
#: finds the finished install instead of writing the same ``.partial`` directory.
_INSTALL_LOCK = threading.Lock()


#: ``InstallError.kind`` values. The install route turns them into the stable
#: error codes Settings maps to its own wording, so the card never shows pip's text.
KIND_DOWNLOAD = "download"
KIND_INSTALL = "install"

#: pip output that means the wheels could not be fetched: no route to the index,
#: a proxy or DNS failure, or an index that answered with nothing. Offline, pip
#: retries, then reports "No matching distribution found" for the first pin.
_DOWNLOAD_FAILURE_MARKERS: tuple[str, ...] = (
    "No matching distribution found",
    "Could not find a version that satisfies",
    "NewConnectionError",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "ProxyError",
    "Max retries exceeded",
    "Temporary failure in name resolution",
    "Name or service not known",
    "nodename nor servname",
    "getaddrinfo failed",
    "Network is unreachable",
    "Connection refused",
    "connection broken",
)


class InstallError(RuntimeError):
    """An install that did not finish.

    The message is for the log and the audit record. ``kind`` is what Settings
    reads: ``KIND_DOWNLOAD`` when the wheels could not be fetched, else
    ``KIND_INSTALL``.
    """

    def __init__(self, message: str, kind: str = KIND_INSTALL) -> None:
        super().__init__(message)
        self.kind = kind


def python_tag() -> str:
    """``cp312-darwin-arm64``: the interpreter and machine a native wheel needs."""
    return (
        f"cp{sys.version_info[0]}{sys.version_info[1]}-{sys.platform}-"
        f"{platform.machine().lower() or 'unknown'}"
    )


def extra_dir(extra: str, home: Path | None = None) -> Path:
    return Path(home or config_dir()) / EXTRAS_LEAF / extra / python_tag()


def lock_file(extra: str) -> Path:
    return LOCKS_DIR / f"{extra}.txt"


def is_installed(extra: str, home: Path | None = None) -> bool:
    return (extra_dir(extra, home) / MARKER_FILE).is_file()


def install_supported(extra: str) -> bool:
    """True on the desktop bundle, for an extra this installer knows, with pip present.

    Elsewhere the user owns their environment and gets a ``pip install`` command
    instead (``extras.pip_install_channel_available``).
    """
    return (
        extra in INSTALLABLE
        and platform_compat.is_bundled_interpreter()
        and lock_file(extra).is_file()
        and importlib.util.find_spec("pip") is not None
    )


def activate(home: Path | None = None) -> None:
    """Append every finished install for this interpreter to ``sys.path``. Idempotent.

    Called before anything probes or imports an optional SDK. Only reads the disk.
    """
    added = False
    for extra in sorted(INSTALLABLE):
        target = extra_dir(extra, home)
        if not (target / MARKER_FILE).is_file():
            continue
        entry = str(target)
        if entry not in sys.path:
            sys.path.append(entry)
            added = True
    if added:
        importlib.invalidate_caches()


def install_extra(extra: str, home: Path | None = None) -> Path:
    """Install *extra*'s locked wheels for this interpreter; blocking. Returns the directory.

    Built in a sibling ``.partial`` directory and renamed into place, so a failed
    or interrupted install never leaves a half-filled directory that ``activate``
    would load. A finished install is returned untouched: the running gateway may
    already have imported it (on Windows its native files are then locked), so it
    is never deleted or rebuilt. Every failure, a disk or permission error
    included, is an ``InstallError`` and leaves no ``.partial`` behind.
    """
    if extra not in INSTALLABLE:
        raise InstallError(f"no installer for '{extra}'")
    lock = lock_file(extra)
    if not lock.is_file():
        raise InstallError(f"lock file missing: {lock}")
    target = extra_dir(extra, home)
    with _INSTALL_LOCK:
        return _install_locked(extra, lock, target, home)


def _install_locked(extra: str, lock: Path, target: Path, home: Path | None) -> Path:
    if (target / MARKER_FILE).is_file():
        activate(home)
        return target
    partial = target.with_name(f".{target.name}.partial")
    try:
        _lay_down(extra, lock, target, partial)
    except OSError as exc:
        raise InstallError(f"could not write the install: {exc}") from exc
    finally:
        # After a successful rename there is nothing here; otherwise this is the
        # half-built tree, never the finished ``target``.
        shutil.rmtree(partial, ignore_errors=True)
    logger.info("installed the %s SDK into %s", extra, target)
    activate(home)
    return target


def _pip_failure_summary(output: str, returncode: int) -> str:
    """The lines that explain the failure: pip's ``ERROR:`` lines, else its last three.

    pip prints one ``WARNING: Retrying ...`` line per network retry before the
    error that matters, so the raw tail is mostly noise. This summary goes to the
    route's warning and the audit record; Settings shows its own wording for the
    error's ``kind``, and the whole output goes to the gateway log.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    picked = [line for line in lines if line.startswith("ERROR:")] or lines[-3:]
    return "\n".join(picked)[-ERROR_DETAIL_CAP:] or f"pip exited {returncode}"


def _pip_failure_kind(output: str) -> str:
    """``KIND_DOWNLOAD`` when pip's output says the wheels could not be fetched."""
    lowered = output.lower()
    if any(marker.lower() in lowered for marker in _DOWNLOAD_FAILURE_MARKERS):
        return KIND_DOWNLOAD
    return KIND_INSTALL


def _redact_urls(text: str) -> str:
    """*text* with every URL in it cut to scheme and host.

    pip names the index it reads ("Looking in indexes: ..."), and a private
    mirror can carry its credential as userinfo or as a path or query token, so
    nothing past the host is kept (``asset_downloader.redact_url``).
    """
    from kiro_crew.asset_downloader import redact_url

    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), text)


def _redact_pip_output(text: str, index_env: dict[str, str]) -> str:
    """pip's *text* with no part of the configured index left in it.

    Whole URLs go through ``_redact_urls``, but urllib3's retry and error lines
    print only the request PATH (``... connection broken by ...: /<token>/simple/``),
    with no scheme or host for a URL pattern to find. A mirror that carries its
    credential in the path would leak there, so every configured index's userinfo,
    path and query are removed by value first.
    """
    secrets: set[str] = set()
    for name in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL"):
        for url in index_env.get(name, "").split():
            parts = urllib.parse.urlsplit(url)
            path = parts.path.rstrip("/")
            for piece in (parts.username, parts.password, path, parts.query):
                if piece and len(piece) > 1:
                    secrets.add(piece)
    for piece in sorted(secrets, key=len, reverse=True):
        text = text.replace(piece, "<index path>")
    return _redact_urls(text)


def _parse_pip_config(text: str) -> dict[str, str]:
    """The index settings in ``pip config list`` output, as ``PIP_*`` variables.

    pip prints one ``<section>.<key>=<repr of value>`` line per setting. Only the
    ``global`` and ``install`` sections reach ``pip install``, and ``install``
    wins, as it does in pip. A value pip stored over several lines (two extra
    indexes) becomes the space-separated list the ``PIP_*`` variable takes.
    """
    found: dict[str, dict[str, str]] = {"global": {}, "install": {}}
    for line in text.splitlines():
        name, sep, raw = line.strip().partition("=")
        section, _, key = name.partition(".")
        if not sep or section not in found or key not in _PIP_CONFIG_KEYS:
            continue
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            try:
                raw = str(ast.literal_eval(raw))
            except (ValueError, SyntaxError):
                raw = raw[1:-1]
        value = " ".join(raw.split())
        if value:
            found[section][_PIP_CONFIG_KEYS[key]] = value
    return {**found["global"], **found["install"]}


def _pip_config_list() -> str:
    """``pip config list`` from the gateway's own pip, or ``""`` on any failure."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            # -I: no current directory on sys.path, so a planted pip.py in the
            # gateway's working directory cannot run in place of pip.
            [sys.executable, "-I", "-m", "pip", "config", "list"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_PIP_CONFIG_TIMEOUT_SECS,
            check=False,
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "") if result.returncode == 0 else ""


def _configured_index_env() -> dict[str, str]:
    """The package index the user set up for pip, as ``PIP_*`` variables; ``{}`` if none.

    A network that reaches only a company mirror needs it: the install's own
    environment drops every ``PIP_*`` variable and all pip config (see
    ``_child_env``), so without this pip talks only to pypi.org. pip's own config
    files give the index settings (index URLs, trusted hosts, CA bundle, client
    certificate), and the gateway's ``PIP_*`` variables override them key by key,
    as pip itself does: a mirror set in the environment still gets the client
    certificate kept in ``pip.conf``. Only those keys are kept.
    """
    from_env = {name: os.environ[name] for name in _PIP_INDEX_ENV if os.environ.get(name)}
    return {**_parse_pip_config(_pip_config_list()), **from_env}


def _describe_index(index_env: dict[str, str]) -> str:
    """Which index an attempt uses, for the log: scheme and host only, never a credential."""
    from kiro_crew.asset_downloader import redact_url

    if not index_env:
        return "pypi.org (pip's default)"
    primary = index_env.get("PIP_INDEX_URL")
    parts = [redact_url(primary) if primary else "pypi.org"]
    parts += [redact_url(url) for url in index_env.get("PIP_EXTRA_INDEX_URL", "").split()]
    return ", ".join(parts) + " (your pip settings)"


def _lay_down(extra: str, lock: Path, target: Path, partial: Path) -> None:
    """Run pip into *partial*, mark it finished, and rename it to *target*.

    pip first uses the index the user configured, if any; when that fails to
    download, it runs once more against pypi.org (a mirror login that expired
    answers 401 for every wheel). The lock's hashes pin every file either way.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable,
        "-I",
        "-m",
        "pip",
        "install",
        "--target",
        str(partial),
        "--no-deps",
        "--require-hashes",
        "--only-binary",
        ":all:",
        "--no-input",
        "--disable-pip-version-check",
        "-r",
        str(lock),
    ]
    # Lazy: the browser harness installer pulls in ~35 ms of modules, and this
    # module loads at gateway start while pip runs only on an install.
    from kiro_crew.browser_harness.install import _child_env

    configured = _configured_index_env()
    attempts = [configured, {}] if configured else [{}]
    for number, index_env in enumerate(attempts, start=1):
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir()
        logger.info(
            "installing the %s SDK, attempt %d, from %s",
            extra,
            number,
            _describe_index(index_env),
        )
        try:
            result = subprocess.run(
                argv,
                cwd=str(partial),
                env={**_child_env(), **index_env},
                capture_output=True,
                timeout=_PIP_TIMEOUT_SECS,
                check=False,
                **UTF8_TEXT,
            )
        except subprocess.TimeoutExpired as exc:
            # The lock is a few megabytes: running out the clock is a slow or stalled link.
            raise InstallError(
                f"timed out after {_PIP_TIMEOUT_SECS:.0f}s", kind=KIND_DOWNLOAD
            ) from exc
        except OSError as exc:
            raise InstallError(str(exc)) from exc
        if result.returncode == 0:
            break
        output = _redact_pip_output(
            ((result.stderr or "") + (result.stdout or "")).strip(), index_env
        )
        logger.warning("pip could not install the %s SDK:\n%s", extra, output[-20_000:])
        kind = _pip_failure_kind(output)
        if number < len(attempts) and kind == KIND_DOWNLOAD:
            continue
        raise InstallError(_pip_failure_summary(output, result.returncode), kind=kind)
    (partial / MARKER_FILE).write_text(
        json.dumps({"extra": extra, "python": python_tag()}) + "\n", encoding="utf-8"
    )
    # Only an unfinished leftover can be here (a finished one returned above), and
    # ``activate`` never loads one, so removing it touches nothing the gateway runs.
    shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        raise InstallError(f"could not remove an unfinished install at {target}")
    partial.rename(target)
