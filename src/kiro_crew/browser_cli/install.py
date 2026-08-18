"""Detection, installation, and the capability gate for ``@playwright/cli``.

The CLI has no capability gating of its own — every command is always available
to whoever can run the binary — so the capability cannot be narrowed after the
fact. **Presence of ``playwright-cli`` is therefore consent**, whichever tool
installed it, and :func:`available` is the whole gate. This module deliberately
holds no toggle, flag, or consent file: a second gate would be a lie, because a
binary on PATH is reachable from any shell turn regardless of what a flag says.

Installation is global (``npm install -g``) rather than ``npx``. ``npx``
re-resolves the package through the registry on every invocation, so an expired
registry token would take browsing down at use time; a global binary resolves
once, at install time, where a failure is visible to the operator.

Node is located through :func:`kiro_crew.env.find_node_tool` rather than bare
``shutil.which``: the gateway can run with a PATH that omits the version-manager
shim directory a global npm install writes into, so a plain PATH lookup misses
a binary that is genuinely present.

Every function here blocks (subprocess, filesystem), so a caller on the event
loop offloads it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.browser_cli import os_deps
from kiro_crew.config.paths import config_dir
from kiro_crew.env import augmented_path, find_node_tool, node_augmented_path
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# The CLI's own floor. Node 19 and older lack APIs its bundle uses, so a lower
# version does not fail at install — it fails at first browse with an opaque
# stack, which is why detection rejects it up front rather than letting install
# "succeed" into a broken state.
MIN_NODE_MAJOR = 20

CLI_BIN = "playwright-cli"
NPM_SPEC = "@playwright/cli@latest"

# ``install --skills`` writes the command reference where an agent can read it.
# ``agents`` is the agent-neutral target (the default, ``claude``, writes a
# Claude-specific layout) and ``--global`` puts it in the home directory so it
# is found from any working directory rather than only inside one workspace.
_SKILLS_TARGET = "agents"

# Browser binaries live in Playwright's own cache, keyed by platform, and
# ``PLAYWRIGHT_BROWSERS_PATH`` overrides it. Probing this directory keeps
# ``detect()`` free of a subprocess that would launch a browser to answer.
_BROWSERS_CACHE_ENV = "PLAYWRIGHT_BROWSERS_PATH"

# A version probe answers immediately or something is wrong; an install talks to
# the npm registry and then downloads a browser, so its budget is minutes.
_PROBE_TIMEOUT_S = 20.0
_NPM_INSTALL_TIMEOUT_S = 900.0
_BROWSER_INSTALL_TIMEOUT_S = 1800.0
_SKILLS_INSTALL_TIMEOUT_S = 180.0

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def cli_env() -> dict[str, str]:
    """Environment for a CLI/npm child, with the Node bin directories on PATH.

    The gateway's own PATH is not sufficient: a global ``npm install -g`` lands
    in a version-manager-owned bin directory that the gateway process may never
    have had on PATH, so a child that inherits it unchanged cannot find the
    binary that was just installed.

    Two layers, node bins outermost so they win:

    1. :func:`node_augmented_path` prepends the Node bin dirs, so ``npm``/``node``
       resolve to the managed toolchain.
    2. :func:`augmented_path` (the inner layer) contributes the broad non-login
       PATH -- ``~/.local/bin``, ``/opt/homebrew/bin``, the mise shims -- because
       the mise-managed per-version ``npm`` is itself a wrapper script that runs
       ``mise reshim`` after a global install. That hook needs the ``mise``
       binary itself on PATH, and mise installs to ``~/.local/bin`` (or
       Homebrew's bin), NOT to any Node bin dir. A GUI- or daemon-launched
       gateway inherits a minimal PATH lacking those dirs, so without this layer
       the wrapper dies ``mise: command not found`` and, under its
       ``set -euo pipefail``, fails the whole ``npm install -g`` with rc 127.

    This also aligns the exec env with discovery: :func:`cli_path` already
    searches over ``node_augmented_path(augmented_path(PATH))``, so the child's
    PATH now matches the path the binary was resolved on.
    """
    env = dict(os.environ)
    env["PATH"] = node_augmented_path(augmented_path(env.get("PATH", "")))
    return env


def _run(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Run *argv*, returning ``(returncode, stdout, stderr)``.

    A timeout or a missing executable is reported as a non-zero return code with
    the reason on stderr, so callers branch on one shape instead of catching
    three exception types at every call site.
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=cli_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s: {' '.join(argv)}"
    except OSError as exc:
        return 127, "", f"{exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def cli_path() -> str | None:
    """Absolute path to ``playwright-cli``, or ``None`` when it is not present.

    Searched over :func:`augmented_path` and NOT the bare inherited ``PATH``,
    because the two installers disagree about where the wrapper goes.
    ``npm install -g`` drops it beside npm, which :func:`find_node_tool` finds
    via ``node_bin_dirs``; ``playwright-cli.sh`` / ``.ps1`` deliberately write
    it to ``~/.local/bin`` instead, so it works without touching the npm prefix.
    That directory is in ``_EXTRA_PATH_DIRS`` but not in ``node_bin_dirs``, and a
    gateway started by systemd, launchd or the Windows service manager does not
    inherit it on ``$PATH``. Without this the standalone install would SUCCEED
    and the panel would keep reporting the CLI as absent -- the worst shape of
    failure available here, since nothing errors and the offer never withdraws.
    """
    return find_node_tool(CLI_BIN, base_path=augmented_path(os.environ.get("PATH", "")))


def _first_version(text: str) -> str | None:
    """First semver-looking token in *text*, or ``None``.

    Both ``node --version`` (``v24.18.0``) and ``playwright-cli --version``
    (``0.1.18``) are matched by the same scan, and a version banner that carries
    extra output around the number still parses.
    """
    m = _VERSION_RE.search(text)
    return m.group(0) if m else None


def _node_version() -> str | None:
    """Version reported by the resolved ``node``, or ``None`` if absent/mute."""
    node = find_node_tool("node")
    if node is None:
        return None
    rc, out, err = _run([node, "--version"], _PROBE_TIMEOUT_S)
    if rc != 0:
        logger.debug("node --version failed (rc=%d): %s", rc, err.strip())
        return None
    return _first_version(out)


def _node_major(version: str | None) -> int | None:
    """Major component of *version*, or ``None`` when it is unparseable."""
    if not version:
        return None
    m = _VERSION_RE.search(version)
    return int(m.group(1)) if m else None


def _browsers_cache_dir() -> Path | None:
    """Playwright's browser cache directory for this platform.

    ``None`` on a platform whose cache location this does not know, which reads
    back as "cannot confirm a browser" rather than as a missing browser.
    """
    override = os.environ.get(_BROWSERS_CACHE_ENV, "").strip()
    if override:
        return Path(override)
    if platform_compat.IS_MACOS:
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if platform_compat.IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        return Path(local) / "ms-playwright" if local else None
    if platform_compat.IS_LINUX:
        return Path.home() / ".cache" / "ms-playwright"
    return None


# The engines Playwright downloads, in the order the panel lists them. A fixed
# tuple, not free input: it is what validates the engine name before it reaches
# argv (see `install_browser`), which is what keeps that spawn benign.
BROWSER_ENGINES: tuple[str, ...] = ("chromium", "firefox", "webkit")


def _cached_browser_names() -> set[str] | None:
    """Directory names in Playwright's browser cache, or ``None`` if unreadable."""
    cache = _browsers_cache_dir()
    if cache is None:
        return None
    try:
        return {child.name for child in cache.iterdir() if child.is_dir()}
    except OSError:
        return None


# playwright-core ships this manifest beside its entry point, listing the browser
# revision each engine needs. It is the file `install-browser` consults, so it is
# the authority on what "present" means for THIS installed CLI. Reading it is a
# plain file read, which keeps `detect()` subprocess-free.
_BROWSERS_MANIFEST = "browsers.json"
_PLAYWRIGHT_CORE_PKG = "playwright-core"

#: The CLI's own package coordinates. The manifest is only trusted when it is
#: served to THIS package, so the scope and name are what anchors the search.
_CLI_PKG_SCOPE = "@playwright"
_CLI_PKG_NAME = "cli"

#: Where the standalone installer puts the package tree. `npm --global --prefix`
#: writes under ``<prefix>/lib/node_modules`` on POSIX and ``<prefix>/node_modules``
#: on Windows, so both are probed.
_STANDALONE_PREFIX_ENV = "KIROCREW_PLAYWRIGHT_CLI_HOME"
_STANDALONE_PREFIX_DIR = "playwright-cli"
_NODE_MODULES = "node_modules"


def _standalone_node_modules() -> list[Path]:
    """``node_modules`` roots of a standalone (unprivileged) CLI install.

    Probed by KNOWN PATH rather than found by searching, because the standalone
    installer generates a **wrapper script** instead of a symlink: the package
    tree is not an ancestor of the launcher on PATH, so no walk up from the
    resolved launcher can reach it. Without this the revision could not be read
    for that install shape at all, and the check would silently degrade to the
    presence-only answer whose false positives it exists to remove.
    """
    prefix_override = os.environ.get(_STANDALONE_PREFIX_ENV, "").strip()
    prefix = Path(prefix_override) if prefix_override else config_dir() / _STANDALONE_PREFIX_DIR
    return [prefix / "lib" / _NODE_MODULES, prefix / _NODE_MODULES]


def _launcher_node_modules(anchor: Path) -> list[Path]:
    """``node_modules`` roots to probe relative to the resolved launcher.

    A launcher that is a SYMLINK resolves INTO the package tree, so an ancestor is
    already the package directory and none of these are needed. A launcher that is
    a real FILE resolves to itself, and then the tree has to be found beside it:
    ``npm install -g`` writes a ``.cmd`` batch wrapper on Windows, and a generated
    shell wrapper is what the standalone installer produces. Without this, those
    shapes read no revision at all and fall back to presence-only -- the exact
    false positive the revision gate exists to remove.

    Bounded to the launcher's own install prefix rather than walked toward the
    filesystem root, because an unbounded walk is what allowed a foreign tree to
    supply the revision. Every candidate still has to hold ``@playwright/cli``
    (see :func:`_cli_package_dirs`), so a stray ``playwright-core`` from an
    unrelated install is still unreachable.
    """
    return [
        # <prefix>/playwright-cli.cmd  ->  <prefix>/node_modules  (npm -g, Windows)
        anchor.parent / _NODE_MODULES,
        # <prefix>/bin/playwright-cli  ->  <prefix>/node_modules
        anchor.parent.parent / _NODE_MODULES,
        # <prefix>/bin/playwright-cli  ->  <prefix>/lib/node_modules  (npm -g, POSIX)
        anchor.parent.parent / "lib" / _NODE_MODULES,
    ]


def _cli_package_dirs() -> list[Path]:
    """``@playwright/cli`` package directories on this host, most specific first.

    Three sources, in priority order: an ancestor of the resolved launcher (an
    ``npm install -g`` symlink resolves into the package tree), a ``node_modules``
    beside the launcher (a real-file wrapper resolves to itself, so nothing in its
    ancestry is the package), and the standalone installer's known prefix.
    """
    dirs: list[Path] = []
    cli = cli_path()
    if cli is not None:
        try:
            anchor: Path | None = Path(cli).resolve()
        except OSError:
            anchor = None
        if anchor is not None:
            for parent in anchor.parents:
                if parent.name == _CLI_PKG_NAME and parent.parent.name == _CLI_PKG_SCOPE:
                    dirs.append(parent)
                    break
            for node_modules in _launcher_node_modules(anchor):
                package = node_modules / _CLI_PKG_SCOPE / _CLI_PKG_NAME
                if package.is_dir():
                    dirs.append(package)
    for node_modules in _standalone_node_modules():
        package = node_modules / _CLI_PKG_SCOPE / _CLI_PKG_NAME
        if package.is_dir():
            dirs.append(package)
    return dirs


def _manifest_for_cli_package(package: Path) -> Path | None:
    """The ``browsers.json`` of the ``playwright-core`` serving *package*.

    Two layouts, both anchored ON the package so the manifest can only come from
    the tree that will launch the browser: nested inside the package's own
    ``node_modules``, or hoisted as a sibling in the ``node_modules`` that holds
    ``@playwright/cli`` (``<pkg>/../..`` — up past ``@playwright``).
    """
    for candidate in (
        package / _NODE_MODULES / _PLAYWRIGHT_CORE_PKG / _BROWSERS_MANIFEST,
        package.parent.parent / _PLAYWRIGHT_CORE_PKG / _BROWSERS_MANIFEST,
    ):
        if candidate.is_file():
            return candidate
    return None


def _browsers_manifest_path() -> Path | None:
    """Locate the installed ``playwright-core/browsers.json``, or ``None``.

    Resolution is anchored on the ``@playwright/cli`` package
    (:func:`_cli_package_dirs`) rather than walked up from the launcher toward
    the filesystem root. The anchor is the correctness property, not a
    shortcut: an unbounded walk passes through ``$HOME`` on the standalone
    layout, where a single unrelated ``~/node_modules/playwright-core`` would
    supply a revision from a DIFFERENT install. That reports a **working**
    browser broken, and keeps reporting it after the download the panel offers,
    because the gate goes on reading the foreign manifest. Requiring the
    manifest to be served to the CLI package makes a foreign tree unreachable.

    ``None`` when no manifest can be attributed to a CLI package, which callers
    treat as "revision unknown" and answer with the documented presence-only
    fallback.
    """
    for package in _cli_package_dirs():
        manifest = _manifest_for_cli_package(package)
        if manifest is not None:
            return manifest
    return None


def _required_revisions() -> dict[str, str] | None:
    """Required revision per engine, read from ``browsers.json``, or ``None``.

    ``None`` means the required revision cannot be determined -- the manifest is
    absent, unreadable, or not the shape this expects. Callers treat that as
    "cannot confirm a revision" and fall back to the older presence-only check
    rather than turning a working browser into a reported-broken one.

    Keyed by the manifest's own engine names (``chromium``,
    ``chromium-headless-shell``, ``firefox``, ``webkit``, ...). Only entries with
    a string ``name`` and ``revision`` are kept, so a malformed row is skipped
    rather than crashing the read.
    """
    path = _browsers_manifest_path()
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    browsers = data.get("browsers") if isinstance(data, dict) else None
    if not isinstance(browsers, list):
        return None
    revisions: dict[str, str] = {}
    for entry in browsers:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        revision = entry.get("revision")
        if isinstance(name, str) and isinstance(revision, str):
            revisions[name] = revision
    return revisions or None


def _cache_dir_name_for(engine: str, revision: str) -> str:
    """The cache directory name that satisfies *engine* at *revision*.

    Playwright names the directory ``<engine>-<revision>`` (``chromium-1232``).
    One name, not a set: the only caller passes engines from
    :data:`BROWSER_ENGINES`, none of which contains a hyphen, so an underscore
    variant of the same name could never match anything.
    """
    return f"{engine}-{revision}"


def browsers_present() -> dict[str, bool]:
    """Which engines have a build for the REVISION the installed CLI needs.

    Reported per engine rather than as one boolean so the panel can offer each
    download separately: a user who wants to check a page in Firefox should not
    have to discover that "browser installed" only ever meant Chromium.

    A cache dir carries the revision (``chromium-1232``), and playwright-core
    launches only the exact revision bound to its own version. A prefix match
    (``name.startswith(engine)``) ignores that revision, so a stale
    ``chromium-1208`` left over from before a CLI upgrade reads as present while
    the launch fails ``Browser "chromium" is not installed`` -- and because the
    gate reads ready, the panel never offers the download that would fix it. So
    ``browsers.json`` supplies the required revision and the match is exact.

    Degradation: when the required revision cannot be determined (manifest
    absent/unreadable -- see :func:`_required_revisions`), fall back to the older
    prefix match rather than reporting a browser broken on missing metadata. A
    missing manifest is an unknown, not evidence of a stale cache.
    """
    names = _cached_browser_names()
    if names is None:
        return {engine: False for engine in BROWSER_ENGINES}
    required = _required_revisions()
    if required is None:
        # Cannot confirm a revision: preserve the historical presence-only
        # behaviour rather than failing closed on absent metadata.
        return {
            engine: any(name.startswith(engine) for name in names) for engine in BROWSER_ENGINES
        }
    result: dict[str, bool] = {}
    for engine in BROWSER_ENGINES:
        revision = required.get(engine)
        if revision is None:
            # The engine is not in the manifest at all: we cannot say which
            # revision it needs, so degrade to presence-only for this one engine.
            result[engine] = any(name.startswith(engine) for name in names)
            continue
        wanted = _cache_dir_name_for(engine, revision)
        result[engine] = wanted in names
    return result


def _browser_present() -> bool:
    """Whether a downloaded Chromium build exists in Playwright's cache.

    Chromium only, and that narrowness is the point: it is the engine
    ``attach``/``--extension`` supports, so a cache holding solely Firefox or
    WebKit does not make the capability work. This stays the single
    ``browser_ok`` capability gate even though `browsers_present` reports all
    three, because the other two are extras rather than prerequisites.
    """
    return browsers_present().get("chromium", False)


_INSTALLER_BASE = "https://raw.githubusercontent.com/kirodotdev/KiroCrew/main"


def _standalone_install_command() -> str:
    """The command that installs the CLI where `npm install -g` cannot.

    `playwright-cli.sh` / `.ps1` bootstrap their own Node into the user's home
    directory and classify the enterprise failures npm reports as one
    undifferentiated error, so they are the answer for the two states this
    module can detect but not fix: no usable Node, and a registry that refuses
    the request.

    Download-then-run rather than piping into a shell, because a machine locked
    down enough to need this is usually also one where piping a script from the
    network into `sh` is forbidden -- and because it is the form that lets the
    operator read what they are about to run.

    The PowerShell form wraps the download in try/catch and exits on failure.
    Downloaded into a FRESH TEMPORARY path, never the working directory under a
    fixed name. The operator pastes this into whatever shell they happen to have
    open, so the destination is a directory this command does not own: a file
    already named `playwright-cli.sh` there -- their own copy, mid-edit, or
    something unrelated -- would be truncated by the download. An unpredictable
    name also retires the planted-file hazard the chaining below guards against.

    `;` in PowerShell is a statement SEPARATOR, not `&&`: a failed download
    otherwise falls straight through to the `powershell -File` call and runs
    whatever is at that path. `-ErrorAction Stop` alone does NOT prevent this;
    measured on pwsh 7.6, the following statement still runs, because the
    terminating error ends the pipeline rather than the command. `&&` would be the
    obvious fix and is unavailable: this command targets Windows PowerShell 5.1,
    which has no `&&`.
    """
    if os.name == "nt":
        return (
            '$p = Join-Path $env:TEMP "playwright-cli-$([guid]::NewGuid()).ps1"; '
            f"try {{ irm {_INSTALLER_BASE}/playwright-cli.ps1 "
            "-OutFile $p -ErrorAction Stop } "
            "catch { Write-Error $_; exit 1 }; "
            "powershell -ExecutionPolicy Bypass -File $p"
        )
    # `&&` gives the shell form the same guarantee for free.
    # `_pwcli_dir`, not `d`: this runs at the top level of whatever interactive
    # shell the operator pasted it into, so the assignment persists in THEIR
    # session. `d` is a common scratch name and overwriting it silently is the
    # same discourtesy as writing playwright-cli.sh over their file. The directory
    # is left in place -- /tmp is reaped by the OS, and putting an `rm -rf` into a
    # string users are invited to edit is a worse trade than a stale temp dir.
    return (
        "_pwcli_dir=$(mktemp -d) && curl -fsSL "
        f'{_INSTALLER_BASE}/playwright-cli.sh -o "$_pwcli_dir/playwright-cli.sh" '
        '&& sh "$_pwcli_dir/playwright-cli.sh"'
    )


def detect() -> dict[str, Any]:
    """Report what is installed, without changing anything.

    ``installed`` describes the CLI binary alone. It is intentionally
    independent of ``node_ok`` and ``browser_ok`` so a caller can tell "not
    installed" apart from "installed but unusable here", which are different
    problems with different fixes.
    """
    path = cli_path()
    node_version = _node_version()
    major = _node_major(node_version)
    cli_version: str | None = None
    if path is not None:
        rc, out, err = _run([path, "--version"], _PROBE_TIMEOUT_S)
        if rc == 0:
            cli_version = _first_version(out)
        else:
            logger.debug("%s --version failed (rc=%d): %s", CLI_BIN, rc, err.strip())
    return {
        "installed": path is not None,
        "cli_path": path,
        "cli_version": cli_version,
        "node_ok": major is not None and major >= MIN_NODE_MAJOR,
        "node_version": node_version,
        "browser_ok": _browser_present(),
        # Per-engine, so the panel can offer each download rather than
        # implying "browser" means only the one attach needs.
        "browsers": browsers_present(),
        # What to run when THIS install cannot proceed. Composed here rather than
        # in the dashboard for three reasons: only the gateway knows which OS it
        # runs on, so the operator gets one correct command instead of two to
        # choose between; a shell command is not translatable copy and must not
        # enter the i18n catalogs, whose pseudolocale accents every Latin
        # character and would corrupt it; and the frontend's untranslated-literal
        # gate forbids holding it as a string there.
        "standalone_install": _standalone_install_command(),
    }


def available() -> bool:
    """Whether the browse capability exists on this host.

    **This is the consent gate.** Presence of the binary is consent regardless
    of who installed it — see the module docstring for why no additional gate is
    possible. Node is not consulted: a host with the CLI installed and Node
    broken has granted the capability and has a repairable environment, and
    reporting that as "not consented" would send the operator to the wrong fix.
    """
    return cli_path() is not None


# A failing npm run can emit a very large log; the operator needs the head of it,
# not megabytes in a log line and a dashboard card.
_STDERR_CAP = 2000


# npm-specific credential shapes. MEASURED: the shared `redact_credentials` only
# matches header-style secrets (`Authorization: Bearer ...`) and leaves every form
# npm actually emits intact -- the registry query (`?_authToken=`), the .npmrc line
# (`//host/:_authToken=`), an inline-credential proxy URL, and `*_TOKEN=` env echo.
# Scoped here rather than added to the shared helper: this is the one surface that
# emits npm output, and widening a security primitive every caller depends on is a
# change that deserves its own review.
_NPM_SECRET_RES = (
    re.compile(r"(_authToken\s*=\s*)[^\s&]+", re.I),
    re.compile(r"(_password\s*=\s*)[^\s&]+", re.I),
    # Bounded prefix ({0,40}), not `*`: an unbounded run before a required
    # keyword backtracks catastrophically on a large log -- MEASURED as a 120s
    # timeout on 50 KB of stderr, which would have hung the install task on a
    # real npm failure, not merely slowed a test.
    re.compile(r"([A-Z0-9_]{0,40}(?:TOKEN|SECRET|PASSWORD|APIKEY|API_KEY)\s*=\s*)[^\s&]+", re.I),
    # scheme://user:secret@host -- keep the user, drop the secret.
    re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"),
)


def _redact(text: str) -> str:
    """Redact credential-shaped content before it reaches a log or the dashboard.

    Runs the shared two-pass used on every external surface, then the npm shapes
    that pass leaves untouched (see :data:`_NPM_SECRET_RES`).
    """
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    for pattern in _NPM_SECRET_RES:
        # The last pattern has a trailing group (the `@`); the rest have one.
        text = pattern.sub(
            lambda m: m.group(1) + "[REDACTED]" + (m.group(m.re.groups) if m.re.groups > 1 else ""),
            text,
        )
    return text


def _step(
    name: str,
    argv: list[str],
    timeout: float,
    hint: str = "",
    failure_signal: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Run one install step and describe its outcome.

    stderr is carried only on failure: a successful ``npm install`` writes
    progress and deprecation notices there, and surfacing those to an operator
    reads as a broken install.

    It is redacted HERE, at the source, rather than only where the dashboard
    renders it. npm quotes the command's own environment back on failure -- a
    registry line carrying ``_authToken=``, a proxy URL with inline credentials --
    and the log is the longer-lived of the two surfaces: `kirocrew logs` output
    gets pasted into bug reports. Redacting at the boundary would have left the
    secret in the log file, which is the copy that outlives the session.

    *failure_signal* inspects the child's output for a failure the exit code does
    NOT report. A zero exit is otherwise taken at face value, which is wrong for
    exactly one case -- see :func:`os_deps.host_deps_unsatisfied`.

    *hint* is our own trusted remediation line, appended AFTER the cap so a long
    stderr cannot push the actionable part out of the operator's view. It is not
    redacted because it is a constant composed here, never external output.
    """
    rc, out, err = _run(argv, timeout)
    ok = rc == 0
    # Both streams: the diagnostic is on stderr today, and a step that starts
    # printing it to stdout must not silently reopen the bug this guards.
    if ok and failure_signal is not None and failure_signal(f"{err}\n{out}"):
        ok = False
    # Redact BEFORE truncating: a credential straddling the truncation
    # boundary no longer matches its regex (e.g. the trailing ``@`` in a
    # ``://user:pass@host`` URL is past the cap), so truncating first can
    # leak partial secrets. The npm-specific patterns use bounded
    # repetition (``{0,40}``) and the shared credential regex is a fixed
    # alternation with no nested quantifiers, so redacting the full
    # stderr is linear in input length — measured at <200 ms on 50 KB of
    # adversarial input, well below the subprocess timeout.
    detail = "" if ok else _redact((err.strip() or out.strip()))[:_STDERR_CAP]
    if not ok:
        logger.warning("playwright-cli install step %s failed (rc=%d): %s", name, rc, detail)
        if hint:
            detail = f"{detail}\n\n{hint}" if detail else hint
    return {
        "name": name,
        "ok": ok,
        "returncode": rc,
        "stderr": detail,
    }


def _download_browser(path: str, engine: str | None = None) -> list[dict[str, Any]]:
    """Download a browser build, adapting to what this host's OS allows.

    Shared by :func:`install` and :func:`install_browser` so the two cannot
    disagree about a host: they answer different product questions but face the
    same package manager.

    ``--with-deps`` is passed only where Playwright can honour it (see
    :mod:`kiro_crew.browser_cli.os_deps`), and even there a refusal is not fatal.
    Installing OS packages needs root, a managed workstation often withholds it,
    and the download itself needs no privilege at all -- so the flag is dropped
    and the download retried rather than losing the browser over a permission the
    operator may never have. Returns every attempt, so the panel shows what was
    tried instead of only the last verdict.

    Every attempt is judged on its output as well as its exit code: a build whose
    libraries are missing downloads "successfully" and cannot launch.
    """
    base = [path, "install-browser"] + ([engine] if engine else [])
    suffix = f"-{engine}" if engine else ""
    hint = os_deps.missing_deps_hint()

    def attempt(step_name: str, argv: list[str], with_hint: bool) -> dict[str, Any]:
        return _step(
            step_name,
            argv,
            _BROWSER_INSTALL_TIMEOUT_S,
            hint=hint if with_hint else "",
            failure_signal=os_deps.host_deps_unsatisfied,
        )

    if not os_deps.with_deps_supported():
        return [attempt(f"install-browser{suffix}", base, True)]

    first = attempt(f"install-browser{suffix}", base + ["--with-deps"], False)
    if first["ok"]:
        return [first]
    return [first, attempt(f"install-browser{suffix}-no-deps", base, True)]


def install() -> dict[str, Any]:
    """Install the CLI, a browser, and the skills reference.

    Steps run in order and stop at the first failure, because each one depends
    on its predecessor: the browser download is driven by the binary the first
    step installs. The result carries every step attempted so an operator sees
    which one failed rather than only that something did.

    The browser step adapts to the host's package manager; see
    :func:`_download_browser`.
    """
    steps: list[dict[str, Any]] = []

    npm = find_node_tool("npm")
    if npm is None:
        steps.append(
            {
                "name": "npm-install-global",
                "ok": False,
                "returncode": 127,
                "stderr": "npm not found; install Node.js 20 or newer first",
            }
        )
        return {"ok": False, "steps": steps}

    steps.append(
        _step("npm-install-global", [npm, "install", "-g", NPM_SPEC], _NPM_INSTALL_TIMEOUT_S)
    )
    if not steps[-1]["ok"]:
        return {"ok": False, "steps": steps}

    # Resolved after the global install, not before: the binary does not exist
    # until that step succeeds.
    path = cli_path()
    if path is None:
        steps.append(
            {
                "name": "resolve-binary",
                "ok": False,
                "returncode": 127,
                "stderr": f"{CLI_BIN} not found on PATH after a successful global install",
            }
        )
        return {"ok": False, "steps": steps}

    steps.extend(_download_browser(path))
    if not steps[-1]["ok"]:
        return {"ok": False, "steps": steps}

    steps.append(
        _step(
            "install-skills",
            [path, "install", "--skills", _SKILLS_TARGET, "--global"],
            _SKILLS_INSTALL_TIMEOUT_S,
        )
    )
    # The LAST step decides, not every step: a recovered ``--with-deps`` refusal
    # leaves its failed attempt in the list for the operator to see, and that
    # entry must not veto an install the retry actually completed. Every earlier
    # gate has already returned on a real failure, so only this step is undecided.
    return {"ok": steps[-1]["ok"], "steps": steps}


def install_browser(engine: str) -> dict[str, Any]:
    """Download one engine's browser build.

    Separate from :func:`install` because the two answer different questions.
    ``install`` is "make browsing work at all" and downloads only the engine
    ``attach`` needs; this is "I also want to check this page in Firefox", which
    is a later, optional choice the old Browser Mode panel exposed as an engine
    selector and which would otherwise have no surface at all.

    *engine* is validated against :data:`BROWSER_ENGINES` before it can reach
    argv. That check is what keeps this spawn benign (fixed argv, no free input)
    rather than an agent-influenced one -- see ``test_spawn_audit``.
    """
    if engine not in BROWSER_ENGINES:
        return {
            "ok": False,
            "steps": [
                {
                    "name": "install-browser",
                    "ok": False,
                    "returncode": 2,
                    "stderr": f"unknown engine {engine!r}; expected one of {BROWSER_ENGINES}",
                }
            ],
        }
    path = cli_path()
    if path is None:
        return {
            "ok": False,
            "steps": [
                {
                    "name": "resolve-binary",
                    "ok": False,
                    "returncode": 127,
                    "stderr": f"{CLI_BIN} not found on PATH; install the CLI first",
                }
            ],
        }
    steps = _download_browser(path, engine)
    return {"ok": steps[-1]["ok"], "steps": steps}
