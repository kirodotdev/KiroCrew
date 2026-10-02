"""Packaging guards for the vendored llama.cpp native-library payload.

In-process embeddings load `libllama` by base name through ctypes. If any file
in the per-platform closure is absent, the import fails and memory silently
degrades to keyword search behind a single WARNING — the runtime keeps working,
so nothing goes red and a release can ship broken.

That is not hypothetical. `MANIFEST.in` ends with `global-exclude *.so`, which
strips precisely `libllama.so`: every other Linux lib ends `.so.0` and the
macOS/Windows libs are `.dylib`/`.dll`, so they escape the glob. Because
`python -m build` builds the wheel FROM the sdist, that one rule shipped a
Linux wheel whose vendored llama_cpp could not load its own shared library, on
BOTH x86_64 and aarch64. It is re-included after the excludes, which makes the
fix depend on rule ORDER — an ordinary-looking edit re-breaks it silently.

These tests pin the payload per lane, because each lane selects these files by
a different mechanism and can drop them independently: the source tree (what
git carries), the sdist rules (MANIFEST.in), and the wheel's package_data. That
the desktop bundle stayed correct while the wheel was broken is the evidence for
keeping them separate — one lane passing says nothing about another. The desktop
bundle has no mechanism of its own: it pip-installs the project into its bundled
interpreter, so it inherits the wheel's package_data.

Every check runs unconditionally. The higher-fidelity alternative, shelling out
to build a real sdist, skips wherever `build`/`setuptools` is missing (this
project's own dev venv included), and a skip scores as a pass — so the guard
would be absent exactly where it matters.

These tests MODEL MANIFEST.in rather than executing it, so they are the weaker
half of the defense by construction: `build.yml` (every PR) and
`build-wheel.yml` (release/nightly) build the real wheel AND sdist and run
`scripts/verify_vendored_payload.py` against the actual artifacts. Both lanes
build the sdist on purpose — `python -m build --wheel` never evaluates
MANIFEST.in, so a wheel-only build cannot observe an sdist regression at all.
"""

from __future__ import annotations

import ast
import io
import os
import runpy
import subprocess
import sys
import sysconfig
import tarfile
import zipfile
from pathlib import Path

import pytest

import kiro_crew.embeddings as embeddings_mod
from kiro_crew._llama_lib_path import _BUNDLED_LIBS_DIR_NAMES
from kiro_crew.embeddings import (
    _LIB_PATH_ENV,
    _LIBS_DIR_NAME,
    _REQUIRED_VENDORED_LIBS,
    _platform_libs_dirname,
    verify_vendored_libs,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VENDOR_SRC = _REPO_ROOT / "src" / "kiro_crew" / "_vendor"
_LIBS_SRC = _VENDOR_SRC / _LIBS_DIR_NAME

# `*.so` is the glob that stripped libllama.so. Any required lib matching a
# packaging exclude must be re-included explicitly, so assert the exclusion
# patterns MANIFEST.in actually uses still have a matching re-include.
_PACKAGING_EXCLUDE_GLOBS = ("*.so", "*.py[cod]")


def test_source_tree_carries_every_required_lib() -> None:
    """The checkout itself must hold the full closure for all five platforms.

    Guards the upstream-upgrade path: re-extracting a new llama-cpp-python
    version can quietly drop a file (e.g. a renamed soname), and every
    downstream artifact is built from this tree.
    """
    assert verify_vendored_libs(_VENDOR_SRC) == {}


def test_required_libs_cover_every_supported_platform() -> None:
    """Every platform `_platform_libs_dirname` can return must declare a closure.

    Without this, adding a platform mapping but no required-libs entry makes
    `verify_vendored_libs` vacuously pass for it — the guard would report a
    complete payload for a platform it never checked.
    """
    shipped = {p.name for p in _LIBS_SRC.iterdir() if p.is_dir()}
    assert shipped == set(_REQUIRED_VENDORED_LIBS)


def test_every_platform_closure_names_a_libllama() -> None:
    """`libllama` is the entry point ctypes opens; a closure without it is unusable.

    The dependency libs vary by platform and upstream build, but the file
    ctypes resolves by base name does not — so this is the one member that can
    be asserted for all platforms at once, and it is exactly the file the
    `*.so` glob removed.
    """
    for plat, required in _REQUIRED_VENDORED_LIBS.items():
        assert any(
            "llama" in name and "ggml" not in name for name in required
        ), f"{plat} declares no libllama entry"


def test_verify_reports_a_missing_lib(tmp_path: Path) -> None:
    """A dropped file must be REPORTED, not tolerated.

    Mirrors the real defect: a tree that has every ggml lib but no libllama —
    which is what a published Linux wheel actually contained.
    """
    plat = "linux_x86_64"
    libs = tmp_path / _LIBS_DIR_NAME / plat
    libs.mkdir(parents=True)
    for name in _REQUIRED_VENDORED_LIBS[plat]:
        if name != "libllama.so":
            (libs / name).write_bytes(b"\x7fELF")

    missing = verify_vendored_libs(tmp_path)

    assert missing[plat] == ["libllama.so"]


def test_verify_reports_an_absent_platform_dir_as_fully_missing(tmp_path: Path) -> None:
    """A vanished platform dir is the severe form of the bug, never an exemption."""
    missing = verify_vendored_libs(tmp_path)

    assert set(missing) == set(_REQUIRED_VENDORED_LIBS)
    assert missing["linux_aarch64"] == sorted(_REQUIRED_VENDORED_LIBS["linux_aarch64"])


def test_manifest_reincludes_libs_after_the_excludes() -> None:
    """MANIFEST.in's re-include must come AFTER the `global-exclude` it undoes.

    Later rules win in MANIFEST.in, so ordering is the whole fix. Asserting the
    order (not just the line's presence) is what catches a re-sort or a newly
    appended `global-exclude` that silently re-strips the libs.
    """
    lines = [
        line.strip()
        for line in (_REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    reinclude = f"recursive-include src/kiro_crew/_vendor/{_LIBS_DIR_NAME} *"
    assert reinclude in lines, "MANIFEST.in must re-include the vendored native libs"

    last_reinclude = max(i for i, line in enumerate(lines) if line == reinclude)
    for glob in _PACKAGING_EXCLUDE_GLOBS:
        excludes = [i for i, line in enumerate(lines) if line == f"global-exclude {glob}"]
        for idx in excludes:
            assert idx < last_reinclude, (
                f"'global-exclude {glob}' at line {idx} comes after the vendored-libs "
                "re-include, so it strips native libs back out of the sdist"
            )


def test_package_data_declares_the_libs_explicitly() -> None:
    """setup.cfg must name each platform dir, not rely on `**` recursion alone.

    setuptools' `**` handling in package_data has varied across versions, so the
    wheel's copy of these files is pinned by explicit per-platform globs.
    """
    cfg = (_REPO_ROOT / "setup.cfg").read_text(encoding="utf-8")
    for plat in _REQUIRED_VENDORED_LIBS:
        assert (
            f"_vendor/{_LIBS_DIR_NAME}/{plat}/*" in cfg
        ), f"setup.cfg [options.package_data] does not explicitly ship {plat}"


def test_both_ci_lanes_run_the_shared_payload_verifier() -> None:
    """The PR lane and the release lane must share one artifact check.

    `build.yml` gates every PR; `build-wheel.yml` gates the published artifact.
    When each carried its own inline copy they could drift, and a gate that
    diverges stops guarding without ever failing. Both must also build the
    sdist: `python -m build --wheel` never evaluates MANIFEST.in, so a
    wheel-only build cannot observe an sdist regression at all.
    """
    assert (_REPO_ROOT / "scripts" / "verify_vendored_payload.py").is_file()
    for lane in ("build.yml", "build-wheel.yml"):
        text = (_REPO_ROOT / ".github" / "workflows" / lane).read_text(encoding="utf-8")
        assert "scripts/verify_vendored_payload.py" in text, f"{lane} skips the shared verifier"
        assert "python -m build --wheel" not in text, (
            f"{lane} builds only the wheel, so MANIFEST.in is never evaluated there"
        )


def test_manifest_rules_keep_every_required_lib() -> None:
    """Evaluate MANIFEST.in's include/exclude rules over the required libs.

    Models MANIFEST.in's rule kinds in file order with later rules winning,
    rather than shelling out to the build backend. A subprocess sdist build is
    the more faithful check but SKIPS wherever `build`/`setuptools` is absent
    (this project's dev venv included), and a skip scores as a pass — precisely
    how a packaging regression reaches users unnoticed. `build-wheel.yml`
    performs the faithful artifact check where those tools do exist.

    Every directive that can REMOVE a file is modelled, not just the `*.so`
    glob that caused the original bug: an unmodelled remover would make this
    test pass while the real sdist drops the lib, which is a worse failure than
    having no test. `test_manifest_directives_are_all_modelled` fails if
    MANIFEST.in starts using a directive this parser does not understand.
    """
    import fnmatch

    # (kind, dir, filename-pattern) in file order. `recursive-include DIR PAT`
    # and `recursive-exclude DIR PAT` match PAT against the basename at any
    # depth under DIR; `global-exclude PAT` matches the basename tree-wide;
    # `prune DIR` drops everything under DIR.
    rules: list[tuple[str, str, str]] = []
    for raw in (_REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
        fields = raw.strip().split()
        if not fields or fields[0].startswith("#"):
            continue
        directive, args = fields[0], fields[1:]
        if directive == "global-exclude":
            rules += [("exclude", "", pat) for pat in args]
        elif directive == "prune" and args:
            rules.append(("exclude", args[0].rstrip("/"), "*"))
        elif directive in ("recursive-include", "recursive-exclude") and len(args) > 1:
            kind = "include" if directive == "recursive-include" else "exclude"
            rules += [(kind, args[0].rstrip("/"), pat) for pat in args[1:]]

    for plat, required in _REQUIRED_VENDORED_LIBS.items():
        directory = f"src/kiro_crew/_vendor/{_LIBS_DIR_NAME}/{plat}"
        for name in required:
            shipped = False
            for kind, rule_dir, pattern in rules:
                under = not rule_dir or directory == rule_dir or directory.startswith(rule_dir + "/")
                if under and fnmatch.fnmatch(name, pattern):
                    shipped = kind == "include"
            assert shipped, f"MANIFEST.in rules exclude {directory}/{name} from the sdist"


def test_manifest_directives_are_all_modelled() -> None:
    """Fail if MANIFEST.in uses a directive the rule model above ignores.

    The model is only trustworthy while it understands every directive in the
    file. A new `exclude`/`graft`/`include` line touching this tree would
    otherwise be silently skipped, turning the guard above into a false
    negative — the one outcome worse than no guard at all.
    """
    modelled = {"global-exclude", "prune", "recursive-include", "recursive-exclude", "include"}
    used = set()
    for raw in (_REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
        fields = raw.strip().split()
        if fields and not fields[0].startswith("#"):
            used.add(fields[0])

    # `include` needs no modelling: it takes literal paths, and none of the
    # required libs is named by one. Any OTHER unmodelled directive can remove
    # files and must be added to the model before this test can pass.
    assert used <= modelled, f"unmodelled MANIFEST.in directives: {sorted(used - modelled)}"


def test_running_platform_payload_is_complete() -> None:
    """The installed tree this test runs against must be usable on THIS host.

    Catches an install-time (not just build-time) drop — e.g. a wheel whose
    package_data missed the running platform's directory.
    """
    plat = _platform_libs_dirname()
    if plat is None:
        pytest.skip(f"no vendored libs for {sys.platform}/{sysconfig.get_platform()}")

    assert verify_vendored_libs().get(plat) is None


def _stub_libs_tree(root: Path, plat: str, *, complete: bool) -> Path:
    """Create a fake vendored-libs tree, optionally missing its libllama."""
    libs = root / _LIBS_DIR_NAME / plat
    libs.mkdir(parents=True, exist_ok=True)
    for name in _REQUIRED_VENDORED_LIBS[plat]:
        if not complete and "llama" in name and "ggml" not in name:
            continue
        (libs / name).write_bytes(b"\x7fELF")
    return libs


class TestIncompletePayloadRefusal:
    """`_load_llama_class()` behavior when the bundled closure is incomplete."""

    @staticmethod
    def _load(monkeypatch, vendor: Path, plat: str = "linux_x86_64"):
        monkeypatch.setattr(embeddings_mod, "_VENDOR_DIR", vendor)
        monkeypatch.setattr(embeddings_mod, "_platform_libs_dirname", lambda: plat)
        embeddings_mod._load_llama_class.cache_clear()
        try:
            return embeddings_mod._load_llama_class()
        finally:
            embeddings_mod._load_llama_class.cache_clear()

    def test_an_incomplete_payload_refuses_and_names_the_file(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """Refuse early with the absent filename, not ctypes' base-name message."""
        monkeypatch.delenv(_LIB_PATH_ENV, raising=False)
        _stub_libs_tree(tmp_path, "linux_x86_64", complete=False)

        with caplog.at_level("WARNING", logger=embeddings_mod.__name__):
            assert self._load(monkeypatch, tmp_path) is None

        assert "libllama.so" in caplog.text
        assert _LIB_PATH_ENV not in os.environ

    def test_an_operator_override_is_not_refused(self, tmp_path, monkeypatch, caplog) -> None:
        """An explicit LLAMA_CPP_LIB_PATH must survive an incomplete bundled tree.

        The libs then load from the operator's directory, so the bundled tree no
        longer decides whether the runtime works. Refusing on it would disable
        the documented escape hatch (a GPU build, or a hand-restored lib dir)
        for exactly the users an incomplete wheel stranded — turning a
        diagnostic into a second outage.

        Asserts the refusal did NOT fire rather than the return value: with a
        stub tree the real ctypes import fails either way, so `None` cannot tell
        "refused early" from "tried and failed". The warning is what separates
        them, and it is the observable a user would act on.
        """
        _stub_libs_tree(tmp_path, "linux_x86_64", complete=False)
        override = tmp_path / "operator-libs"
        override.mkdir()
        monkeypatch.setenv(_LIB_PATH_ENV, str(override))

        with caplog.at_level("WARNING", logger=embeddings_mod.__name__):
            self._load(monkeypatch, tmp_path)

        assert "is incomplete" not in caplog.text, (
            "the completeness gate refused despite an operator-set "
            f"{_LIB_PATH_ENV}, disabling the documented override"
        )
        assert os.environ[_LIB_PATH_ENV] == str(override), "the override was overwritten"

    def test_an_inherited_bundled_path_does_not_exempt_the_gate(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """A bundled directory in the environment is not the operator's override.

        A process that bypasses the entry prelude still applies this install's
        completeness gate. The inherited value remains unchanged, proving the
        loader classifies it without mutating the environment.
        """
        _stub_libs_tree(tmp_path, "linux_x86_64", complete=False)
        inherited = (
            tmp_path
            / "previous-install"
            / "site-packages"
            / "kiro_crew"
            / "_vendor"
            / "llama_cpp_libs"
            / "linux_x86_64"
        )
        monkeypatch.setenv(_LIB_PATH_ENV, str(inherited))

        with caplog.at_level("INFO", logger=embeddings_mod.__name__):
            assert self._load(monkeypatch, tmp_path) is None

        assert "is incomplete" in caplog.text
        assert "libllama.so" in caplog.text
        assert str(inherited) in caplog.text
        assert "left in the environment" in caplog.text
        assert os.environ[_LIB_PATH_ENV] == str(inherited)


class TestPayloadVerifierWithoutRuntimeDependencies:
    """Run the real build gate with only Python's standard library available."""

    @staticmethod
    def _run(script: Path, dist: Path) -> subprocess.CompletedProcess[str]:
        # -I ignores PYTHONPATH/user-site; -S also excludes the venv's packages.
        # The verifier must not import the runtime just to read its declarations.
        return subprocess.run(
            [sys.executable, "-I", "-S", str(script), str(dist)],
            cwd=dist,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    @pytest.mark.parametrize("missing_from", [None, "wheel", "sdist", "both"])
    def test_artifact_contents(self, tmp_path: Path, missing_from: str | None) -> None:
        """Both archives pass when complete; either missing member stays fatal."""
        wheel = tmp_path / "kirocrew-0.0.0-py3-none-any.whl"
        sdist = tmp_path / "kirocrew-0.0.0.tar.gz"
        missing = f"kiro_crew/_vendor/{_LIBS_DIR_NAME}/linux_x86_64/libllama.so"
        with zipfile.ZipFile(wheel, "w") as whl, tarfile.open(sdist, "w:gz") as tar:
            for platform, required in _REQUIRED_VENDORED_LIBS.items():
                for name in required:
                    rel = f"kiro_crew/_vendor/{_LIBS_DIR_NAME}/{platform}/{name}"
                    if rel != missing or missing_from not in ("wheel", "both"):
                        whl.writestr(rel, b"stub")
                    if rel != missing or missing_from not in ("sdist", "both"):
                        info = tarfile.TarInfo(f"kirocrew-0.0.0/src/{rel}")
                        info.size = 4
                        tar.addfile(info, io.BytesIO(b"stub"))

        result = self._run(_REPO_ROOT / "scripts" / "verify_vendored_payload.py", tmp_path)

        assert result.returncode == (0 if missing_from is None else 1), result.stderr
        if missing_from is None:
            assert "payload complete" in result.stdout
            assert result.stderr == ""
        else:
            assert "payload incomplete" in result.stderr
            expected = []
            if missing_from in ("wheel", "both"):
                expected.append(f"  {wheel.name}: missing {missing}")
            if missing_from in ("sdist", "both"):
                expected.append(f"  {sdist.name}: missing src/{missing}")
            assert result.stderr.splitlines()[1:] == expected

    def test_missing_artifacts(self, tmp_path: Path) -> None:
        """An empty dist still reports missing artifacts, not an import error."""
        result = self._run(_REPO_ROOT / "scripts" / "verify_vendored_payload.py", tmp_path)
        assert result.returncode == 2, result.stderr
        assert "expected a wheel AND an sdist" in result.stderr


class TestPayloadDeclarations:
    @staticmethod
    def _read(source: Path):
        script = runpy.run_path(str(_REPO_ROOT / "scripts" / "verify_vendored_payload.py"))
        return script["_read_lib_declarations"](source)

    def test_reads_both_literals_from_the_startup_leaf(self, tmp_path: Path) -> None:
        """One file carries both declarations, so the gate reads one file.

        No import is followed and no second file is opened: a leaf that holds
        only one of the two is the missing-declaration failure below, not a
        cue to go looking in `embeddings.py`.
        """
        source = tmp_path / "_llama_lib_path.py"
        source.write_text(
            "_LIBS_DIR_NAME = 'native'\n"
            "_REQUIRED_VENDORED_LIBS = {'example': ('example.so',)}\n",
            encoding="utf-8",
        )

        assert self._read(source) == ("native", {"example": ("example.so",)})

    @pytest.mark.parametrize("annotation", ["", ": str"])
    def test_reads_literals_without_executing_source(self, tmp_path: Path, annotation: str) -> None:
        source = tmp_path / "_llama_lib_path.py"
        source.write_text(
            "raise RuntimeError('must not execute')\n"
            f"_LIBS_DIR_NAME{annotation} = 'native'\n"
            "_REQUIRED_VENDORED_LIBS: dict = {'example': ('example.so',)}\n",
            encoding="utf-8",
        )
        assert self._read(source) == ("native", {"example": ("example.so",)})

    @pytest.mark.parametrize(
        "declaration",
        ["", "_REQUIRED_VENDORED_LIBS = dict()", "_REQUIRED_VENDORED_LIBS: dict"],
    )
    def test_missing_or_computed_manifest_fails(self, tmp_path: Path, declaration: str) -> None:
        source = tmp_path / "_llama_lib_path.py"
        source.write_text(f"_LIBS_DIR_NAME = 'native'\n{declaration}\n", encoding="utf-8")
        with pytest.raises(ValueError):
            self._read(source)

    def test_the_bundled_dir_names_are_derived_from_the_closure(self) -> None:
        """`_BUNDLED_LIBS_DIR_NAMES` must be the closure's keys, not a second list.

        The two sets being equal today is what a re-listed literal also looks
        like, and that duplicate is exactly what drifted: a platform added to
        one and not the other makes `_is_bundled_libs_dir` disagree with the
        payload gate about what a bundled directory is. So this reads the leaf
        and pins the DERIVATION -- the frozenset is built from the dict's name,
        and no platform name appears as a literal anywhere else in the file.
        """
        leaf = _REPO_ROOT / "src" / "kiro_crew" / "_llama_lib_path.py"
        tree = ast.parse(leaf.read_text(encoding="utf-8"), filename=str(leaf))

        assert _BUNDLED_LIBS_DIR_NAMES == frozenset(_REQUIRED_VENDORED_LIBS)

        derived = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_BUNDLED_LIBS_DIR_NAMES" for t in node.targets
            )
        ]
        assert len(derived) == 1, "_BUNDLED_LIBS_DIR_NAMES must have exactly one assignment"
        call = derived[0]
        assert (
            isinstance(call, ast.Call)
            and getattr(call.func, "id", None) == "frozenset"
            and [getattr(a, "id", None) for a in call.args] == ["_REQUIRED_VENDORED_LIBS"]
            and not call.keywords
        ), "_BUNDLED_LIBS_DIR_NAMES must be frozenset(_REQUIRED_VENDORED_LIBS)"

        declaration = next(
            node
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(t, ast.Name) and t.id == "_REQUIRED_VENDORED_LIBS"
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
        )
        inside = {id(node) for node in ast.walk(declaration)}
        elsewhere = sorted(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and node.value in _REQUIRED_VENDORED_LIBS
            and id(node) not in inside
        )
        assert elsewhere == [], f"platform names re-listed outside the closure: {elsewhere}"


class TestVendoredLoaderLibPathPrecedence:
    """The ``kiro_crew DIVERGENCE FROM UPSTREAM`` in ``_vendor/llama_cpp/llama_cpp.py``.

    The host hands the bundled directory to the vendored loader through a
    process-local seam (a module in ``sys.modules``) so that nothing is ever
    written to ``LLAMA_CPP_LIB_PATH`` -- a child spawned while the import runs
    would inherit that. These tests execute the REAL vendored module up to and
    including its ``load_shared_library`` call, with that call recorded instead
    of performed, and check which directory reaches it: the seam first, the
    operator's variable second, upstream's ``<package>/lib`` default last.
    """

    _LOADER = _VENDOR_SRC / "llama_cpp" / "llama_cpp.py"

    def _resolved_base_path(self, monkeypatch, *, seam: str | None, env: str | None) -> Path:
        import types

        source = self._LOADER.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(self._LOADER))
        prefix: list[ast.stmt] = []
        for node in tree.body:
            prefix.append(node)
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_lib" for t in node.targets)
                and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "load_shared_library"
            ):
                break
        else:
            pytest.fail("the vendored loader no longer assigns `_lib = load_shared_library(...)`")
        module_prefix = ast.Module(body=prefix, type_ignores=[])
        code = compile(module_prefix, str(self._LOADER), "exec")

        recorded: list[Path] = []
        extensions = types.ModuleType("llama_cpp._ctypes_extensions")
        extensions.load_shared_library = lambda name, base_path: recorded.append(base_path)  # type: ignore[attr-defined]
        extensions.byref = object()  # type: ignore[attr-defined]
        extensions.ctypes_function_for_shared_library = lambda lib: lib  # type: ignore[attr-defined]
        package = types.ModuleType("llama_cpp")
        package.__path__ = [str(self._LOADER.parent)]  # type: ignore[attr-defined]
        for name in [n for n in sys.modules if n == "llama_cpp" or n.startswith("llama_cpp.")]:
            monkeypatch.delitem(sys.modules, name)
        monkeypatch.setitem(sys.modules, "llama_cpp", package)
        monkeypatch.setitem(sys.modules, "llama_cpp._ctypes_extensions", extensions)

        monkeypatch.setitem(sys.modules, embeddings_mod._LIB_PATH_SEAM, object())
        monkeypatch.delitem(sys.modules, embeddings_mod._LIB_PATH_SEAM)
        if seam is not None:
            seam_module = types.ModuleType(embeddings_mod._LIB_PATH_SEAM)
            seam_module.LIBS_DIR = seam  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, embeddings_mod._LIB_PATH_SEAM, seam_module)
        if env is None:
            monkeypatch.delenv(_LIB_PATH_ENV, raising=False)
        else:
            monkeypatch.setenv(_LIB_PATH_ENV, env)

        namespace = {"__name__": "llama_cpp.llama_cpp", "__file__": str(self._LOADER)}
        # The code object is the repository's own vendored loader, read from
        # disk and cut at its library-load statement: no external input reaches
        # it, which is what the audit rule guards against.
        exec(code, namespace)  # noqa: S102  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
        assert len(recorded) == 1, "the prefix must reach exactly one load_shared_library call"
        return Path(recorded[0])

    def test_the_seam_names_the_directory(self, monkeypatch, tmp_path: Path) -> None:
        bundled = tmp_path / "bundled"
        assert self._resolved_base_path(monkeypatch, seam=str(bundled), env=None) == bundled

    def test_the_seam_wins_over_a_stale_environment_value(self, monkeypatch, tmp_path: Path) -> None:
        """The host applies the override rule before publishing; what it published stands."""
        bundled = tmp_path / "bundled"
        stale = tmp_path / "previous-install"
        assert self._resolved_base_path(monkeypatch, seam=str(bundled), env=str(stale)) == bundled

    def test_without_a_seam_the_operator_variable_is_upstream_behaviour(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        operator = tmp_path / "gpu-build"
        assert self._resolved_base_path(monkeypatch, seam=None, env=str(operator)) == operator

    def test_without_either_the_upstream_default_applies(self, monkeypatch) -> None:
        resolved = self._resolved_base_path(monkeypatch, seam=None, env=None)
        assert resolved == self._LOADER.parent.resolve() / "lib"

    def test_the_seam_is_published_by_the_host_under_that_key(self) -> None:
        """Pin the two literals to each other; the vendor README lists this divergence."""
        source = self._LOADER.read_text(encoding="utf-8")
        assert f'sys.modules.get("{embeddings_mod._LIB_PATH_SEAM}")' in source
        readme = (_VENDOR_SRC / "README.md").read_text(encoding="utf-8")
        assert embeddings_mod._LIB_PATH_SEAM in readme
