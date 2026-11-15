"""Turning Task Runner off in a test's own data home (``taskrunner.enabled``)."""

from __future__ import annotations

import json

from kiro_crew.config.loader import config_path


def turn_task_runner_off() -> None:
    """Write ``taskrunner.enabled: false`` to the config.json of this test's data home."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"taskrunner": {"enabled": False}}), encoding="utf-8")
