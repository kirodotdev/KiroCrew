"""A projection rewrite built from a ``cli.json`` that changed since its read is not published.

The settings lock holds back Kiro Crew's own writers, not an editor. A save that
lands while the projection publishes its aliases would be overwritten by the
stale document, and the rewrite would restamp Kiro Crew's effort ownership record
back to valid, so a later automatic clear would delete the level the operator kept.
"""

from __future__ import annotations

import json

import native_skill_projection_helpers
import pytest

from kiro_crew.acp import skill_projection as projection
from kiro_crew.providers.acp import _clear_cli_overlay_effort, _write_cli_overlay

native_tree = native_skill_projection_helpers.native_tree

_MODEL = "claude-opus-4.7"


def test_an_operator_save_during_alias_publication_is_kept_and_stays_unowned(
    native_tree, monkeypatch
):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    _write_cli_overlay(project, _MODEL, "max")
    cli_json = project / ".kiro" / "settings" / "cli.json"
    operator_save = json.dumps(json.loads(cli_json.read_text(encoding="utf-8"))).encode()
    real_announce = projection._announce_publication
    saves = []

    def _operator_saves_while_aliases_publish(path, raw):
        if not saves:
            cli_json.write_bytes(operator_save)
            saves.append(path)
        return real_announce(path, raw)

    monkeypatch.setattr(projection, "_announce_publication", _operator_saves_while_aliases_publish)

    assert projection.prepare_native_skill_projection(project) is None

    assert saves, "the alias publication never ran, so the race was not exercised"
    assert cli_json.read_bytes() == operator_save
    assert _clear_cli_overlay_effort(project, None, owned_only=True) is True
    data = json.loads(cli_json.read_text(encoding="utf-8"))
    assert data["chat.modelDefaults"][_MODEL]["output_config"]["effort"] == "max"


@pytest.mark.parametrize("pinned_walk", [True, False], ids=["pinned", "by-name"])
def test_a_save_right_after_the_read_is_kept_on_either_walk(native_tree, monkeypatch, pinned_walk):
    from kiro_crew import pinned_fs

    if pinned_walk and not pinned_fs.supports_pinned_walk():
        pytest.skip("no descriptor-relative walk on this host")
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: pinned_walk)
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    _write_cli_overlay(project, _MODEL, "max")
    cli_json = project / ".kiro" / "settings" / "cli.json"
    operator_save = json.dumps(json.loads(cli_json.read_text(encoding="utf-8"))).encode()
    reader = "read_pinned_cli_json" if pinned_walk else "read_named_cli_json"
    real_read = getattr(projection, reader)
    saves = []

    def _operator_saves_once_the_read_returns(*args, **kwargs):
        result = real_read(*args, **kwargs)
        if not saves and result is not None:
            cli_json.write_bytes(operator_save)
            saves.append(reader)
        return result

    monkeypatch.setattr(projection, reader, _operator_saves_once_the_read_returns)

    assert projection.prepare_native_skill_projection(project) is None

    assert saves, "the locked read never ran, so the race was not exercised"
    assert cli_json.read_bytes() == operator_save
    assert _clear_cli_overlay_effort(project, None, owned_only=True) is True
    data = json.loads(cli_json.read_text(encoding="utf-8"))
    assert data["chat.modelDefaults"][_MODEL]["output_config"]["effort"] == "max"
