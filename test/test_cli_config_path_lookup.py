"""``cli_config`` reads the config paths from the loader at every call.

A module-level ``from loader import config_path`` would keep whatever object the loader
held when ``cli_config`` was first imported. A test that monkeypatched the loader and
then imported ``cli_config`` first would leave every later ``config set`` in the process
writing to its temp file, where the pending-save refusal never fires.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import cli_config
from kiro_crew.config import loader


def test_cli_config_follows_the_loader_and_its_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_base, real_local = loader.config_path(), loader.config_local_path()
    with monkeypatch.context() as mp:
        mp.setattr(loader, "config_path", lambda: tmp_path / "config.json")
        mp.setattr(loader, "config_local_path", lambda: tmp_path / "config.local.json")

        assert cli_config.config_path() == tmp_path / "config.json"
        assert cli_config.config_local_path() == tmp_path / "config.local.json"

    assert cli_config.config_path() == real_base
    assert cli_config.config_local_path() == real_local
