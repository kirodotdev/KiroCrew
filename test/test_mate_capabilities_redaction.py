# SPDX-License-Identifier: Apache-2.0
"""A crewmate's Capabilities view never shows a transport credential.

If the in-place mate path (``mate_capabilities``) returned its rows raw, an
``env``/``headers`` token in ``mates/agents/<id>.json`` would reach the browser
on GET, preview and PUT. Every mate response goes through ``safe_view``, and a
masked leaf sent back is restored from the stored file via ``retain_paths`` (or
refused), never written as the mask.
"""

from __future__ import annotations

import json

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_capabilities
from kiro_crew.agent_capabilities import CapabilityError, CapabilityService

ENV_TOKEN = "env-tok-7f3a9c2e51"
HEADER_TOKEN = "hdr-tok-b84d01e6aa"
REAL_WORKER_SUMMARY = mate_capabilities.worker_summary

SPEC = {
    "name": "qwe",
    "prompt": "hi",
    "tools": ["@remote"],
    "allowedTools": [],
    "mcpServers": {
        "local": {"command": "srv", "args": ["--stdio"], "env": {"API_TOKEN": ENV_TOKEN}},
        "remote": {
            "type": "http",
            "url": "https://mcp.example.test/v1",
            "headers": {"Authorization": HEADER_TOKEN},
        },
    },
}


@pytest.fixture
def mate(monkeypatch, tmp_path):
    kiro = tmp_path / "home"
    (kiro / "agents").mkdir(parents=True)
    mates = kiro / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: kiro / "agents")
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(mate_capabilities, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_capabilities, "binding_is_mate", lambda member: True)
    monkeypatch.setattr(mate_capabilities, "skill_catalog", lambda member, catalog: {})
    monkeypatch.setattr(mate_capabilities, "worker_summary", lambda member, catalog: None)
    path = mates / "qwe.json"
    path.write_text(json.dumps(SPEC), encoding="utf-8")
    return CapabilityService(), path


def _leaks(payload) -> bool:
    text = json.dumps(payload)
    return ENV_TOKEN in text or HEADER_TOKEN in text


def _row(view, key):
    return next(r for r in view["rows"] if r["section"] == "mcpServers" and r["id"] == key)


def _body(revision, operations):
    return {
        "revision": revision,
        "operations": operations,
        "preview_token": revision,
        "accept_parent": [],
        "accept_members": [],
    }


def test_get_masks_env_and_header_tokens(mate):
    service, _ = mate
    view = service.get("qwe")
    assert not _leaks(view)
    assert _row(view, "local")["value"]["env"]["API_TOKEN"] == "[REDACTED]"
    assert _row(view, "remote")["value"]["headers"]["Authorization"] == "[REDACTED]"


def test_preview_and_put_never_return_a_token(mate):
    service, path = mate
    view = service.get("qwe")
    body = _body(
        view["revision"], [{"section": "tools", "id": "fs_read", "action": "set", "value": True}]
    )
    assert not _leaks(service.preview("qwe", body))
    result = service.put("qwe", body)
    assert not _leaks(result)
    assert "fs_read" in json.loads(path.read_text())["tools"]


def test_a_masked_value_sent_back_with_retain_paths_keeps_the_stored_secret(mate):
    service, path = mate
    view = service.get("qwe")
    edited = dict(_row(view, "local")["value"], args=["--stdio", "--verbose"])
    op = {
        "section": "mcpServers",
        "id": "local",
        "action": "set",
        "value": edited,
        "retain_paths": ["/env/API_TOKEN"],
    }
    preview = service.preview("qwe", _body(view["revision"], [op]))
    assert not _leaks(preview)
    result = service.put("qwe", _body(view["revision"], [op]))
    assert not _leaks(result)
    stored = json.loads(path.read_text())["mcpServers"]["local"]
    assert stored["env"]["API_TOKEN"] == ENV_TOKEN
    assert stored["args"] == ["--stdio", "--verbose"]


def test_a_mask_without_retain_paths_is_refused_and_the_file_is_unchanged(mate):
    service, path = mate
    before = path.read_text()
    view = service.get("qwe")
    op = {
        "section": "mcpServers",
        "id": "remote",
        "action": "set",
        "value": _row(view, "remote")["value"],
    }
    for call in (service.preview, service.put):
        with pytest.raises(CapabilityError) as exc:
            call("qwe", _body(view["revision"], [op]))
        assert exc.value.code == "redacted_value_not_writable"
    assert path.read_text() == before


def test_a_retain_path_onto_an_unmasked_leaf_is_refused(mate):
    service, _ = mate
    view = service.get("qwe")
    op = {
        "section": "mcpServers",
        "id": "local",
        "action": "set",
        "value": {"command": "srv", "args": ["--stdio"], "env": {"API_TOKEN": "typed-new"}},
        "retain_paths": ["/env/API_TOKEN"],
    }
    with pytest.raises(CapabilityError) as exc:
        service.put("qwe", _body(view["revision"], [op]))
    assert exc.value.code == "invalid_retain_path"


def test_the_workers_server_rows_are_masked_too(mate, monkeypatch):
    from kiro_crew import mate_worker

    service, _ = mate
    monkeypatch.setattr(mate_capabilities, "worker_summary", REAL_WORKER_SUMMARY)
    monkeypatch.setattr(mate_worker, "member_worker_spec", lambda member: SPEC)
    view = service.get("qwe")
    assert view["worker"]["rows"]
    assert not _leaks(view)
