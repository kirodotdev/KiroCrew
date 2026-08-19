"""Tests for the dependency-only sync that stands in for a blocked reinstall.

The module exists because Windows cannot rewrite a running console script, so
these tests pin what the substitution rests on: it is shaped as PARITY with
``pip install -e .`` rather than as an improvement on it (same order, same scope,
extras left alone), it never hands the project itself to pip, it applies the two
gates a dependency-only install cannot inherit from pip (the interpreter floor and
a repointed console script), and it refuses to write to a venv that serves a
different checkout.
"""

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.apps.builtins.dev_fleet import dep_sync

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

    assert rc == 1
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

    assert rc == 1
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

    assert rc == 1
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

    assert rc == 1
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
