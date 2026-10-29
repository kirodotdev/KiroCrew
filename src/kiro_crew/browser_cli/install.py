"""Detection, installation, and the capability gate for ``@playwright/cli``.

The CLI has no capability gating of its own — every command is available to
whoever can run the binary — so :func:`available` reports host capability only.
It is not an approval decision: dashboard shell turns still follow the ordinary
approval ladder unless the user has granted trust or enabled auto-approve.

Installation is global within a product-owned npm prefix rather than ``npx``.
``npx`` re-resolves the package through the registry on every invocation, so an
expired registry token would take browsing down at use time. The managed prefix
is ``<data-home>/playwright-cli``. Every agent sandbox exposes that leaf
read-only, and gateway execution resolves it by absolute path before considering
fixed, non-writable system locations. ``PATH`` is never an execution source.

Node is located through :func:`kiro_crew.env.find_node_tool` rather than bare
``shutil.which``: the gateway can run with a PATH that omits the version-manager
shim directory a managed npm install needs at execution time.

Every function here blocks (subprocess, filesystem), so a caller on the event
loop offloads it.
"""

from __future__ import annotations

import contextlib
import enum
import importlib as _importlib
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys as _sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import github_runner, platform_compat
from kiro_crew.browser_cli import os_deps
from kiro_crew.browser_cli.install_browsers import (
    _DEFAULT_BROWSER_ENGINE,
    BROWSER_ENGINES,
    _browser_present,
    browser_status,
    browsers_present,
)
from kiro_crew.browser_cli.install_launcher import (
    _CLI_PKG_NAME,
    _CLI_PKG_SCOPE,
    CLI_BIN,
    _cli_package_for_launcher,
    _data_home_overlap_remedy,
    _first_version,
    _managed_leaf_unresolved_reason,
    _node_major,
    _node_runtime_executable,
    _node_version,
    _pinned_managed_cli_root,
    _resolve_system_cli,
    _stage_managed_node,
    agent_writable_root_over_data_home,
    bootstrap_tool_provenance,
    cli_command,
    cli_path,
    staged_node_failure,
)
from kiro_crew.browser_cli.install_runner import (
    _BROWSER_INSTALL_TIMEOUT_S,
    _CLI_PACKAGE_JSON_MAX_BYTES,
    _NPM_INSTALL_TIMEOUT_S,
    _SKILLS_INSTALL_TIMEOUT_S,
    _STDERR_CAP,
    INTERRUPTED_RC,
    TIMEOUT_RC,
    _run,
    redact_install_output,
)
from kiro_crew.browser_cli.managed_launcher import ensure_managed_launcher
from kiro_crew.config.paths import config_dir
from kiro_crew.env import augmented_path, find_node_tool, node_augmented_path
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger("kiro_crew.browser_cli.install")

# The CLI's own floor. Node 19 and older lack APIs its bundle uses, so a lower
# version does not fail at install — it fails at first browse with an opaque
# stack, which is why detection rejects it up front rather than letting install
# "succeed" into a broken state.
MIN_NODE_MAJOR = 20

# Verified on September 19, 2026, with ``@playwright/cli@0.1.21``. These launches:
# ``npx --yes @playwright/cli@0.1.21 show --port 45613 --host 127.0.0.1``
# ``npx --yes @playwright/cli@0.1.21 show --port 0 --host 127.0.0.1``
# printed ``Listening on http://127.0.0.1:45613`` and
# ``Listening on http://127.0.0.1:42963``, respectively. Each line was read only
# after its listener had bound. ``@latest`` may change that wording; the parser
# then fails closed and reports the installed version for diagnosis, and the
# daily ``playwright-cli-banner.yml`` workflow catches the drift first.
NPM_SPEC = "@playwright/cli@latest"

# ``install --skills`` writes the command reference where an agent can read it.
# ``agents`` is the agent-neutral target (the default, ``claude``, writes a
# Claude-specific layout) and ``--global`` puts it in the home directory so it
# is found from any working directory rather than only inside one workspace.
_SKILLS_TARGET = "agents"


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


def _cli_package_json_for_command(command: list[str] | None) -> Path | None:
    """Package metadata served by *command*, or the vetted launcher fallback."""
    if command is not None and len(command) >= 2:
        try:
            entry = Path(command[1]).resolve(strict=True)
        except OSError:
            entry = None
        if (
            entry is not None
            and entry.name == "playwright-cli.js"
            and entry.parent.name == _CLI_PKG_NAME
            and entry.parent.parent.name == _CLI_PKG_SCOPE
        ):
            return entry.parent / "package.json"
    path = cli_path()
    if path is None:
        return None
    package = _cli_package_for_launcher(Path(path))
    return package / "package.json" if package is not None else None


def installed_cli_version(command: list[str] | None = None) -> str | None:
    """Return the attributed package version without spawning the CLI."""
    manifest = _cli_package_json_for_command(command)
    if manifest is None:
        return None
    try:
        info = manifest.stat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size <= 0
        or info.st_size > _CLI_PACKAGE_JSON_MAX_BYTES
    ):
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    return _first_version(version) if isinstance(version, str) else None


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
    command = cli_command(path) if path is not None else None
    cli_version = installed_cli_version(command)
    status = browser_status()
    return {
        "installed": command is not None,
        "cli_path": path if command is not None else None,
        "cli_version": cli_version,
        "node_ok": major is not None and major >= MIN_NODE_MAJOR,
        "node_version": node_version,
        "browser_ok": _browser_present(status),
        # Per-engine, so the panel can offer each download rather than
        # implying "browser" means only the one attach needs. ``browsers`` is
        # the boolean projection older dashboards read; ``browser_status``
        # keeps "could not read" apart from "not downloaded".
        "browsers": browsers_present(status),
        "browser_status": status,
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

    Presence is an availability signal, not consent to skip shell approval.
    Node is not consulted: a host with the CLI installed and Node broken has a
    repairable environment, and reporting that as absent would send the operator
    to the wrong fix.
    """
    return cli_path() is not None


def _step(
    name: str,
    argv: list[str],
    timeout: float,
    hint: str = "",
    failure_signal: Callable[[str], bool] | None = None,
    hint_requires_signal: bool = False,
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
    redacted because it is a constant composed here, never external output. A
    remedy rides on every ordinary failure; with *hint_requires_signal* it rides
    only on a failure *failure_signal* recognized, which is the rule for a verdict
    about the host (the engine cannot run here): the missing-library report is
    its only evidence, and a download that failed for another reason must keep
    its own error rather than be dressed as a platform limit.
    """
    rc, out, err = _run(argv, timeout)
    # Both streams: the diagnostic is on stderr today, and a step that starts
    # printing it to stdout must not silently reopen the bug this guards.
    signalled = failure_signal is not None and failure_signal(f"{err}\n{out}")
    ok = rc == 0 and not signalled
    # Redact BEFORE truncating: a credential straddling the truncation
    # boundary does not match its regex (e.g. the trailing ``@`` in a
    # ``://user:pass@host`` URL is past the cap), so truncating first can
    # leak partial secrets. The npm-specific patterns use bounded
    # repetition (``{0,40}``) and the shared credential regex is a fixed
    # alternation with no nested quantifiers, so redacting the full
    # stderr is linear in input length — measured at <200 ms on 50 KB of
    # adversarial input, well below the subprocess timeout.
    detail = "" if ok else redact_install_output((err.strip() or out.strip()))[-_STDERR_CAP:]
    # The hint names missing OS libraries. A step the gateway interrupted or that
    # ran out of time did not fail for that reason, so it carries no hint; the
    # job layer reads the same return codes to report interrupted / timeout.
    ordinary_failure = not ok and rc not in (INTERRUPTED_RC, TIMEOUT_RC)
    hint_applies = ordinary_failure and (signalled or not hint_requires_signal)
    step_hint = hint if hint_applies else ""
    if not ok:
        logger.warning("playwright-cli install step %s failed (rc=%d): %s", name, rc, detail)
        if step_hint:
            detail = f"{detail}\n\n{step_hint}" if detail else step_hint
    return {
        "name": name,
        "ok": ok,
        "returncode": rc,
        "stderr": detail,
        "hint": step_hint,
    }


#: Stage names reported through ``on_stage``, in the order :func:`install` runs
#: them. The dashboard's install job publishes ``preparing`` itself before the
#: worker starts, so the installer never reports it.
STAGE_INSTALLING_CLI = "installing_cli"
STAGE_DOWNLOADING_BROWSER = "downloading_browser"
STAGE_INSTALLING_SKILLS = "installing_skills"
STAGE_FINISHING = "finishing"

StageCallback = Callable[[str], None]


def _emit_stage(on_stage: StageCallback | None, stage: str) -> None:
    """Report *stage*; a failing callback is logged, never allowed to stop an install."""
    if on_stage is None:
        return
    try:
        on_stage(stage)
    except Exception:  # noqa: BLE001 - progress reporting must not fail the install
        logger.debug("browser install stage callback failed for %s", stage, exc_info=True)


def _download_browser(command: list[str], engine: str | None = None) -> list[dict[str, Any]]:
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
    tried instead of only the last verdict. The engine-aware remedy rides only on
    the attempt without the flag: it is the one a human has to act on.

    Every attempt is judged on its output as well as its exit code: a build whose
    libraries are missing downloads "successfully" and cannot launch.
    """
    selected_engine = engine or _DEFAULT_BROWSER_ENGINE
    base = [*command, "install-browser", selected_engine]
    # Keep baseline step names stable for the dashboard; optional engine
    # downloads name their engine so outcomes remain distinguishable.
    suffix = f"-{engine}" if engine else ""
    hint = os_deps.missing_deps_hint(selected_engine)
    # A remedy ("install these, then retry") rides on any ordinary failure. The
    # verdict that this host cannot run the engine does not: its only evidence is
    # Playwright's own report of the missing libraries, so a WebKit download that
    # fails for another reason on such a host is reported as that failure, not as
    # a platform limit.
    verdict = os_deps.engine_unsupported_here(selected_engine)

    def attempt(step_name: str, argv: list[str], with_hint: bool) -> dict[str, Any]:
        return _step(
            step_name,
            argv,
            _BROWSER_INSTALL_TIMEOUT_S,
            hint=hint if with_hint else "",
            failure_signal=os_deps.host_deps_unsatisfied,
            hint_requires_signal=verdict,
        )

    if not os_deps.with_deps_supported():
        return [attempt(f"install-browser{suffix}", base, True)]

    first = attempt(f"install-browser{suffix}", base + ["--with-deps"], False)
    if first["ok"]:
        return [first]
    return [first, attempt(f"install-browser{suffix}-no-deps", base, True)]


def _log_bootstrap_tools(npm: str | None, node: str | None) -> None:
    """Log which npm and Node the install bootstrap is about to run.

    These come from the gateway's PATH and version-manager directories, the one
    place the browser install still reads that environment, so the resolved
    paths -- and whether the gateway user could have replaced them -- are
    recorded for whoever later asks what ran. ``kirocrew doctor`` reports the
    same answer.
    """
    for name, path in (("npm", npm), ("node", node)):
        if path is None:
            logger.info("browser install bootstrap: %s not found", name)
            continue
        info = bootstrap_tool_provenance(path)
        logger.info(
            "browser install bootstrap: %s=%s (real %s, user-writable at=%s, "
            "own toolchain=%s, group/other-writable=%s)",
            name,
            path,
            info["real_path"],
            info["writable_at"] if info["writable_at"] is not None else "not checked",
            info["own_toolchain"],
            info["shared_write"],
        )


def install(on_stage: StageCallback | None = None) -> dict[str, Any]:
    """Install the CLI, a browser, and the skills reference.

    Steps run in order and stop at the first failure, because each one depends
    on its predecessor: the browser download is driven by the binary the first
    step installs. The result carries every step attempted so an operator sees
    which one failed rather than only that something did.

    *on_stage* is called with each :data:`STAGE_INSTALLING_CLI` ..
    :data:`STAGE_FINISHING` transition as it happens, on the calling thread.
    """
    steps: list[dict[str, Any]] = []
    _emit_stage(on_stage, STAGE_INSTALLING_CLI)

    overlap = agent_writable_root_over_data_home()
    if overlap is not None and _resolve_system_cli()[0] is None:
        steps.append(
            {
                "name": "check-install-boundary",
                "ok": False,
                "returncode": 1,
                "stderr": redact_install_output(
                    f"refusing to install {CLI_BIN}: {_data_home_overlap_remedy(overlap)}"
                ),
            }
        )
        return {"ok": False, "steps": steps}

    # Both bootstrap tools are resolved ONCE, here, and the same answers are
    # what runs: the gateway PATH and version-manager dirs are not re-read
    # between the npm step and the Node staging step.
    npm = find_node_tool("npm")
    node = find_node_tool("node")
    _log_bootstrap_tools(npm, node)
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

    try:
        with _pinned_managed_cli_root() as managed_root:
            steps.append(
                _step(
                    "npm-install-global",
                    [
                        npm,
                        "install",
                        "-g",
                        "--prefix",
                        str(managed_root),
                        NPM_SPEC,
                    ],
                    _NPM_INSTALL_TIMEOUT_S,
                )
            )
    except OSError as exc:
        steps.append(
            {
                "name": "npm-install-global",
                "ok": False,
                "returncode": 127,
                "stderr": f"refusing unsafe managed CLI prefix: {exc}",
            }
        )
        return {"ok": False, "steps": steps}
    if not steps[-1]["ok"]:
        return {"ok": False, "steps": steps}

    try:
        if node is None:
            raise OSError("node not found on the gateway PATH or version-manager dirs")
        runtime_node = _node_runtime_executable(node)
        if runtime_node is None:
            raise OSError("Node did not report an executable process.execPath")
        staged_node = _stage_managed_node(runtime_node)
    except OSError as exc:
        steps.append(
            {
                "name": "stage-node",
                "ok": False,
                "returncode": 127,
                "stderr": f"could not stage Node for direct gateway execution: {exc}",
            }
        )
        return {"ok": False, "steps": steps}
    steps.append(
        {
            "name": "stage-node",
            "ok": True,
            "returncode": 0,
            "stderr": "",
            "path": str(staged_node),
        }
    )
    # The agent-facing launcher (``managed-bin``), which puts ``attach`` under its
    # time limit. Written after the staged Node it names; a failure is logged and
    # leaves npm's own shim in use, and the next gateway start retries it.
    ensure_managed_launcher()

    # Resolved after the global install, not before: the binary does not exist
    # until that step succeeds.
    path = cli_path()
    if path is None:
        steps.append(
            {
                "name": "resolve-binary",
                "ok": False,
                "returncode": 127,
                "stderr": _managed_leaf_unresolved_reason(),
            }
        )
        return {"ok": False, "steps": steps}

    command = cli_command(path)
    if command is None:
        steps.append(
            {
                "name": "resolve-binary",
                "ok": False,
                "returncode": 127,
                "stderr": (
                    f"{CLI_BIN} could not be bound to the staged Node and package entrypoint"
                ),
            }
        )
        return {"ok": False, "steps": steps}

    _emit_stage(on_stage, STAGE_DOWNLOADING_BROWSER)
    steps.extend(_download_browser(command))
    if not steps[-1]["ok"]:
        return {"ok": False, "steps": steps}

    _emit_stage(on_stage, STAGE_INSTALLING_SKILLS)
    steps.append(
        _step(
            "install-skills",
            [*command, "install", "--skills", _SKILLS_TARGET, "--global"],
            _SKILLS_INSTALL_TIMEOUT_S,
        )
    )
    _emit_stage(on_stage, STAGE_FINISHING)
    # The LAST step decides. Every earlier gate has already returned on a real
    # failure, so only this step is undecided.
    return {"ok": steps[-1]["ok"], "steps": steps}


def install_browser(engine: str, on_stage: StageCallback | None = None) -> dict[str, Any]:
    """Download one engine's browser build.

    Separate from :func:`install` because the two answer different questions.
    ``install`` is "make browsing work at all" and downloads only the engine
    ``attach`` needs; this is "I also want to check this page in Firefox", which
    is a later, optional choice the old Browser Mode panel exposed as an engine
    selector and which would otherwise have no surface at all.

    *engine* is validated against :data:`BROWSER_ENGINES` before it can reach
    argv. That check is what keeps this spawn benign (fixed argv, no free input)
    rather than an agent-influenced one -- see ``test_spawn_audit``.

    *on_stage* receives :data:`STAGE_DOWNLOADING_BROWSER` before the download
    and :data:`STAGE_FINISHING` after it, on the calling thread.
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
                    "stderr": f"no vetted {CLI_BIN} launcher is installed; install the CLI first",
                }
            ],
        }
    command = cli_command(path)
    if command is None:
        return {
            "ok": False,
            "steps": [
                {
                    "name": "resolve-binary",
                    "ok": False,
                    "returncode": 127,
                    "stderr": f"no safe direct {CLI_BIN} command is available; reinstall the CLI",
                }
            ],
        }
    # A gateway-node staged before stage-node ran its probe can be a copy that
    # cannot load its shared libraries; report it here, not as a loader crash
    # inside the download.
    node_failure = staged_node_failure(command)
    if node_failure is not None:
        node_rc, node_detail = node_failure
        return {
            "ok": False,
            "steps": [
                {
                    "name": "check-node",
                    "ok": False,
                    "returncode": node_rc,
                    "stderr": redact_install_output(node_detail),
                }
            ],
        }
    _emit_stage(on_stage, STAGE_DOWNLOADING_BROWSER)
    steps = _download_browser(command, engine)
    _emit_stage(on_stage, STAGE_FINISHING)
    return {"ok": steps[-1]["ok"], "steps": steps}


# This module keeps detection (detect, available, the installed CLI version, the standalone
# installer command) and the install steps (install, install_browser and the step runner they
# share). The owners below hold what those stand on: running a child and redacting its output
# (``install_runner``), the launcher, its Node and its package (``install_launcher``), the
# upstream seam verdicts (``install_seams``) and the browser cache state
# (``install_browsers``). Every name an owner defines still resolves as
# ``kiro_crew.browser_cli.install.<name>``, and a write through this module
# (``monkeypatch.setattr``, ``mock.patch``) reaches every owner holding the name
# (:mod:`kiro_crew._facade_mirror`). No owner imports this module; an owner imports only the
# owners listed before it. The owners log as ``kiro_crew.browser_cli.install``.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.browser_cli.install_runner",
    "kiro_crew.browser_cli.install_launcher",
    "kiro_crew.browser_cli.install_seams",
    "kiro_crew.browser_cli.install_browsers",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.browser_cli.install_browsers import (  # noqa: F401
        _BROWSERS_CACHE_ENV,
        _INSTALLATION_MARKER,
        STATUS_DOWNLOADED,
        STATUS_MISSING,
        STATUS_UNKNOWN,
        _browsers_cache_dir,
        _browsers_manifest_path,
        _cache_dir_name_for,
        _cached_browser_names,
        _default_cache_root,
        _download_complete,
        _has_complete_special_build,
        _is_special_build_name,
        _playwright_arch,
        _playwright_env,
        _playwright_host_platform,
        _required_revisions,
        _revision_overrides,
        _status_of,
    )
    from kiro_crew.browser_cli.install_launcher import (  # noqa: F401
        _BROWSERS_MANIFEST,
        _MANAGED_TOOLS_LEAF,
        _MISSING_LIBRARY_RE,
        _NODE_EXECUTABLE_PROBE,
        _NODE_MODULES,
        _PLAYWRIGHT_CORE_PKG,
        _REFUSAL_LEGACY,
        _REFUSAL_UNTRUSTED,
        _STANDALONE_PREFIX_ENV,
        _VERSION_RE,
        CLI_LEGACY_UNTRUSTED_REASON,
        CLI_NOT_INSTALLED_REASON,
        _agent_writable_roots,
        _cli_package_dirs,
        _common_candidate_rejection,
        _direct_cli_command,
        _gateway_writable_component,
        _launcher_node_modules,
        _managed_candidate,
        _managed_cli_candidates,
        _managed_cli_root,
        _managed_node_path,
        _manifest_for_cli_package,
        _probe_staged_node,
        _regular_file_within,
        _resolve_cli_candidate,
        _resolve_executable,
        _resolve_executable_file_for_system,
        _resolve_managed_cli,
        _staged_node_runs,
        _standalone_node_modules,
        _system_candidate,
        _system_cli_candidates,
        _system_node_candidates,
        _system_node_for_launcher,
        _under,
        _unrunnable_node_remedy,
        _warn_cli_refusal,
        _warned_cli_refusals,
        legacy_launcher_refused,
    )
    from kiro_crew.browser_cli.install_runner import (  # noqa: F401
        _INTERRUPTED_REASON,
        _NPM_SECRET_RES,
        _PROBE_TIMEOUT_S,
        _REAP_TIMEOUT_S,
        InstallScope,
        _collect_after_kill,
        _current_scope,
        _scope_local,
        cli_env,
        kill_cli_process_tree,
        run_in_scope,
    )
    from kiro_crew.browser_cli.install_seams import (  # noqa: F401
        _LIFECYCLE_SOURCE_MAX_BYTES,
        ATTRIBUTION_REMEDY,
        SeamSupport,
        _seams_support,
        _serving_core_root,
        _source_contains,
        cli_dashboard_socket_support,
        cli_lifecycle_env_support,
    )
