"""PPTX Maker — one-time provisioning of the vendored engine and app resources.

The presentation engine is a separate public OSS project, so it is not vendored
into this repository. It is fetched as a **digest-pinned tarball** into the app's
data dir on first use (see :mod:`.engine_source`), given its own uv-managed
virtualenv, and then the app's agent configs are rendered against the resulting
absolute paths.

**Nothing has to be installed by hand.** ``pip install kirocrew`` is the only
prerequisite: ``uv`` is a declared Python dependency and is resolved through the
installed package (:func:`resolve_uv`) rather than assumed to be on ``PATH``, and
the engine arrives over plain HTTPS, so ``git`` is no longer required at all.

Why this is a Python job and not a ``setup.onInstall`` shell script: a BUILTIN
app's source lives read-only inside the installed Python package, and the
platform only writes ``app.json``/``installed.json`` into
``~/.kiro/crew/apps/<name>/`` — it never stages a builtin's other files there. A
``setup.onInstall`` script therefore has nothing to run (the lifecycle runner
execs with that directory as its cwd), and manifest-declared ``agents``/``skills``
paths resolve to files that do not exist. This module closes that gap for this
app by staging its own resources into the install dir and then calling the
platform's OWN registrar (``bridges.register_app``) rather than re-implementing
agent symlinking or skill linking here.

Provisioning is user-triggered (``POST /engine/provision``) and idempotent, so
re-running it after an app update re-points the agents at the current engine.

Everything here is BLOCKING (network, uv, file copies) and runs on the subprocess
executor via ``routes.off_loop`` — never on the gateway event loop.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.apps.builtins.pptx_maker.backend import engine_source, paths
from kiro_crew.apps.manager import app_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, sandboxed_spawn_argv

logger = logging.getLogger("kirocrew.app.pptx-maker")

# The engine pin lives in `engine_source` (the module that verifies it). Re-exported
# here because this module's own API — and the routes/UI that read it — predate the
# split, and the pin is one concept regardless of which file holds the constant.
ENGINE_REPO = engine_source.ENGINE_REPO
ENGINE_TAG = engine_source.ENGINE_TAG
ENGINE_COMMIT = engine_source.ENGINE_COMMIT

# Timeouts (seconds). The dependency resolve is the slow leg — a cold `uv sync`
# builds wheels for the engine's native dependencies.
UV_SYNC_TIMEOUT = 900
UV_INSTALL_TIMEOUT = 300

# The `uv` executable name, per platform. `sysconfig`'s EXE is "" on POSIX and
# ".exe" on Windows, which is exactly the suffix the uv wheel's own locator uses.
_UV_BASENAME = "uv"

# Placeholders substituted into the shipped agent configs. The engine's absolute
# location is only known at provision time (it lives under the data home), so the
# configs ship as templates and are rendered here.
PLACEHOLDER_ENGINE_ROOT = "{ENGINE_ROOT}"
PLACEHOLDER_ENGINE_MCP_DIR = "{ENGINE_MCP_DIR}"
PLACEHOLDER_APP_PROMPTS = "{APP_PROMPTS}"

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_AGENTS_SUBDIR = "agents"
_PROMPTS_SUBDIR = "prompts"

# Max captured log characters handed back to the UI.
LOG_TAIL_CHARS = 4000


@dataclass
class ProvisionOutcome:
    """Result of one provisioning run."""

    ok: bool
    log: str
    engine_tag: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "log": self.log[-LOG_TAIL_CHARS:], "engineTag": self.engine_tag}


_uv_path_cache: str | None = None
_uv_path_resolved = False


def _frozen_bundle_dirs() -> list[str]:
    """Candidate directories for a bundled ``uv`` in a frozen build.

    PyInstaller's one-folder bundle has neither a scripts dir nor site-packages,
    so the uv wheel's own locator cannot find the binary there (it walks
    ``sysconfig`` paths only) — it raises ``UvNotFound``. ``packaging/
    kirocrew-backend.spec`` therefore stages the binary at the bundle root, which
    is ``sys._MEIPASS`` at runtime and, for a one-folder build, the directory
    holding ``sys.executable``. Both are checked because the two differ for a
    one-FILE build (``_MEIPASS`` is the extraction temp dir).
    """
    if not getattr(sys, "frozen", False):
        return []
    candidates = [getattr(sys, "_MEIPASS", ""), os.path.dirname(sys.executable or "")]
    return [c for c in candidates if c]


def resolve_uv() -> str | None:
    """Absolute path to a usable ``uv``, or ``None`` when genuinely absent.

    ``uv`` is a DECLARED Python dependency (``setup.cfg``), so a stock
    ``pip install kirocrew`` always has the binary — but not necessarily on
    ``PATH``: a wheel install puts it in the venv's scripts dir, and the gateway
    may run with a minimal ``PATH`` (an installed launchd/systemd service). So it
    is resolved through the INSTALLED PACKAGE rather than looked up by name.

    Order, widest-trust first:

    1. ``uv.find_uv_bin()`` — the wheel's own locator, the normal pip case. It
       raises ``UvNotFound`` (a ``FileNotFoundError`` subclass) when the binary
       is missing, e.g. an odd repackaging;
    2. the frozen-bundle location — the DMG/Electron install, where there is no
       scripts dir for the locator to walk (see :func:`_frozen_bundle_dirs`);
    3. ``shutil.which("uv")`` — a user's own, possibly newer, uv still works;
    4. ``None``.

    Never raises: an absent uv is a reportable condition, so the caller can fail
    with an actionable message instead of a traceback in a background job.

    Cached process-wide: this runs on every provision and the answer cannot
    change within a process (the interpreter's own site-packages and the frozen
    bundle are both fixed at startup).
    """
    global _uv_path_cache, _uv_path_resolved
    if _uv_path_resolved:
        return _uv_path_cache
    _uv_path_cache = _resolve_uv_uncached()
    _uv_path_resolved = True
    return _uv_path_cache


def _resolve_uv_uncached() -> str | None:
    """The resolution ladder itself. See :func:`resolve_uv`."""
    try:
        # Optional-dependency import (the `top-level-imports` carve-out): `uv` is a
        # declared dependency, but this must still answer on an install where the
        # wheel is absent or repackaged without its binary — a missing uv is a
        # reported "engine unavailable", never an ImportError at module load.
        import uv as uv_package

        found = uv_package.find_uv_bin()
        if found and os.path.isfile(found):
            return found
    except (ImportError, FileNotFoundError, OSError) as exc:
        logger.debug("pptx-maker: uv.find_uv_bin() did not resolve: %s", exc)

    executable = _UV_BASENAME + (sysconfig.get_config_var("EXE") or "")
    for directory in _frozen_bundle_dirs():
        candidate = os.path.join(directory, executable)
        if os.path.isfile(candidate):
            return candidate

    return shutil.which(_UV_BASENAME)


def reset_uv_cache() -> None:
    """Forget the resolved ``uv`` path (tests only)."""
    global _uv_path_cache, _uv_path_resolved
    _uv_path_cache = None
    _uv_path_resolved = False


def _run(argv: list[str], *, cwd: str, timeout: int) -> tuple[int, str]:
    """Run *argv* under the shared sandbox, returning ``(exit code, output)``.

    Uses the :func:`kiro_crew.sandbox.sandboxed_spawn_argv` chokepoint so the
    child gets a CREDENTIAL-SCRUBBED environment as well as the OS sandbox.
    Provisioning runs ``uv`` against a freshly-downloaded third-party tree, so
    these spawns execute third-party code and must not inherit the gateway's raw
    ``os.environ`` (cloud credentials, ``SSH_AUTH_SOCK``, bot tokens).

    ``argv[0]`` is an ABSOLUTE path (see :func:`resolve_uv`), which the
    chokepoint passes through unchanged — so uv is found even when the scrubbed
    env's ``PATH`` does not contain it. uv itself is a static Rust binary and
    needs nothing from the cleared Python env.

    ``strip_python_env`` clears ``PYTHONPATH`` for the same reason the engine
    calls clear it: the engine pins its own native dependencies and must not
    inherit the gateway's.

    BLOCKING — callers are already off the loop.
    """
    wrapped, env, cleanup = sandboxed_spawn_argv(argv, strip_python_env=True)
    wrapped = cgroup_scope_argv(wrapped)  # cgroup DoS ceiling
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no request-derived values
            wrapped,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            preexec_fn=resource_limit_preexec(),
        )
    except subprocess.TimeoutExpired:
        return 1, f"{argv[0]} timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{argv[0]} could not be run: {exc}"
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _current_tag(engine_root: Path) -> str:
    """The tag recorded for the installed engine tree, or ``"unknown"``.

    Read from the source marker a digest-verified install writes, so the reported
    tag can only ever be one whose bytes were verified — never a version this
    code merely hoped was on disk.
    """
    return engine_source.installed_tag(engine_root)


def _ensure_engine(engine_root: Path, log: list[str], uv_bin: str) -> bool:
    """Fetch and verify the pinned engine tree. See :mod:`.engine_source`.

    Kept as a thin seam (rather than calling ``engine_source`` inline) so
    :func:`provision`'s failure ladder — refuse before building, never register
    agents against a tree that did not land — stays readable in one place.
    """
    del uv_bin  # the fetch needs no external binary; only the venv build does
    return engine_source.install_engine(engine_root, log)


def _ensure_venv(engine_root: Path, log: list[str], uv_bin: str) -> bool:
    """Resolve the engine's dependencies into its own uv-managed venv.

    *uv_bin* is the ABSOLUTE path from :func:`resolve_uv` — never the bare name
    ``"uv"``, which would only work when the gateway's ``PATH`` happens to carry
    the venv's scripts dir (it does not under an installed launchd/systemd
    service, and there is no scripts dir at all in the frozen DMG bundle).
    """
    mcp_dir = engine_root / "mcp-local"
    if not mcp_dir.is_dir():
        log.append("the engine checkout has no mcp-local directory")
        return False
    log.append("resolving engine dependencies…")
    code, out = _run(
        [uv_bin, "sync", "--project", str(mcp_dir)],
        cwd=str(engine_root),
        timeout=UV_SYNC_TIMEOUT,
    )
    if code != 0:
        log.append(f"dependency resolve failed: {out}")
        return False
    # Reinstall the engine's skill package EDITABLE. A normal install copies only
    # the Python package into site-packages and drops its sibling data dirs
    # (bundled templates and example styles), which the engine resolves relative
    # to the package — so the builtin styles/templates would silently be missing.
    log.append("installing the engine skill package…")
    code, out = _run(
        [
            uv_bin,
            "pip",
            "install",
            "--python",
            str(engine_root / "mcp-local" / ".venv" / "bin" / "python"),
            "--editable",
            str(engine_root / "skill"),
        ],
        cwd=str(engine_root),
        timeout=UV_INSTALL_TIMEOUT,
    )
    if code != 0:
        log.append(f"skill package install failed: {out}")
        return False
    return True


def _json_escape(value: str) -> str:
    """Escape *value* for interpolation into a JSON **string literal**.

    The placeholders below sit inside quoted JSON strings, so the substituted
    text has to be JSON-escaped or the rendered config is not JSON at all. This
    is not cosmetic: on Windows every one of these values is an absolute path
    full of backslashes (``C:\\Users\\me\\.kiro\\crew\\...``), and ``\\U``/``\\c``
    are invalid JSON escapes — so a naive substitution made ``json.loads`` below
    raise for EVERY template and this app shipped Windows users no agent configs
    at all.

    ``json.dumps`` adds the surrounding quotes the template already provides, so
    they are stripped. Going through ``json.dumps`` rather than a hand-rolled
    ``replace("\\\\", "\\\\\\\\")`` means every other character JSON reserves is
    handled too, not just the one that bit us.
    """
    return json.dumps(value)[1:-1]


def _render_agents(install_dir: Path, log: list[str]) -> int:
    """Render the shipped agent templates into the app's install dir.

    Returns the number written. The templates carry placeholders for the engine's
    absolute location because it is only known now (it lives under the data home,
    which is itself overridable via ``KIROCREW_HOME``).

    Every substituted value is JSON-escaped (:func:`_json_escape`) because the
    placeholders sit inside JSON string literals — see that helper for why a raw
    path cannot be spliced in.
    """
    source_dir = _PACKAGE_ROOT / _AGENTS_SUBDIR
    if not source_dir.is_dir():
        return 0
    target_dir = install_dir / _AGENTS_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    engine_root = paths.engine_root()
    written = 0
    for template in sorted(source_dir.glob("*.json")):
        try:
            rendered = (
                template.read_text(encoding="utf-8")
                .replace(PLACEHOLDER_ENGINE_MCP_DIR, _json_escape(str(paths.engine_mcp_dir())))
                .replace(PLACEHOLDER_ENGINE_ROOT, _json_escape(str(engine_root)))
                .replace(
                    PLACEHOLDER_APP_PROMPTS,
                    _json_escape(str(install_dir / _PROMPTS_SUBDIR)),
                )
            )
            # Parse before writing: a malformed agent config is silently ignored
            # by kiro-cli, which would surface as "the mode is missing" with no
            # explanation, so fail loudly here instead.
            json.loads(rendered)
            atomic_write(target_dir / template.name, rendered)
            written += 1
        except (OSError, ValueError) as exc:
            log.append(f"agent {template.name} could not be written: {exc}")
    return written


def _copy_tree(source: Path, target: Path) -> None:
    """Replace *target* with a copy of *source*, following no symlinks."""
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=False)


def _stage_static(install_dir: Path, log: list[str]) -> None:
    """Copy the app's prompt files into the install dir.

    Copied rather than symlinked because the package directory is read-only on a
    wheel install, and the rendered agent configs point at the install-dir copy.

    The SKILL is deliberately NOT staged here: it lives in
    ``src/kiro_crew/builtin_skills/pptx-maker/``, which the gateway copies into
    the user's skills dir on every start, so it reaches every ``pip``/DMG install
    without depending on this app being provisioned (the skill-bundling rule in
    ``AGENTS.md``). Staging a second copy under the install dir and declaring it
    in the manifest would register the same skill twice.
    """
    for subdir in (_PROMPTS_SUBDIR,):
        source = _PACKAGE_ROOT / subdir
        if not source.is_dir():
            continue
        try:
            _copy_tree(source, install_dir / subdir)
        except OSError as exc:
            log.append(f"{subdir} could not be staged: {exc}")


def _seed_deck_root(log: list[str]) -> None:
    """Give a brand-new engine install a deck directory that needs no OS grant.

    Only for a first install (no engine config yet, no decks in the engine's
    default location): the engine defaults to ``~/Documents``, which on macOS is
    behind a file-access prompt the gateway cannot answer, so a first-run deck
    would fail to write with a permission error the user cannot act on. An
    existing config or existing decks are never touched.
    """
    config_path = paths.engine_config_path()
    if config_path.exists():
        return
    default_root = Path.home() / paths.ENGINE_DEFAULT_DECK_ROOT
    try:
        if default_root.is_dir() and any(default_root.iterdir()):
            return
    except OSError:
        return
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            config_path,
            json.dumps({"output_dir": paths.SEEDED_DECK_ROOT}, indent=2) + "\n",
        )
        log.append(f"deck output directory set to {paths.SEEDED_DECK_ROOT}")
    except OSError as exc:
        log.append(f"deck output directory could not be set: {exc}")


def provision() -> ProvisionOutcome:
    """Provision the engine and register this app's agents and skill.

    Idempotent. Safe to re-run: the engine tree is re-fetched only if it does not
    already match the pin, the venv is re-resolved, and the agent configs are
    re-rendered against the current paths.

    BLOCKING — call through ``routes.off_loop``.
    """
    log: list[str] = []
    engine_root = paths.engine_root()
    install_dir = app_dir(paths.APP_NAME)

    # `uv` ships as a declared Python dependency, so this only fails on a
    # genuinely broken install. Reported precisely (and only about uv — `git` is
    # no longer used) so the message is actionable rather than a guess.
    uv_bin = resolve_uv()
    if uv_bin is None:
        log.append(
            "`uv` could not be found. It ships with KiroCrew as a Python "
            "dependency, so this usually means the install is incomplete — "
            "reinstall with `pip install --force-reinstall uv`, or install uv "
            "yourself and put it on PATH."
        )
        return ProvisionOutcome(ok=False, log="\n".join(log))

    if not _ensure_engine(engine_root, log, uv_bin):
        return ProvisionOutcome(ok=False, log="\n".join(log))
    if not _ensure_venv(engine_root, log, uv_bin):
        return ProvisionOutcome(ok=False, log="\n".join(log))

    _stage_static(install_dir, log)
    written = _render_agents(install_dir, log)
    log.append(f"{written} agent config(s) written")

    # Hand off to the platform's own registrar so the agents land in
    # ~/.kiro/agents and the skill in the skills tree through exactly the same
    # path an installed app uses — no bespoke symlinking in this app.
    try:
        # circular import: bridges imports the app manager, which imports the
        # builtins package that owns this module.
        from kiro_crew.apps.bridges import register_app

        result = register_app(paths.APP_NAME)
        log.append(f"registered {len(result.agents)} agent(s) and {len(result.skills)} skill(s)")
        for err in result.errors:
            log.append(f"registration warning: {err}")
    except Exception as exc:  # noqa: BLE001 - provisioning must report, not raise
        logger.warning("pptx-maker: resource registration failed: %s", exc)
        log.append(f"resource registration failed: {exc}")

    _seed_deck_root(log)

    # Analyze any template the engine has no metadata for yet — the ones it ships
    # on a fresh clone, plus anything the user dropped into the templates dir by
    # hand. Without this such a template lists with no theme colours or layout
    # count until it is re-imported through the UI. Best effort: an un-analyzed
    # template is still usable, so a failure here must not fail provisioning.
    try:
        # circular import: engine imports paths, which imports the app manager,
        # which imports the builtins package that owns this module.
        from kiro_crew.apps.builtins.pptx_maker.backend import engine

        analyzed = engine.scan_new_templates()
        if analyzed:
            log.append(f"analyzed {len(analyzed)} template(s)")
    except Exception as exc:  # noqa: BLE001 - provisioning must report, not raise
        log.append(f"template analysis skipped: {exc}")

    tag = _current_tag(engine_root)
    log.append(f"engine ready at {tag}")
    return ProvisionOutcome(ok=True, log="\n".join(log), engine_tag=tag)
