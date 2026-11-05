"""The Claude Code backend's ``[Steering resources]`` load honours inclusion.

``_load_steering_resources`` is the gateway's own read of the steering the
Claude Code backend never loads itself. It applies the same inclusion rule as
the folder-steering collection (``folder_steering.every_session_body``): a
``manual``, ``auto`` or ``fileMatch`` document is withheld, and the front
matter is stripped from every document that is delivered.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.agent_files import AGENT_FILENAME


@pytest.fixture
def steering_home(tmp_path, monkeypatch):
    """A home whose agents dir holds a ``kirocrew`` spec globbing ``steer/*.md``.

    ``Path.home`` is redirected rather than ``HOME``: Windows ``expanduser``
    reads ``USERPROFILE``.
    """
    from kiro_crew.config import paths as config_paths

    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / AGENT_FILENAME).write_text(
        json.dumps({"name": "kirocrew", "resources": ["file://steer/*.md"]}), encoding="utf-8"
    )
    monkeypatch.setattr(config_paths, "_agents_dir_override", lambda: agents)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    steer = tmp_path / "steer"
    steer.mkdir()
    return steer


def _write(steer: Path, name: str, text: str) -> None:
    (steer / name).write_bytes(text.encode("utf-8"))


def _load() -> str:
    from kiro_crew.context import _load_steering_resources

    return _load_steering_resources()


@pytest.mark.parametrize("mode", ["manual", "auto", "fileMatch", "FILEMATCH", '"manual"'])
def test_a_document_not_for_every_session_is_withheld(steering_home, mode):
    _write(steering_home, "a.md", "ALWAYS-RULE")
    _write(steering_home, "b.md", f"---\ninclusion: {mode}\n---\nWITHHELD-RULE\n")

    loaded = _load()

    assert "ALWAYS-RULE" in loaded
    assert "WITHHELD-RULE" not in loaded


def test_an_always_document_is_delivered_without_its_front_matter(steering_home):
    _write(steering_home, "a.md", "---\ninclusion: always\nname: conv\n---\nALWAYS-RULE\n")

    loaded = _load()

    assert "ALWAYS-RULE" in loaded
    assert "---" not in loaded
    assert "inclusion" not in loaded
    assert "name: conv" not in loaded


def test_crlf_and_a_byte_order_mark_do_not_hide_the_fence(steering_home):
    _write(steering_home, "a.md", "ALWAYS-RULE")
    _write(steering_home, "b.md", "﻿---\r\ninclusion: manual\r\n---\r\nWITHHELD-RULE\r\n")
    _write(steering_home, "c.md", "﻿---\r\ninclusion: always\r\n---\r\nKEPT-RULE\r\n")

    loaded = _load()

    assert "WITHHELD-RULE" not in loaded
    assert "KEPT-RULE" in loaded
    assert "---" not in loaded


def test_a_document_without_front_matter_is_delivered_verbatim(steering_home):
    _write(steering_home, "a.md", "# Rules\n\nPLAIN-RULE\n")

    assert _load() == "# Rules\n\nPLAIN-RULE\n"


def test_an_all_withheld_root_loads_nothing(steering_home):
    _write(steering_home, "a.md", "---\ninclusion: manual\n---\nWITHHELD-RULE\n")

    assert _load() == ""
