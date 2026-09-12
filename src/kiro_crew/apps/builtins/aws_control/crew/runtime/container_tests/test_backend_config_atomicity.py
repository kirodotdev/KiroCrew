"""The container's config must never be read half-written.

`config.json` is written by the supervisor and read by the gateway at the NEXT
start, which is what makes a partial write the interesting failure: nothing reads
the file during the write, so an interrupted one is a defect that surfaces after
the run that caused it is gone, on a container that boots on a config it cannot
parse.

So the write is a sibling temp in the same directory, fsynced, then one atomic
replace -- and a symlink at the destination is refused rather than consumed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from container.common import ConfigError
from container.supervisor import backend as backend_mod

from ._settings_helper import make_settings


def _config_path(settings) -> Path:
    return settings.config_dir / "config.json"


def test_an_interrupted_write_leaves_the_previous_config_intact(
    tmp_path: Path, monkeypatch
) -> None:
    """The destination is either the old file or the new one, never a prefix of the new.

    The failure is injected at the last possible moment -- after the temp is written
    and fsynced, at the replace itself -- because that is the window a
    write-in-place implementation cannot survive.
    """
    settings = make_settings(tmp_path)
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    path = _config_path(settings)
    before = json.dumps({"agent": {"default_agent": "previous"}}) + "\n"
    path.write_text(before, encoding="utf-8")

    def _boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backend_mod.os, "replace", _boom)

    with pytest.raises(ConfigError, match="publish"):
        backend_mod.write_backend_config(settings)

    assert path.read_text(encoding="utf-8") == before, "the previous config was damaged"
    assert json.loads(path.read_text(encoding="utf-8")), "and it still parses"


def test_an_interrupted_write_leaves_no_temporary_behind(tmp_path: Path, monkeypatch) -> None:
    """A failed write must not accumulate files in the data home.

    A leftover temp is not a security problem, but it is how a directory silently
    fills over a task's lifetime, and it makes the next failure harder to read.
    """
    settings = make_settings(tmp_path)
    settings.config_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        backend_mod.os, "replace", lambda src, dst: (_ for _ in ()).throw(OSError(5, "I/O error"))
    )
    with pytest.raises(ConfigError):
        backend_mod.write_backend_config(settings)

    leftovers = [p.name for p in settings.config_dir.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, leftovers


def test_the_write_publishes_a_complete_document(tmp_path: Path) -> None:
    """The success path, asserted on the bytes rather than on the call succeeding."""
    settings = make_settings(tmp_path)
    path = backend_mod.write_backend_config(settings)

    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    config = json.loads(text)
    assert config["agent"]["sandbox"] == "auto"
    assert config["slack"]["enabled"] is False
    leftovers = [p.name for p in settings.config_dir.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, leftovers


def test_a_symlink_at_the_destination_is_refused(tmp_path: Path) -> None:
    """A link there means something else chose the path, so the container stops.

    ``os.replace`` would not write through the link -- ``rename`` unlinks it rather
    than following it -- so this is not about preventing a write to the target. It is
    about not consuming a planted link silently.
    """
    settings = make_settings(tmp_path)
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim.json"
    victim.write_text("{}", encoding="utf-8")
    _config_path(settings).symlink_to(victim)

    with pytest.raises(ConfigError, match="symlink"):
        backend_mod.write_backend_config(settings)

    assert victim.read_text(encoding="utf-8") == "{}"
    assert _config_path(settings).is_symlink(), "the link is left for a human to look at"


def test_a_pre_planted_temp_file_is_an_error_not_a_target(tmp_path: Path, monkeypatch) -> None:
    """The temp is opened ``O_EXCL``, so a file already at that name is refused.

    Without it the write would happily use whatever is at the temp path, which is a
    second way into the same directory.
    """
    settings = make_settings(tmp_path)
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    planted = settings.config_dir / f"config.json.{99999}.tmp"
    planted.write_text("planted", encoding="utf-8")
    monkeypatch.setattr(backend_mod.os, "getpid", lambda: 99999)

    with pytest.raises(ConfigError, match="temporary file"):
        backend_mod.write_backend_config(settings)

    assert planted.read_text(encoding="utf-8") == "planted"
