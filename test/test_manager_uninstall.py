"""The uninstall owner's early refusals and its readers of the base ``config.json`` grant.

``uninstall_app`` refuses an unsafe name or an app with no record before it touches
anything, and the ``agent.apps_trusted`` helpers it uses read the BASE config document
only, answering "no grant" for every shape that cannot hold one: a missing file, a file
that does not parse, a document or ``agent`` section that is not an object, and a value
of the wrong type. ``trust_grant_removal_blocked`` names the one case a write could not
fix (an overlay pinning this app's grant) and an unreadable base file, and ignores an
unreadable overlay, as the loader does.

Each test patches the config paths through ``kiro_crew.apps.manager``, the import path
callers use, so it also exercises that a facade patch reaches the owner's code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps import manager


@pytest.fixture()
def config_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point the base config and its overlay at files under *tmp_path*."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    base = tmp_path / "config.json"
    local = tmp_path / "config.local.json"
    monkeypatch.setattr(manager, "config_path", lambda: base)
    monkeypatch.setattr(manager, "config_local_path", lambda: local)
    return base, local


def _write(path: Path, value: object) -> None:
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def test_uninstall_refuses_an_unsafe_name(config_files: tuple[Path, Path]) -> None:
    result = manager.uninstall_app("../escape")
    assert result.ok is False
    assert result.error == "unsafe app name: '../escape'"


def test_uninstall_refuses_an_app_with_no_record(config_files: tuple[Path, Path]) -> None:
    result = manager.uninstall_app("never-installed")
    assert result.ok is False
    assert result.error == "app 'never-installed' is not installed"


@pytest.mark.parametrize(
    "document",
    [
        "{not json",
        ["a", "list"],
        {"agent": "not an object"},
        {"agent": {"apps_trusted": "my-app"}},
        {"agent": {"apps_trusted": ["other-app"]}},
    ],
    ids=["unparseable", "not-an-object", "agent-not-an-object", "not-a-list", "absent"],
)
def test_a_document_without_this_grant_reads_as_no_grant(
    config_files: tuple[Path, Path], document: object
) -> None:
    base, _local = config_files
    _write(base, document)
    assert manager._has_trust_grant("my-app") is False
    assert manager._trust_grant_local("my-app") is False
    assert manager._trust_grant_repository("my-app") == ""


def test_a_missing_base_file_reads_as_no_grant(config_files: tuple[Path, Path]) -> None:
    assert manager._has_trust_grant("my-app") is False
    assert manager._trust_grant_local("my-app") is False
    assert manager._trust_grant_repository("my-app") == ""


def test_each_grant_reader_answers_from_the_base_document(
    config_files: tuple[Path, Path],
) -> None:
    base, _local = config_files
    _write(
        base,
        {
            "agent": {
                "apps_trusted": ["my-app"],
                "apps_trusted_local": ["my-app"],
                "apps_trusted_repositories": {"my-app": "example/repo", "odd": 7},
            }
        },
    )
    assert manager._has_trust_grant("my-app") is True
    assert manager._trust_grant_local("my-app") is True
    assert manager._trust_grant_repository("my-app") == "example/repo"
    assert manager._trust_grant_repository("odd") == ""


def test_an_unreadable_overlay_does_not_block_a_removal(config_files: tuple[Path, Path]) -> None:
    base, local = config_files
    _write(local, "{not json")
    _write(base, {"agent": {"apps_trusted": ["my-app"]}})
    assert manager.trust_grant_removal_blocked("my-app") is None


def test_an_overlay_pinning_this_grant_blocks_a_removal(config_files: tuple[Path, Path]) -> None:
    _base, local = config_files
    _write(local, {"agent": {"apps_trusted": ["my-app"]}})
    assert manager.trust_grant_removal_blocked("my-app") == (
        f"apps_trusted is set in {local}, which overrides config.json"
    )
    assert manager.trust_grant_removal_blocked("other-app") is None


def test_an_unreadable_base_file_blocks_a_removal_and_the_drop(
    config_files: tuple[Path, Path],
) -> None:
    base, _local = config_files
    _write(base, "{not json")
    blocked = manager.trust_grant_removal_blocked("my-app")
    assert blocked is not None and blocked.startswith(f"{base} is unreadable: ")
    with pytest.raises(RuntimeError, match="is unreadable"):
        manager._drop_trust_grant("my-app")


@pytest.mark.parametrize(
    "document",
    [["a", "list"], {"agent": "not an object"}, {"other": 1}],
    ids=["not-an-object", "agent-not-an-object", "no-agent-section"],
)
def test_dropping_from_a_document_without_a_grant_writes_nothing(
    config_files: tuple[Path, Path], document: object
) -> None:
    base, _local = config_files
    _write(base, document)
    before = base.read_bytes()
    manager._drop_trust_grant("my-app")
    assert base.read_bytes() == before


def test_dropping_with_no_base_file_creates_none(config_files: tuple[Path, Path]) -> None:
    base, _local = config_files
    manager._drop_trust_grant("my-app")
    assert not base.exists()
