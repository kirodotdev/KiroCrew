"""The Harness installer's first attempt from the user's own pip index, with a fake runner.

``browser_harness/install.py::_pip_install``: with pip index settings the pip step runs
first with them and wheels only, then, if that fails, once more exactly as without them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import sdk_extras
from kiro_crew.browser_harness import build_deps, install


@pytest.fixture(autouse=True)
def _no_old_glibc(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here runs as on a host whose glibc the locked wheels cover: without the
    pin an old-glibc test host would refuse every install below."""
    _floor_monkeypatch.setattr(build_deps, "refusal_for_this_host", lambda: None)


#: The real config reader, read before the autouse fixture below replaces it.
_REAL_CONFIGURED_INDEX_ENV = sdk_extras._configured_index_env


@pytest.fixture(autouse=True)
def _no_pip_index_settings(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here reads the host's own pip settings or spawns a real ``pip config list``;
    a test that needs index settings sets them itself."""
    _floor_monkeypatch.setattr(sdk_extras, "_configured_index_env", lambda *a, **k: {})


@pytest.fixture(autouse=True)
def runtime_site(_floor_monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Resolve lock distributions only from fake runtime sites, never the test venv."""
    dirs: list[Path] = []
    _floor_monkeypatch.setattr(
        install, "_runtime_search_path", lambda: [str(d) for d in dirs], raising=False
    )
    return dirs


def _ok(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    # ``python -m venv <prefix>/venv`` creates the interpreter the next step runs;
    # ``--clear`` empties the directory first.
    if argv[1:4] == ["-I", "-m", "venv"]:
        if "--clear" in argv:
            shutil.rmtree(argv[-1], ignore_errors=True)
        python = install.venv_python(Path(argv[-1]).parent)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("", encoding="utf-8")
    return subprocess.CompletedProcess(argv, 0, "", "")


def _gateway_ships(monkeypatch, *present: str) -> None:
    """Pin which of ``pip`` / ``ensurepip`` the installer's ``-I`` children can import."""
    monkeypatch.setattr(install, "_has_module", lambda name, run: name in present)


#: A mirror URL whose userinfo carries a credential that must never be shown.
_SECRET_INDEX = "https://user:s3cret-tok@mirror.invalid/simple/"


def _with_index(monkeypatch, url: str = "https://mirror.invalid/simple/") -> dict[str, str]:
    index_env = {"PIP_INDEX_URL": url}
    monkeypatch.setattr(sdk_extras, "_configured_index_env", lambda *a, **k: dict(index_env))
    return index_env


def _pip_runner(calls: list[tuple[list[str], dict | None]], *results):
    """Record every spawn with the ``env`` it was given (``None``: the default); answer the
    install spawns with *results* in order (an exit code and output, or an exception)."""
    answers = list(results)

    def runner(argv: list[str], timeout: float, **kw) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kw.get("env")))
        if "install" in argv and answers:
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            code, output = answer
            return subprocess.CompletedProcess(argv, code, "", output)
        return _ok(argv, timeout)

    return runner


def _install_spawns(calls: list[tuple[list[str], dict | None]]):
    return [(argv, env) for argv, env in calls if "install" in argv]


def _default_install_argv(prefix: Path) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-m",
        "pip",
        "--python",
        str(install.venv_python(prefix)),
        *install._PIP_ARGS,
        str(install.LOCK_FILE),
    ]


def test_pip_index_settings_make_the_first_install_wheels_only_from_that_index(
    tmp_path: Path, monkeypatch
) -> None:
    _gateway_ships(monkeypatch, "pip")
    index_env = _with_index(monkeypatch)
    calls: list[tuple[list[str], dict | None]] = []
    install.install_sync(
        tmp_path, runner=_pip_runner(calls, (0, "")), components=(install.COMPONENT_HARNESS,)
    )
    spawns = _install_spawns(calls)
    assert len(spawns) == 1
    argv, env = spawns[0]
    default = _default_install_argv(tmp_path / install.PREFIX_LEAF)
    assert argv == [*default, "--only-binary", ":all:"]
    assert env == {**install._child_env(), **index_env}
    assert env["PIP_INDEX_URL"] == index_env["PIP_INDEX_URL"]
    assert env["PIP_CONFIG_FILE"] == os.devnull
    # Every other spawn keeps the installer's own environment.
    assert all(e is None for a, e in calls if "install" not in a)


def test_a_failed_first_install_runs_once_more_exactly_as_without_settings(
    tmp_path: Path, monkeypatch
) -> None:
    _gateway_ships(monkeypatch, "pip")
    _with_index(monkeypatch)
    calls: list[tuple[list[str], dict | None]] = []
    # A hash mismatch is not a download failure: the Feishu installer would not retry it.
    mismatch = (1, "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.")
    install.install_sync(
        tmp_path,
        runner=_pip_runner(calls, mismatch, (0, "")),
        components=(install.COMPONENT_HARNESS,),
    )
    spawns = _install_spawns(calls)
    assert len(spawns) == 2
    assert spawns[1] == (_default_install_argv(tmp_path / install.PREFIX_LEAF), None)
    assert install.install_state(tmp_path).installed is True


def test_without_pip_index_settings_the_install_runs_once_with_the_installers_env(
    tmp_path: Path, monkeypatch
) -> None:
    _gateway_ships(monkeypatch, "pip")
    calls: list[tuple[list[str], dict | None]] = []
    install.install_sync(
        tmp_path, runner=_pip_runner(calls, (0, "")), components=(install.COMPONENT_HARNESS,)
    )
    assert _install_spawns(calls) == [(_default_install_argv(tmp_path / install.PREFIX_LEAF), None)]


@pytest.mark.parametrize("gateway_pip", [True, False])
def test_the_pip_settings_are_read_by_the_pip_that_installs(
    tmp_path: Path, monkeypatch, gateway_pip: bool
) -> None:
    _gateway_ships(monkeypatch, "pip" if gateway_pip else "ensurepip")
    monkeypatch.setattr(sdk_extras, "_configured_index_env", _REAL_CONFIGURED_INDEX_ENV)
    for name in sdk_extras._PIP_INDEX_ENV:
        monkeypatch.delenv(name, raising=False)
    read_with: list[list[str]] = []

    def config_list(argv, **kwargs):  # type: ignore[no-untyped-def]
        read_with.append(argv)
        return subprocess.CompletedProcess(argv, 0, "global.index-url='https://m.invalid/s/'\n", "")

    monkeypatch.setattr(sdk_extras.subprocess, "run", config_list)
    calls: list[tuple[list[str], dict | None]] = []
    install.install_sync(
        tmp_path, runner=_pip_runner(calls, (0, "")), components=(install.COMPONENT_HARNESS,)
    )
    reader = (
        sys.executable if gateway_pip else str(install.venv_python(tmp_path / install.PREFIX_LEAF))
    )
    assert read_with == [[reader, "-I", "-m", "pip", "config", "list"]]
    assert _install_spawns(calls)[0][1]["PIP_INDEX_URL"] == "https://m.invalid/s/"  # type: ignore[index]


def test_a_configured_index_credential_never_reaches_the_log_or_the_error(
    tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    _gateway_ships(monkeypatch, "pip")
    _with_index(monkeypatch, _SECRET_INDEX)
    calls: list[tuple[list[str], dict | None]] = []
    first = (
        1,
        f"Looking in indexes: {_SECRET_INDEX}\nWARNING: Retrying after connection broken by "
        "'ProxyError()': /simple/anyio/\nERROR: No matching distribution found for anyio",
    )
    caplog.set_level("DEBUG", logger=install.logger.name)
    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(
            tmp_path,
            runner=_pip_runner(calls, first, (1, "ERROR: no route to pypi.org")),
            components=(install.COMPONENT_HARNESS,),
        )
    assert len(_install_spawns(calls)) == 2
    assert "mirror.invalid" in caplog.text
    for shown in (caplog.text, excinfo.value.detail, str(excinfo.value)):
        assert "s3cret-tok" not in shown and "user:" not in shown


def test_a_stop_during_the_first_install_does_not_start_the_second(
    tmp_path: Path, monkeypatch
) -> None:
    _gateway_ships(monkeypatch, "pip")
    _with_index(monkeypatch)
    calls: list[tuple[list[str], dict | None]] = []
    stopped = install.InstallError(install.STAGE_PREPARING, "install stopped")
    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(
            tmp_path, runner=_pip_runner(calls, stopped), components=(install.COMPONENT_HARNESS,)
        )
    assert excinfo.value.detail == "install stopped"
    spawns = _install_spawns(calls)
    assert len(spawns) == 1 and "--only-binary" in spawns[0][0]


def test_a_timed_out_first_install_runs_the_second_once(tmp_path: Path, monkeypatch) -> None:
    _gateway_ships(monkeypatch, "pip")
    _with_index(monkeypatch)
    calls: list[tuple[list[str], dict | None]] = []
    timeout = subprocess.TimeoutExpired(["pip"], install._PIP_TIMEOUT_SECS)
    install.install_sync(
        tmp_path,
        runner=_pip_runner(calls, timeout, (0, "")),
        components=(install.COMPONENT_HARNESS,),
    )
    spawns = _install_spawns(calls)
    assert len(spawns) == 2 and spawns[1][1] is None
    assert install.install_state(tmp_path).installed is True


def test_when_both_installs_fail_the_error_ends_naming_the_index_tried_first(
    tmp_path: Path, monkeypatch
) -> None:
    _gateway_ships(monkeypatch, "pip")
    _with_index(monkeypatch, _SECRET_INDEX)
    calls: list[tuple[list[str], dict | None]] = []
    second = "x" * (install.ERROR_DETAIL_CAP * 2) + "\nERROR: the fallback's reason"
    with pytest.raises(install.InstallError) as excinfo:
        install.install_sync(
            tmp_path,
            runner=_pip_runner(calls, (1, "FIRST-ATTEMPT-OUTPUT"), (1, second)),
            components=(install.COMPONENT_HARNESS,),
        )
    detail = excinfo.value.detail
    assert excinfo.value.stage == install.STAGE_INSTALLING_HARNESS
    assert detail.splitlines()[-1] == (
        "pip first tried https://mirror.invalid (your pip settings), which failed too; "
        "the gateway log has its reason"
    )
    assert "ERROR: the fallback's reason" in detail
    assert "FIRST-ATTEMPT-OUTPUT" not in detail


@pytest.mark.parametrize(
    ("refusal", "message"),
    [(OSError, "[Errno 7] Argument list too long"), (ValueError, "embedded null byte")],
    ids=["e2big", "nul"],
)
def test_a_first_install_that_cannot_start_runs_the_second_as_without_settings(
    tmp_path: Path, monkeypatch, refusal: type[Exception], message: str
) -> None:
    _gateway_ships(monkeypatch, "pip")
    _with_index(monkeypatch)
    calls: list[tuple[list[str], dict | None]] = []
    install.install_sync(
        tmp_path,
        runner=_pip_runner(calls, refusal(message), (0, "")),
        components=(install.COMPONENT_HARNESS,),
    )
    spawns = _install_spawns(calls)
    assert len(spawns) == 2
    assert spawns[1] == (_default_install_argv(tmp_path / install.PREFIX_LEAF), None)
    assert install.install_state(tmp_path).installed is True


def test_an_index_url_python_cannot_parse_still_runs_the_second_install(
    tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    _gateway_ships(monkeypatch, "pip")
    bad = "https://[mirror/s3cret-tok/simple/"
    _with_index(monkeypatch, bad)
    calls: list[tuple[list[str], dict | None]] = []
    caplog.set_level("DEBUG", logger=install.logger.name)
    install.install_sync(
        tmp_path,
        runner=_pip_runner(calls, (1, f"Looking in indexes: {bad}\nERROR: bad URL"), (0, "")),
        components=(install.COMPONENT_HARNESS,),
    )
    spawns = _install_spawns(calls)
    assert len(spawns) == 2
    assert spawns[1] == (_default_install_argv(tmp_path / install.PREFIX_LEAF), None)
    assert install.install_state(tmp_path).installed is True
    assert "s3cret-tok" not in caplog.text
