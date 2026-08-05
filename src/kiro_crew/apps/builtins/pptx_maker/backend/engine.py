"""PPTX Maker — the vendored engine bridge.

The presentation engine (`spec-driven-presentation-maker`, public OSS) is fetched
as a digest-pinned tarball into the app's data dir with its own uv-managed
virtualenv (see :mod:`.engine_source`). This module is the ONLY place that talks
to it, and it talks in exactly two ways:

* :func:`engine_status` — cheap filesystem probes for the provisioning banner.
* :func:`run_engine_snippet` — run a fixed Python snippet inside the engine venv
  and parse its JSON stdout.

Why call the engine as a subprocess rather than importing it: it is a separately
versioned third-party checkout with its own dependency closure (``lxml``,
``python-pptx``), installed into its own venv. Importing it into the gateway
process would put an unpinned dependency set on the gateway's import path.

Every function here is BLOCKING and must be called through
:func:`kiro_crew.apps.builtins.pptx_maker.backend.routes.off_loop` (which hands
it to ``subprocess_executor()``). Nothing in this module may be awaited
directly — a ``subprocess.run`` on the gateway's single event loop freezes every
session (AUTOSDE ``no-blocking-call-on-event-loop``).

The snippets are module-level constants, never built from request data: the
engine is driven through its own documented Python API, and the only values that
cross the boundary are ``argv`` entries the caller has already contained through
``paths.py``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew.apps.builtins.pptx_maker.backend import engine_source, paths
from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, sandboxed_spawn_argv

logger = logging.getLogger("kirocrew.app.pptx-maker")

# Timeouts (seconds). The engine calls here are metadata reads over a handful of
# small files, so these are generous; deck GENERATION does not run through this
# module at all (the agent drives it over MCP).
ENGINE_CALL_TIMEOUT = 30
ENGINE_ANALYZE_TIMEOUT = 60

# Optional system dependencies. The core path (.pptx via python-pptx, animated
# SVG preview) works without either; these only improve preview fidelity, so a
# missing one is reported, never fatal.
OPTIONAL_DEPS: dict[str, str] = {
    "soffice": "LibreOffice",
    "pdftoppm": "poppler",
}

# Icon asset packs the engine can download with its own bundled scripts. Run
# once per engine version into the engine's user config dir, so the packs
# survive an app update (which replaces the vendored checkout).
ICON_SOURCES: tuple[tuple[str, str], ...] = (
    ("aws", "download_aws_icons.py"),
    ("material", "download_material_icons.py"),
)
ICON_MARKER_FILENAME = ".pptx-maker-provisioned.json"
ICON_DOWNLOAD_TIMEOUT = 600

# Cap on captured subprocess log text handed to the UI.
LOG_TAIL_CHARS = 4000

# Engine API snippets. Fixed source, never interpolated with request data — the
# only inputs are argv values the caller has already contained.
_USER_DIR_SNIPPET = "from sdpm.config import get_user_config_dir; print(get_user_config_dir())"

_LISTS_SNIPPET = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "from sdpm.api import (get_styles_dirs, list_styles_filtered,\n"
    "                      get_templates_dirs, list_templates_with_metadata)\n"
    "from sdpm.config import get_state\n"
    "bundled_styles, bundled_templates = Path(sys.argv[1]), Path(sys.argv[2])\n"
    # Append the vendored skill tree as the authoritative builtin dir (last wins
    # for the 'builtin' label) so bundled styles/templates resolve regardless of
    # whether the engine was installed editable.
    "def merge(dirs, bundled):\n"
    "    out = [Path(d) for d in dirs if Path(d) != bundled]\n"
    "    if bundled.is_dir(): out.append(bundled)\n"
    "    return out\n"
    "state = get_state()\n"
    "style_dirs = merge(get_styles_dirs(), bundled_styles)\n"
    "template_dirs = merge(get_templates_dirs(), bundled_templates)\n"
    "styles = list_styles_filtered(style_dirs, state.get('pinned_styles', []), include_all=True)\n"
    "templates = list_templates_with_metadata(template_dirs,\n"
    "                                        state.get('template_metadata', {}))\n"
    "print(json.dumps({'styles': styles, 'templates': templates,\n"
    "                  'stylesDirs': [str(d) for d in style_dirs]}))\n"
)

_ANALYZE_SNIPPET = (
    "import sys, json\n"
    "from pathlib import Path\n"
    "from sdpm.api import analyze_and_store_template\n"
    "from sdpm.config import get_state, update_state\n"
    "meta = analyze_and_store_template(Path(sys.argv[1]), sys.argv[2])\n"
    "tm = get_state().get('template_metadata', {})\n"
    "tm[meta['name']] = meta\n"
    "update_state('template_metadata', tm)\n"
    "print(json.dumps(meta, ensure_ascii=False))\n"
)

_SCAN_TEMPLATES_SNIPPET = (
    "import sys, json\n"
    "from pathlib import Path\n"
    "from sdpm.api import get_templates_dirs, analyze_and_store_template\n"
    "from sdpm.config import get_state, update_state\n"
    "bundled = Path(sys.argv[1])\n"
    "dirs = [Path(d) for d in get_templates_dirs() if Path(d) != bundled]\n"
    "if bundled.is_dir(): dirs.append(bundled)\n"
    "state = get_state()\n"
    "known = state.get('template_metadata', {})\n"
    "added = []\n"
    "for d in dirs:\n"
    "    if not d.is_dir(): continue\n"
    "    for t in sorted(d.glob('*.pptx')):\n"
    "        if t.stem in known: continue\n"
    "        try:\n"
    "            known[t.stem] = analyze_and_store_template(t, '')\n"
    "            added.append(t.stem)\n"
    "        except Exception:\n"
    "            pass\n"
    "if added: update_state('template_metadata', known)\n"
    "print(json.dumps(added))\n"
)


@dataclass
class EngineResult:
    """One engine subprocess call's outcome."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and bool(self.stdout.strip())

    def json(self) -> object | None:
        """Parse stdout as JSON, or ``None`` when it is absent/malformed."""
        if not self.ok:
            return None
        try:
            return json.loads(self.stdout)
        except ValueError:
            logger.debug("pptx-maker: engine returned non-JSON stdout")
            return None


@dataclass
class ProvisionState:
    """Progress of a background provisioning job (icon packs).

    ``state`` is one of ``idle`` / ``running`` / ``done`` / ``error``.
    """

    state: str = "idle"
    log: str = ""
    started: float = 0.0
    per_source: dict[str, str] = field(default_factory=dict)

    def elapsed(self) -> int:
        return int(time.time() - self.started) if self.started else 0


def _spawn(argv: list[str], *, cwd: str, timeout: int) -> EngineResult:
    """Run *argv* to completion under the shared sandbox + resource ceiling.

    Routed through :func:`kiro_crew.sandbox.sandboxed_spawn_argv` rather than a
    bare :func:`wrap_argv`, because that chokepoint also returns a
    CREDENTIAL-SCRUBBED environment. The child here is a third-party engine
    cloned from a public repository at provision time, driven with
    model-authored deck content — inheriting the gateway's raw ``os.environ``
    would hand it ``AWS_SECRET*``/``AWS_SESSION*``, ``SSH_AUTH_SOCK``,
    ``GNUPGHOME``, ``GIT_ASKPASS`` and every bot token in
    ``_AGENT_DENIED_ENV_KEYS``. ``strip_python_env`` clears ``PYTHONPATH`` so
    the engine venv's own packages win over anything on the gateway's path (the
    engine pins ``lxml``, and inheriting a different build breaks the .pptx
    writer).

    BLOCKING — callers must be off the event loop.
    """
    # ``mode="strict"``, not the ``standard`` default. The env scrub above stops
    # this child INHERITING a credential; it does not stop it READING one off
    # disk, and standard mode deliberately leaves ``~/.aws``/``~/.ssh`` visible so
    # git-over-SSH and the AWS CLI keep working. A .pptx engine needs neither, and
    # it executes model-authored deck content — which could just as well open a
    # credentials file and typeset it into a slide. Strict escalates only the
    # credential-dir hiding, so the engine venv and the deck tree stay visible and
    # uv's network access is unaffected.
    # No preview-tool PATH is injected here on purpose: these children run only the
    # metadata snippets and the icon scripts. `pdftoppm`/`soffice` are shelled out to
    # from `skill/sdpm/api.py` inside the engine's MCP server, which kiro-cli spawns
    # from the rendered agent config — so the managed tool dir belongs on THAT
    # process's PATH (`provision.mcp_tools_path`), and putting it here would only
    # look like a fix.
    wrapped, env, cleanup = sandboxed_spawn_argv(argv, mode="strict", strip_python_env=True)
    wrapped = cgroup_scope_argv(wrapped)  # cgroup DoS ceiling
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, contained paths only
            wrapped,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            preexec_fn=resource_limit_preexec(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("pptx-maker: engine spawn failed: %s", exc)
        return EngineResult(returncode=1, stderr=str(exc))
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass
    return EngineResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def engine_status() -> dict:
    """Whether the vendored engine is ready — filesystem probes only.

    Drives the provisioning banner, so it must answer while the engine is only
    half-installed rather than raising.

    The ``clone`` key predates the switch from ``git clone`` to a digest-pinned
    tarball and is kept as the wire name the dashboard already reads; what it now
    reports is ``engine_source.is_installed`` — the source marker only a
    sha256-verified extraction can have written. A tree left by an older,
    git-based install has no marker, so it reads as "not installed" and the next
    provision replaces it with a verified one.
    """
    root = paths.engine_root()
    source = engine_source.is_installed(root)
    venv = paths.engine_python().is_file()
    return {"ready": source and venv, "clone": source, "venv": venv}


def run_engine_snippet(
    snippet: str, argv: list[str] | None = None, *, timeout: int = ENGINE_CALL_TIMEOUT
) -> EngineResult | None:
    """Run *snippet* in the engine venv. ``None`` when the engine is not ready.

    BLOCKING — call through ``off_loop``.
    """
    python = paths.engine_python()
    if not python.is_file():
        return None
    return _spawn(
        [str(python), "-c", snippet, *(argv or [])],
        cwd=str(paths.engine_mcp_dir()),
        timeout=timeout,
    )


def engine_tag() -> str:
    """The installed engine's version tag, or ``"unknown"``.

    A cheap file read now, not a ``git describe`` subprocess: the engine arrives
    as a digest-pinned tarball with no ``.git`` at all, and the tag is recorded in
    the source marker that only a sha256-verified extraction writes. So the
    answer stays honest — an unverified or absent tree reports ``"unknown"``
    rather than the tag this code was compiled with — and it keys the icon-pack
    marker exactly as before.
    """
    return engine_source.installed_tag(paths.engine_root())


def user_config_dir() -> Path | None:
    """The engine's user config dir, asked of the engine itself.

    Resolved by the engine rather than re-derived here so this app and the engine
    can never disagree about where user styles/templates live.

    BLOCKING — call through ``off_loop``.
    """
    result = run_engine_snippet(_USER_DIR_SNIPPET)
    if result is None or not result.ok:
        return None
    return Path(result.stdout.strip())


def user_subdir(sub: str) -> Path | None:
    """A named subdirectory of the engine's user config dir (``styles``/``templates``)."""
    base = user_config_dir()
    return (base / sub) if base is not None else None


def optional_dep_path(name: str) -> str | None:
    """Absolute path to *name*, preferring the user's OWN install over the managed one.

    ``PATH`` is consulted first so a real system LibreOffice/poppler always wins —
    the same precedence :func:`papyrus.backend.latex.find_compiler_sync` gives a
    user's own TeX distribution over the managed Tectonic. The app-managed
    directory is only a fallback for a host that has neither.

    Returns ``None`` when the tool is available from neither source.
    """
    found = shutil.which(name)
    if found:
        return found
    # `shutil.which` with an explicit `path=` so the managed dir is probed even
    # though it is not on this process's PATH.
    managed = shutil.which(name, path=str(paths.preview_tools_bin()))
    return managed or None


def missing_optional_deps() -> list[str]:
    """Optional preview binaries available from neither ``PATH`` nor the managed dir."""
    return [name for name in OPTIONAL_DEPS if optional_dep_path(name) is None]


def load_lists() -> dict:
    """Styles + templates + style dirs, straight from the engine's own API.

    Returns the engine's shape (``{"styles", "templates", "stylesDirs"}``);
    ``{}``-shaped empty lists when the engine is not ready.

    BLOCKING — call through ``off_loop``.
    """
    result = run_engine_snippet(
        _LISTS_SNIPPET,
        [
            str(paths.engine_skill_dir() / "references" / "examples" / "styles"),
            str(paths.engine_skill_dir() / "templates"),
        ],
    )
    data = result.json() if result is not None else None
    if isinstance(data, dict):
        return {
            "styles": list(data.get("styles") or []),
            "templates": list(data.get("templates") or []),
            "stylesDirs": [str(d) for d in (data.get("stylesDirs") or [])],
        }
    return {"styles": [], "templates": [], "stylesDirs": []}


def analyze_template(path: Path, description: str) -> dict:
    """Analyze an imported .pptx and persist its metadata via the engine.

    Returns the engine's metadata dict, or just the description when the
    analysis could not run (an un-analyzed template is still usable).

    BLOCKING — call through ``off_loop``.
    """
    result = run_engine_snippet(
        _ANALYZE_SNIPPET, [str(path), description], timeout=ENGINE_ANALYZE_TIMEOUT
    )
    data = result.json() if result is not None else None
    return data if isinstance(data, dict) else {"description": description}


def scan_new_templates() -> list[str]:
    """Analyze any template the engine has no metadata for yet.

    Returns the stems that were newly analyzed. Idempotent: a template already
    in ``state.json`` is skipped, so this is safe to run on every startup.

    BLOCKING — call through ``off_loop``.
    """
    result = run_engine_snippet(
        _SCAN_TEMPLATES_SNIPPET,
        [str(paths.engine_skill_dir() / "templates")],
        timeout=ENGINE_ANALYZE_TIMEOUT,
    )
    data = result.json() if result is not None else None
    return [str(name) for name in data] if isinstance(data, list) else []


def icon_script_path(script: str) -> Path:
    """Path to one of the engine's bundled icon-download scripts."""
    return paths.engine_skill_dir() / "scripts" / script


def icon_vendor_output(source: str) -> Path:
    """Where an icon script writes its output inside the engine checkout."""
    return paths.engine_skill_dir() / "assets" / source


def run_icon_script(source: str, script: str) -> EngineResult:
    """Run one bundled icon-download script in the engine venv.

    BLOCKING — call through ``off_loop``.
    """
    python = paths.engine_python()
    script_path = icon_script_path(script)
    if not python.is_file() or not script_path.is_file():
        return EngineResult(returncode=1, stderr=f"[{source}] script or venv missing")
    return _spawn(
        [str(python), str(script_path)],
        cwd=str(script_path.parent),
        timeout=ICON_DOWNLOAD_TIMEOUT,
    )
