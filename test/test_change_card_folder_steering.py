"""The ``folder.steering`` change card: an agent asks, the person's browser applies.

The catalog half (params, preview, risk, apply plan, undo) is pure. The end-to-end
half runs the REAL card routes, the REAL card hook and the REAL folder PATCH
handler over an in-memory folder store: the agent proposes on the internal
credential, a direct agent PATCH is refused, and only the owner's card step
writes the folder.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from guide_route_helpers import in_dashboard_turn

from kiro_crew import change_card_catalog as catalog
from kiro_crew import pinned_fs
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.chat_folders import api_chat_folder_update
from kiro_crew.dashboard.handlers import change_cards as routes
from kiro_crew.dashboard.state import _ChatSlot

KIND = "folder.steering"
SLOT = "chat-1"
SK = f"dashboard:{SLOT}"

_needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="steering_dirs are refused where a directory cannot be opened relative to a descriptor",
)


def _preview(params: dict[str, Any], current: list[str] | None, name: str = "Docs"):
    p = catalog.validate_params(KIND, params)
    before = {"exists": True, "steering_dirs": current or []}
    return catalog.build_preview(KIND, p, before, {"label": name})


# ── catalog ──


@pytest.mark.parametrize(
    "params,code",
    [
        ({"folder": "f1"}, "invalid_param"),
        ({"steering_dirs": []}, "missing_param"),
        ({"folder": "f1", "steering_dirs": "/a"}, "invalid_param"),
        ({"folder": "f1", "steering_dirs": [""]}, "invalid_param"),
        ({"folder": "../x", "steering_dirs": []}, "invalid_param"),
        ({"folder": "f1", "steering_dirs": [], "project_dir": "/x"}, "unknown_param"),
    ],
)
def test_bad_params_are_refused(params, code):
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(KIND, params)
    assert exc.value.code == code


def test_the_kind_is_listed_for_list_change_kinds():
    assert KIND in {k["id"] for k in catalog.list_kinds()}


def test_preview_shows_folder_current_and_new_and_plans_the_folder_patch():
    preview = _preview({"folder": "f1", "steering_dirs": ["/a", "/b"]}, ["/a"])
    assert preview["title"] == "Change steering for folder “Docs”"
    assert preview["changes"] == [
        {"label": "Folder", "after": "Docs"},
        {"label": "Change", "add": ["/b"], "remove": []},
        {"label": "Current", "after": ["/a"]},
        {"label": "New", "after": ["/a", "/b"]},
    ]
    assert preview["apply"] == [
        {
            "method": "PATCH",
            "path": "/api/chat/folders/f1",
            "body": {"steering_dirs": ["/a", "/b"]},
        }
    ]


@pytest.mark.parametrize(
    "current,new,risk",
    [
        ([], ["/a"], "widen"),
        (["/a"], ["/a", "/b"], "widen"),
        (["/a"], ["/b"], "widen"),
        (["/a", "/b"], ["/a"], "tighten"),
        (["/a"], [], "tighten"),
        (["/a", "/b"], ["/b", "/a"], "normal"),
    ],
)
def test_risk_adding_widens_and_only_removing_tightens(current, new, risk):
    assert _preview({"folder": "f1", "steering_dirs": new}, current)["risk"] == risk


def test_the_same_list_and_a_missing_folder_are_refused():
    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"folder": "f1", "steering_dirs": ["/a"]}, ["/a"])
    assert exc.value.code == "no_change"
    p = catalog.validate_params(KIND, {"folder": "f1", "steering_dirs": []})
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(KIND, p, {"exists": False}, {})
    assert exc.value.code == "not_found"


def test_undo_writes_the_previous_list_back():
    p = catalog.validate_params(KIND, {"folder": "f1", "steering_dirs": ["/b"]})
    plan, reason = catalog.build_undo(KIND, p, {"exists": True, "steering_dirs": ["/a"]}, [{}], 1)
    assert reason is None
    assert plan == [
        {
            "method": "PATCH",
            "path": "/api/chat/folders/f1",
            "body": {"steering_dirs": ["/a"]},
        }
    ]


def test_undo_is_offered_only_while_the_folder_holds_what_the_card_wrote():
    p = {"folder": "f1", "steering_dirs": ["/a"]}
    assert cards.after_shows_the_write(KIND, p, {"exists": True, "steering_dirs": ["/a"]})
    assert not cards.after_shows_the_write(KIND, p, {"exists": True, "steering_dirs": ["/real/a"]})


def test_the_folder_patch_is_hooked_but_writes_no_memory_line():
    route = ("PATCH", "/api/chat/folders/{id}")
    assert route in catalog.HOOKED_ROUTES
    assert route not in catalog.MEMORY_ROUTES


# ── end to end: real card routes, real hook, real folder route ──


class _State:
    """The slice of ``DashboardState`` the card routes and the folder PATCH read."""

    owner_id = ""

    def __init__(self, folders: list[dict[str, Any]]) -> None:
        self._slots: dict[str, _ChatSlot] = {SLOT: in_dashboard_turn(_ChatSlot(SLOT))}
        self._change_card_store = CardStore(None)
        self._folders = folders
        self._tags: list[Any] = []
        self.conversation_log = None
        self.push_slots_update = MagicMock()

    def get_slot(self, name: str):
        return self._slots.get(name)

    async def deliver_ws_owners(self, kind: str, payload: dict[str, Any]) -> int:
        return 1

    def broadcast_ws(self, kind: str, payload: dict[str, Any]) -> None:
        return None

    async def read_folders(self, fn: Any) -> Any:
        return fn(copy.deepcopy(self._folders))

    async def mutate_folders(self, fn: Any, on_committed: Any = None) -> Any:
        changed, value = fn(self._folders)
        if changed and on_committed is not None:
            on_committed()
        return value


@web.middleware
async def _fake_auth(request: web.Request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    if request.headers.get("X-Test-Auth") == "internal":
        request["internal_auth"] = True
        request["peer_verified"] = True
    return await handler(request)


def _run(state: _State, fn):
    async def main():
        app = web.Application(middlewares=[_fake_auth, routes.change_card_middleware])
        app["state"] = state
        routes.register_change_card_routes(app)
        app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
        async with TestClient(TestServer(app)) as client:
            out = await fn(client)
            if routes._BACKGROUND:
                await asyncio.gather(*list(routes._BACKGROUND))
            return out

    return asyncio.run(main())


AGENT = {"X-Test-Auth": "internal", "X-Session-Key": SK}


def _step(card: dict[str, Any], op: str = "apply") -> dict[str, str]:
    return {
        "X-Test-Auth": "owner",
        "X-Card-Id": card["id"],
        "X-Card-Revision": str(card["revision"]),
        "X-Card-Op": op,
        "X-Card-Step": "0",
    }


@_needs_pinned_walk
def test_agent_proposes_owner_applies_and_undoes(tmp_path):
    keep, new = tmp_path / "keep", tmp_path / "new"
    keep.mkdir()
    new.mkdir()
    keep_s, new_s = str(keep.resolve()), str(new.resolve())
    state = _State([{"id": "f1", "name": "Docs", "parent_id": None, "steering_dirs": [keep_s]}])

    async def go(c):
        # The agent cannot write the field itself...
        r = await c.patch("/api/chat/folders/f1", json={"steering_dirs": [new_s]}, headers=AGENT)
        assert r.status == 403, await r.text()
        assert (await r.json())["code"] == "steering_dirs_forbidden"
        assert state._folders[0]["steering_dirs"] == [keep_s]
        # ...it proposes the card, which changes nothing yet.
        r = await c.post(
            "/api/cards/agent/propose",
            json={"kind": KIND, "params": {"folder": "f1", "steering_dirs": [keep_s, new_s]}},
            headers=AGENT,
        )
        assert r.status == 200, await r.text()
        card = await r.json()
        assert card["kind"] == KIND and card["risk"] == "widen"
        assert state._folders[0]["steering_dirs"] == [keep_s]
        step = card["plan"]["apply"][0]
        # The agent's own credential cannot press the card's button.
        r = await c.patch(
            step["path"], json=step["body"], headers={**_step(card), "X-Test-Auth": "internal"}
        )
        assert r.status == 403
        # The person's browser applies it through the folder route.
        r = await c.patch(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 200, await r.text()
        assert state._folders[0]["steering_dirs"] == [keep_s, new_s]
        r = await c.get(f"/api/cards/agent/status?card_id={card['id']}", headers=AGENT)
        applied = await r.json()
        assert applied["status"] == "applied", applied
        undo = applied["plan"]["undo"][0]
        assert undo["body"] == {"steering_dirs": [keep_s]}
        r = await c.patch(undo["path"], json=undo["body"], headers=_step(applied, "undo"))
        assert r.status == 200, await r.text()
        assert state._folders[0]["steering_dirs"] == [keep_s]

    _run(state, go)


def _save_lands_first(state: _State, dirs: list[str]) -> None:
    """Another tab's save lands after the card's checks, just before the write."""
    real = state.mutate_folders

    async def mutate(fn: Any, on_committed: Any = None) -> Any:
        state._folders[0]["steering_dirs"] = dirs
        state.mutate_folders = real  # type: ignore[method-assign]
        return await real(fn, on_committed)

    state.mutate_folders = mutate  # type: ignore[method-assign]


@_needs_pinned_walk
def test_apply_does_not_overwrite_a_save_made_after_its_check(tmp_path):
    keep, new, other = tmp_path / "keep", tmp_path / "new", tmp_path / "other"
    for d in (keep, new, other):
        d.mkdir()
    keep_s, new_s, other_s = (str(d.resolve()) for d in (keep, new, other))
    state = _State([{"id": "f1", "name": "Docs", "parent_id": None, "steering_dirs": [keep_s]}])

    async def go(c):
        r = await c.post(
            "/api/cards/agent/propose",
            json={"kind": KIND, "params": {"folder": "f1", "steering_dirs": [keep_s, new_s]}},
            headers=AGENT,
        )
        card = await r.json()
        step = card["plan"]["apply"][0]
        _save_lands_first(state, [other_s])
        r = await c.patch(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 409, await r.text()
        assert state._folders[0]["steering_dirs"] == [other_s]

    _run(state, go)


@_needs_pinned_walk
def test_undo_does_not_overwrite_a_save_made_after_its_check(tmp_path):
    keep, new, other = tmp_path / "keep", tmp_path / "new", tmp_path / "other"
    for d in (keep, new, other):
        d.mkdir()
    keep_s, new_s, other_s = (str(d.resolve()) for d in (keep, new, other))
    state = _State([{"id": "f1", "name": "Docs", "parent_id": None, "steering_dirs": [keep_s]}])

    async def go(c):
        r = await c.post(
            "/api/cards/agent/propose",
            json={"kind": KIND, "params": {"folder": "f1", "steering_dirs": [keep_s, new_s]}},
            headers=AGENT,
        )
        card = await r.json()
        step = card["plan"]["apply"][0]
        r = await c.patch(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 200, await r.text()
        r = await c.get(f"/api/cards/agent/status?card_id={card['id']}", headers=AGENT)
        applied = await r.json()
        undo = applied["plan"]["undo"][0]
        _save_lands_first(state, [other_s])
        r = await c.patch(undo["path"], json=undo["body"], headers=_step(applied, "undo"))
        assert r.status == 409, await r.text()
        assert state._folders[0]["steering_dirs"] == [other_s]

    _run(state, go)


@_needs_pinned_walk
def test_a_plain_folder_patch_cannot_supply_the_expected_list(tmp_path):
    keep, other = tmp_path / "keep", tmp_path / "other"
    keep.mkdir()
    other.mkdir()
    keep_s, other_s = str(keep.resolve()), str(other.resolve())
    state = _State([{"id": "f1", "name": "Docs", "parent_id": None, "steering_dirs": [other_s]}])

    async def go(c):
        # The person's own save is unconditional; a body field changes nothing.
        body = {"steering_dirs": [keep_s], "expect_steering_dirs": ["/elsewhere"]}
        r = await c.patch("/api/chat/folders/f1", json=body)
        assert r.status == 200, await r.text()
        assert state._folders[0]["steering_dirs"] == [keep_s]

    _run(state, go)


@_needs_pinned_walk
def test_a_card_step_that_differs_from_the_plan_is_refused(tmp_path):
    a, other = tmp_path / "a", tmp_path / "other"
    a.mkdir()
    other.mkdir()
    state = _State([{"id": "f1", "name": "Docs", "parent_id": None}])

    async def go(c):
        r = await c.post(
            "/api/cards/agent/propose",
            json={"kind": KIND, "params": {"folder": "f1", "steering_dirs": [str(a)]}},
            headers=AGENT,
        )
        card = await r.json()
        r = await c.patch(
            "/api/chat/folders/f1",
            json={"steering_dirs": [str(other.resolve())]},
            headers=_step(card),
        )
        assert r.status == 409
        assert (await r.json())["code"] == "plan_mismatch"
        assert "steering_dirs" not in state._folders[0]

    _run(state, go)


@_needs_pinned_walk
def test_the_card_shows_and_writes_a_linked_dir_as_the_route_stores_it(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    real_s = str(real.resolve())
    state = _State([{"id": "f1", "name": "Docs", "parent_id": None}])

    async def go(c):
        r = await c.post(
            "/api/cards/agent/propose",
            json={"kind": KIND, "params": {"folder": "f1", "steering_dirs": [str(link)]}},
            headers=AGENT,
        )
        assert r.status == 200, await r.text()
        card = await r.json()
        # The person approves the directory the gateway will actually read.
        assert card["params"]["steering_dirs"] == [real_s]
        new_row = next(ch for ch in card["changes"] if ch["label"] == "New")
        assert new_row["after"] == [real_s]
        step = card["plan"]["apply"][0]
        r = await c.patch(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 200, await r.text()
        assert state._folders[0]["steering_dirs"] == [real_s]
        r = await c.get(f"/api/cards/agent/status?card_id={card['id']}", headers=AGENT)
        applied = await r.json()
        # The read-back matches what the card wrote, so Undo is offered.
        assert applied["plan"]["undo"], applied

    _run(state, go)


def test_a_dir_the_route_would_refuse_is_refused_at_proposal(tmp_path):
    state = _State([{"id": "f1", "name": "Docs", "parent_id": None}])

    async def go(c):
        r = await c.post(
            "/api/cards/agent/propose",
            json={
                "kind": KIND,
                "params": {"folder": "f1", "steering_dirs": [str(tmp_path / "nope")]},
            },
            headers=AGENT,
        )
        assert r.status == 400, await r.text()
        assert (await r.json())["code"] == "invalid_param"

    _run(state, go)


def test_agent_create_with_steering_is_refused(tmp_path):
    from kiro_crew.dashboard.chat_folders import api_chat_folder_create

    state = _State([])

    async def go(c):
        r = await c.post(
            "/api/chat/folders",
            data=json.dumps({"name": "Docs", "steering_dirs": [str(tmp_path)]}),
            headers={**AGENT, "Content-Type": "application/json"},
        )
        assert r.status == 403, await r.text()
        assert (await r.json())["code"] == "steering_dirs_forbidden"
        assert state._folders == []

    async def main():
        app = web.Application(middlewares=[_fake_auth])
        app["state"] = state
        app.router.add_post("/api/chat/folders", api_chat_folder_create)
        async with TestClient(TestServer(app)) as client:
            await go(client)

    asyncio.run(main())


def test_a_long_canonical_dir_the_route_accepts_survives_a_rebuild():
    from kiro_crew.dashboard.chat_folders import MAX_FOLDER_STEERING_DIR_LEN

    assert catalog.FOLDER_STEERING_DIR_MAX == MAX_FOLDER_STEERING_DIR_LEN
    long_dir = "/" + "d" * 600
    p = catalog.validate_params(KIND, {"folder": "f1", "steering_dirs": [long_dir]})
    assert catalog.validate_params(KIND, p) == p


@_needs_pinned_walk
def test_apply_refuses_a_dir_swapped_for_a_link_after_approval(tmp_path):
    approved, elsewhere = tmp_path / "approved", tmp_path / "elsewhere"
    approved.mkdir()
    elsewhere.mkdir()
    state = _State([{"id": "f1", "name": "Docs", "parent_id": None}])

    async def go(c):
        r = await c.post(
            "/api/cards/agent/propose",
            json={"kind": KIND, "params": {"folder": "f1", "steering_dirs": [str(approved)]}},
            headers=AGENT,
        )
        card = await r.json()
        step = card["plan"]["apply"][0]
        approved.rmdir()
        approved.symlink_to(elsewhere, target_is_directory=True)
        r = await c.patch(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 409, await r.text()
        assert (await r.json())["code"] == "steering_dirs_changed"
        assert "steering_dirs" not in state._folders[0]

    _run(state, go)
