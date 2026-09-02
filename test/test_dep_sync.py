"""Tests for the dependency-only sync that stands in for a blocked reinstall.

The module exists because Windows cannot rewrite a running console script, so
these tests pin what the substitution rests on: it is shaped as PARITY with
``pip install -e .`` rather than as an improvement on it (same order, same scope,
extras left alone), it never hands the project itself to pip, it applies the two
gates a dependency-only install cannot inherit from pip (the interpreter floor and
a repointed console script), and it refuses to write to a venv that serves a
different checkout.
"""

import ast
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew import dep_sync

_SETUP_CFG = textwrap.dedent("""
    [options]
    python_requires = >=3.10
    install_requires =
        # a full-line comment configparser keeps
        aiohttp>=3.9
        tzdata>=2024.1; platform_system == "Windows"

    [options.extras_require]
    voice =
        boto3>=1.34,<2
    """).strip()


@pytest.fixture
def repo(tmp_path):
    """A checkout whose working tree carries the declarations, post-merge."""
    (tmp_path / "setup.cfg").write_text(_SETUP_CFG, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _preconditions_ok(request):
    """Stub the venv probe for the ``main()`` tests only.

    The probe has its own direct tests; stubbing it here keeps the main() tests
    about what main does with the answers, and stops them reaching a real
    interpreter through the mocked subprocess. The venv-mapping COMPARISON is
    deliberately left real, so a main() test can make the venv foreign by changing
    the probe's answer.
    """
    if not request.node.name.startswith("test_main_"):
        yield
        return
    with patch.object(dep_sync, "installed_package_origin", side_effect=_origin_inside):
        yield


def _origin_inside(target_py):
    """A probe answer that resolves inside whatever repo the test passed."""
    return str(Path(_origin_inside.repo) / "src" / "kiro_crew" / "__init__.py")


def test_normalize_folds_the_spellings_pep503_treats_as_one():
    """The folding matters only through `rejected_specs`, so it is tested here."""
    assert dep_sync.normalize("Kiro_Crew") == "kiro-crew"
    assert dep_sync.normalize("KIROCREW") == "kirocrew"
    assert dep_sync.normalize("kiro.crew") == "kiro-crew"


def test_declared_requirements_reads_install_requires_and_drops_comments(repo):
    specs = dep_sync.declared_requirements(repo)

    assert specs == ["aiohttp>=3.9", 'tzdata>=2024.1; platform_system == "Windows"']


def test_declared_requirements_leaves_extras_alone(repo):
    """`pip install -e .` installs no extra, so neither does this.

    Inferring which extras the operator asked for is what this design removed: pip
    records no such thing, so the inference has unavoidable false negatives -- an
    extra whose platform-specific dependency legitimately went uninstalled reads as
    inactive and its other requirements get skipped.
    """
    specs = dep_sync.declared_requirements(repo)

    assert specs is not None
    assert not any("boto3" in spec for spec in specs)


def test_declared_requirements_returns_none_when_unreadable(tmp_path):
    assert dep_sync.declared_requirements(tmp_path) is None


def test_pyproject_is_read_with_a_parser_not_by_matching_text(repo):
    """A table header may carry a trailing comment, and a parser knows that.

    `[project] # comment` is valid TOML. The text reader compared the whole
    header line, so it read this as a different table and skipped the body --
    which meant an explicit `dependencies` and a raised floor underneath it were
    both invisible, and the step installed a stale setup.cfg list. Every question
    asked of pyproject now goes through a real parser wherever one exists.
    """
    (repo / "pyproject.toml").write_text(
        "[project] # the table this module has to read\n"
        'name = "kirocrew"\n'
        'requires-python = ">=3.13"\n'
        'dependencies = ["aiohttp"]\n',
        encoding="utf-8",
    )

    assert dep_sync.project_table(repo) is not None
    assert dep_sync.requires_python(repo) == ">=3.13"
    assert dep_sync.dependency_authority_moved(repo) is not None


def test_text_fallback_also_survives_a_commented_header(repo, monkeypatch):
    """The 3.10-without-tomli path has no parser, so its reader must not regress."""
    monkeypatch.setattr(dep_sync, "_toml", None)
    (repo / "pyproject.toml").write_text(
        '[project]   # trailing comment\nname = "kirocrew"\n'
        'requires-python = ">=3.13"\ndependencies = ["aiohttp"]\n',
        encoding="utf-8",
    )

    assert dep_sync.project_table(repo) is None
    assert dep_sync.requires_python(repo) == ">=3.13"
    assert dep_sync.dependency_authority_moved(repo) is not None


def test_requires_python_is_read_from_the_working_tree(repo):
    assert dep_sync.requires_python(repo) == ">=3.10"


def test_requires_python_prefers_pyproject_because_setuptools_ignores_setup_cfg(repo):
    """The gate must read the file the BUILD reads, or it stops firing.

    Once a ``[project]`` table exists setuptools takes ``requires-python`` from it
    and ignores setup.cfg's ``python_requires``. This repository carries the value
    in both files, so a revision raising the floor in the authoritative one would
    leave the setup.cfg copy stale -- and a gate reading the stale copy is a gate
    that silently passes the interpreter it should refuse.
    """
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "kirocrew"\nrequires-python = ">=3.13"\n' 'dynamic = ["dependencies"]\n',
        encoding="utf-8",
    )

    # setup.cfg still says >=3.10; pyproject wins.
    assert dep_sync.requires_python(repo) == ">=3.13"


def test_requires_python_falls_back_when_pyproject_declares_it_dynamic(repo):
    """A field listed as dynamic is still setup.cfg's to declare."""
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "kirocrew"\ndynamic = ["requires-python", "dependencies"]\n',
        encoding="utf-8",
    )

    assert dep_sync.requires_python(repo) == ">=3.10"


def test_requires_python_reads_a_static_omission_as_no_floor_at_all(repo):
    """The same fallthrough as the console script, at the other field it owns.

    A `[project]` table that declares `requires-python` statically is the whole
    answer, so a table that omits it declares no floor. setup.cfg's copy is one
    setuptools ignores here, and enforcing a stale copy can only over-refuse a
    revision that is in fact installable. No sentinel is needed, unlike the
    console-script answer: "no floor declared" and "the floor could not be read"
    both leave this gate not firing, which is the same safe outcome.
    """
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "kirocrew"\ndynamic = ["dependencies"]\n',
        encoding="utf-8",
    )

    # setup.cfg still says >=3.10; the static omission means it is not consulted.
    assert dep_sync.requires_python(repo) is None


def test_python_floor_breach_reports_the_highest_unmet_floor():
    assert dep_sync.python_floor_breach(">=3.10", (3, 12, 0)) is None
    assert dep_sync.python_floor_breach(">=3.13", (3, 10, 0)) == "3.13.0"
    assert dep_sync.python_floor_breach(">=3.11,>=3.13", (3, 10, 0)) == "3.13.0"
    assert dep_sync.python_floor_breach(">=3.10,<4", (3, 10, 0)) is None


def test_python_floor_breach_reads_every_spelling_that_declares_a_floor():
    """A floor spelling this misses is a gate that does not fire."""
    # Compatible release: `~=3.11` is `>=3.11, ==3.*`, so it declares a floor.
    assert dep_sync.python_floor_breach("~=3.11", (3, 10, 7)) == "3.11.0"
    assert dep_sync.python_floor_breach("~=3.10", (3, 12, 0)) is None
    # Three components compared as three: `>=3.10.5` must not truncate to (3, 10)
    # and then be satisfied by 3.10.0.
    assert dep_sync.python_floor_breach(">=3.10.5", (3, 10, 0)) == "3.10.5"
    assert dep_sync.python_floor_breach(">=3.10.5", (3, 10, 5)) is None
    # `>` excludes the version it names, unlike `>=`.
    assert dep_sync.python_floor_breach(">3.10", (3, 10, 0)) == "3.10.0"
    assert dep_sync.python_floor_breach(">3.10", (3, 10, 1)) is None
    assert dep_sync.python_floor_breach(">=3.10", (3, 10, 0)) is None
    # An equality clause pins the version it names, so that version is the floor
    # too. `==3.12.*` is the spelling a revision uses to require one minor
    # series; read as no floor at all, it would install on 3.10 and then fail to
    # import. `===` is arbitrary equality and pins just as hard.
    assert dep_sync.python_floor_breach("==3.12.*", (3, 10, 0)) == "3.12.0"
    assert dep_sync.python_floor_breach("==3.12.*", (3, 12, 4)) is None
    assert dep_sync.python_floor_breach("===3.10.5", (3, 10, 0)) == "3.10.5"
    assert dep_sync.python_floor_breach("===3.10.5", (3, 10, 5)) is None
    # `===` is read as one operator, not as `==` with a stray `=` in front, so
    # its floor is the version it names rather than a missed match.
    assert dep_sync._PY_FLOOR.search("===3.10.5").group("op") == "==="


def test_dependency_authority_moved_detects_a_migration_to_pyproject(repo):
    """This module reads ONE file; a move would make it install yesterday's set."""
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "kirocrew"\ndependencies = ["aiohttp"]\n', encoding="utf-8"
    )

    assert dep_sync.dependency_authority_moved(repo) is not None


def test_dependency_authority_moved_matches_dynamic_items_not_substrings(repo):
    """`["optional-dependencies"]` contains the text but declares nothing here.

    Reading it as a substring would treat explicit `[project].dependencies` as
    still dynamic and install a stale setup.cfg list while reporting success.
    """
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "kirocrew"\ndependencies = ["aiohttp"]\n'
        'dynamic = ["optional-dependencies"]\n',
        encoding="utf-8",
    )

    assert dep_sync.dependency_authority_moved(repo) is not None


def test_dependency_authority_intact_when_fields_stay_dynamic(repo):
    """setuptools keeps reading setup.cfg for a field listed as dynamic."""
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "kirocrew"\ndynamic = ["dependencies", "version"]\n',
        encoding="utf-8",
    )

    assert dep_sync.dependency_authority_moved(repo) is None


def test_console_script_target_prefers_the_pyproject_declaration(repo):
    """`scripts` is not dynamic here, so pyproject is what builds the wrapper."""
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "kirocrew"\ndynamic = ["dependencies"]\n\n'
        '[project.scripts]\nkirocrew = "kiro_crew.new:main"\n',
        encoding="utf-8",
    )

    assert dep_sync.console_script_target(repo, "kirocrew") == "kiro_crew.new:main"


def test_console_script_target_falls_back_to_setup_cfg(repo):
    (repo / "setup.cfg").write_text(
        _SETUP_CFG + "\n\n[options.entry_points]\nconsole_scripts =\n"
        "    kirocrew = kiro_crew.old:main\n",
        encoding="utf-8",
    )

    assert dep_sync.console_script_target(repo, "kirocrew") == "kiro_crew.old:main"


def test_console_script_target_reports_a_removal_rather_than_reading_a_stale_copy(repo):
    """A static `[project.scripts]` that omits the script means it was REMOVED.

    Falling through to setup.cfg here is what hides the removal: this repository
    carries the same entry point in both files, so the stale copy AGREES with the
    installed wrapper and the comparison reports success on a script the revision
    deleted -- the wrapper left dispatching to a target that may no longer exist.
    """
    (repo / "setup.cfg").write_text(
        _SETUP_CFG + "\n\n[options.entry_points]\nconsole_scripts =\n"
        "    kirocrew = kiro_crew.old:main\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "kirocrew"\ndynamic = ["dependencies"]\n\n'
        '[project.scripts]\nsomething-else = "kiro_crew.other:main"\n',
        encoding="utf-8",
    )

    assert dep_sync.console_script_target(repo, "kirocrew") == dep_sync.SCRIPT_REMOVED


def test_console_script_target_reads_no_scripts_table_as_a_removal_too(repo):
    """No `scripts` and not dynamic is the same statement: setuptools builds none."""
    (repo / "setup.cfg").write_text(
        _SETUP_CFG + "\n\n[options.entry_points]\nconsole_scripts =\n"
        "    kirocrew = kiro_crew.old:main\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "kirocrew"\ndynamic = ["dependencies"]\n',
        encoding="utf-8",
    )

    assert dep_sync.console_script_target(repo, "kirocrew") == dep_sync.SCRIPT_REMOVED


def test_console_script_target_still_reads_setup_cfg_when_scripts_is_dynamic(repo):
    """A field listed as dynamic is still setup.cfg's to declare."""
    (repo / "setup.cfg").write_text(
        _SETUP_CFG + "\n\n[options.entry_points]\nconsole_scripts =\n"
        "    kirocrew = kiro_crew.old:main\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "kirocrew"\ndynamic = ["dependencies", "scripts"]\n',
        encoding="utf-8",
    )

    assert dep_sync.console_script_target(repo, "kirocrew") == "kiro_crew.old:main"


def test_rejected_specs_refuses_paths_archives_and_the_project_itself():
    """The premise -- pip is never asked for the project -- is enforced, not assumed."""
    for hostile in [".", "./local", "/abs/path", r"C:\pkgs\x", "file:./x", "x.whl", "-e"]:
        assert dep_sync.rejected_specs([hostile]), hostile

    # Only spellings PEP 503 actually folds onto this project's name. `kiro_crew`
    # normalizes to `kiro-crew`, which is a DIFFERENT distribution, so it is not
    # claimed here.
    for spelling in [
        "kirocrew",
        "KiroCrew",  # brand-ok: a PEP 503 spelling of the distribution name
        "KIROCREW",
        "kirocrew>=1",
    ]:
        rejected = dep_sync.rejected_specs([spelling])
        assert rejected and "names this project" in rejected[0], spelling

    assert dep_sync.rejected_specs(["aiohttp>=3.9", "boto3"]) == []


def test_installed_package_origin_reports_where_the_package_resolves():
    """The import path is what the installed dependencies live alongside."""

    class _Proc:
        returncode = 0
        stdout = "/checkouts/main/src/kiro_crew/__init__.py\n"

    with patch.object(dep_sync.subprocess, "run", return_value=_Proc()):
        origin = dep_sync.installed_package_origin(Path("py"))

    assert origin is not None
    assert Path(origin).name == "__init__.py"


def test_installed_package_origin_is_none_when_the_package_is_absent():
    class _Proc:
        returncode = 0
        stdout = "\n"

    with patch.object(dep_sync.subprocess, "run", return_value=_Proc()):
        assert dep_sync.installed_package_origin(Path("py")) is None


def test_venv_serving_another_checkout_is_reported(tmp_path):
    """The harm this guards: upgrading a runtime another checkout is served by."""
    reason = dep_sync.venv_not_mapped_to(
        str(tmp_path / "other" / "src" / "kiro_crew" / "__init__.py"), tmp_path / "main"
    )

    assert reason is not None
    assert "other" in reason
    assert "main" in reason


def test_an_unresolvable_package_is_not_taken_as_a_match(tmp_path):
    """Unproven is refused, not assumed."""
    assert dep_sync.venv_not_mapped_to(None, tmp_path / "main") is not None


def test_a_venv_serving_this_checkout_passes(tmp_path):
    repo = tmp_path / "main"
    origin = repo / "src" / "kiro_crew" / "__init__.py"

    assert dep_sync.venv_not_mapped_to(str(origin), repo) is None


def test_a_sibling_directory_sharing_the_prefix_does_not_count_as_inside(tmp_path):
    """`<repo>-wt` starts with `<repo>` as a string but is a different checkout."""
    sibling = tmp_path / "main-wt" / "src" / "kiro_crew" / "__init__.py"

    assert dep_sync.venv_not_mapped_to(str(sibling), tmp_path / "main") is not None


def test_main_hands_every_spec_to_pip_and_stops_on_failure(repo):
    """pip decides satisfaction; a failed install must not report success."""
    _origin_inside.repo = repo
    calls = []

    class _Proc:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = ""
            # The pip call captures now, so the failure path decodes stderr.
            self.stderr = b""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "pip" in cmd:
            return _Proc(1)
        return _Proc(0)

    with patch.object(dep_sync.subprocess, "run", side_effect=fake_run):
        rc = dep_sync.main([str(repo), "py"])

    assert rc == 1
    pip_call = next(c for c in calls if "pip" in c)
    assert "aiohttp>=3.9" in pip_call
    assert 'tzdata>=2024.1; platform_system == "Windows"' in pip_call
    assert "-e" not in pip_call


def test_main_ends_pip_option_parsing_before_the_specs(repo):
    """A declaration beginning with `-` must never be read as a pip option."""
    _origin_inside.repo = repo
    calls = []

    class _Proc:
        returncode = 0
        stdout = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Proc()

    with (
        patch.object(dep_sync, "declared_requirements", return_value=["aiohttp"]),
        patch.object(dep_sync, "installed_console_script_target", return_value=None),
        patch.object(dep_sync.subprocess, "run", side_effect=fake_run),
    ):
        rc = dep_sync.main([str(repo), "py"])

    assert rc == 0
    pip_call = next(c for c in calls if "pip" in c)
    assert pip_call.index("--") == pip_call.index("install") + 1
    assert pip_call[-1] == "aiohttp"


def test_main_refuses_a_declaration_that_names_the_project(repo, capsys):
    """Refused BEFORE pip runs, so the venv is never touched."""
    _origin_inside.repo = repo

    with (
        patch.object(dep_sync, "declared_requirements", return_value=["."]),
        patch.object(dep_sync, "requires_python", return_value=None),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        rc = dep_sync.main([str(repo), "py"])

    assert rc == dep_sync.REFUSED
    assert not sp.run.called
    err = capsys.readouterr().err
    assert "will not hand to pip" in err
    assert "No dependency was installed" in err


def test_main_refuses_when_the_interpreter_is_below_the_declared_floor(repo, capsys):
    """The gate `pip install -e .` applies while building; nothing else provides it."""
    _origin_inside.repo = repo

    with (
        patch.object(dep_sync, "requires_python", return_value=">=3.13"),
        patch.object(dep_sync, "interpreter_version", return_value=(3, 10, 4)),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        rc = dep_sync.main([str(repo), "py"])

    assert rc == dep_sync.REFUSED
    assert not sp.run.called
    err = capsys.readouterr().err
    assert "3.13" in err


def test_main_refuses_a_venv_serving_another_checkout(repo, capsys):
    """Refused before pip runs, on the venv's identity alone."""
    with (
        patch.object(
            dep_sync,
            "installed_package_origin",
            return_value="/checkouts/other/src/kiro_crew/__init__.py",
        ),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        rc = dep_sync.main([str(repo), "py"])

    assert rc == dep_sync.REFUSED
    assert not sp.run.called
    err = capsys.readouterr().err
    assert "other" in err


def test_main_refuses_when_the_requirements_moved_to_pyproject(repo, capsys):
    """Reading a stale setup.cfg while reporting success is the failure to avoid."""
    _origin_inside.repo = repo
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "kirocrew"\ndependencies = ["aiohttp"]\n', encoding="utf-8"
    )

    with patch.object(dep_sync, "subprocess") as sp:
        rc = dep_sync.main([str(repo), "py"])

    assert rc == dep_sync.REFUSED
    assert not sp.run.called
    assert "stale" in capsys.readouterr().err


def test_main_reports_a_repointed_console_script_after_installing(repo, capsys):
    """The one gap: a moved entry point cannot be refreshed while it is locked.

    The dependencies are already installed by the time this is known, so it is
    reported as a failure of the step with the manual remedy -- not dressed up as a
    refusal that left the checkout untouched, which would be false.
    """

    class _Proc:
        returncode = 0
        stdout = ""

    _origin_inside.repo = repo

    with (
        patch.object(dep_sync, "console_script_target", return_value="kiro_crew.new:main"),
        patch.object(
            dep_sync, "installed_console_script_target", return_value="kiro_crew.old:main"
        ),
        patch.object(dep_sync.subprocess, "run", return_value=_Proc()),
    ):
        rc = dep_sync.main([str(repo), "py"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "kiro_crew.new:main" in err
    assert "kiro_crew.old:main" in err
    assert "No dependency was installed" not in err


def test_main_reports_a_removed_console_script_as_a_removal(repo, capsys):
    """A removal is a different sentence from a repoint, and must read as one.

    The sentinel is a phrase, not a `module:attr`, so splicing it into the repoint
    wording would report the script as "repointed to removed by the merged
    revision" -- an operator cannot act on that.
    """

    class _Proc:
        returncode = 0
        stdout = ""

    _origin_inside.repo = repo

    with (
        patch.object(dep_sync, "console_script_target", return_value=dep_sync.SCRIPT_REMOVED),
        patch.object(
            dep_sync, "installed_console_script_target", return_value="kiro_crew.old:main"
        ),
        patch.object(dep_sync.subprocess, "run", return_value=_Proc()),
    ):
        rc = dep_sync.main([str(repo), "py"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "no longer declared" in err
    assert "kiro_crew.old:main" in err
    assert "repointed to" not in err
    assert "No dependency was installed" not in err


def test_main_tolerates_an_unreadable_installed_entry_point(repo):
    """An unreadable probe must not fail a sync it has no evidence against."""

    class _Proc:
        returncode = 0
        stdout = ""

    _origin_inside.repo = repo

    with (
        patch.object(dep_sync, "console_script_target", return_value="kiro_crew.cli:main"),
        patch.object(dep_sync, "installed_console_script_target", return_value=None),
        patch.object(dep_sync.subprocess, "run", return_value=_Proc()),
    ):
        assert dep_sync.main([str(repo), "py"]) == 0


def test_main_rejects_a_wrong_argument_count():
    assert dep_sync.main(["only-one"]) == 2
    assert dep_sync.main(["a", "b", "c"]) == 2


# --- the probe every caller shares: which install is even possible ------------
def _make_scripts(tmp_path, *names):
    """A fake venv Scripts/ dir, returning the interpreter path inside it."""
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    for name in names:
        (scripts / name).write_bytes(b"MZ")
    return scripts / "python.exe"


def _raise_on(monkeypatch, name, exc):
    """Make Path.open raise *exc* for the file called *name* only."""
    real_open = Path.open

    def fake_open(self, *args, **kwargs):
        if self.name == name:
            raise exc
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)


def test_locked_console_scripts_is_a_posix_noop(tmp_path, monkeypatch):
    """POSIX can unlink an executing binary, so there is nothing to detect."""
    py = _make_scripts(tmp_path, "kirocrew.exe")
    monkeypatch.setattr(sys, "platform", "linux")
    _raise_on(monkeypatch, "kirocrew.exe", PermissionError(13, "in use"))

    assert dep_sync.locked_console_scripts(py) == []


def test_locked_console_scripts_flags_a_locked_script(tmp_path, monkeypatch):
    """The real failure: the exe the gateway is executing cannot be replaced."""
    py = _make_scripts(tmp_path, "kirocrew.exe")
    monkeypatch.setattr(sys, "platform", "win32")
    _raise_on(monkeypatch, "kirocrew.exe", PermissionError(13, "in use"))

    locked = dep_sync.locked_console_scripts(py)
    assert len(locked) == 1
    assert locked[0].endswith("kirocrew.exe")


def test_locked_console_scripts_passes_a_writable_script(tmp_path, monkeypatch):
    """A venv the gateway is NOT running from must still get its reinstall."""
    py = _make_scripts(tmp_path, "kirocrew.exe")
    monkeypatch.setattr(sys, "platform", "win32")

    assert dep_sync.locked_console_scripts(py) == []


def test_locked_console_scripts_ignores_unrelated_executables(tmp_path, monkeypatch):
    """Only the scripts pip would rewrite matter.

    Some other locked exe sharing the Scripts dir must not suppress the
    reinstall — that would turn an unrelated process into a silent skip.
    """
    py = _make_scripts(tmp_path, "kirocrew.exe", "unrelated.exe")
    monkeypatch.setattr(sys, "platform", "win32")
    _raise_on(monkeypatch, "unrelated.exe", PermissionError(13, "in use"))

    assert dep_sync.locked_console_scripts(py) == []


def test_locked_console_scripts_lets_pip_judge_other_errors(tmp_path, monkeypatch):
    """An unreadable-for-other-reasons script is not evidence of a lock.

    Skipping on any OSError would suppress installs that would have worked.
    """
    py = _make_scripts(tmp_path, "kirocrew.exe")
    monkeypatch.setattr(sys, "platform", "win32")
    _raise_on(monkeypatch, "kirocrew.exe", OSError(5, "I/O error"))

    assert dep_sync.locked_console_scripts(py) == []


# --- sync_or_reinstall: the reinstall stays the default -----------------------
def _maps():
    """Answer the pre-branch foreign-venv guard with "it maps".

    Scoped per test rather than autouse: this module also asserts that the guard
    REFUSES, and a blanket stub would silence the tests that prove it.
    """
    return patch.object(dep_sync, "venv_not_mapped_to", return_value=None)


def _origin_stub():
    """Skip the probe subprocess; the interpreter paths here do not exist."""
    return patch.object(dep_sync, "installed_package_origin", return_value="<stub>")


def test_sync_or_reinstall_prefers_the_reinstall_when_nothing_is_locked(tmp_path):
    """No lock means no substitute. The reinstall is the fuller operation.

    Only the reinstall also rewrites a console script the incoming revision
    repointed, so substituting where pip could have run would quietly downgrade
    every caller on every platform.
    """
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    with (
        _origin_stub(),
        _maps(),
        patch.object(dep_sync, "locked_console_scripts", return_value=[]),
        patch.object(dep_sync, "sync", side_effect=AssertionError("must not substitute")),
        patch.object(dep_sync.subprocess, "run", side_effect=fake_run),
    ):
        rc = dep_sync.sync_or_reinstall(tmp_path, Path("/venv/bin/python"), timeout=42)

    assert rc == 0
    assert seen["argv"][1:] == ["-m", "pip", "install", "-e", str(tmp_path), "--quiet"]
    assert seen["timeout"] == 42


def test_sync_or_reinstall_substitutes_when_a_script_is_locked(tmp_path):
    """A locked script routes to the substitute, and the caller is told why.

    The reinstall must not merely fail here: pip's uninstall is not atomic, so
    reaching the locked script means the editable .pth is already gone.
    """
    messages = []

    with (
        _origin_stub(),
        _maps(),
        patch.object(dep_sync, "locked_console_scripts", return_value=[r"C:\v\kirocrew.exe"]),
        patch.object(dep_sync, "sync", return_value=0) as sync_mock,
        patch.object(dep_sync.subprocess, "run", side_effect=AssertionError("must not reinstall")),
    ):
        rc = dep_sync.sync_or_reinstall(
            tmp_path, Path("/venv/bin/python"), lambda m, e: messages.append((m, e))
        )

    assert rc == 0
    assert sync_mock.call_count == 1
    assert any("kirocrew.exe" in m and "dependency-only" in m for m, _ in messages)


def test_sync_or_reinstall_guards_the_reinstall_branch_too(tmp_path):
    """The foreign-venv refusal covers the branch pip can still run.

    Guarding only the substitute would rebuild, inside this shared function, the
    exact asymmetry it was written to remove: three of its four callers take the
    checkout from configuration, so a venv serving a DIFFERENT checkout is
    reachable on all three, and `pip install -e <repo>` against it silently
    repoints that other checkout's editable install at this repo.
    """
    messages = []

    with (
        patch.object(dep_sync, "installed_package_origin", return_value="/other/src/x.py"),
        patch.object(
            dep_sync,
            "locked_console_scripts",
            side_effect=AssertionError("must refuse before probing the lock"),
        ),
        patch.object(dep_sync.subprocess, "run", side_effect=AssertionError("must not install")),
    ):
        rc = dep_sync.sync_or_reinstall(
            tmp_path, Path("/venv/bin/python"), lambda m, e: messages.append((m, e))
        )

    assert rc == dep_sync.REFUSED
    joined = " ".join(m for m, _ in messages)
    # Not "/other": the message renders a resolved path, so the separator (and on
    # Windows the drive) is the platform's, not the one written in the stub.
    assert "other" in joined
    assert "No dependency was installed" in joined


def test_a_refusal_is_a_distinct_exit_code_from_a_failed_attempt():
    """REFUSED must not be spelled the same as "the install failed".

    A caller that reacts to a failed install by repairing what it can has to be
    able to tell the two apart: running that repair after a refusal would write
    into the very venv the refusal was protecting. Pinned as a contract because
    the Slack auto-update branches on it.
    """
    assert dep_sync.REFUSED != 0
    assert dep_sync.REFUSED != 1


@pytest.mark.parametrize("pip_rc", [1, 2, 3, 120])
def test_an_attempted_install_never_returns_the_refusal_code(tmp_path, pip_rc):
    """pip's own exit code is NOT propagated, on either branch.

    pip spends 2 on UNKNOWN_ERROR, which is REFUSED's value. Propagating it would
    tell a caller "nothing was touched" about a run that may have changed the
    venv — and the Slack auto-update reads exactly that to decide whether to skip
    its core-dep repair, so the collision would skip the repair of a modified
    venv. Both branches normalize an attempted failure to 1.
    """
    (tmp_path / "setup.cfg").write_text(_SETUP_CFG, encoding="utf-8")

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=pip_rc, stdout=b"", stderr=b"boom")

    # The substitute branch (pip install <specs>).
    with (
        _maps(),
        _origin_stub(),
        patch.object(dep_sync, "interpreter_version", return_value=(3, 12, 0)),
        patch.object(dep_sync.subprocess, "run", side_effect=fake_run),
    ):
        assert dep_sync.sync(tmp_path, Path("/venv/bin/python"), lambda m, e: None) == 1

    # And the reinstall branch (pip install -e <repo>).
    with (
        _maps(),
        _origin_stub(),
        patch.object(dep_sync, "locked_console_scripts", return_value=[]),
        patch.object(dep_sync.subprocess, "run", side_effect=fake_run),
    ):
        rc = dep_sync.sync_or_reinstall(tmp_path, Path("/venv/bin/python"), lambda m, e: None)
    assert rc == 1


def test_sync_or_reinstall_reports_a_failed_reinstall_through_emit(tmp_path):
    """pip's own diagnosis has to reach the caller, not just the exit code.

    Three of the four callers publish this text somewhere a person reads it, and
    an exit code alone leaves them with nothing to act on.
    """
    messages = []

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"no matching distribution")

    with (
        _origin_stub(),
        _maps(),
        patch.object(dep_sync, "locked_console_scripts", return_value=[]),
        patch.object(dep_sync.subprocess, "run", side_effect=fake_run),
    ):
        rc = dep_sync.sync_or_reinstall(
            tmp_path, Path("/venv/bin/python"), lambda m, e: messages.append((m, e))
        )

    assert rc == 1
    assert any(e and "no matching distribution" in m for m, e in messages)


def test_sync_or_reinstall_survives_undecodable_pip_output(tmp_path):
    """pip's stderr is captured as BYTES and decoded leniently.

    `text=True` would decode with the locale's codec, so on a non-UTF-8 console
    a byte pip emitted would raise UnicodeDecodeError and lose the very message
    the capture exists to surface.
    """
    messages = []

    def fake_run(argv, **kwargs):
        assert kwargs.get("text") is not True
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"\xff\xfe bad bytes")

    with (
        _origin_stub(),
        _maps(),
        patch.object(dep_sync, "locked_console_scripts", return_value=[]),
        patch.object(dep_sync.subprocess, "run", side_effect=fake_run),
    ):
        rc = dep_sync.sync_or_reinstall(
            tmp_path, Path("/venv/bin/python"), lambda m, e: messages.append((m, e))
        )

    assert rc == 1
    assert any("bad bytes" in m for m, _ in messages)


def test_sync_or_reinstall_bounds_both_branches(tmp_path):
    """``timeout`` reaches the substitute as well as the reinstall.

    Leaving the substitute unbounded was the wrong trade: a killed dependency
    install is rerunnable and a partial set is already this module's accepted
    exposure, while an unbounded one hangs the surface that called it — the
    dashboard endpoint had a hard 120s bound before this refactor and would
    otherwise have come out of it with none.
    """
    with (
        _origin_stub(),
        _maps(),
        patch.object(dep_sync, "locked_console_scripts", return_value=["x.exe"]),
        patch.object(dep_sync, "sync", return_value=0) as sync_mock,
    ):
        dep_sync.sync_or_reinstall(tmp_path, Path("/venv/bin/python"), timeout=7)

    assert sync_mock.call_args.kwargs["timeout"] == 7


def test_sync_kills_a_hung_pip_and_says_the_set_may_be_partial(tmp_path):
    """A timed-out install reports honestly instead of claiming a clean refusal.

    The refusal wording ("No dependency was installed") would be a lie here: pip
    was killed mid-run, so some of the set may be on disk. The message has to say
    so, because the operator's next step depends on it.
    """
    messages = []
    (tmp_path / "setup.cfg").write_text(_SETUP_CFG, encoding="utf-8")

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    with (
        _maps(),
        patch.object(dep_sync, "installed_package_origin", return_value="<stub>"),
        patch.object(dep_sync, "interpreter_version", return_value=(3, 12, 0)),
        patch.object(dep_sync.subprocess, "run", side_effect=fake_run),
    ):
        rc = dep_sync.sync(
            tmp_path, Path("/venv/bin/python"), lambda m, e: messages.append((m, e)), timeout=5
        )

    assert rc == 1
    joined = " ".join(m for m, _ in messages)
    assert "may be partially installed" in joined
    assert "No dependency was installed" not in joined


def test_installed_package_origin_fails_closed_on_an_unrunnable_interpreter(tmp_path):
    """An interpreter that cannot be RUN answers None, it does not raise.

    Callers resolve the path by a filesystem check, so a venv deleted between that
    check and this probe would otherwise raise OSError out of a request handler.
    None is read as "cannot be shown to serve this checkout", which is the right
    answer for an interpreter that is not there.
    """
    with patch.object(dep_sync.subprocess, "run", side_effect=FileNotFoundError(2, "gone")):
        assert dep_sync.installed_package_origin(tmp_path / "python") is None
    # And that answer refuses rather than proceeding.
    assert dep_sync.venv_not_mapped_to(None, tmp_path) is not None


def _decoy_package(tmp_path):
    """A directory holding a decoy ``kiro_crew`` package, as a checkout's src/ does."""
    decoy = tmp_path / "decoy-src"
    (decoy / "kiro_crew").mkdir(parents=True)
    (decoy / "kiro_crew" / "__init__.py").write_text("", encoding="utf-8")
    return decoy


def test_origin_probe_ignores_a_decoy_package_in_the_callers_cwd(tmp_path, monkeypatch):
    """The probe describes the venv, never the caller's working directory.

    The reproduction from the field: the caller (the Dev Fleet backend) runs
    with its CWD inside a checkout's ``src/``, which contains ``kiro_crew/``.
    An unisolated ``python -c`` puts that CWD at ``sys.path[0]``, so
    ``find_spec`` resolves the decoy for ANY target interpreter and the guard
    refuses a healthy venv. The probe's answer must not be the decoy.
    """
    decoy = _decoy_package(tmp_path)
    monkeypatch.chdir(decoy)

    origin = dep_sync.installed_package_origin(Path(sys.executable))

    # Whatever this interpreter genuinely serves (an editable install, or no
    # install at all -> None), it is not the package sitting in the caller's CWD.
    assert origin is None or not origin.startswith(str(decoy))


def test_origin_probe_ignores_pythonpath(tmp_path, monkeypatch):
    """The second route to the same false positive: an inherited PYTHONPATH.

    PYTHONPATH entries rank ahead of site-packages, so a decoy on the caller's
    PYTHONPATH shadows the venv's own install exactly like the CWD does. The
    probe runs the interpreter isolated, so the inherited value is ignored.
    """
    decoy = _decoy_package(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(decoy))

    origin = dep_sync.installed_package_origin(Path(sys.executable))

    assert origin is None or not origin.startswith(str(decoy))


def test_every_interpreter_probe_runs_isolated_with_a_neutral_cwd(tmp_path):
    """All three venv probes share the isolation, not just the origin one.

    ``interpreter_version`` and ``installed_console_script_target`` ask the
    same kind of question of the same interpreter; a probe left unisolated
    would re-open the CWD/PYTHONPATH route this module just closed.
    """
    target = tmp_path / "venv" / "bin" / "python"
    target.parent.mkdir(parents=True)
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(dep_sync.subprocess, "run", side_effect=fake_run):
        dep_sync.interpreter_version(target)
        dep_sync.installed_package_origin(target)
        dep_sync.installed_console_script_target(target, "kirocrew")

    assert len(seen) == 3
    for cmd, kwargs in seen:
        assert cmd[0] == str(target)
        assert cmd[1] == "-I", "probe must run the interpreter isolated"
        assert cmd[2:4] == ["-X", "utf8"], (
            "-I implies -E, so the UTF-8 pin must ride the argv -- an env "
            "PYTHONIOENCODING is ignored and a non-ASCII path would come "
            "back locale-mangled"
        )
        assert kwargs.get("cwd") == target.parent, "probe needs a neutral cwd"


def test_probe_forwards_a_timeout_to_the_child(tmp_path):
    """Interactive callers (the doctor's deps check, the STT toolchain scan)
    bound the probe so a wedged interpreter cannot hang them; the sync path's
    default ``None`` keeps its unbounded wait."""
    target = tmp_path / "venv" / "bin" / "python"
    target.parent.mkdir(parents=True)
    seen: list[dict] = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(dep_sync.subprocess, "run", side_effect=fake_run):
        dep_sync._probe_interpreter(target, "print(1)", timeout=7)
        dep_sync._probe_interpreter(target, "print(1)")

    assert seen[0].get("timeout") == 7
    assert seen[1].get("timeout") is None


def test_probe_absolutizes_a_relative_interpreter_path(tmp_path, monkeypatch):
    """A relative target -- ``shutil.which`` returns one for a relative PATH
    entry -- must not be re-resolved against the probe's own cwd pin: spawning
    ``tools/python3`` from ``cwd=tools`` would look for ``tools/tools/python3``
    and report a healthy interpreter as unprobeable."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "python3").touch()
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["argv0"] = cmd[0]
        seen["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(dep_sync.subprocess, "run", side_effect=fake_run):
        dep_sync._probe_interpreter(Path("tools/python3"), "print(1)")

    assert seen["argv0"] == str(tmp_path / "tools" / "python3")
    assert seen["cwd"] == tmp_path / "tools"


def test_sync_captures_pip_output_instead_of_writing_it_to_stderr(tmp_path, capsys):
    """pip's output must reach ``emit``, never the inherited stderr.

    pip echoes the index URL it resolved against, and an authenticated index
    carries its credential there. On the in-process paths the inherited stderr IS
    the gateway's log, so streaming would write that credential to a file nobody
    reviews. Handing it to ``emit`` puts it in front of the caller that knows where
    it is about to be published and is the one redacting it.
    """
    messages = []
    (tmp_path / "setup.cfg").write_text(_SETUP_CFG, encoding="utf-8")
    secret = "https://user:s3cr3t@index.example.com/simple"

    def fake_run(argv, **kwargs):
        # Captured, so pip's stream never reaches this process's stderr.
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is not True
        return SimpleNamespace(returncode=1, stdout=b"", stderr=f"looking in {secret}".encode())

    with (
        _maps(),
        patch.object(dep_sync, "installed_package_origin", return_value="<stub>"),
        patch.object(dep_sync, "interpreter_version", return_value=(3, 12, 0)),
        patch.object(dep_sync.subprocess, "run", side_effect=fake_run),
    ):
        rc = dep_sync.sync(tmp_path, Path("/venv/bin/python"), lambda m, e: messages.append((m, e)))

    assert rc == 1
    assert any(secret in m for m, _ in messages), messages
    # And it did NOT leak to the streams this module was printing to before.
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_module_imports_stdlib_only():
    """The invariant ``kiro_crew._bootstrap`` depends on, enforced.

    That caller reaches for this module precisely when a declared dependency is
    missing from the venv, so a third-party import here would fail in exactly the
    case the module exists to repair. Reading the AST rather than the import graph
    keeps this honest even when the offending package happens to be installed on
    the machine running the tests.
    """
    tree = ast.parse(Path(dep_sync.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    # `tomllib` needs no allowance: it is stdlib on every interpreter this
    # project supports, so `sys.stdlib_module_names` already covers it and this
    # assertion would catch a floor that regressed below 3.11. `tomli` is the
    # vestigial third-party fallback dep_sync still carries for the retired
    # pre-3.11 leg; its import is guarded, so a missing one degrades a reader
    # rather than breaking the module.
    third_party = roots - set(sys.stdlib_module_names) - {"tomli"}
    assert not third_party, f"dep_sync must import stdlib only; found {sorted(third_party)}"
