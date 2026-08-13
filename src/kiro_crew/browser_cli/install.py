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

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
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
    """
    env = dict(os.environ)
    env["PATH"] = node_augmented_path(env.get("PATH", ""))
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


def browsers_present() -> dict[str, bool]:
    """Which engines have a downloaded build, keyed by engine name.

    Reported per engine rather than as one boolean so the panel can offer each
    download separately: a user who wants to check a page in Firefox should not
    have to discover that "browser installed" only ever meant Chromium.
    """
    names = _cached_browser_names()
    if names is None:
        return {engine: False for engine in BROWSER_ENGINES}
    return {engine: any(name.startswith(engine) for name in names) for engine in BROWSER_ENGINES}


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
    return (
        "d=$(mktemp -d) && curl -fsSL "
        f'{_INSTALLER_BASE}/playwright-cli.sh -o "$d/playwright-cli.sh" '
        '&& sh "$d/playwright-cli.sh"'
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


def _step(name: str, argv: list[str], timeout: float) -> dict[str, Any]:
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
    """
    rc, _out, err = _run(argv, timeout)
    ok = rc == 0
    # Redact BEFORE truncating: a credential straddling the truncation
    # boundary no longer matches its regex (e.g. the trailing ``@`` in a
    # ``://user:pass@host`` URL is past the cap), so truncating first can
    # leak partial secrets. The npm-specific patterns use bounded
    # repetition (``{0,40}``) and the shared credential regex is a fixed
    # alternation with no nested quantifiers, so redacting the full
    # stderr is linear in input length — measured at <200 ms on 50 KB of
    # adversarial input, well below the subprocess timeout.
    detail = "" if ok else _redact(err.strip())[:_STDERR_CAP]
    if not ok:
        logger.warning("playwright-cli install step %s failed (rc=%d): %s", name, rc, detail)
    return {
        "name": name,
        "ok": ok,
        "returncode": rc,
        "stderr": detail,
    }


def install() -> dict[str, Any]:
    """Install the CLI, a browser, and the skills reference.

    Steps run in order and stop at the first failure, because each one depends
    on its predecessor: the browser download is driven by the binary the first
    step installs. The result carries every step attempted so an operator sees
    which one failed rather than only that something did.

    ``--with-deps`` is Linux-only. It installs OS packages through the system
    package manager, which needs privileges and has no meaning on macOS or
    Windows, where the browser download alone is sufficient.
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

    browser_argv = [path, "install-browser"]
    if platform_compat.IS_LINUX:
        browser_argv.append("--with-deps")
    steps.append(_step("install-browser", browser_argv, _BROWSER_INSTALL_TIMEOUT_S))
    if not steps[-1]["ok"]:
        return {"ok": False, "steps": steps}

    steps.append(
        _step(
            "install-skills",
            [path, "install", "--skills", _SKILLS_TARGET, "--global"],
            _SKILLS_INSTALL_TIMEOUT_S,
        )
    )
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


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
    argv = [path, "install-browser", engine]
    if platform_compat.IS_LINUX:
        argv.append("--with-deps")
    step = _step(f"install-browser-{engine}", argv, _BROWSER_INSTALL_TIMEOUT_S)
    return {"ok": step["ok"], "steps": [step]}
