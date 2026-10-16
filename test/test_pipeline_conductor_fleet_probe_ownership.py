"""Fleet probe (``fleet_probe.py``): comparing paths, and who owns a process.

Path normalisation and containment, the program and venv a process runs from, the
trusted kernel link, ownership classes failing toward ``unknown``, and a process age
bound to one incarnation of its pid.

Shared fixtures and helpers are in ``pipeline_conductor_fleet_probe_helpers.py``;
``test_pipeline_conductor_fleet_probe.py`` heads the suite and states how it is organised.
"""

from __future__ import annotations

import os

import pytest
from pipeline_conductor_fleet_probe_helpers import mod_fixture  # noqa: F401 - fixture
from pipeline_conductor_fleet_probe_helpers import proc_pid, stat_fields

from conftest import make_dir_link

# --------------------------------------------------------------------------
# Comparing paths, and who owns a process
# --------------------------------------------------------------------------


def test_norm_path_strips_the_extended_length_prefix(mod):
    assert mod._norm_path("\\\\?\\D:\\work") == os.path.normcase(os.path.normpath("D:\\work"))


def test_under_uses_a_separator_boundary(mod):
    root = os.path.normpath("/oss/wt-a")
    assert mod._under(root, root) is True
    assert mod._under(os.path.join(root, "src"), root) is True
    assert mod._under(os.path.normpath("/oss/wt-a-old"), root) is False


def test_program_path_takes_the_first_token(mod):
    assert mod._program_path("/usr/bin/python3 -m pytest") == "/usr/bin/python3"
    assert mod._program_path("") == ""


@pytest.mark.parametrize(
    ("program", "base"),
    [
        ("/usr/bin/bash", "bash"),
        ("C:\\Python\\Py.EXE", "py"),
        ("bash", "bash"),
    ],
)
def test_basename_folds_case_and_separators(mod, program, base):
    assert mod._basename(program) == base


def test_venv_root_identifies_the_checkout_that_owns_an_interpreter(mod, tmp_path):
    venv = tmp_path / "wt" / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    assert mod._venv_root(str(venv / "bin" / "python")) == str(venv)
    assert mod._venv_root("/usr/bin/python3") is None
    assert mod._venv_root("python3") is None


def test_trusted_program_base_reads_the_kernel_link(mod, tmp_path):
    entry = tmp_path / "101"
    entry.mkdir()
    (entry / "exe").symlink_to("/usr/bin/bash")
    assert mod._trusted_program_base(entry) == "bash"


def test_trusted_program_base_tolerates_a_deleted_binary(mod, tmp_path):
    entry = tmp_path / "102"
    entry.mkdir()
    (entry / "exe").symlink_to("/usr/bin/bash (deleted)")
    assert mod._trusted_program_base(entry) == "bash"


def test_trusted_program_base_refuses_an_untrusted_directory(mod, tmp_path):
    entry = tmp_path / "103"
    entry.mkdir()
    (entry / "exe").symlink_to("/home/someone/bin/bash")
    assert mod._trusted_program_base(entry) is None


def test_trusted_program_base_refuses_an_unreadable_link(mod, tmp_path):
    entry = tmp_path / "104"
    entry.mkdir()
    assert mod._trusted_program_base(entry) is None


@pytest.mark.parametrize(
    ("argv", "exe_base"),
    [
        (["bash", "-c", "pytest -q"], "bash"),
        (["bash", "-lc", "pytest -q"], "bash"),
        (["sh", "-euxc", "pytest -q"], "sh"),
        (["bash", "-o", "pipefail", "-c", "pytest -q"], "bash"),
        (["bash", "--rcfile", "/dev/null", "-c", "pytest -q"], "bash"),
        (["bash", "--posix", "-c", "pytest -q"], "bash"),
        (["bash", "-e", "-c", "pytest -q"], "bash"),
        (["busybox", "sh", "-c", "pytest -q"], "busybox"),
    ],
)
def test_shell_command_wrapper_is_recognised(mod, argv, exe_base):
    assert mod._is_shell_command_wrapper(argv, exe_base) is True


@pytest.mark.parametrize(
    ("argv", "exe_base"),
    [
        ([], "bash"),
        (["bash", "-c", "pytest"], None),
        (["bash", "script.sh"], "bash"),
        (["python3", "-c", "import pytest"], "python3"),
        (["/usr/bin/wget", "-c", "http://example.invalid/x"], "busybox"),
    ],
)
def test_a_real_tool_is_not_treated_as_a_wrapper(mod, argv, exe_base):
    assert mod._is_shell_command_wrapper(argv, exe_base) is False


def test_program_class_never_guesses_fleet(mod, tmp_path):
    wt = tmp_path / "wt"
    (wt / "bin").mkdir(parents=True)
    assert mod._program_class("/usr/bin/python3 -m pytest", []) == "unknown"
    assert mod._program_class("", [str(wt)]) == "unknown"
    assert mod._program_class(f"{wt}/bin/python -m pytest", [str(wt)]) == "fleet"
    assert mod._program_class("/usr/bin/python3 -m pytest", [str(wt)]) == "unknown"


def test_program_class_attributes_a_venv_interpreter_elsewhere(mod, tmp_path):
    other = tmp_path / "other" / ".venv"
    (other / "bin").mkdir(parents=True)
    (other / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    fleet = tmp_path / "wt"
    fleet.mkdir()
    assert mod._program_class(f"{other}/bin/python -m pytest", [str(fleet)]) == "foreign"


def test_owner_class_reads_the_working_directory(mod, tmp_path):
    fleet = tmp_path / "wt"
    (fleet / "src").mkdir(parents=True)
    entry = tmp_path / "201"
    entry.mkdir()
    make_dir_link(entry / "cwd", fleet / "src")
    assert mod._owner_class(entry, [str(fleet)]) == "fleet"
    assert mod._owner_class(entry, [str(tmp_path / "somewhere-else")]) == "foreign"
    assert mod._owner_class(entry, []) == "unknown"


def test_owner_class_absorbs_a_symlinked_fleet_root(mod, tmp_path):
    real = tmp_path / "real-wt"
    (real / "src").mkdir(parents=True)
    link = tmp_path / "link-wt"
    make_dir_link(link, real)
    entry = tmp_path / "202"
    entry.mkdir()
    make_dir_link(entry / "cwd", real / "src")
    assert mod._owner_class(entry, [str(link)]) == "fleet"


def test_owner_class_falls_back_to_the_program_when_the_cwd_is_unreadable(mod, tmp_path):
    fleet = tmp_path / "wt"
    (fleet / "bin").mkdir(parents=True)
    entry = tmp_path / "203"
    entry.mkdir()
    assert mod._owner_class(entry, [str(fleet)], f"{fleet}/bin/python -m pytest") == "fleet"
    assert mod._owner_class(entry, [str(fleet)], "/usr/bin/python3 -m pytest") == "unknown"


def test_a_root_that_cannot_be_resolved_reads_as_unknown_not_as_fleet(mod, tmp_path, monkeypatch):
    """A root that cannot be compared must widen nothing.

    The refusal is forced through the resolver rather than through a path the
    platform happens to reject. An embedded NUL raises on POSIX and resolves on
    Windows, so asserting on one measures the standard library instead of this
    branch -- and the branch is the safety-relevant half, since ``fleet`` is the
    class that stops a session.
    """
    entry = tmp_path / "204"
    entry.mkdir()
    make_dir_link(entry / "cwd", tmp_path)
    elsewhere = str(tmp_path / "some-other-root")

    def refuse(path):
        raise OSError("cannot resolve")

    monkeypatch.setattr(mod.os.path, "realpath", refuse)
    assert mod._owner_class(entry, [elsewhere]) == "unknown"
    assert mod._program_class("/usr/bin/python3 -m pytest", [elsewhere]) == "unknown"


# --------------------------------------------------------------------------
# Process age, bound to one incarnation of a pid
# --------------------------------------------------------------------------


def test_starttime_survives_a_comm_containing_parentheses(mod, tmp_path):
    entry = tmp_path / "301"
    entry.mkdir()
    (entry / "stat").write_text(f"301 ((sh )nasty)) {stat_fields(4242)}\n", encoding="utf-8")
    assert mod._proc_starttime_ticks(tmp_path, "301") == 4242


def test_starttime_is_none_when_unreadable_or_short(mod, tmp_path):
    assert mod._proc_starttime_ticks(tmp_path, "404") is None
    entry = tmp_path / "302"
    entry.mkdir()
    (entry / "stat").write_text("302 (py) S 1 2 3\n", encoding="utf-8")
    assert mod._proc_starttime_ticks(tmp_path, "302") is None


def test_age_is_measured_against_the_captured_incarnation(mod, tmp_path, monkeypatch):
    proc_pid(tmp_path, "303", ["pytest", "-q"], starttime=50_000)
    (tmp_path / "uptime").write_text("1000.0 900.0\n", encoding="utf-8")
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    assert mod._proc_age_secs(tmp_path, "303", 50_000) == 500


def test_age_is_refused_when_the_pid_was_recycled(mod, tmp_path, monkeypatch):
    proc_pid(tmp_path, "304", ["pytest", "-q"], starttime=50_000)
    (tmp_path / "uptime").write_text("1000.0 900.0\n", encoding="utf-8")
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    assert mod._proc_age_secs(tmp_path, "304", 49_999) is None
    assert mod._proc_age_secs(tmp_path, "304", None) is None
    assert mod._proc_age_secs(tmp_path, "999", 50_000) is None


def test_age_is_refused_without_a_usable_clock(mod, tmp_path, monkeypatch):
    proc_pid(tmp_path, "305", ["pytest", "-q"], starttime=50_000)
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    assert mod._proc_age_secs(tmp_path, "305", 50_000) is None, "no uptime file"
    (tmp_path / "uptime").write_text("1000.0 900.0\n", encoding="utf-8")

    def no_sysconf(name):
        raise ValueError("unsupported")

    monkeypatch.setattr(os, "sysconf", no_sysconf, raising=False)
    assert mod._proc_age_secs(tmp_path, "305", 50_000) is None
    monkeypatch.setattr(os, "sysconf", lambda name: 0, raising=False)
    assert mod._proc_age_secs(tmp_path, "305", 50_000) is None


def test_age_is_refused_on_a_platform_with_no_clock_tick_source(mod, tmp_path, monkeypatch):
    """The answer a platform without ``os.sysconf`` gets, asserted on every platform.

    The field is never omitted there: an absent age would read as a new process,
    while an unavailable one is reported as unknown.
    """
    proc_pid(tmp_path, "307", ["pytest", "-q"], starttime=50_000)
    (tmp_path / "uptime").write_text("1000.0 900.0\n", encoding="utf-8")
    monkeypatch.delattr(os, "sysconf", raising=False)
    assert mod._proc_age_secs(tmp_path, "307", 50_000) is None


def test_age_clamps_a_clock_that_reads_backwards(mod, tmp_path, monkeypatch):
    proc_pid(tmp_path, "306", ["pytest", "-q"], starttime=500_000)
    (tmp_path / "uptime").write_text("10.0 5.0\n", encoding="utf-8")
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    assert mod._proc_age_secs(tmp_path, "306", 500_000) == 0
