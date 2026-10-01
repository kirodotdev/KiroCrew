"""The runtime config is sealed read-only for every sandboxed process.

``config.json`` and ``config.local.json`` carry the switches that loosen confinement
(``agent.sandbox``, ``agent.apps_allow_third_party``, ...). The file-edit tool fence
never sees a spawned shell's ``open()``, so the OS disposition is the load-bearing half:
without it an in-sandbox shell could write ``agent.sandbox: "off"`` and run its next
spawn unconfined. Both files are sealed because the loader merges the overlay over the
base with the overlay winning.
"""

from __future__ import annotations

import argparse
import errno
import json
from unittest.mock import patch

import pytest

from kiro_crew import sandbox

_CONFIG_LEAVES = ("config.json", "config.local.json")


@pytest.mark.parametrize("leaf", _CONFIG_LEAVES)
def test_config_leaf_is_read_only_in_every_mode(leaf):
    assert leaf in sandbox._CREW_READONLY_LEAVES
    assert leaf not in sandbox._CREW_HIDDEN_LEAVES
    assert leaf not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
    for prefix in (".kiro/crew", ".kirocrew"):
        assert f"{prefix}/{leaf}" in sandbox._CREW_READONLY_TARGETS


@pytest.mark.parametrize("leaf", _CONFIG_LEAVES)
def test_config_leaf_is_precreated_so_the_linux_bind_has_a_file(leaf):
    # An absent overlay is the default state, and an absent name is exactly the one an
    # agent would create to win the merge.
    assert leaf in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES


def _set_args(*, local: bool = True):
    return argparse.Namespace(
        config_action="set", key="agent.sandbox", value="off", file=None, local=local
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A data home with the two config paths pointed at it, and NO sandbox marker.

    ``cli.main()`` pops ``KIROCREW_SANDBOX_ACTIVE`` before dispatch, so the marker is
    never in the environment when ``kirocrew config`` runs -- inside the sandbox or
    out. The hint has to be decided from the failure itself; a test that set the
    marker would pass against a hint that never fires in production.
    """
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    d = tmp_path / "crew"
    d.mkdir()
    (d / "config.json").write_text(
        json.dumps({"session": {"autocompact_pct": 90.0}}), encoding="utf-8"
    )
    with (
        patch("kiro_crew.cli_config.config_path", return_value=d / "config.json"),
        patch("kiro_crew.cli_config.config_local_path", return_value=d / "config.local.json"),
        patch("kiro_crew.config.loader.config_path", return_value=d / "config.json"),
        patch("kiro_crew.config.loader.config_dir", return_value=d),
        patch("kiro_crew.cli_config.sel"),
    ):
        yield d


def _assert_hint(err: str) -> None:
    assert "config.local.json are read-only" in err
    assert "dashboard" in err


@pytest.mark.parametrize("code", [errno.EPERM, errno.EACCES, errno.EBUSY, errno.EROFS])
@pytest.mark.parametrize("leaf", _CONFIG_LEAVES)
def test_sandboxed_config_set_reports_the_seal_on_an_in_place_write(home, capsys, code, leaf):
    """``open(path, "w")`` refused by the seal names the sealed file as ``filename``."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(code, "denied", str(home / leaf)),
    ):
        with pytest.raises(SystemExit) as exc:
            _config_cmd(_set_args(local=leaf == "config.local.json"))
    assert exc.value.code == 1
    _assert_hint(capsys.readouterr().err)


@pytest.mark.parametrize("code", [errno.EPERM, errno.EBUSY])
def test_sandboxed_config_set_reports_the_seal_on_the_publishing_rename(home, capsys, code):
    """``os.replace(tmp, path)`` names the temp as ``filename`` and the seal as ``filename2``.

    That is the shape ``atomic_write`` fails with: the temp lands (the data-home root is
    writable), and the rename over the sealed leaf is what the OS refuses -- ``EPERM``
    from Seatbelt, ``EBUSY`` from a Linux bind mount.
    """
    from kiro_crew.cli_config import _config_cmd

    tmp = home / "tmpa1b2c3.tmp"
    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(code, "denied", str(tmp), None, str(home / "config.local.json")),
    ):
        with pytest.raises(SystemExit) as exc:
            _config_cmd(_set_args())
    assert exc.value.code == 1
    _assert_hint(capsys.readouterr().err)


def test_a_denial_against_an_unrelated_file_is_not_relabelled(home):
    """Errno alone is not the seal: the same errno against another path is re-raised."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=PermissionError(errno.EPERM, "denied", str(home / "other.json")),
    ):
        with pytest.raises(PermissionError):
            _config_cmd(_set_args())


def test_a_denial_with_no_filename_is_not_relabelled(home):
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=PermissionError(errno.EPERM, "denied"),
    ):
        with pytest.raises(PermissionError):
            _config_cmd(_set_args())


def test_a_non_denial_errno_against_the_config_is_not_relabelled(home):
    """A full data home names the same file but is not the seal."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(errno.ENOSPC, "full", str(home / "config.local.json")),
    ):
        with pytest.raises(OSError) as exc:
            _config_cmd(_set_args())
    assert exc.value.errno == errno.ENOSPC


def test_config_edit_editor_exec_denial_is_not_relabelled(home):
    """``config edit`` failing to exec the editor is an editor problem, not the seal."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.os.execvp",
        side_effect=PermissionError(errno.EACCES, "denied", "vi"),
    ):
        with pytest.raises(PermissionError):
            _config_cmd(argparse.Namespace(config_action="edit"))


def test_sandboxed_config_defaults_adopt_reports_the_seal(home, capsys):
    """``config defaults --adopt`` writes ``config.json`` through its own error path."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(errno.EPERM, "denied", str(home / "config.json")),
    ):
        with pytest.raises(SystemExit) as exc:
            _config_cmd(
                argparse.Namespace(config_action="defaults", keys=[], adopt=True, keep=False)
            )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    _assert_hint(err)
    assert "Could not write" not in err


def test_config_defaults_adopt_keeps_its_own_message_for_other_failures(home, capsys):
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(errno.ENOSPC, "full", str(home / "config.json")),
    ):
        with pytest.raises(SystemExit) as exc:
            _config_cmd(
                argparse.Namespace(config_action="defaults", keys=[], adopt=True, keep=False)
            )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Could not write" in err
    assert "config.local.json are read-only" not in err
