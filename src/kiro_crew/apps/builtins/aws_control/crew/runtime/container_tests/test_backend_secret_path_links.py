"""A link planted where the boot secret goes must stop the task, not be followed.

The gateway writes ``run/gateway-<port>.secret`` itself, with an ordinary open that
follows a symlink, and it truncates that file on every start. The crew's model
worker can write inside the data home, and what it writes is driven by prompt
content, so the path is reachable by an untrusted caller. A link there would turn
the gateway's own secret write into a truncating write to the link's target.

Nothing in this container can make the gateway's writer link-safe, so the
supervisor refuses instead: a real directory and a real file path, or no task.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from container import common
from container.common import ConfigError
from container.supervisor import backend as backend_mod

from ._settings_helper import make_settings


def test_a_symlinked_run_directory_refuses_to_start(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    run_dir = settings.backend_run_dir
    if run_dir.exists():
        run_dir.rmdir()
    run_dir.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(ConfigError, match="symlink"):
        backend_mod.start_backend(settings, argv=["/bin/true"])


def test_a_symlinked_secret_path_refuses_to_start(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.backend_run_dir.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim.json"
    victim.write_text("do not truncate me", encoding="utf-8")
    common.secret_path(settings.backend_run_dir, settings.backend_port).symlink_to(victim)

    with pytest.raises(ConfigError, match="symlink"):
        backend_mod.start_backend(settings, argv=["/bin/true"])

    assert victim.read_text(encoding="utf-8") == "do not truncate me"


def test_a_directory_where_the_secret_goes_refuses_to_start(tmp_path: Path) -> None:
    """Not a link, but the same class: the gateway expects to write a file there."""
    settings = make_settings(tmp_path)
    settings.backend_run_dir.mkdir(parents=True, exist_ok=True)
    common.secret_path(settings.backend_run_dir, settings.backend_port).mkdir()

    with pytest.raises(ConfigError, match="not a regular file"):
        backend_mod.start_backend(settings, argv=["/bin/true"])


def test_a_clean_run_directory_starts(tmp_path: Path) -> None:
    """Non-vacuity: the guard must not refuse the ordinary case.

    A guard that refused everything would make the three tests above pass while
    saying nothing about links.
    """
    settings = make_settings(tmp_path)
    group = backend_mod.start_backend(settings, argv=["/bin/true"])
    try:
        assert settings.backend_run_dir.is_dir()
        assert not settings.backend_run_dir.is_symlink()
    finally:
        group.terminate(2.0)


def test_an_existing_secret_file_is_left_for_the_gateway_to_rewrite(tmp_path: Path) -> None:
    """A real file is not the attack, and removing it would break the ordinary restart.

    The gateway truncates and rewrites this path itself; the guard's job is to be sure
    the path it rewrites is the path, not a link to somewhere else.
    """
    settings = make_settings(tmp_path)
    settings.backend_run_dir.mkdir(parents=True, exist_ok=True)
    secret = common.secret_path(settings.backend_run_dir, settings.backend_port)
    secret.write_text("stale", encoding="utf-8")

    group = backend_mod.start_backend(settings, argv=["/bin/true"])
    try:
        assert secret.read_text(encoding="utf-8") == "stale"
    finally:
        group.terminate(2.0)
