"""The AWS Control crew image's build context must reach every install lane.

The image is built from files on disk: two Dockerfiles, ``requirements.txt``,
``requirements-dev.txt``, the container's own source, and a vendor placeholder. If any of
them is absent from an installed copy, the build fails at image-build time with a missing
file -- long after the install that dropped it, and with nothing in the install output
saying so.

Two lanes select these files by DIFFERENT mechanisms and can drop them independently, so
each is pinned separately. This follows ``test_vendored_llama_payload.py``, which exists
because exactly one of these lanes silently shipped a broken wheel:

* the **sdist**, governed by ``MANIFEST.in``. ``python -m build`` builds the wheel FROM the
  sdist, so a file this file does not reach is absent from every published wheel whatever
  ``package_data`` says. ``python -m build --wheel`` never evaluates ``MANIFEST.in`` at all,
  so a wheel-only build cannot observe a regression here.
* the **wheel**, governed by ``[options.package_data]`` in ``setup.cfg``. A pattern there
  globs by fixed directory name, and ``*`` does not cross a path separator, so an entry
  written for ``apps/builtins/*/`` does not descend into ``aws_control/crew/runtime/``.

Neither lane is reached by the patterns that were already present: the tree's Python files
are found by ``packages = find:``, its ``.md`` files by
``recursive-include src/kiro_crew/apps *.md``, and its Dockerfiles by nothing, because they
have no suffix to match.

These tests MODEL both files rather than executing them, which makes them the weaker half
of the defence by construction -- ``build.yml`` builds the real sdist and wheel. The
stronger alternative here, shelling out to ``python -m build``, skips wherever ``build`` is
missing, and a skip scores as a pass, so the guard would be absent exactly where it matters.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "MANIFEST.in"
SETUP_CFG = REPO_ROOT / "setup.cfg"
RUNTIME = REPO_ROOT / "src" / "kiro_crew" / "apps" / "builtins" / "aws_control" / "crew" / "runtime"


def _payload() -> list[Path]:
    """The members no other rule reaches: everything that is not ``.py`` or ``.md``.

    ``.whl`` is excluded because a wheel under ``vendor/`` is build INPUT rather than
    content: it is staged there to build a MicroVM image and must reach no lane. Were
    it in this list, the guards saying it must ship and the guards saying it must not
    would contradict each other on any machine that had staged one.
    """
    return [
        p
        for p in sorted(RUNTIME.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".py", ".md", ".whl"}
    ]


def _sdist_rules() -> list[tuple[str, list[str]]]:
    """``MANIFEST.in``'s ``recursive-include`` rules that survive its excludes.

    Position is as load-bearing as presence: ``global-exclude`` and ``prune`` lines apply
    in file order, so an include written above them is undone by them. Only the rules
    below the last exclude are in force, and those are what this returns.
    """
    lines = [ln.strip() for ln in MANIFEST.read_text(encoding="utf-8").splitlines()]
    last_exclude = max(
        (i for i, ln in enumerate(lines) if ln.startswith(("global-exclude", "prune", "exclude"))),
        default=-1,
    )
    rules: list[tuple[str, list[str]]] = []
    for i, ln in enumerate(lines):
        if i <= last_exclude or not ln.startswith("recursive-include"):
            continue
        parts = ln.split()
        if len(parts) >= 3:
            rules.append((parts[1], parts[2:]))
    return rules


def _reaches_sdist(path: Path, rules: list[tuple[str, list[str]]]) -> bool:
    """Model ``recursive-include`` the way distutils reads it.

    The file is under ``dir`` and its NAME matches a pattern -- not the whole path
    matched with ``fnmatch``, whose ``*`` crosses separators and would accept rules
    distutils rejects.
    """
    rel = path.relative_to(REPO_ROOT).as_posix()
    return any(
        rel.startswith(f"{d}/") and any(fnmatch.fnmatch(path.name, p) for p in pats)
        for d, pats in rules
    )


def _package_data_patterns() -> list[str]:
    """The ``[options.package_data]`` patterns, in file order."""
    text = SETUP_CFG.read_text(encoding="utf-8")
    block = text.split("[options.package_data]", 1)
    assert len(block) == 2, "setup.cfg has no [options.package_data] section"

    patterns: list[str] = []
    for line in block[1].splitlines()[1:]:
        if line.startswith("["):
            break
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.endswith("="):  # the `kiro_crew =` key line
            continue
        if not line[0].isspace():  # a new top-level key ends the section's values
            break
        patterns.append(entry)
    return patterns


def _wheel_selected(pkg_root: Path) -> set[Path]:
    """What ``package_data`` selects, EXPANDED against the tree rooted at ``pkg_root``.

    ``Path.glob`` rather than ``fnmatch``, because that is what setuptools does and the
    two disagree on the thing that matters here -- ``fnmatch``'s ``*`` crosses a path
    separator, so it would accept a pattern that never descends into
    ``aws_control/crew/runtime/`` and report a lane as covered while every wheel shipped
    nothing.

    Takes the root so one resolver answers for the real tree and for a fixture tree
    holding a state the real one must not be left in, such as a staged wheel.
    """
    patterns = _package_data_patterns()
    assert patterns, "no package_data patterns parsed"
    return {p.resolve() for pat in patterns for p in pkg_root.glob(pat) if p.is_file()}


def _wheel_shipped() -> set[Path]:
    """What ``package_data`` selects out of the real source tree."""
    return _wheel_selected(REPO_ROOT / "src" / "kiro_crew")


def test_the_payload_this_guards_is_not_empty() -> None:
    """Non-vacuity, and it names what is at stake.

    Every assertion below quantifies over this list. Were it empty they would all pass
    while guarding nothing, and the day someone moved the Dockerfiles out they would go on
    passing.
    """
    payload = _payload()
    assert payload, "no non-.py/.md members under crew/runtime -- these guards are vacuous"
    names = {p.name for p in payload}
    assert "Dockerfile" in names, "the extensionless member is the reason for these rules"
    assert "requirements.txt" in names


def test_the_sdist_rules_reach_the_payload() -> None:
    """``MANIFEST.in`` must carry every member, by a rule placed after the excludes."""
    rules = _sdist_rules()
    assert rules, "MANIFEST.in has no recursive-include after its excludes"

    unreached = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _payload()
        if not _reaches_sdist(path, rules)
    ]

    assert not unreached, (
        "these files are in no sdist, so no published wheel carries them and the image "
        f"cannot be built from an installed copy: {unreached}"
    )


def test_the_wheel_rules_reach_the_payload() -> None:
    """``package_data`` must carry every member too.

    Independent of the sdist lane: the desktop bundle pip-installs the project, so it
    inherits this lane and not the other.
    """
    shipped = _wheel_shipped()
    pkg_root = REPO_ROOT / "src" / "kiro_crew"
    unreached = [
        path.relative_to(pkg_root).as_posix()
        for path in _payload()
        if path.resolve() not in shipped
    ]

    assert not unreached, (
        "these files are in no wheel, so a pip or desktop install cannot build the "
        f"image: {unreached}"
    )


# --------------------------------------------------------------------------
# The other direction: what must NOT travel
# --------------------------------------------------------------------------
# The rules above answer "does the image's build context reach an installed copy".
# These answer the opposite question, because a rule broad enough to satisfy the first
# was: `container_tests/` is the image's OWN test suite, 4,000 lines of it, and it was
# in every sdist, wheel and DMG. Nothing on a user's machine can run it -- the tests
# exercise a Linux container's supervisor, and the machine that installed the app never
# runs that container.
#
# Both directions are asserted against the same parsed rules, so a future widening that
# restores the payload by taking the whole tree cannot pass both.


def _container_test_files() -> list[Path]:
    return [
        p
        for p in sorted((RUNTIME / "container_tests").rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    ]


def test_the_container_test_suite_is_worth_excluding() -> None:
    """Non-vacuity for the two guards below, and it names the scale.

    An empty list would make both pass while proving nothing, including on the day the
    suite is renamed and starts shipping again under its new name.
    """
    files = _container_test_files()
    assert len(files) >= 20, f"expected the image's test suite here, found {len(files)} files"


def test_the_sdist_rules_do_not_carry_the_container_test_suite() -> None:
    rules = _sdist_rules()
    assert rules, "MANIFEST.in has no recursive-include after its excludes"
    shipped = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _container_test_files()
        if _reaches_sdist(path, rules)
    ]
    assert not shipped, (
        "the image's own test suite is in the sdist, so every published wheel carries "
        f"tests nothing on a user's machine can run: {len(shipped)} files, e.g. {shipped[:3]}"
    )


def test_the_wheel_rules_do_not_carry_the_container_test_suite() -> None:
    shipped_paths = _wheel_shipped()
    pkg_root = REPO_ROOT / "src" / "kiro_crew"
    shipped = [
        path.relative_to(pkg_root).as_posix()
        for path in _container_test_files()
        if path.resolve() in shipped_paths
    ]
    assert not shipped, (
        "the image's own test suite is in the wheel, so a pip or desktop install carries "
        f"tests nothing on that machine can run: {len(shipped)} files, e.g. {shipped[:3]}"
    )


# --------------------------------------------------------------------------
# The staged wheel, which must travel in NEITHER lane
# --------------------------------------------------------------------------
# `vendor/` is the one directory in this tree whose contents are build INPUT rather
# than shipped content: `scripts/build_microvm_image_zip.py` stages a Kiro Crew wheel
# there and `cloud/microvm/engine.py` reads it back to assemble a MicroVM recipe. A
# packaging rule that reaches it therefore puts each wheel inside the next one.
#
# Measured on one tree: 36.20 MB built with the directory empty, 72.22 MB built with
# one wheel staged, the staged wheel appearing as a 35.94 MB member. `MAX_RECIPE_BYTES`
# is 64 MiB, so the FIRST image build on a machine succeeds and the second refuses with
# "the recipe zip is N bytes, over the 67108864-byte ceiling" -- a failure that names a
# size and not its cause.
#
# `.gitignore` does not prevent this. It governs what git carries; setuptools packages
# from the working tree and never consults it.
#
# Both lanes are asserted because neither implies the other. `include_package_data` is
# on, so setuptools reads MANIFEST.in through the egg-info manifest even for a
# wheel-only build: a rule in EITHER file can put the staged wheel in a wheel.

_STAGED_WHEEL = "kirocrew-0.9.0-py3-none-any.whl"
_VENDOR_REL = "apps/builtins/aws_control/crew/runtime/vendor"


def test_the_staging_directory_ships_its_placeholder() -> None:
    """Non-vacuity, and it pins what the vendor rules exist to carry.

    The directory must exist in an installed copy so that a build with nothing staged
    reports a missing WHEEL rather than a missing directory. If the placeholder stopped
    shipping, the guards below would still pass while the vendor rules carried nothing
    at all -- which is the state they were in when the only thing they ever shipped was
    the staged wheel.
    """
    placeholder = RUNTIME / "vendor" / ".gitkeep"
    assert placeholder.is_file(), "the placeholder that keeps the staging directory is gone"

    rules = _sdist_rules()
    assert _reaches_sdist(placeholder, rules), "no sdist rule carries the staging placeholder"
    assert placeholder.resolve() in _wheel_shipped(), "package_data drops the staging placeholder"


def test_the_sdist_rules_do_not_carry_a_staged_wheel() -> None:
    """A wheel staged for an image build must reach no sdist, and so no published wheel."""
    rules = _sdist_rules()
    assert rules, "MANIFEST.in has no recursive-include after its excludes"

    staged = RUNTIME / "vendor" / _STAGED_WHEEL
    assert not _reaches_sdist(staged, rules), (
        f"an sdist rule carries {_VENDOR_REL}/*.whl, so every wheel built from an sdist "
        "embeds the previous wheel and the next image build refuses on size"
    )


def test_the_wheel_rules_do_not_carry_a_staged_wheel(tmp_path: Path) -> None:
    """Same for ``package_data``, evaluated with a wheel actually staged.

    Against a fixture tree rather than the real one: the real source tree must not be
    left holding a wheel, since ``engine._staged_wheel`` refuses a directory with more
    than one and an interrupted test would break the next image build. The patterns and
    the resolver are the real ones.
    """
    pkg_root = tmp_path / "kiro_crew"
    vendor = pkg_root / _VENDOR_REL
    vendor.mkdir(parents=True)
    (vendor / ".gitkeep").write_text("", encoding="utf-8")
    (vendor / _STAGED_WHEEL).write_bytes(b"PK\x03\x04 not really a wheel")

    selected = {p.name for p in _wheel_selected(pkg_root)}

    assert ".gitkeep" in selected, (
        "the fixture tree selected nothing, so this guard is measuring a resolver that "
        "cannot see the directory rather than a rule that excludes the wheel"
    )
    assert _STAGED_WHEEL not in selected, (
        f"a package_data pattern reaches {_VENDOR_REL}/*.whl, so a pip or desktop install "
        "carries the previous wheel inside this one and the next image build refuses on size"
    )
