"""The script-hook routes in ``handlers/hooks_scripts.py``, driven on a real store.

Each route is called through the hooks facade, the way the route tables reach it, with a
``ScriptHookStore`` rooted in ``tmp_path``. A create, an update, a delete and a toggle each
persist and answer the stored hook; each refusal (a body that is not JSON, one the schema
or the store's invariants reject, an unknown id, a store that cannot be written) answers
its own coded status and leaves the store as it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from webhooks_api_helpers import wired_fixture  # noqa: F401
from webhooks_api_helpers import _payload, _req

from kiro_crew import hooks as hooks_mod
from kiro_crew.dashboard.handlers import hooks as H
from kiro_crew.hooks import ScriptHookStore

pytestmark = pytest.mark.asyncio


def _valid(**overrides: object) -> dict:
    body: dict = {"name": "fmt", "event": "Stop", "command": "true"}
    body.update(overrides)
    return body


@pytest.fixture
def store(wired: Path, monkeypatch: pytest.MonkeyPatch) -> ScriptHookStore:
    """A real store in the test's own directory, handed to every route."""
    real = ScriptHookStore(wired)
    monkeypatch.setattr(H, "_get_hook_store", lambda _state: real)
    return real


def _on_disk(path: Path) -> list[dict]:
    return json.loads((path / "hooks.json").read_text(encoding="utf-8"))["hooks"]


class _Refusing:
    """A store whose every write fails the way an unwritable ``hooks.json`` does."""

    def get(self, _hook_id: str) -> None:
        return None

    def update(self, *_args: object) -> None:
        raise OSError("read-only file system")

    delete = toggle = update


class _Invalid:
    """A store whose invariants refuse what the request schema let through."""

    def create(self, _data: dict) -> None:
        raise ValueError("a hook needs a command or skills")

    def update(self, _hook_id: str, _data: dict) -> None:
        raise ValueError("a hook needs a command or skills")


class TestCreate:
    async def test_a_valid_hook_is_stored_and_answered(self, store, wired):
        resp = await H.api_hooks_create(_req("POST", "/api/hooks", _valid()))

        assert resp.status == 200
        hook = (await _payload(resp))["hook"]
        assert hook["name"] == "fmt"
        assert [h.id for h in store.list_all()] == [hook["id"]]
        assert [h["id"] for h in _on_disk(wired)] == [hook["id"]]

    async def test_a_body_that_is_not_json_is_refused(self, store):
        resp = await H.api_hooks_create(_req("POST", "/api/hooks"))

        assert resp.status == 400
        assert (await _payload(resp))["code"] == "invalid_json"
        assert store.list_all() == []

    async def test_a_body_the_schema_rejects_is_refused(self, store):
        resp = await H.api_hooks_create(_req("POST", "/api/hooks", {"name": "fmt"}))

        assert resp.status == 400
        assert (await _payload(resp))["code"] == "invalid_hook"
        assert store.list_all() == []

    async def test_a_store_invariant_failure_is_a_400(self, wired, monkeypatch):
        monkeypatch.setattr(H, "_get_hook_store", lambda _state: _Invalid())

        resp = await H.api_hooks_create(_req("POST", "/api/hooks", _valid()))

        assert resp.status == 400
        assert (await _payload(resp))["code"] == "invalid_hook"


class TestListAndLazyStore:
    async def test_the_list_reads_every_stored_hook(self, store):
        created = store.create(_valid())

        resp = await H.api_hooks(_req("GET", "/api/hooks"))

        assert resp.status == 200
        assert [h["id"] for h in (await _payload(resp))["hooks"]] == [created.id]

    async def test_the_first_read_builds_the_store_once_and_registers_it(self, wired, monkeypatch):
        registered: list[object] = []
        monkeypatch.setattr(hooks_mod, "ScriptHookStore", lambda: ScriptHookStore(wired))
        monkeypatch.setattr(hooks_mod, "set_global_hook_store", registered.append)
        request = _req("GET", "/api/hooks")
        request.app["state"]._hook_store = None

        first = H._get_hook_store(request.app["state"])
        second = H._get_hook_store(request.app["state"])

        assert first is second
        assert registered == [first]


class TestUpdate:
    async def test_a_changed_field_is_stored_and_answered(self, store, wired):
        hook = store.create(_valid())

        resp = await H.api_hook_detail(
            _req(
                "PUT", f"/api/hooks/{hook.id}", {"name": "renamed"}, match_info={"hook_id": hook.id}
            )
        )

        assert resp.status == 200
        assert (await _payload(resp))["hook"]["name"] == "renamed"
        assert store.get(hook.id).name == "renamed"
        assert [h["name"] for h in _on_disk(wired)] == ["renamed"]

    async def test_an_unknown_hook_is_not_found(self, store):
        resp = await H.api_hook_detail(
            _req("PUT", "/api/hooks/nope", {"name": "renamed"}, match_info={"hook_id": "nope"})
        )

        assert resp.status == 404
        assert (await _payload(resp))["code"] == "hook_not_found"

    async def test_a_body_that_is_not_json_is_refused(self, store):
        hook = store.create(_valid())

        resp = await H.api_hook_detail(
            _req("PUT", f"/api/hooks/{hook.id}", match_info={"hook_id": hook.id})
        )

        assert resp.status == 400
        assert (await _payload(resp))["code"] == "invalid_json"
        assert store.get(hook.id).name == "fmt"

    async def test_a_body_the_schema_rejects_is_refused(self, store):
        hook = store.create(_valid())
        timeout = store.get(hook.id).timeout

        resp = await H.api_hook_detail(
            _req("PUT", f"/api/hooks/{hook.id}", {"timeout": 0}, match_info={"hook_id": hook.id})
        )

        assert resp.status == 400
        assert (await _payload(resp))["code"] == "invalid_hook"
        assert store.get(hook.id).timeout == timeout

    async def test_a_store_invariant_failure_is_a_400(self, wired, monkeypatch):
        monkeypatch.setattr(H, "_get_hook_store", lambda _state: _Invalid())

        resp = await H.api_hook_detail(
            _req("PUT", "/api/hooks/h1", {"name": "renamed"}, match_info={"hook_id": "h1"})
        )

        assert resp.status == 400
        assert (await _payload(resp))["code"] == "invalid_hook"

    async def test_an_unwritable_store_is_a_503(self, wired, monkeypatch):
        monkeypatch.setattr(H, "_get_hook_store", lambda _state: _Refusing())

        resp = await H.api_hook_detail(
            _req("PUT", "/api/hooks/h1", {"name": "renamed"}, match_info={"hook_id": "h1"})
        )

        assert resp.status == 503
        assert (await _payload(resp))["code"] == "store_unavailable"


class TestDelete:
    async def test_a_stored_hook_is_deleted(self, store, wired):
        hook = store.create(_valid())

        resp = await H.api_hook_detail(
            _req("DELETE", f"/api/hooks/{hook.id}", match_info={"hook_id": hook.id})
        )

        assert resp.status == 200
        assert (await _payload(resp)) == {"ok": True}
        assert store.get(hook.id) is None
        assert _on_disk(wired) == []

    async def test_an_unknown_hook_is_not_found(self, store):
        resp = await H.api_hook_detail(
            _req("DELETE", "/api/hooks/nope", match_info={"hook_id": "nope"})
        )

        assert resp.status == 404
        assert (await _payload(resp))["code"] == "hook_not_found"

    async def test_an_unwritable_store_is_a_503(self, wired, monkeypatch):
        monkeypatch.setattr(H, "_get_hook_store", lambda _state: _Refusing())

        resp = await H.api_hook_detail(
            _req("DELETE", "/api/hooks/h1", match_info={"hook_id": "h1"})
        )

        assert resp.status == 503
        assert (await _payload(resp))["code"] == "store_unavailable"


class TestToggle:
    async def test_a_stored_hook_flips_and_stays_flipped(self, store, wired):
        hook = store.create(_valid())
        assert hook.enabled is True

        resp = await H.api_hook_toggle(
            _req("POST", f"/api/hooks/{hook.id}/toggle", match_info={"hook_id": hook.id})
        )

        assert resp.status == 200
        assert (await _payload(resp))["hook"]["enabled"] is False
        assert [h["enabled"] for h in _on_disk(wired)] == [False]

    async def test_an_unknown_hook_is_not_found(self, store):
        resp = await H.api_hook_toggle(
            _req("POST", "/api/hooks/nope/toggle", match_info={"hook_id": "nope"})
        )

        assert resp.status == 404
        assert (await _payload(resp))["code"] == "hook_not_found"

    async def test_an_unwritable_store_is_a_503(self, wired, monkeypatch):
        monkeypatch.setattr(H, "_get_hook_store", lambda _state: _Refusing())

        resp = await H.api_hook_toggle(
            _req("POST", "/api/hooks/h1/toggle", match_info={"hook_id": "h1"})
        )

        assert resp.status == 503
        assert (await _payload(resp))["code"] == "store_unavailable"
