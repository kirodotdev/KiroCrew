"""The ``Host crew-sbx-*`` block writer: idempotent, own marked block, other bytes untouched."""

from __future__ import annotations

import os
import stat

import pytest
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401

from kiro_crew.managed_env import ssh_config as sc

PY = "/opt/py thon/bin/python3"  # a space: the ProxyCommand must quote it

FOREIGN = (
    "Host github.com\n  User git\n\n"
    "# my own crew-sbx block, hand written\nHost crew-sbx-*\n  User mine\n  ProxyCommand /bin/true\n"
)


def test_new_file_gets_exactly_the_block(tmp_path):
    p = tmp_path / "ssh" / "config"
    assert sc.ensure_block(p, PY) is True
    text = p.read_text()
    assert text == sc.render_block(PY)
    assert text.startswith(sc.BEGIN) and text.endswith(sc.END + "\n")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    for line in (
        "User root",
        "StrictHostKeyChecking no",
        "UserKnownHostsFile /dev/null",
        "ForwardAgent no",
        "ForwardX11 no",
        "GatewayPorts no",
        "Host crew-sbx-*",
    ):
        assert line in text
    assert "RemoteForward" not in text and "IdentityFile" not in text
    assert f"ProxyCommand '{PY}' -m kiro_crew.managed_env.mint_and_attach %n" in text


def test_second_run_changes_nothing(tmp_path):
    p = tmp_path / "config"
    p.write_text(FOREIGN)
    assert sc.ensure_block(p, PY) is True
    first = p.read_bytes()
    st1 = p.stat().st_mtime_ns
    assert sc.ensure_block(p, PY) is False
    assert p.read_bytes() == first and p.stat().st_mtime_ns == st1
    assert first.count(sc.BEGIN.encode()) == 1


def test_a_foreign_block_stays_byte_identical(tmp_path):
    p = tmp_path / "config"
    p.write_text(FOREIGN)
    sc.ensure_block(p, PY)
    text = p.read_text()
    assert text.startswith(FOREIGN)  # nothing before or inside the foreign text changed
    assert text[len(FOREIGN) :].lstrip("\n") == sc.render_block(PY)


@pytest.mark.parametrize("tail", ["", "\n", "\n\n"])
def test_other_text_around_the_block_is_preserved_on_update(tmp_path, tail):
    p = tmp_path / "config"
    p.write_text("Host a\n  User a\n")
    sc.ensure_block(p, "/old/python3")
    p.write_text(p.read_text() + "\nHost z\n  User z" + tail)
    before_tail = p.read_text().split(sc.END + "\n", 1)[1]
    assert sc.ensure_block(p, PY) is True
    text = p.read_text()
    assert text.startswith("Host a\n  User a\n")
    assert text.endswith(before_tail) and "/old/python3" not in text and PY in text


@pytest.mark.parametrize(
    "damaged",
    [
        sc.BEGIN + "\nHost x\n",
        "Host x\n" + sc.END + "\n",
        sc.END + "\n" + sc.BEGIN + "\n",
        sc.render_block(PY) + sc.render_block(PY),
    ],
)
def test_damaged_markers_are_refused_and_nothing_is_written(tmp_path, damaged):
    p = tmp_path / "config"
    p.write_text(damaged)
    with pytest.raises(sc.SshConfigError):
        sc.ensure_block(p, PY)
    assert p.read_text() == damaged


def test_existing_file_mode_is_kept_and_symlink_target_is_written(tmp_path):
    real = tmp_path / "real"
    real.write_text(FOREIGN)
    real.chmod(0o640)
    link = tmp_path / "config"
    os.symlink(real, link)
    sc.ensure_block(link, PY)
    assert link.is_symlink() and sc.BEGIN in real.read_text()
    assert stat.S_IMODE(real.stat().st_mode) == 0o640


@pytest.mark.parametrize("bad", ["", "python3", "/x/py\nHost evil", "/x\0"])
def test_python_path_must_be_absolute_and_one_line(tmp_path, bad):
    with pytest.raises(sc.SshConfigError):
        sc.ensure_block(tmp_path / "config", bad)
    assert not (tmp_path / "config").exists()


def test_no_temp_file_is_left_behind(tmp_path):
    p = tmp_path / "config"
    sc.ensure_block(p, PY)
    assert [n.name for n in tmp_path.iterdir()] == ["config"]
