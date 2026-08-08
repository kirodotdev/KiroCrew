"""Build gate: every unguarded third-party import is declared in setup.cfg.

Static AST check that fails the build when a module-level third-party import
in core ``kiro_crew`` source is NOT declared in ``setup.cfg [options]
install_requires``. This prevents the recurring pattern where a dependency
is present in a dev environment but missing from ``setup.cfg``
— silently breaking pip-based installs (editable, one-line, auto-update) with
``ModuleNotFoundError`` at startup.

Hermetic — no network, no package installation, pure AST + configparser.

Scope: only core kiro_crew modules (excludes apps/builtins/, knowledge/,
workflows/, aidlc/ sub-trees which have their own dependency management).

The historical PyYAML gap and the opentelemetry
gap both would have been caught by this gate on day one.
"""

from __future__ import annotations

import ast
import configparser
import pathlib
import sys

# --- Dist name -> importable root package mapping ---
# pip distribution names and Python importable names often differ.
_DIST_TO_IMPORT: dict[str, str] = {
    "slack-sdk": "slack_sdk",
    "pyyaml": "yaml",
    "python-docx": "docx",
    "pysqlite3-binary": "pysqlite3",
    "amazon-transcribe": "amazon_transcribe",
    "cron-descriptor": "cron_descriptor",
    "opentelemetry-api": "opentelemetry",
    "opentelemetry-sdk": "opentelemetry",
    "snowballstemmer": "snowballstemmer",
    "defusedxml": "defusedxml",
    "pdfplumber": "pdfplumber",
    "websockets": "websockets",
}

# --- Modules explicitly exempt from the check ---
_EXEMPT: set[str] = {
    # Optional integration; guarded by try/except in the import site.
    "playwright",
    # Test-only; not a runtime dep.
    "pytest",
    "_pytest",
    # uvloop optional perf dep; guarded at import site.
    "uvloop",
    # yarl is a transitive dep of aiohttp; always present when aiohttp is.
    "yarl",
    # httpx is optional for quip connector; guarded.
    "httpx",
}

# Sub-trees within kiro_crew that are NOT core startup and have their own
# dependency management (app builtins have requirements.txt, knowledge/
# and workflows/ are feature modules loaded lazily).
_EXCLUDED_SUBTREES: tuple[str, ...] = (
    "apps/builtins/",
    "knowledge/",
    "workflows/",
    "aidlc/",
    # Fork-only artifact-deploy reaper Lambda payload; boto3/botocore come
    # from the AWS Lambda runtime, not core startup imports.
    "deploy/skills/",
    # Builtin skill scripts are standalone CLI tools with sibling imports
    # (e.g. preflight.py imports push_guard.py via sys.path); they are not
    # core startup code and have no bearing on pip install requirements.
    "builtin_skills/",
)


def _src_root() -> pathlib.Path:
    """Locate the kiro_crew source tree."""
    try:
        import kiro_crew  # noqa: PLC0415

        return pathlib.Path(kiro_crew.__file__).resolve().parent
    except Exception:
        return pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


def _setup_cfg_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "setup.cfg"


def _parse_install_requires() -> set[str]:
    """Parse setup.cfg and return the set of declared import root names."""
    cfg = configparser.ConfigParser()
    cfg.read(_setup_cfg_path())
    raw = cfg.get("options", "install_requires", fallback="")
    declared: set[str] = set()
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip version specifiers and markers
        dist_name = (
            line.split(">=")[0]
            .split("<=")[0]
            .split("==")[0]
            .split("!=")[0]
            .split("<")[0]
            .split(">")[0]
            .split(";")[0]
            .strip()
        )
        normalized = dist_name.lower().replace("_", "-")
        import_name = _DIST_TO_IMPORT.get(normalized, dist_name.replace("-", "_"))
        declared.add(import_name)
    return declared


def _is_in_try_except_importerror(node: ast.stmt, tree: ast.Module) -> bool:
    """Check if an import is inside a try block that catches ImportError."""
    for top_node in ast.walk(tree):
        if not isinstance(top_node, ast.Try):
            continue
        catches_import_error = any(
            (
                handler.type is None  # bare except
                or (
                    isinstance(handler.type, ast.Name)
                    and handler.type.id in ("ImportError", "ModuleNotFoundError", "Exception")
                )
                or (
                    isinstance(handler.type, ast.Tuple)
                    and any(
                        isinstance(elt, ast.Name)
                        and elt.id in ("ImportError", "ModuleNotFoundError", "Exception")
                        for elt in handler.type.elts
                    )
                )
            )
            for handler in top_node.handlers
        )
        if catches_import_error:
            for body_stmt in top_node.body:
                if body_stmt is node:
                    return True
    return False


def test_otlp_extra_declares_exact_http_exporter_version():
    """The documented kirocrew[otlp] install path must remain usable."""
    cfg = configparser.ConfigParser()
    cfg.read(_setup_cfg_path())
    requirements = [
        line.strip()
        for line in cfg.get("options.extras_require", "otlp").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert requirements == ["opentelemetry-exporter-otlp-proto-http==1.44.0"]


def _pyproject_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject_text() -> str:
    return _pyproject_path().read_text(encoding="utf-8")


def test_pyproject_declares_optional_dependencies_dynamic():
    """``optional-dependencies`` MUST be in pyproject's ``[project] dynamic``.

    The extras (voice/desktop/dev/otlp) live in setup.cfg
    ``[options.extras_require]``. Once a ``[project]`` table exists, setuptools
    ignores setup.cfg metadata for any field not declared dynamic — so dropping
    this entry silently strips EVERY extra from the built metadata. pip then
    treats ``pip install -e ".[dev]"`` as a plain install and exits 0 with only
    a warning ("does not provide the extra 'dev'"), so no test tooling is
    installed and ``make test`` dies on a missing ``.venv/bin/pytest``. The same
    omission also broke the published wheel's ``kirocrew[voice]`` install path.
    """
    text = _pyproject_text()
    dynamic_lines = [ln for ln in text.splitlines() if ln.strip().startswith("dynamic")]
    assert dynamic_lines, "pyproject.toml [project] declares no `dynamic` field"
    joined = " ".join(dynamic_lines)
    assert "optional-dependencies" in joined, (
        "pyproject.toml [project].dynamic must include "
        '"optional-dependencies", otherwise setuptools drops every extra '
        "declared in setup.cfg [options.extras_require] and "
        'pip install ".[dev]" / ".[voice]" becomes a silent no-op.\n'
        f"Found: {joined.strip()}"
    )


def test_declared_extras_match_setup_cfg():
    """Every setup.cfg extra stays reachable; guards the dynamic wiring above."""
    cfg = configparser.ConfigParser()
    cfg.read(_setup_cfg_path())
    assert cfg.has_section("options.extras_require")
    extras = set(cfg.options("options.extras_require"))
    # These four are referenced by docs, CI, and the Makefile; losing any of
    # them breaks a documented install path.
    assert {
        "otlp",
        "voice",
        "desktop",
        "dev",
    } <= extras, f"expected the documented extras to exist in setup.cfg; got {sorted(extras)}"


def _extra_requirements(extra: str) -> list[str]:
    cfg = configparser.ConfigParser()
    cfg.read(_setup_cfg_path())
    return [
        line.strip()
        for line in cfg.get("options.extras_require", extra).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_dev_extra_covers_test_imports():
    """``[dev]`` MUST install everything ``test/conftest.py`` imports.

    conftest.py imports hypothesis unconditionally at module scope, so a `[dev]`
    install missing it makes the ENTIRE suite fail at collection with
    ModuleNotFoundError — not one test, all of them.
    """
    declared = " ".join(_extra_requirements("dev")).lower()
    for required in ("pytest", "hypothesis"):
        assert required in declared, (
            f"setup.cfg [options.extras_require] dev must declare {required!r} — "
            "test/conftest.py imports it at module scope, so the whole suite "
            f"fails to collect without it. Declared: {declared}"
        )


def test_python_requires_agrees_between_pyproject_and_setup_cfg():
    """setup.cfg ``python_requires`` must match pyproject ``requires-python``.

    pyproject's value is the one that lands in the built metadata, so a looser
    bound in setup.cfg is dead config that advertises support for interpreters
    the package cannot actually run on.
    """
    cfg = configparser.ConfigParser()
    cfg.read(_setup_cfg_path())
    cfg_req = cfg.get("options", "python_requires", fallback="").strip()

    proj_req = ""
    for line in _pyproject_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("requires-python"):
            proj_req = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            break

    assert proj_req, "pyproject.toml declares no requires-python"
    assert cfg_req == proj_req, (
        "python_requires disagreement: setup.cfg says "
        f"{cfg_req!r} but pyproject.toml requires-python says {proj_req!r}. "
        "pyproject wins in the built metadata, so keep them identical."
    )


def _collect_unguarded_imports(filepath: pathlib.Path) -> list[tuple[str, str]]:
    """Unguarded module-level third-party imports in a single file."""
    source = filepath.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    stdlib = sys.stdlib_module_names if hasattr(sys, "stdlib_module_names") else set()
    results: list[tuple[str, str]] = []

    for node in ast.iter_child_nodes(tree):
        imports_to_check: list[tuple[str, ast.stmt]] = []

        if isinstance(node, ast.Import):
            for alias in node.names:
                imports_to_check.append((alias.name.split(".")[0], node))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:  # absolute imports only
                imports_to_check.append((node.module.split(".")[0], node))
        elif isinstance(node, ast.Try):
            # Check if this try catches ImportError
            catches_import_error = any(
                (
                    handler.type is None
                    or (
                        isinstance(handler.type, ast.Name)
                        and handler.type.id in ("ImportError", "ModuleNotFoundError", "Exception")
                    )
                    or (
                        isinstance(handler.type, ast.Tuple)
                        and any(
                            isinstance(elt, ast.Name)
                            and elt.id in ("ImportError", "ModuleNotFoundError", "Exception")
                            for elt in handler.type.elts
                        )
                    )
                )
                for handler in node.handlers
            )
            if not catches_import_error:
                # Unguarded try body — scan for imports
                for stmt in node.body:
                    if isinstance(stmt, ast.Import):
                        for alias in stmt.names:
                            imports_to_check.append((alias.name.split(".")[0], stmt))
                    elif isinstance(stmt, ast.ImportFrom):
                        if stmt.module and stmt.level == 0:
                            imports_to_check.append((stmt.module.split(".")[0], stmt))
            # else: guarded, skip all body imports

        for root, stmt in imports_to_check:
            if root in stdlib or root.startswith("_"):
                continue
            if root == "kiro_crew":
                continue
            if root in _EXEMPT:
                continue
            results.append((root, str(filepath)))

    return results


def test_all_unguarded_third_party_imports_are_declared():
    """Every module-level unguarded third-party import in core kiro_crew
    MUST be in setup.cfg install_requires."""
    src_root = _src_root()
    declared = _parse_install_requires()

    undeclared: list[str] = []
    for py_file in sorted(src_root.rglob("*.py")):
        # Skip vendored code, test fixtures, and excluded subtrees
        rel_str = str(py_file.relative_to(src_root))
        if "_vendor" in py_file.parts or "tests_fixtures" in py_file.parts:
            continue
        if any(rel_str.startswith(excl) for excl in _EXCLUDED_SUBTREES):
            continue

        for root_module, filepath in _collect_unguarded_imports(py_file):
            if root_module not in declared:
                rel = py_file.relative_to(src_root)
                undeclared.append(f"  {root_module} (in {rel})")

    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for entry in undeclared:
        if entry not in seen:
            seen.add(entry)
            unique.append(entry)

    assert not unique, (
        "Unguarded module-level third-party imports not declared in "
        "setup.cfg install_requires (pip installs will crash):\n"
        + "\n".join(unique)
        + "\n\nFix: add the dep to setup.cfg install_requires, OR wrap the "
        "import in try/except ImportError with a no-op fallback, OR add to "
        "_EXEMPT in this test with a reason comment."
    )


def test_noop_recorder_when_otel_missing(monkeypatch):
    """When opentelemetry is not importable, get_recorder() returns a no-op."""
    import importlib

    # Save and remove all opentelemetry + provider modules from sys.modules
    to_remove = [
        k
        for k in list(sys.modules)
        if k.startswith("opentelemetry")
        or k
        in (
            "kiro_crew.metrics.provider",
            "kiro_crew.metrics.recorder",
            "kiro_crew.metrics.local_exporter",
        )
    ]
    saved = {k: sys.modules.pop(k) for k in to_remove}

    import builtins

    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError(f"No module named '{name}'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)

    try:
        # Re-import provider — it should set _OTEL_AVAILABLE = False and
        # degrade to the MetricsRecorder(None) no-op path (contract).
        spec = importlib.util.find_spec("kiro_crew.metrics.provider")
        assert spec is not None
        prov = importlib.util.module_from_spec(spec)
        sys.modules["kiro_crew.metrics.provider"] = prov
        spec.loader.exec_module(prov)

        assert prov._OTEL_AVAILABLE is False

        recorder = prov.get_recorder()
        assert not recorder.enabled

        # Methods should be callable without raising
        recorder.counter("test.counter", 1)
        recorder.histogram("test.hist", 42.0)
        recorder.up_down_counter("test.updown", -1)
    finally:
        monkeypatch.undo()
        # Restore modules
        sys.modules.update(saved)
        # Remove our injected module
        sys.modules.pop("kiro_crew.metrics.provider", None)
        # Re-import cleanly
        importlib.import_module("kiro_crew.metrics.provider")
