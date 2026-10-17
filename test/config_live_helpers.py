"""The temp ``config.json`` fixture the ``test_config_live*`` modules run against.

Every module there reloads, writes or watches one config file; ``cfg_file`` points the loader
at a fresh one under ``tmp_path`` and resets the process watcher on both sides of the test.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from _hot_reload_helpers import write_config as _write

from kiro_crew.config import live

# ── helpers ───────────────────────────────────────────────────────────────


@pytest.fixture(name="cfg_file")
def cfg_file_fixture(tmp_path: Path):
    live.reset_for_tests()
    cfg = tmp_path / "config.json"
    _write(cfg, {"agent": {"model": "model-a", "log_level": "INFO"}})
    with (
        patch("kiro_crew.config.loader.config_path", return_value=cfg),
        patch("kiro_crew.config.loader.config_local_path", return_value=tmp_path / "local.json"),
    ):
        yield cfg
    live.reset_for_tests()
