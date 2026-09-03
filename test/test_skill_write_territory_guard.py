"""Pins the read-only write-path contract for the open-standard skill-key territories.

``kiro-user/`` and ``kiro-workspace/`` skill keys resolve per-machine / per-session on
the READ path (``~/.kiro/skills`` and ``<project>/.kiro/skills`` via
``_resolve_skill_root``), while ``skills.create/update/delete_skill`` would join the key
onto a core root — so the same key names a different file on write than the reader was
shown (issue #8244). The FEAT-002 guard refuses the mutating verbs (PUT/DELETE -> 405 with
``Allow: GET`` and code ``readonly_skill_prefix``; create -> 400 with code
``reserved_skill_prefix``) while leaving reads and non-prefixed writes untouched.

These tests invoke ``prompts.api_skill_detail`` / ``prompts.api_skills_create`` directly
with a fake request and a recording fake skills object, following the direct-handler
pattern in ``test/test_dashboard_pinned_write_migration.py``.

NOT EXECUTED IN THE INTEGRATIONS_ONLY SANDBOX. Importing the dashboard handler modules
pulls ``aiohttp`` and its dependency chain, so these run in CI only.

CI invocation:

    python -m pytest test/test_skill_write_territory_guard.py -n0 -q
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import kiro_crew.dashboard.handlers.prompts as prompts_mod


class _FakeRequest:
    """The slice of ``web.Request`` the two skill CRUD handlers actually read."""

    def __init__(self, method: str, *, name: str = "", body: dict | None = None) -> None:
        self.method = method
        self.match_info = {"name": name}
        self.app = {"state": SimpleNamespace(context_builder=None)}
        # The GET read path reads X-Session-Key; an empty header set is enough to
        # let it fall through the mutating-verb guard into the (not-found) read.
        self.headers: dict = {}
        self._body = body or {}

    async def json(self) -> dict:
        return self._body


class _RecordingSkills:
    """Records every CRUD call so a test can assert a mutating call was (not) reached."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def create_skill(self, name: str, content: str) -> bool:
        self.calls.append(("create_skill", (name, content)))
        return True

    def update_skill(self, name: str, content: str) -> bool:
        self.calls.append(("update_skill", (name, content)))
        return True

    def delete_skill(self, name: str) -> bool:
        self.calls.append(("delete_skill", (name,)))
        return True

    def load_skill(self, name: str):
        self.calls.append(("load_skill", (name,)))
        return None

    def called(self, method: str) -> bool:
        return any(c[0] == method for c in self.calls)


@pytest.fixture
def recorder(monkeypatch) -> _RecordingSkills:
    rec = _RecordingSkills()
    monkeypatch.setattr(prompts_mod, "_get_skills", lambda _state: rec)
    return rec


class TestReadOnlySkillWriteGuard:
    """PUT/DELETE/create for kiro-user/ and kiro-workspace/ keys are refused, not written."""

    # ── PUT is refused for both read-only territories ──

    @pytest.mark.asyncio
    async def test_put_kiro_workspace_returns_405_and_does_not_update(self, recorder):
        resp = await prompts_mod.api_skill_detail(
            _FakeRequest("PUT", name="kiro-workspace/foo", body={"content": "x"})
        )
        assert resp.status == 405
        assert resp.headers.get("Allow") == "GET"
        assert resp.body is not None
        assert json.loads(resp.body)["code"] == "readonly_skill_prefix"
        assert not recorder.called("update_skill")

    @pytest.mark.asyncio
    async def test_put_kiro_user_returns_405_and_does_not_update(self, recorder):
        resp = await prompts_mod.api_skill_detail(
            _FakeRequest("PUT", name="kiro-user/foo", body={"content": "x"})
        )
        assert resp.status == 405
        assert resp.headers.get("Allow") == "GET"
        assert json.loads(resp.body)["code"] == "readonly_skill_prefix"
        assert not recorder.called("update_skill")

    # ── DELETE is refused for both read-only territories ──

    @pytest.mark.asyncio
    async def test_delete_kiro_workspace_returns_405_and_does_not_delete(self, recorder):
        resp = await prompts_mod.api_skill_detail(_FakeRequest("DELETE", name="kiro-workspace/foo"))
        assert resp.status == 405
        assert resp.headers.get("Allow") == "GET"
        assert json.loads(resp.body)["code"] == "readonly_skill_prefix"
        assert not recorder.called("delete_skill")

    @pytest.mark.asyncio
    async def test_delete_kiro_user_returns_405_and_does_not_delete(self, recorder):
        resp = await prompts_mod.api_skill_detail(_FakeRequest("DELETE", name="kiro-user/foo"))
        assert resp.status == 405
        assert resp.headers.get("Allow") == "GET"
        assert json.loads(resp.body)["code"] == "readonly_skill_prefix"
        assert not recorder.called("delete_skill")

    # ── create is refused, including for a name that only sanitises into the territory ──

    @pytest.mark.asyncio
    async def test_create_kiro_workspace_returns_400_and_does_not_create(self, recorder):
        resp = await prompts_mod.api_skills_create(
            _FakeRequest("POST", body={"name": "kiro-workspace/foo", "content": "body"})
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "reserved_skill_prefix"
        assert not recorder.called("create_skill")

    @pytest.mark.asyncio
    async def test_create_mixed_case_sanitises_into_territory_and_is_refused(self, recorder):
        # 'Kiro-Workspace/Foo' lowercases to 'kiro-workspace/foo' — the guard checks
        # the sanitised name, which is what create_skill would actually write.
        resp = await prompts_mod.api_skills_create(
            _FakeRequest("POST", body={"name": "Kiro-Workspace/Foo", "content": "body"})
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "reserved_skill_prefix"
        assert not recorder.called("create_skill")

    # ── regression: the guard is scoped to the two prefixes; plain names still write ──

    @pytest.mark.asyncio
    async def test_put_plain_name_still_updates(self, recorder):
        resp = await prompts_mod.api_skill_detail(
            _FakeRequest("PUT", name="my-skill", body={"content": "after"})
        )
        assert resp.status == 200
        assert recorder.called("update_skill")

    @pytest.mark.asyncio
    async def test_delete_plain_name_still_deletes(self, recorder):
        resp = await prompts_mod.api_skill_detail(_FakeRequest("DELETE", name="my-skill"))
        assert resp.status == 200
        assert recorder.called("delete_skill")

    @pytest.mark.asyncio
    async def test_create_plain_name_still_creates(self, recorder):
        resp = await prompts_mod.api_skills_create(
            _FakeRequest("POST", body={"name": "my-skill", "content": "body"})
        )
        assert resp.status == 200
        assert recorder.called("create_skill")

    # ── regression: a GET for a read-only prefix is NOT caught by the mutating-verb guard ──

    @pytest.mark.asyncio
    async def test_get_read_only_prefix_is_not_short_circuited_by_the_guard(
        self, recorder, monkeypatch
    ):
        # The guard only fires for PUT/DELETE. A GET for a kiro-workspace/ key must fall
        # through to the read path (a full routed read is covered by test_skill_browser.py).
        # Threading the whole session/slot read state through the fake request is heavy,
        # so we stub the read-path collaborators to a deterministic not-found: the point
        # is that the method-GET path is NOT answered by the new 405 guard.
        monkeypatch.setattr(prompts_mod, "_deny_foreign_app_skill_slot", lambda *a, **k: None)
        monkeypatch.setattr(prompts_mod, "_resolve_skill_root", lambda *a, **k: None)
        resp = await prompts_mod.api_skill_detail(_FakeRequest("GET", name="kiro-workspace/foo"))
        # Not the 405 refusal: the guard did not fire for GET. It resolves to a normal
        # not-found because no matching skill root exists in this stubbed environment.
        assert resp.status != 405
        assert resp.status == 404
        # No mutating call was made on the read path.
        assert not recorder.called("update_skill")
        assert not recorder.called("delete_skill")
