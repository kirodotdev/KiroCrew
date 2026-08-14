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
git carries), the sdist rules (MANIFEST.in), the wheel's package_data, and the
PyInstaller spec. That the desktop bundle stayed correct while the wheel was
broken is the evidence for keeping them separate — one lane passing says
nothing about another.

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

import os
import sys
import sysconfig
from pathlib import Path

import pytest

import kiro_crew.embeddings as embeddings_mod
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


def test_pyinstaller_spec_bundles_the_vendor_tree() -> None:
    """The desktop lane walks the tree itself and must include `_vendor`.

    This lane never reads MANIFEST.in, which is why the desktop bundle stayed
    correct while the wheel was broken — and why it needs its own assertion
    rather than inheriting the sdist one.
    """
    spec = (_REPO_ROOT / "packaging" / "kirocrew-backend.spec").read_text(encoding="utf-8")
    assert '"_vendor"' in spec


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
