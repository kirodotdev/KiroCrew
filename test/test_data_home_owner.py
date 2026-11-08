"""A data home another account owns: refused root runs, found leftovers, the fix line."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew import data_home_owner
from kiro_crew.platform_compat import IS_POSIX
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

pytestmark = pytest.mark.skipif(not IS_POSIX, reason="ownership by uid is POSIX only")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "crew"
    (home / "members").mkdir(parents=True)
    (home / "config.json").write_text("{}", encoding="utf-8")
    return home


def test_a_home_this_account_owns_is_ok(tmp_path):
    access = data_home_owner.check_data_home(_home(tmp_path))
    assert access.ok
    assert access.foreign == ()
    assert access.fix_command == ""


def test_missing_entries_are_not_reported(tmp_path):
    home = tmp_path / "crew"
    home.mkdir()
    assert data_home_owner.check_data_home(home).ok


def test_files_another_account_owns_are_reported_with_the_fix(tmp_path, monkeypatch):
    home = _home(tmp_path)
    real = os.geteuid()
    # Seen from another account, every existing entry belongs to someone else.
    monkeypatch.setattr(data_home_owner.os, "geteuid", lambda: real + 1)
    access = data_home_owner.check_data_home(home)
    assert not access.ok
    assert access.foreign == (str(home), str(home / "config.json"), str(home / "members"))
    assert access.fix_command.startswith(f"sudo find -P {home} -xdev ! -user ")
    assert access.fix_command.endswith(f" && chmod -R u+rwX {home}")
    assert access.to_json()["ok"] is False


def test_the_privileged_part_skips_hard_links_and_symlink_targets(tmp_path):
    import shlex

    words = shlex.split(data_home_owner.fix_command(tmp_path))
    privileged = words[: words.index("&&")]
    # -P: the walk never follows a symlink.
    assert privileged[:4] == ["sudo", "find", "-P", str(tmp_path.resolve())]
    # Only another account's entries, on this filesystem, and a non-directory
    # only while it has a single link.
    assert privileged[4:7] == ["-xdev", "!", "-user"]
    assert privileged[8:14] == ["(", "-type", "d", "-o", "-links", "1"]
    # chown -h changes a symlink itself, and -execdir hands it ./<name> from the
    # entry's own directory, never a full path.
    assert privileged[15:18] == ["-execdir", "chown", "-h"]
    assert privileged[-1] == ";"
    assert "-R" not in privileged and "-L" not in privileged and "-exec" not in privileged


_FAKE_CHOWN = """
import os, sys
home, outside, log = sys.argv[1:4]
paths = sys.argv[6:]
inner = os.path.join(home, "a")
if not os.path.islink(inner) and any(os.path.basename(p) == "f" for p in paths):
    # The race: once the walk has found a/f, swap a for a symlink to outside.
    os.rename(inner, inner + ".real")
    os.symlink(outside, inner)
with open(log, "a", encoding="utf-8") as out:
    for p in paths:
        # chown -h follows every parent but not the last name.
        parent = os.path.realpath(os.path.dirname(p) or ".")
        out.write(os.path.join(parent, os.path.basename(p)) + "\\n")
"""


def test_a_parent_swapped_for_a_symlink_mid_repair_is_not_followed(tmp_path, monkeypatch):
    import shlex
    import shutil
    import subprocess

    if shutil.which("find") is None:
        pytest.skip("no find on this host")
    root = tmp_path.resolve()
    home = root / "crew"
    (home / "a").mkdir(parents=True)
    (home / "a" / "f").write_text("x", encoding="utf-8")
    outside = root / "outside"
    outside.mkdir()
    (outside / "f").write_text("system", encoding="utf-8")
    log = root / "chowned.log"
    bindir = root / "bin"
    bindir.mkdir()
    (bindir / "fake_chown.py").write_text(_FAKE_CHOWN, encoding="utf-8")
    chown = bindir / "chown"
    chown.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(bindir / "fake_chown.py"))} '
        f'{shlex.quote(str(home))} {shlex.quote(str(outside))} {shlex.quote(str(log))} "$@"\n',
        encoding="utf-8",
    )
    chown.chmod(0o755)
    # Seen as another account's files, so every entry is selected.
    monkeypatch.setattr(data_home_owner, "_account_name", lambda uid: str(os.geteuid() + 1))
    words = shlex.split(data_home_owner.fix_command(home))
    privileged = words[1 : words.index("&&")]
    subprocess.run(
        privileged,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
        capture_output=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    changed = log.read_text(encoding="utf-8").split()
    assert str(home / "a.real" / "f") in changed
    assert not [p for p in changed if p.startswith(str(outside))]


def test_a_linked_home_is_checked_and_repaired_at_its_real_folder(tmp_path):
    real = _home(tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(real)
    assert data_home_owner.check_data_home(link).ok
    assert data_home_owner.fix_command(link).startswith(f"sudo find -P {real} ")


def test_a_symlinked_config_is_not_reported_or_followed(tmp_path, monkeypatch):
    home = _home(tmp_path)
    (home / "config.json").unlink()
    (home / "config.json").symlink_to(tmp_path / "elsewhere.json")
    (tmp_path / "elsewhere.json").write_text("{}", encoding="utf-8")
    real = os.geteuid()
    monkeypatch.setattr(data_home_owner.os, "geteuid", lambda: real + 1)
    assert str(home / "config.json") not in data_home_owner.check_data_home(home).foreign


def test_a_read_only_config_this_account_owns_is_usable(tmp_path):
    home = _home(tmp_path)
    (home / "config.json").chmod(0o400)
    assert data_home_owner.check_data_home(home).ok


def test_an_unusable_file_this_account_owns_is_reported(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setattr(
        data_home_owner.os, "access", lambda path, mode: Path(path).name != "config.json"
    )
    assert data_home_owner.check_data_home(home).foreign == (str(home / "config.json"),)


def test_the_fix_line_quotes_a_path_with_spaces(tmp_path):
    home = tmp_path / "my crew"
    assert f"'{home}'" in data_home_owner.fix_command(home)


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


def test_the_gateway_warning_names_the_files_and_the_fix(monkeypatch, caplog, tmp_path):
    found = data_home_owner.DataHomeAccess(
        home=str(tmp_path), foreign=(str(tmp_path / "config.json"),), fix_command="FIX"
    )
    monkeypatch.setattr(data_home_owner, "check_data_home", lambda home: found)
    with caplog.at_level("WARNING", logger=data_home_owner.__name__):
        data_home_owner.warn_if_data_home_not_owned(tmp_path)
    assert str(tmp_path / "config.json") in caplog.text
    assert "FIX" in caplog.text


def test_the_gateway_is_quiet_when_this_account_owns_the_home(caplog, tmp_path):
    with caplog.at_level("WARNING", logger=data_home_owner.__name__):
        data_home_owner.warn_if_data_home_not_owned(tmp_path)
    assert caplog.text == ""


def test_doctor_reports_the_files_and_the_fix(monkeypatch, capsys, tmp_path):
    from kiro_crew.doctor_checks import install

    found = data_home_owner.DataHomeAccess(
        home=str(tmp_path), foreign=(str(tmp_path / "config.json"),), fix_command="FIX"
    )
    monkeypatch.setattr(data_home_owner, "check_data_home", lambda home: found)
    issues: list[str] = []
    install._doctor_data_home_owner(issues)
    out = capsys.readouterr().out
    assert str(tmp_path / "config.json") in out
    assert "fix: FIX" in out
    assert issues == ["data home files belong to another user; run: FIX"]


def test_doctor_is_quiet_when_this_account_owns_the_home(monkeypatch, capsys, tmp_path):
    from kiro_crew.doctor_checks import install

    monkeypatch.setattr(
        data_home_owner, "check_data_home", lambda home: data_home_owner.DataHomeAccess(str(home))
    )
    issues: list[str] = []
    install._doctor_data_home_owner(issues)
    assert issues == []
    assert "✅" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_api_status_carries_the_data_home_check(monkeypatch, tmp_path):
    from test_handlers_system_meminfo_unavailable import _status_body

    from kiro_crew.dashboard import handlers_system

    found = data_home_owner.DataHomeAccess(
        home=str(tmp_path), foreign=(str(tmp_path / "config.json"),), fix_command="FIX"
    )
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
        "fix_command": "FIX",
    }
