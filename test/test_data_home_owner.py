"""A data home another account owns: refused root runs, found leftovers, the explanation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew import data_home_owner
from kiro_crew.platform_compat import IS_POSIX

pytestmark = pytest.mark.skipif(not IS_POSIX, reason="ownership by uid is POSIX only")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "crew"
    (home / "members").mkdir(parents=True)
    (home / "config.json").write_text("{}", encoding="utf-8")
    return home


def _found(tmp_path: Path, owner: str = "root") -> data_home_owner.DataHomeAccess:
    return data_home_owner.DataHomeAccess(
        home=str(tmp_path), foreign=(str(tmp_path / "config.json"),), owner=owner
    )


def test_a_home_this_account_owns_is_ok(tmp_path):
    access = data_home_owner.check_data_home(_home(tmp_path))
    assert access.ok
    assert access.foreign == ()
    assert access.owner == ""
    assert access.explain() == ""


def test_missing_entries_are_not_reported(tmp_path):
    home = tmp_path / "crew"
    home.mkdir()
    assert data_home_owner.check_data_home(home).ok


def test_files_another_account_owns_are_reported_with_their_owner(tmp_path, monkeypatch):
    home = _home(tmp_path).resolve()
    real = os.geteuid()
    # Seen from another account, every existing entry belongs to someone else.
    monkeypatch.setattr(data_home_owner.os, "geteuid", lambda: real + 1)
    access = data_home_owner.check_data_home(home)
    assert not access.ok
    assert access.foreign == (str(home), str(home / "config.json"), str(home / "members"))
    assert access.owner == data_home_owner._account_name(real)
    assert access.to_json() == {
        "ok": False,
        "home": str(home),
        "foreign": list(access.foreign),
        "owner": access.owner,
        "help_url": data_home_owner.HELP_URL,
    }


def test_the_explanation_names_folder_owner_cause_and_docs_but_no_command(tmp_path):
    text = _found(tmp_path).explain()
    assert f"Some Kiro Crew files in {tmp_path} belong to root" in text
    assert str(tmp_path / "config.json") in text
    assert "probably started as root once" in text and "Stop running it as root" in text
    assert text.endswith(data_home_owner.HELP_URL)
    # Kiro Crew offers no line that changes owners as root.
    assert "chown" not in text and "sudo " not in text


def test_the_docs_section_the_explanation_links_exists():
    doc = Path(data_home_owner.__file__).parent / "docs" / "troubleshooting.md"
    anchor = data_home_owner.HELP_URL.rsplit("#", 1)[1]
    headings = [
        line.lstrip("#").strip().lower().replace(" ", "-")
        for line in doc.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    ]
    assert anchor in headings


def test_files_this_account_owns_but_cannot_use_have_no_other_owner(tmp_path, monkeypatch):
    home = _home(tmp_path).resolve()
    monkeypatch.setattr(
        data_home_owner.os, "access", lambda path, mode: Path(path).name != "config.json"
    )
    access = data_home_owner.check_data_home(home)
    assert access.foreign == (str(home / "config.json"),)
    assert access.owner == ""
    assert "can't be used by this account" in access.explain()


def test_a_linked_home_is_checked_and_reported_at_its_real_folder(tmp_path):
    real = _home(tmp_path).resolve()
    link = tmp_path / "linked"
    link.symlink_to(real)
    access = data_home_owner.check_data_home(link)
    assert access.ok
    assert access.home == str(real)


def test_a_symlinked_config_is_not_reported_or_followed(tmp_path, monkeypatch):
    home = _home(tmp_path)
    (home / "config.json").unlink()
    (home / "config.json").symlink_to(tmp_path / "elsewhere.json")
    (tmp_path / "elsewhere.json").write_text("{}", encoding="utf-8")
    real = os.geteuid()
    monkeypatch.setattr(data_home_owner.os, "geteuid", lambda: real + 1)
    foreign = data_home_owner.check_data_home(home).foreign
    assert str(home.resolve() / "config.json") not in foreign


def test_a_read_only_config_this_account_owns_is_usable(tmp_path):
    home = _home(tmp_path)
    (home / "config.json").chmod(0o400)
    assert data_home_owner.check_data_home(home).ok


@pytest.mark.skipif(IS_POSIX and os.geteuid() == 0, reason="needs a home root does not own")
def test_root_aimed_at_a_persons_home_is_refused(tmp_path, monkeypatch):
    home = tmp_path / "not-yet" / "crew"
    monkeypatch.setattr(data_home_owner.os, "geteuid", lambda: 0)
    refusal = data_home_owner.root_run_refusal(home, "gateway", ["gateway", "--port", "auto"])
    assert "running as root" in refusal
    assert str(home) in refusal
    assert refusal.endswith("Run it without sudo: kirocrew gateway --port auto")


def test_a_normal_account_is_never_refused(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("this host runs the suite as root")
    assert data_home_owner.root_run_refusal(_home(tmp_path), "gateway", ["gateway"]) == ""


def test_root_in_roots_own_home_is_left_alone(monkeypatch):
    monkeypatch.setattr(data_home_owner.os, "geteuid", lambda: 0)
    # The closest existing folder is "/", which root owns.
    home = Path("/") / "kirocrew-test-absent" / "crew"
    assert data_home_owner.root_run_refusal(home, "gateway", ["gateway"]) == ""


@pytest.mark.skipif(IS_POSIX and os.geteuid() == 0, reason="needs a home root does not own")
@pytest.mark.parametrize("command", sorted(data_home_owner.ROOT_ALLOWED_COMMANDS))
def test_the_documented_root_commands_are_left_alone(tmp_path, monkeypatch, command):
    monkeypatch.setattr(data_home_owner.os, "geteuid", lambda: 0)
    assert data_home_owner.root_run_refusal(_home(tmp_path), command, [command]) == ""


@pytest.mark.parametrize("argv", [["status"], []], ids=["subcommand", "bare"])
def test_the_cli_refuses_before_it_touches_the_data_home(monkeypatch, capsys, argv):
    from kiro_crew import cli

    def touched():
        raise AssertionError("the data home was resolved before the refusal")

    from kiro_crew.platform import tree_liveness

    monkeypatch.setattr(cli, "ensure_data_home", touched)
    # Project detection's saved-path lookup creates the data home.
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    monkeypatch.setattr(cli, "_detect_project_dir", touched)
    # The first step after the refusal for a non-gateway command.
    monkeypatch.setattr(tree_liveness, "hold_running_tree_lock", touched)
    monkeypatch.setattr(
        data_home_owner, "root_run_refusal", lambda home, command, argv: f"refused: {command}"
    )
    monkeypatch.setattr(sys, "argv", ["kirocrew", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert f"refused: {argv[0] if argv else None}" in capsys.readouterr().err


def test_the_gateway_warning_is_the_explanation(monkeypatch, caplog, tmp_path):
    found = _found(tmp_path)
    monkeypatch.setattr(data_home_owner, "check_data_home", lambda home: found)
    with caplog.at_level("WARNING", logger=data_home_owner.__name__):
        data_home_owner.warn_if_data_home_not_owned(tmp_path)
    assert found.explain() in caplog.text


def test_the_gateway_is_quiet_when_this_account_owns_the_home(caplog, tmp_path):
    with caplog.at_level("WARNING", logger=data_home_owner.__name__):
        data_home_owner.warn_if_data_home_not_owned(tmp_path)
    assert caplog.text == ""


def _start_gateway(monkeypatch, found: data_home_owner.DataHomeAccess) -> object:
    """Run ``kirocrew gateway`` with *found* as the ownership check; its exit code."""
    from kiro_crew import cli

    monkeypatch.setattr(data_home_owner, "check_data_home", lambda home: found)
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "gateway"])
    monkeypatch.setattr(
        cli.platform_compat, "limit_malloc_arenas", cli.platform_compat.MallocArenaCap
    )
    # The gateway path arms faulthandler on the real stderr descriptor.
    monkeypatch.setattr(cli.faulthandler, "enable", lambda: None)
    monkeypatch.setattr(cli.asyncio, "run", lambda *a, **k: pytest.fail("the gateway ran"))
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    return excinfo.value.code


def test_a_refused_gateway_lock_names_who_owns_the_files(monkeypatch, capsys, tmp_path):
    from unittest.mock import MagicMock

    from kiro_crew import cli
    from kiro_crew.gateway_lock import LockFileError

    lock = tmp_path / "gateway.lock"
    lock_cls = MagicMock()
    lock_cls.return_value.acquire.side_effect = LockFileError(
        tmp_path, f"could not open {lock}: Permission denied"
    )
    monkeypatch.setattr(cli, "GatewayLock", lock_cls)
    found = _found(tmp_path)
    assert _start_gateway(monkeypatch, found) == 1
    err = capsys.readouterr().err
    assert found.explain() in err
    assert err.index(f"could not open {lock}") < err.index(found.explain())


@pytest.mark.parametrize("owner", ["root", ""], ids=["another account", "this account"])
def test_a_home_that_cannot_hold_files_names_who_owns_it(monkeypatch, capsys, tmp_path, owner):
    from kiro_crew import cli

    monkeypatch.setattr(
        cli.platform_compat,
        "probe_file_persistence",
        lambda directory: f"cannot create files in {directory}: Permission denied",
    )
    monkeypatch.setattr(cli, "maybe_reexec", lambda argv: pytest.fail("reached maybe_reexec"))
    found = _found(tmp_path, owner)
    assert _start_gateway(monkeypatch, found) == 1
    err = capsys.readouterr().err
    if owner:
        assert found.explain() in err
        assert err.index("Error: Cannot persist state: ") < err.index(found.explain())
    else:
        # This account's own read-only home: the refusal line already names the cause.
        assert found.explain() not in err


def test_doctor_reports_the_files_owner_and_docs(monkeypatch, capsys, tmp_path):
    from kiro_crew.doctor_checks import install

    monkeypatch.setattr(data_home_owner, "check_data_home", lambda home: _found(tmp_path))
    issues: list[str] = []
    install._doctor_data_home_owner(issues)
    out = capsys.readouterr().out
    assert "some files belong to root" in out
    assert str(tmp_path / "config.json") in out
    assert "Stop running it as root" in out and data_home_owner.HELP_URL in out
    assert "chown" not in out
    assert issues == [f"data home files belong to root; see {data_home_owner.HELP_URL}"]


def test_doctor_is_quiet_when_this_account_owns_the_home(monkeypatch, capsys, tmp_path):
    from kiro_crew.doctor_checks import install

    monkeypatch.setattr(
        data_home_owner, "check_data_home", lambda home: data_home_owner.DataHomeAccess(str(home))
    )
    issues: list[str] = []
    install._doctor_data_home_owner(issues)
    assert issues == []
    assert "OK:" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_api_status_carries_the_data_home_check(monkeypatch, tmp_path):
    from test_handlers_system_meminfo_unavailable import _status_body

    from kiro_crew.dashboard import handlers_system

    found = _found(tmp_path)
    seen: list[Path] = []

    def check(home):
        seen.append(home)
        return found

    monkeypatch.setattr(handlers_system, "data_home", lambda: tmp_path)
    monkeypatch.setattr(data_home_owner, "check_data_home", check)
    body = await _status_body(monkeypatch, tmp_path, {"os": "Linux", "arch": "x86_64"})
    assert seen == [tmp_path]
    assert body["data_home_access"] == {
        "ok": False,
        "home": str(tmp_path),
        "foreign": [str(tmp_path / "config.json")],
        "owner": "root",
        "help_url": data_home_owner.HELP_URL,
    }
