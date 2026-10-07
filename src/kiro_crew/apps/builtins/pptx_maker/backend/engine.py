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

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.pptx_maker.backend import engine_source, paths
from kiro_crew.sandbox import cgroup_scope_argv, run_limited, sandboxed_spawn_argv
from kiro_crew.security import redact
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger("kirocrew.app.pptx-maker")

#: Bounds the installed tag `/engine` reports; a real tag is like `v0.10.4`.
_MAX_TAG_CHARS = 64

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

#: Fixed install roots for an optional tool whose installer leaves ``PATH`` alone, so
#: a by-name lookup cannot see an install that is present and working. LibreOffice's
#: Windows installer is one: it writes ``soffice`` into its own ``program`` directory
#: and adds nothing to ``PATH``.
#:
#: Literal roots rather than ``%ProgramFiles%``, for the reason
#: ``platform_compat._WINDOWS_GIT_DIRS`` records: ``HKCU\Environment`` is writable
#: without elevation, so resolving through an environment variable would let a
#: poisoned value redirect this lookup into a directory the user can write. An
#: install on another drive misses and is reported missing, which is honest.
#:
#: Naming these roots is NOT by itself what keeps the search out of writable
#: directories — passing one as ``shutil.which(path=…)`` does not bound where that
#: function looks, and it searches the working directory first on a default Windows
#: host. :func:`_soffice_system_install_path` accepts a hit only when its parent is
#: the root it asked about, and that check is what makes this tuple the reachable set.
#:
#: Only ``soffice`` has one: poppler ships no Windows installer, so ``pdftoppm``
#: stays served by the managed launcher :mod:`.preview_tools` writes.
_SOFFICE_WINDOWS_DIRS: tuple[str, ...] = (
    r"C:\Program Files\LibreOffice\program",
    r"C:\Program Files (x86)\LibreOffice\program",
)

# Icon asset packs supported by the engine's public download API.
ICON_SOURCES: tuple[str, ...] = ("aws", "material")
ICON_DOWNLOAD_TIMEOUT = 600

# Cap on captured subprocess log text handed to the UI.
LOG_TAIL_CHARS = 4000

# Engine API snippets. Fixed source, never interpolated with request data — the
# only inputs are argv values the caller has already contained.
_USER_DIR_SNIPPET = "from sdpm.config import get_user_config_dir; print(get_user_config_dir())"

_LISTS_SNIPPET = (
    "import json\n"
    "from sdpm.api import (get_styles_dirs, list_styles_filtered,\n"
    "                      get_templates_dirs, list_templates_with_metadata)\n"
    "from sdpm.config import get_state\n"
    "state = get_state()\n"
    "style_dirs = get_styles_dirs()\n"
    "template_dirs = get_templates_dirs()\n"
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

_INSTALL_ASSETS_SNIPPET = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "from sdpm.knowledge.assets.download import install_assets\n"
    "print(json.dumps(install_assets([sys.argv[1]], Path(sys.argv[2]))))\n"
)

_SCAN_TEMPLATES_SNIPPET = (
    "import json\n"
    "from sdpm.api import get_templates_dirs, analyze_and_store_template\n"
    "from sdpm.config import get_state, update_state\n"
    "dirs = get_templates_dirs()\n"
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
        proc = run_limited(  # noqa: S603 - fixed argv, contained paths only
            wrapped,
            cwd=cwd,
            env=env,
            capture_output=True,
            **UTF8_TEXT,
            timeout=timeout,
            check=False,
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
    marker = engine_source.read_source_marker(root)
    source = engine_source.is_installed(root)
    venv = paths.engine_python().is_file()
    recorded_tag = marker.get("tag")
    # The marker sits in a tree the agent can write, so the tag is display text
    # from an untrusted file: redacted and bounded like every other such string.
    installed_tag = (
        redact(recorded_tag)[:_MAX_TAG_CHARS]
        if isinstance(recorded_tag, str) and recorded_tag
        else None
    )
    return {
        "ready": source and venv,
        "clone": source,
        "venv": venv,
        "installedTag": installed_tag,
        "updateRequired": bool(marker) and not source,
    }


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


def _which_within(name: str, directory: str | os.PathLike[str]) -> str | None:
    """*name* resolved INSIDE *directory*, or ``None`` — never from the working dir.

    ``shutil.which(path=…)`` does not bound where it looks. On Windows it inserts the
    process's working directory AHEAD of the given path whenever
    ``_winapi.NeedCurrentDirectoryForExePath`` says to, and that is the default —
    having ``NoDefaultCurrentDirectoryInExePath`` set is the exception, not the rule.
    Measured on CPython 3.12.14, with ``soffice.com`` planted in the working directory
    and ``path=`` an empty directory: with the variable unset the call returns
    ``.\\soffice.COM``, and with it set the same call returns ``None``. So a probe of a
    trusted directory silently answers with whatever the process happens to be standing
    next to, and the answer is RELATIVE — parent ``.`` — which then follows the process.

    Both callers feed the result to something that EXECUTES it: one directly, one by
    appending its parent to the MCP ``PATH``. A hit is therefore accepted only when its
    containing directory really is the one that was asked about. The LEAF is not
    link-resolved, so a managed launcher that is a symlink to a real binary elsewhere
    still counts; only its parent has to match.
    """
    found = shutil.which(name, path=str(directory))
    if not found:
        return None
    parent = os.path.realpath(os.path.dirname(os.path.abspath(found)))
    expected = os.path.realpath(str(directory))
    if os.path.normcase(parent) != os.path.normcase(expected):
        return None
    return os.path.abspath(found)


def _soffice_system_install_path() -> str | None:
    """``soffice`` resolved from its fixed Windows install root, or ``None``.

    Goes through :func:`_which_within` rather than ``shutil.which`` directly, so the
    Windows executable suffixes still come from ``PATHEXT`` and the executable check is
    the one a ``PATH`` lookup applies, while the reachable set stays the two fixed
    roots rather than "the two fixed roots, plus wherever the process is standing".

    Singular rather than keyed by tool name because ``soffice`` is the only optional
    dep with a fixed install root at all — see :data:`_SOFFICE_WINDOWS_DIRS`.
    """
    if not platform_compat.IS_WINDOWS:
        return None
    for directory in _SOFFICE_WINDOWS_DIRS:
        found = _which_within("soffice", directory)
        if found:
            return found
    return None


def soffice_install_dir() -> str | None:
    """The directory holding a system ``soffice`` that a by-name lookup cannot reach.

    The engine resolves ``pdftoppm``/``soffice`` with ``shutil.which()`` inside its own
    MCP server process, so a tool whose installer left ``PATH`` alone is invisible
    there even when :func:`optional_dep_path` can point at it.
    :func:`.provision.mcp_tools_path` appends what this returns, which is what makes
    such an install reachable by the process that rasterizes slides.

    A tool already on ``PATH`` contributes nothing, so the rendered ``PATH`` grows only
    on a host where the difference is what decides whether previews work.

    Singular: ``soffice`` is the only optional dep with a fixed install root, so this
    can only ever name zero or one directory — a list would imply a registry of them
    that does not exist.
    """
    if shutil.which("soffice"):
        return None
    found = _soffice_system_install_path()
    if not found:
        return None
    return os.path.dirname(found) or None


def optional_dep_path(name: str) -> str | None:
    """Absolute path to *name*, preferring the user's OWN install over the managed one.

    ``PATH`` is consulted first so a real system LibreOffice/poppler always wins —
    the same precedence :func:`papyrus.backend.latex.find_compiler_sync` gives a
    user's own TeX distribution over the managed Tectonic. The tool's fixed install
    root (:data:`_SOFFICE_WINDOWS_DIRS`) is consulted second, so an install whose
    installer does not extend ``PATH`` still counts as the user's own rather than
    reading as absent. The app-managed directory is only a fallback for a host that
    has none of the three.

    That order is what keeps ``/deps`` truthful: resolving a system install through
    the managed probe would report LibreOffice as installed BY Kiro Crew, which the UI
    renders as a claim about the host that this app never made.

    Returns ``None`` when the tool is available from no source.
    """
    found = shutil.which(name)
    if found:
        return found
    system = _soffice_system_install_path() if name == "soffice" else None
    if system:
        return system
    # `_which_within` rather than a bare `shutil.which(path=…)`: the managed result is
    # EXECUTED, and on a default Windows host that call would answer with a binary
    # planted in the process's working directory before it ever looked here.
    managed = _which_within(name, paths.preview_tools_bin())
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
    result = run_engine_snippet(_LISTS_SNIPPET)
    data = result.json() if result is not None else None
    if isinstance(data, dict):
        return {
            "styles": list(data.get("styles") or []),
            "templates": list(data.get("templates") or []),
            "stylesDirs": [str(d) for d in (data.get("stylesDirs") or [])],
        }
    return _user_library_without_engine()


def _user_library_without_engine() -> dict:
    """The user's own styles and templates, read from the engine's config dir.

    The fallback for when the engine cannot answer — before the first install,
    or between a Kiro Crew update and the user's click on "Update" (the pinned
    engine moved on, the installed one is from an older layout). Those files and
    their pins survive the engine update, so the Library keeps showing them
    instead of going blank. Bundled styles/templates ship with the engine and
    are absent until it is installed.

    Same shape as the engine's answer: name/source/pinned for styles, and the
    metadata fields ``list_templates_with_metadata`` reports for templates.
    """
    user_dir = paths.engine_config_path().parent
    try:
        state = json.loads((user_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    pins = state.get("pinned_styles")
    # Only string entries name a style; anything else in a hand-edited or
    # corrupt state.json is ignored rather than crashing the Library read.
    pinned = {p for p in pins if isinstance(p, str)} if isinstance(pins, list) else set()
    metadata = state.get("template_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    styles_dir = user_dir / "styles"
    styles = [
        {"name": f.stem, "description": "", "source": "user", "pinned": f.stem in pinned}
        for f in _regular_files(styles_dir, "*.html")
    ]
    templates = []
    for f in _regular_files(user_dir / "templates", "*.pptx"):
        meta = metadata.get(f.stem)
        meta = meta if isinstance(meta, dict) else {}
        # state.json is user-editable; only well-typed values reach the Library,
        # which renders them directly (an object as a text child crashes it).
        description = meta.get("description")
        theme_colors = meta.get("theme_colors")
        fonts = meta.get("fonts")
        layout_count = meta.get("layout_count")
        templates.append(
            {
                "name": f.stem,
                "source": "user",
                "description": description if isinstance(description, str) else "",
                "theme_colors": theme_colors if isinstance(theme_colors, dict) else {},
                "fonts": fonts if isinstance(fonts, dict) else {},
                "layout_count": (
                    layout_count
                    if isinstance(layout_count, int) and not isinstance(layout_count, bool)
                    else 0
                ),
            }
        )
    return {
        "styles": styles,
        "templates": templates,
        "stylesDirs": [str(styles_dir)] if styles else [],
    }


def _regular_files(directory: Path, pattern: str) -> list[Path]:
    """Sorted non-symlink files matching *pattern*; ``[]`` when unreadable."""
    try:
        return sorted(f for f in directory.glob(pattern) if f.is_file() and not f.is_symlink())
    except OSError:
        return []


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
        timeout=ENGINE_ANALYZE_TIMEOUT,
    )
    data = result.json() if result is not None else None
    return [str(name) for name in data] if isinstance(data, list) else []


def install_asset_source(source: str, destination: Path) -> EngineResult:
    """Install one icon catalog through SDPM's public download API.

    BLOCKING — call through ``off_loop``.
    """
    if source not in ICON_SOURCES:
        return EngineResult(returncode=1, stderr=f"unsupported asset source: {source}")
    result = run_engine_snippet(
        _INSTALL_ASSETS_SNIPPET,
        [source, str(destination)],
        timeout=ICON_DOWNLOAD_TIMEOUT,
    )
    return result or EngineResult(returncode=1, stderr="engine venv missing")
