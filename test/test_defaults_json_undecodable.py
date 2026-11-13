"""A ``defaults.json`` whose bytes are not UTF-8 reads like one that does not parse.

Both readers already skip a ``defaults.json`` that is bad JSON or unreadable and
carry on with what is left. A file that does not decode as UTF-8 is the same
unusable file, so each gives that same answer instead of raising
``UnicodeDecodeError`` out of MCP discovery or model resolution:

* ``mcp_discovery_config._load_agent_config`` skips the project-dir and the
  bundled ``defaults.json``;
* ``KiroCrewConfig._resolve_agent_model`` falls back to the default model.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest

from kiro_crew import mcp_discovery_config
from kiro_crew.config import loader
from kiro_crew.config.loader_config import KiroCrewConfig
from kiro_crew.config.sections import DEFAULT_MODEL

T = TypeVar("T")

#: Not UTF-8: 0xFF never starts a UTF-8 sequence.
UNDECODABLE = b'\xff{"model": "x", "mcpServers": {}}'


def _read(call: Callable[[], T]) -> T:
    """*call*'s answer, failing the test (not erroring it) when it lets the decode out."""
    try:
        return call()
    except UnicodeDecodeError as exc:
        pytest.fail(f"the reader let an undecodable defaults.json raise: {exc}")


def _undecodable_defaults(directory: Path) -> Path:
    path = directory / "defaults.json"
    path.write_bytes(UNDECODABLE)
    return path


def test_project_dir_defaults_that_do_not_decode_are_skipped(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "agents").mkdir(parents=True)
    _undecodable_defaults(project / "agents")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(project))
    empty_home = tmp_path / "home"
    (empty_home / ".kiro" / "agents").mkdir(parents=True)

    loaded = _read(lambda: mcp_discovery_config._load_agent_config(user_home=empty_home))

    assert isinstance(loaded, dict)


def test_bundled_defaults_that_do_not_decode_are_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    bundled = Path(mcp_discovery_config.__file__).resolve().parent / "config" / "defaults.json"
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        # The bundled file ships with the package, so its bytes are swapped at
        # the read rather than on disk.
        if self == bundled:
            return UNDECODABLE.decode("utf-8")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    empty_home = tmp_path / "home"
    (empty_home / ".kiro" / "agents").mkdir(parents=True)

    loaded = _read(lambda: mcp_discovery_config._load_agent_config(user_home=empty_home))

    assert isinstance(loaded, dict)


def test_model_resolution_skips_bundled_defaults_that_do_not_decode(tmp_path, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    _undecodable_defaults(package)
    agents = tmp_path / "agents"
    agents.mkdir()
    # Through the facade: the name is shared across the loader family.
    monkeypatch.setattr(loader, "config_package_dir", lambda: package)
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: agents)

    assert _read(KiroCrewConfig._resolve_agent_model) == DEFAULT_MODEL


@pytest.mark.parametrize("raw", [b"{not json", b"{}"])
def test_model_resolution_answers_the_same_for_bad_json(tmp_path, monkeypatch, raw):
    """The answer the undecodable case is held to: the reader's existing fallback."""
    package = tmp_path / "package"
    package.mkdir()
    (package / "defaults.json").write_bytes(raw)
    agents = tmp_path / "agents"
    agents.mkdir()
    # Through the facade: the name is shared across the loader family.
    monkeypatch.setattr(loader, "config_package_dir", lambda: package)
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: agents)

    assert KiroCrewConfig._resolve_agent_model() == DEFAULT_MODEL
