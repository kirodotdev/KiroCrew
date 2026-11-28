"""``kirocrew cron`` when the OS refuses this process the ``.vault`` dir.

macOS data protection can deny a terminal even a ``stat`` of
``<data home>/.vault`` while the gateway reads it fine. ``cron remove`` must
read the grant-epoch state there before it deletes a job, so it still refuses:
the revoke fence stays fail-closed. These tests pin that it refuses with one
line naming the path and the OS refusal instead of a traceback, and that the
verbs that never touch the vault keep working.

The refusal is simulated by patching ``os.stat`` / ``os.mkdir`` for the vault
path only (``Path.is_dir`` and ``Path.mkdir`` go through them), so the test
runs the same on every OS.
"""

from __future__ import annotations

import argparse
import errno
import os
from unittest.mock import patch

import pytest

from kiro_crew.cli_commands import _cron
from kiro_crew.config.loader import config_dir
from kiro_crew.cron import CronService
from kiro_crew.testing.links import make_dir_link


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    (config_dir() / "crons").mkdir(parents=True)
    (config_dir() / ".vault").mkdir()
    with patch("kiro_crew.cli_commands.sel"):
        yield root


def _add_job() -> str:
    svc = CronService(base_dir=config_dir())
    return svc.add_job(name="t", message="hi", every_secs=3600).id


def _deny_vault(monkeypatch) -> str:
    vault = os.fspath(config_dir() / ".vault")
    real_stat, real_mkdir = os.stat, os.mkdir

    def _is_vault(p) -> bool:
        return os.fspath(p).rstrip(os.sep) == vault

    def fake_stat(p, *a, **k):
        if _is_vault(p):
            raise PermissionError(errno.EPERM, "Operation not permitted", vault)
        return real_stat(p, *a, **k)

    def fake_mkdir(p, *a, **k):
        if _is_vault(p):
            raise FileExistsError(errno.EEXIST, "File exists", vault)
        return real_mkdir(p, *a, **k)

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "mkdir", fake_mkdir)
    return vault


def _job_ids() -> set[str]:
    return {j.id for j in CronService(base_dir=config_dir()).list_jobs(include_disabled=True)}


def test_remove_refuses_with_one_line_naming_the_vault(home, monkeypatch, capsys):
    # A terminal outside the sandbox: the OS (macOS data protection) refused it.
    monkeypatch.setattr(
        "kiro_crew.sandbox_unavailable.agent_confinement_evidence", lambda *a, **k: None
    )
    job_id = _add_job()
    vault = _deny_vault(monkeypatch)

    raised: BaseException | None = None
    try:
        _cron(argparse.Namespace(cron_action="remove", job_id=job_id, managed_by=None))
    except BaseException as exc:  # an escaping OSError is the bug: assert, not error
        raised = exc

    assert isinstance(raised, SystemExit), f"traceback instead of a one-line error: {raised!r}"
    assert raised.code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("Error: ")
    assert err.count("\n") == 1
    assert vault in err
    assert "Operation not permitted" in err
    assert "Traceback" not in err
    # Fail-closed: the grant epoch could not be bumped, so the job stays.
    assert job_id in _job_ids()


def test_list_still_works_when_the_vault_is_denied(home, monkeypatch, capsys):
    job_id = _add_job()
    _deny_vault(monkeypatch)

    _cron(argparse.Namespace(cron_action="list"))

    assert job_id in capsys.readouterr().out


def test_a_policy_permission_error_is_not_relabelled_an_os_refusal(home, monkeypatch):
    def refuse(_args):
        raise PermissionError("Cron job ownership violation")

    monkeypatch.setattr("kiro_crew.cli_commands_cron._cron_dispatch", refuse)

    with pytest.raises(PermissionError, match="ownership violation"):
        _cron(argparse.Namespace(cron_action="list"))


def _mountinfo_field(path: str) -> str:
    """*path* written as mountinfo writes a field: ``\\``, space, tab, newline octal-escaped."""
    for raw, escaped in (("\\", "\\134"), (" ", "\\040"), ("\t", "\\011"), ("\n", "\\012")):
        path = path.replace(raw, escaped)
    return path


def _mount_over_vault(tmp_path, monkeypatch, root: str) -> None:
    """List a mount on ``.vault`` in a stand-in mountinfo, with its source at *root*.

    The launcher's stand-in for the vault is an empty, writable directory, so every
    file call still works on it; what tells it apart is the mount the namespace
    lists on the vault path.
    """
    from kiro_crew import cron_script

    vault = _mountinfo_field(os.path.realpath(config_dir() / ".vault"))
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 1 253:0 / / rw,relatime - ext4 /dev/root rw\n"
        f"8767 22 0:183 {root} {vault} rw,nosuid,nodev - tmpfs tmpfs rw,mode=700\n"
    )
    monkeypatch.setattr(cron_script, "_MOUNTINFO_PATH", str(mountinfo))


def _mask_vault(tmp_path, monkeypatch) -> None:
    """Make ``.vault`` look like the Linux sandbox's stand-in."""
    _mount_over_vault(tmp_path, monkeypatch, "/kirocrew_sb_4242_rlxprp0q")


def test_remove_refuses_on_the_sandbox_stand_in_for_the_vault(home, tmp_path, monkeypatch, capsys):
    """On the stand-in a removal would bump an epoch that vanishes with the sandbox,
    deleting the job while its saved grant stays replayable. It refuses instead, with
    the routes that do reach the vault."""
    job_id = _add_job()
    _mask_vault(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as raised:
        _cron(argparse.Namespace(cron_action="remove", job_id=job_id, managed_by=None))

    assert raised.value.code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("Error: this command runs inside the agent sandbox")
    assert "cron_remove" in err
    assert err.count("\n") == 1
    assert job_id in _job_ids()
    assert list((config_dir() / ".vault").iterdir()) == []


def test_epoch_reads_and_writes_refuse_on_the_stand_in(home, tmp_path, monkeypatch):
    from kiro_crew import cron_script

    _mask_vault(tmp_path, monkeypatch)

    for call in (
        lambda: cron_script.bump_grant_epoch("abc123"),
        lambda: cron_script.commit_grant_epoch("abc123", 1),
        lambda: cron_script.grant_epoch_ids(),
        lambda: cron_script.peek_grant_epoch("abc123"),
    ):
        with pytest.raises(cron_script.GrantEpochsMasked) as raised:
            call()
        assert raised.value.errno == errno.EPERM
        assert raised.value.filename == os.fspath(config_dir() / ".vault")
    assert list((config_dir() / ".vault").iterdir()) == []


def test_remove_still_bumps_the_epoch_on_the_real_vault(home):
    from kiro_crew import cron_script

    job_id = _add_job()
    cron_script.commit_grant_epoch(job_id, 3)

    _cron(argparse.Namespace(cron_action="remove", job_id=job_id, managed_by=None))

    assert job_id not in _job_ids()
    assert cron_script.peek_grant_epoch(job_id) == 5


def test_a_vault_mounted_from_elsewhere_is_the_real_vault(home, tmp_path, monkeypatch):
    """Only the launcher's own stand-in counts: an operator who keeps the vault on
    another filesystem still grants and removes as before."""
    from kiro_crew import cron_script

    _mount_over_vault(tmp_path, monkeypatch, "/secure/vault")
    job_id = _add_job()

    _cron(argparse.Namespace(cron_action="remove", job_id=job_id, managed_by=None))

    assert job_id not in _job_ids()
    assert cron_script.peek_grant_epoch(job_id) == 1


def test_the_top_mount_on_the_vault_decides(home, tmp_path, monkeypatch):
    """A mount stacked over an earlier one at the same point is what a lookup
    reaches, so the last line for the vault path is the one read."""
    from kiro_crew import cron_script

    vault = _mountinfo_field(os.path.realpath(config_dir() / ".vault"))
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"30 22 0:50 /secure/vault {vault} rw - ext4 /dev/sdb rw\n"
        f"31 30 0:183 /kirocrew_sb_4242_abc {vault} rw - tmpfs tmpfs rw\n"
    )
    monkeypatch.setattr(cron_script, "_MOUNTINFO_PATH", str(mountinfo))

    with pytest.raises(cron_script.GrantEpochsMasked):
        cron_script.grant_epoch_ids()


def test_a_data_home_reached_through_a_link_is_the_real_vault(tmp_path, monkeypatch):
    """A data home moved to another disk and linked back is a supported layout: the
    link puts the vault on another device than the link itself, and that alone must
    not read as the sandbox's stand-in."""
    from kiro_crew import cron_script

    real = tmp_path / "other-disk" / "crew"
    (real / "crons").mkdir(parents=True)
    (real / ".vault").mkdir()
    link = tmp_path / "home"
    make_dir_link(link, real)
    monkeypatch.setenv("KIROCREW_HOME", str(link))
    empty = tmp_path / "mountinfo"
    empty.write_text("22 1 253:0 / / rw,relatime - ext4 /dev/root rw\n")
    monkeypatch.setattr(cron_script, "_MOUNTINFO_PATH", str(empty))

    with patch("kiro_crew.cli_commands.sel"):
        job_id = _add_job()
        cron_script.commit_grant_epoch(job_id, 2)
        _cron(argparse.Namespace(cron_action="remove", job_id=job_id, managed_by=None))

    assert job_id not in _job_ids()
    assert cron_script.peek_grant_epoch(job_id) == 4


def test_a_seatbelt_refusal_inside_the_sandbox_names_the_routes_that_work(
    home, monkeypatch, capsys
):
    """macOS Seatbelt refuses the vault outright rather than binding a stand-in, so
    inside the sandbox the Full Disk Access advice cannot help; the sandbox line
    names the routes that reach the vault."""
    monkeypatch.setattr(
        "kiro_crew.sandbox_unavailable.agent_confinement_evidence",
        lambda *a, **k: "the macOS kernel reports this process Seatbelt-confined",
    )
    job_id = _add_job()
    _deny_vault(monkeypatch)

    with pytest.raises(SystemExit) as raised:
        _cron(argparse.Namespace(cron_action="remove", job_id=job_id, managed_by=None))

    assert raised.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: this command runs inside the agent sandbox")
    assert "Full Disk Access" not in err
    assert job_id in _job_ids()


def _write_mountinfo(tmp_path, monkeypatch, *lines: str) -> None:
    from kiro_crew import cron_script

    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("".join(f"{line}\n" for line in lines))
    monkeypatch.setattr(cron_script, "_MOUNTINFO_PATH", str(mountinfo))


def _vault_field() -> str:
    return _mountinfo_field(os.path.realpath(config_dir() / ".vault"))


@pytest.mark.parametrize(
    "line",
    [
        "8767 22 253:0 /var/tmp/kirocrew_sb_4242_abc {vault} rw - ext4 /dev/root rw",
        "8767 22 0:183 /kirocrew_sb_4242_stage-x/kirocrew_sb_4242_abc {vault} rw - tmpfs tmpfs rw",
    ],
    ids=["stand-in-on-the-data-home-disk", "stand-in-inside-a-stage"],
)
def test_every_launcher_stand_in_layout_is_masked(home, tmp_path, monkeypatch, line):
    """The launcher may stage the stand-in on the data home's own disk or inside its
    private stage; the source name, not the device, is what marks it."""
    from kiro_crew import cron_script

    _write_mountinfo(tmp_path, monkeypatch, line.format(vault=_vault_field()))

    with pytest.raises(cron_script.GrantEpochsMasked):
        cron_script.grant_epoch_ids()


@pytest.mark.parametrize(
    "root",
    ["/srv/kirocrew_sb_backup/vault", "/srv/kirocrew_sb_0001_x", "/srv/kirocrew_sb_/x"],
    ids=["prefix-only", "zero-padded-pid", "no-pid"],
)
def test_an_operator_directory_sharing_the_prefix_is_the_real_vault(
    home, tmp_path, monkeypatch, root
):
    """Only the launcher's exact name shape marks a stand-in; an operator's directory
    that merely starts with the same prefix is a real vault."""
    from kiro_crew import cron_script

    _mount_over_vault(tmp_path, monkeypatch, root)

    assert cron_script.grant_epoch_ids() == set()


def test_a_stand_in_below_the_vault_does_not_mask_the_vault(home, tmp_path, monkeypatch):
    """Only a mount on the vault path itself replaces it; one on a path inside it
    leaves the vault the real one."""
    from kiro_crew import cron_script

    _write_mountinfo(
        tmp_path,
        monkeypatch,
        f"8767 22 0:183 /kirocrew_sb_4242_abc {_vault_field()}/sub rw - tmpfs tmpfs rw",
    )

    assert cron_script.grant_epoch_ids() == set()


@pytest.mark.parametrize(
    ("platform", "confined", "masked"),
    [
        ("linux", True, True),
        ("linux", False, False),
        ("darwin", True, False),
        ("win32", True, False),
    ],
    ids=["linux-confined", "linux-unconfined", "macos-seatbelt", "windows"],
)
def test_no_mount_list_is_masked_only_when_confined_on_linux(
    home, tmp_path, monkeypatch, platform, confined, masked
):
    """With no mount list to read, a confined Linux process cannot prove its vault is
    real, so it refuses. macOS and Windows have no mount list and no stand-in: the OS
    refuses a masked vault itself, so a profile that allows the vault reaches it."""
    from kiro_crew import cron_script, cron_script_grants

    monkeypatch.setattr(cron_script, "_MOUNTINFO_PATH", str(tmp_path / "absent"))
    monkeypatch.setattr(cron_script_grants.sys, "platform", platform)
    monkeypatch.setattr(
        "kiro_crew.sandbox_unavailable.agent_confinement_evidence",
        lambda *a, **k: "confined" if confined else None,
    )

    if masked:
        with pytest.raises(cron_script.GrantEpochsMasked):
            cron_script.grant_epoch_ids()
    else:
        assert cron_script.grant_epoch_ids() == set()


def test_a_stand_in_under_a_data_home_with_a_space_is_masked(tmp_path, monkeypatch):
    """mountinfo writes a space in a path as ``\\040``; the check decodes it."""
    from kiro_crew import cron_script

    root = tmp_path / "my home"
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    (config_dir() / ".vault").mkdir(parents=True)
    _write_mountinfo(
        tmp_path,
        monkeypatch,
        f"8767 22 0:183 /kirocrew_sb_4242_abc {_vault_field()} rw - tmpfs tmpfs rw",
    )

    with pytest.raises(cron_script.GrantEpochsMasked):
        cron_script.grant_epoch_ids()
