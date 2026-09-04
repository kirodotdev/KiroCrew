"""``kiro_crew/BUILD_VERSION`` -- a distribution's build stamp over ``__version__``.

The contract is the FILE (its name and the ``<base>.N`` shape), not a Python
API, so the helpers are private and imported here under test-local names.

A distribution that repackages one core release as several builds of its own
(``0.6.0.10``, ``0.6.0.11``, ... of the same ``0.6.0``) had no way to make the
running process say which one it was: the About page's version chip,
``/api/status``, ``/api/health``, ``kirocrew --version`` and the diagnostics
report all read ``kiro_crew.__version__``, several through an import-time copy
(``dashboard/handlers/updates.py::_local_version``, ``ws.py``) that nothing
after import can reach. The stamp is a file BESIDE ``kiro_crew/__init__.py``,
written at packaging time and resolved at import -- the one point that
precedes every copy -- and deliberately not an environment variable, which the
local operator could set to wave a forbidden build past the governance
minimum-version floor.

Pinned here: the shape rule (only the base, or the base plus ``.N`` numeric
segments over a bare numeric base, is honoured; anything else is refused with
a warning and the base stays); the file resolution (absent, blank, unreadable,
foreign, valid); and the reach -- in a fresh interpreter importing a package
that carries the file, the dashboard's import-time copies, ``version_display``
and ``--version`` all report the stamp, and the governance floor sees it too.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew import _BUILD_VERSION_FILENAME as BUILD_VERSION_FILENAME
from kiro_crew import _build_version_override as build_version_override

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
PKG_DIR = SRC / "kiro_crew"


# ── build_version_override: the shape rule ──────────────────────────────────


@pytest.mark.parametrize(
    "candidate, expected",
    [
        ("0.6.0.12", "0.6.0.12"),
        ("0.6.0", "0.6.0"),  # the first build on a new core IS the base
        ("0.6.0.0", "0.6.0.0"),
        ("0.6.0.1.2", None),  # exactly one segment: every named build uses one
        ("  0.6.0.12\n", "0.6.0.12"),  # a trailing newline from `echo >`
        ("0.5.0.16", None),  # a different base
        ("0.6.1", None),
        ("0.6.0-rc.1", None),  # a prerelease stamp is not a build of the base
        ("0.6.0rc1", None),
        ("0.6.0.", None),
        ("0.6.0.x", None),
        ("0.6.0.12a", None),
        ("0.6.01", None),  # prefix without the dot boundary
        ("0.6.0+toolbox.12", None),  # local segments do not parse downstream
        ("", None),
        (None, None),
    ],
)
def test_shape_rule(candidate, expected) -> None:
    assert build_version_override("0.6.0", candidate) == expected


@pytest.mark.parametrize(
    "base, candidate",
    [
        ("0.6.0rc4", "0.6.0rc4.10"),  # composes to something _is_newer cannot parse
        ("0.6.0-nightly.20260904t000000", "0.6.0-nightly.20260904t000000.1"),
        ("0.6.0.dev20260904", "0.6.0.dev20260904.1"),
    ],
)
def test_a_prerelease_base_takes_no_suffix(base, candidate) -> None:
    """Only a bare numeric release can be extended; the release lanes' own
    stamps (rc, nightly, dev) are already the whole build identity."""
    assert build_version_override(base, candidate) is None
    assert build_version_override(base, base) == base  # the base itself still passes


def test_every_non_digit_in_a_segment_is_refused() -> None:
    """The rule, not a sample of it: segments are ASCII decimal only."""
    import string

    for ch in string.printable:
        # "." is the segment separator and surrounding whitespace is stripped
        # (both pinned in test_shape_rule); everything else must be refused.
        if ch.isdigit() or ch == "." or ch.isspace():
            continue
        for bad in (f"0.6.0.{ch}", f"0.6.0.{ch}12", f"0.6.0.1{ch}2", f"0.6.0.12{ch}"):
            assert build_version_override("0.6.0", bad) is None, repr(bad)
    # Unicode digit classes are not ASCII decimal; a build script never writes
    # them, and a comparator downstream would choke.
    assert build_version_override("0.6.0", "0.6.0.\u0663") is None
    assert build_version_override("0.6.0", "0.6.0.\u00b2") is None
    for n in range(0, 200):
        assert build_version_override("0.6.0", f"0.6.0.{n}") == f"0.6.0.{n}"


def test_empty_base_never_matches() -> None:
    assert build_version_override("", "0.6.0.12") is None


# ── _apply_build_version_file: what the import does with the file ───────────


def test_no_file_keeps_the_base(tmp_path) -> None:
    assert kiro_crew._apply_build_version_file("0.6.0", str(tmp_path)) == "0.6.0"


def test_no_package_dir_keeps_the_base() -> None:
    assert kiro_crew._apply_build_version_file("0.6.0", "") == "0.6.0"
    assert kiro_crew._apply_build_version_file("0.6.0", None) == "0.6.0"


def test_a_blank_file_keeps_the_base(tmp_path) -> None:
    (tmp_path / BUILD_VERSION_FILENAME).write_text("  \n", encoding="utf-8")
    assert kiro_crew._apply_build_version_file("0.6.0", str(tmp_path)) == "0.6.0"


def test_a_build_of_the_base_is_applied(tmp_path) -> None:
    (tmp_path / BUILD_VERSION_FILENAME).write_text("0.6.0.12\n", encoding="utf-8")
    assert kiro_crew._apply_build_version_file("0.6.0", str(tmp_path)) == "0.6.0.12"


def test_an_unreadable_file_keeps_the_base(tmp_path) -> None:
    """A directory where the file should be: open() raises, the base stays."""
    (tmp_path / BUILD_VERSION_FILENAME).mkdir()
    assert kiro_crew._apply_build_version_file("0.6.0", str(tmp_path)) == "0.6.0"


def test_a_foreign_value_is_refused_with_one_warning(tmp_path, caplog) -> None:
    (tmp_path / BUILD_VERSION_FILENAME).write_text("0.5.0.16\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="kiro_crew"):
        assert kiro_crew._apply_build_version_file("0.6.0", str(tmp_path)) == "0.6.0"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert BUILD_VERSION_FILENAME in warnings[0].getMessage()
    assert "0.5.0.16" in warnings[0].getMessage()


def test_an_oversized_file_is_refused_not_truncated(tmp_path, caplog) -> None:
    """The read is capped, and a file past the cap is a foreign value: it must
    not be truncated into a prefix that happens to parse as ``<base>.N``."""
    (tmp_path / BUILD_VERSION_FILENAME).write_bytes(b"0.6.0." + b"9" * 4096)
    with caplog.at_level("WARNING", logger="kiro_crew"):
        assert kiro_crew._apply_build_version_file("0.6.0", str(tmp_path)) == "0.6.0"
    assert BUILD_VERSION_FILENAME in caplog.text
    # ...while a stamp comfortably under the cap is still honoured.
    (tmp_path / BUILD_VERSION_FILENAME).write_text("0.6.0.1234567890\n", encoding="utf-8")
    assert kiro_crew._apply_build_version_file("0.6.0", str(tmp_path)) == "0.6.0.1234567890"


def test_an_undecodable_file_keeps_the_base(tmp_path) -> None:
    (tmp_path / BUILD_VERSION_FILENAME).write_bytes(b"\xff\xfe0.6.0.1")
    assert kiro_crew._apply_build_version_file("0.6.0", str(tmp_path)) == "0.6.0"


# ── reach: a fresh interpreter importing a package that carries the file ────


def _stamped_package_root(tmp_path: Path, stamp: str | None) -> Path:
    """A ``kiro_crew`` package that IS the source tree's, with the file beside it.

    ``__init__.py`` is a byte copy of the real one (the code under test) plus a
    ``__path__`` line that points submodule resolution back at ``src/kiro_crew``,
    so ``kiro_crew.dashboard...`` imports the real modules while
    ``kiro_crew.__file__`` -- the anchor the stamp is read beside -- is the
    copy. Nothing is written into the source tree, and no symlink is needed.
    """
    root = tmp_path / "pkgroot"
    pkg = root / "kiro_crew"
    pkg.mkdir(parents=True)
    init_src = (PKG_DIR / "__init__.py").read_text(encoding="utf-8")
    (pkg / "__init__.py").write_text(
        init_src + f"\n__path__.append({str(PKG_DIR)!r})\n", encoding="utf-8"
    )
    if stamp is not None:
        (pkg / BUILD_VERSION_FILENAME).write_text(stamp, encoding="utf-8")
    return root


def _fresh_interpreter(tmp_path: Path, stamp: str | None, code: str) -> subprocess.CompletedProcess:
    root = _stamped_package_root(tmp_path, stamp)
    env = dict(os.environ)
    # The stamped package root first; the parent's sys.path follows only for
    # third-party deps. No bytecode: the child must leave nothing behind, in
    # the source tree or in tmp_path.
    env["PYTHONPATH"] = os.pathsep.join([str(root)] + [p for p in sys.path if p])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", f"PKG = {str(root / 'kiro_crew')!r}\n" + code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(tmp_path),
        timeout=120,
    )


_REACH_PROBE = """
import os
import kiro_crew
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard import ws
from kiro_crew.dashboard import handlers_system
from kiro_crew import release_channel
from kiro_crew.platform import update_governance
assert os.path.dirname(kiro_crew.__file__) == PKG, kiro_crew.__file__
assert os.path.dirname(updates.__file__) != PKG  # the REAL dashboard module
print("version", kiro_crew.__version__)
print("updates", updates._local_version)
print("ws", ws._local_version)
print("display", updates.status_update_fields()["version_display"])
print("health", handlers_system.kiro_crew.__version__)
print("channel", release_channel.channel())
"""


def _parse(out: str) -> dict[str, str]:
    return dict(line.split(" ", 1) for line in out.strip().splitlines())


def test_the_stamp_reaches_every_import_time_copy(tmp_path) -> None:
    """The property the file exists for: a package carrying it names the build
    everywhere a user can read a version."""
    base = _fresh_interpreter(tmp_path / "a", None, _REACH_PROBE)
    assert base.returncode == 0, base.stderr
    got_base = _parse(base.stdout)
    stamped = f"{got_base['version']}.12"

    proc = _fresh_interpreter(tmp_path / "b", stamped + "\n", _REACH_PROBE)
    assert proc.returncode == 0, proc.stderr
    got = _parse(proc.stdout)
    assert got["version"] == stamped
    assert got["updates"] == stamped  # -> the About chip's version_display
    assert got["ws"] == stamped  # -> the reload-on-upgrade compare
    assert got["display"] == stamped  # base_version is the identity on <base>.N
    assert got["health"] == stamped  # -> /api/health identity for the desktop guard
    assert got["channel"] == "stable"  # no prerelease marker: still a stable build
    assert BUILD_VERSION_FILENAME not in proc.stderr


def test_a_refused_file_leaves_every_copy_on_the_base(tmp_path) -> None:
    base = _fresh_interpreter(tmp_path / "a", None, _REACH_PROBE)
    got_base = _parse(base.stdout)
    proc = _fresh_interpreter(tmp_path / "b", "9.9.9.1\n", _REACH_PROBE)
    assert proc.returncode == 0, proc.stderr
    assert _parse(proc.stdout) == got_base
    assert BUILD_VERSION_FILENAME in proc.stderr  # the one warning, on stderr


def test_the_cli_version_flag_reports_the_build(tmp_path) -> None:
    """``kirocrew --version`` is an argparse ``action="version"`` that reads
    ``__version__`` at parser build; it inherits the stamp with no CLI change."""
    base = _fresh_interpreter(
        tmp_path / "a", None, "import kiro_crew; print(kiro_crew.__version__)"
    )
    stamped = f"{base.stdout.strip()}.12"
    proc = _fresh_interpreter(
        tmp_path / "b",
        stamped,
        "import sys\nsys.argv = ['kirocrew', '--version']\n"
        "from kiro_crew.cli import main\n"
        "try:\n    main()\nexcept SystemExit as exc:\n    raise SystemExit(exc.code or 0)\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"kirocrew {stamped}"


def test_the_governance_floor_sees_the_stamp(tmp_path) -> None:
    """A fleet floor is written in the distribution's build numbers. Before
    the stamp the core base could never satisfy ``0.6.0.10`` and every host
    read as permanently below the floor; with it the floor works as written."""
    code = (
        "import kiro_crew\n"
        "from kiro_crew.dashboard.handlers import updates\n"
        "from kiro_crew.platform import update_governance as ug\n"
        "print('version', kiro_crew.__version__)\n"
        "print('required', ug.update_required(updates._local_version))\n"
    )
    base = _fresh_interpreter(
        tmp_path / "a", None, "import kiro_crew; print(kiro_crew.__version__)"
    )
    core = base.stdout.strip()
    proc = _fresh_interpreter(tmp_path / "b", f"{core}.12\n", code)
    assert proc.returncode == 0, proc.stderr
    got = _parse(proc.stdout)
    assert got["version"] == f"{core}.12"
    # Unpinned in this environment: no floor, so not required. The point is
    # that the floor is evaluated over the STAMPED copy, the same value the
    # gateway's `_running_version` and the status payload carry.
    assert got["required"] == "False"


def test_a_child_leaves_no_bytecode_behind(tmp_path) -> None:
    proc = _fresh_interpreter(tmp_path, "0.0.0", "import kiro_crew")
    assert proc.returncode == 0, proc.stderr
    assert not list(tmp_path.rglob("__pycache__"))


def test_update_compare_orders_stamped_builds() -> None:
    """The stamp now feeds the update check's comparator: ``<base>.<n>`` must
    order by ``n`` (incl. the 9->10 / 99->100 boundaries), sit above the bare
    base, and below the next patch line."""
    from kiro_crew.dashboard.handlers.updates import _is_newer

    numbers = list(range(0, 25)) + [98, 99, 100, 101, 999, 1000]
    for n in numbers:
        for m in numbers:
            assert _is_newer(f"0.6.0.{n}", f"0.6.0.{m}") is (n > m), (n, m)
        assert _is_newer("0.6.1", f"0.6.0.{n}") is True
        assert _is_newer(f"0.6.0.{n}", "0.6.0") is (n > 0)


# ── the About page's changelog marks the RELEASE the build belongs to ────────


def test_changelog_marks_the_release_row_for_a_stamped_build() -> None:
    """``0.6.0.12`` is a build OF 0.6.0: its notes are 0.6.0's, so the running
    build must mark that row -- not open an empty ``0.6.0.12`` row above it."""
    from kiro_crew.changelog import build_release_list, running_release

    assert running_release("0.6.0.12") == ("0.6.0", False)
    assert running_release("0.6.0") == ("0.6.0", False)  # three segments untouched
    assert running_release("0.6.0rc3") == ("0.6.0", True)  # the lanes' own stamps still fold
    md = "# Changelog\n\n## [0.6.0] — 2026-09-01\n\nnotes\n\n## [0.5.0] — 2026-08-29\n\nolder\n"
    rows = build_release_list(md, "0.6.0.12")
    assert [r.version for r in rows] == ["0.6.0", "0.5.0"]  # no phantom 0.6.0.12 row
    current = [r for r in rows if r.is_current]
    assert [r.version for r in current] == ["0.6.0"]
    assert current[0].body.strip() == "notes"
    assert current[0].in_progress is False


def test_channel_move_compares_by_release_not_by_stamp() -> None:
    """A stamped ``0.6.0.12`` on the stable lane whose current release is
    ``0.6.0`` is ON that lane, not ahead of it: the feed check must not flag a
    permanent channel move (which the About panel renders as a standing
    "re-run the installer" affordance pointing at the bare wheel)."""
    from kiro_crew.changelog import release_of_build
    from kiro_crew.dashboard.handlers.updates import _is_newer

    assert release_of_build("0.6.0.12") == "0.6.0"
    assert release_of_build("0.6.0") == "0.6.0"
    # The lanes' own spellings are untouched -- including the two that ALSO
    # carry a fourth dot-segment. A nightly venv install compared as its bare
    # release against its own lane's newer nightly would read as permanently
    # ahead, which is the very false move this fold exists to prevent.
    for lane in ("0.6.0rc4", "0.6.0.dev0906", "0.6.0.post1", "0.6.0-nightly.20260904t000000"):
        assert release_of_build(lane) == lane, lane
    assert _is_newer(release_of_build("0.6.0.dev0906"), "0.6.0.dev0907") is False
    assert release_of_build("0.6.0.\u0663") == "0.6.0.\u0663"  # not an ASCII stamp
    assert release_of_build("0.6.0.1.2") == "0.6.0.1.2"  # the stamp rule admits one segment
    # The exact expression _check_release_feed stores as channel_move_pending:
    assert _is_newer(release_of_build("0.6.0.12"), "0.6.0") is not True
    # ...while the verdict direction still says "0.6.0 is not an update":
    assert _is_newer("0.6.0", "0.6.0.12") is False
    # ...and a genuinely ahead build still reads as a move:
    assert _is_newer(release_of_build("0.7.0.3"), "0.6.0") is True


def test_the_feed_check_source_folds_the_stamp() -> None:
    """Pins the call site itself: the compare in ``_check_release_feed`` goes
    through ``release_of_build``, so a refactor cannot quietly revert it."""
    src = (PKG_DIR / "dashboard" / "handlers" / "updates.py").read_text(encoding="utf-8")
    assert "channel_move_pending=_is_newer(release_of_build(_local_version), remote_version)" in src


def test_telemetry_release_is_unchanged_by_the_stamp() -> None:
    """The anonymous usage beacon deliberately reports ``major.minor.patch``
    only (cardinality); a stamped build sends the same value as its base."""
    from kiro_crew import beacon

    assert beacon.release("0.6.0.12") == beacon.release("0.6.0") == "0.6.0"


# ── the version-line regexes the release lanes rely on still see ONE line ───


def test_the_assignment_line_is_still_the_first_version_match() -> None:
    """``build-wheel.yml`` / ``build-desktop.yml`` / ``build-windows.yml`` sed the
    FIRST ``__version__ = "..."`` line, ``nightly.yml`` greps the first line
    naming ``__version__``, and the git-checkout update path regexes the same.
    The stamp code must not put another candidate ahead of the assignment."""
    import re

    src = (PKG_DIR / "__init__.py").read_text(encoding="utf-8")
    first_mention = next(line for line in src.splitlines() if "__version__" in line)
    assert re.fullmatch(r'__version__ = "[^"]+"', first_mention), first_mention
    quoted = re.findall(r'^__version__\s*=\s*"[^"]*"', src, re.MULTILINE)
    assert len(quoted) == 1, quoted


def test_the_file_is_not_shipped_by_this_repo() -> None:
    """The stamp belongs to a distribution's packaging step; the repo and the
    wheels it publishes carry none, and the gitignore keeps a dev experiment
    from being committed."""
    assert not (PKG_DIR / BUILD_VERSION_FILENAME).exists()
    assert f"src/kiro_crew/{BUILD_VERSION_FILENAME}" in (REPO_ROOT / ".gitignore").read_text(
        encoding="utf-8"
    )
