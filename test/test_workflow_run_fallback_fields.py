"""The workflow_run opt-in fallback title/folder plumbing.

The MCP tool forwards the caller's ``title`` and ``folder`` into the run body
unchanged (no dashboard client); the ``/api/workflows/run`` handler resolves the
folder to an EXISTING folder id server-side through the one read-only resolver
(``mcp_dashboard_references._resolve_chat_folder_id``). Nothing is created — a
missing or ambiguous folder rejects the run with a caller-visible 400.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _run_wf(args: dict) -> dict:
    """Call the workflow_run MCP handler with identity + _post stubbed, returning
    the body it POSTed to /api/workflows/run."""
    from kiro_crew.mcp_tools import workflows

    captured: dict = {}

    def _fake_post(path, body=None, **kw):
        captured["path"] = path
        captured["body"] = body
        return {"run_id": "wf_x", "name": body.get("name", "") if body else ""}

    with (
        patch.object(workflows, "_workflow_identity", return_value=("dashboard:chat-1", "")),
        patch.object(workflows.mcp_core, "_post", side_effect=_fake_post),
    ):
        workflows.workflow_run("workflow_run", args)
    return captured.get("body") or {}


def test_mcp_forwards_trimmed_title_raw() -> None:
    body = _run_wf({"source": "def workflow(ctx):\n    return 1", "title": "  MR review  "})
    assert body["title"] == "MR review"


def test_mcp_forwards_folder_ref_raw() -> None:
    body = _run_wf({"source": "def workflow(ctx):\n    return 1", "folder": "MR Reviews"})
    # Forwarded unchanged under ``folder`` for the server to resolve; the MCP
    # tool never resolves and never sets ``folder_id``.
    assert body["folder"] == "MR Reviews"
    assert "folder_id" not in body


def test_mcp_omits_absent_title_and_folder() -> None:
    body = _run_wf({"source": "def workflow(ctx):\n    return 1"})
    assert "title" not in body and "folder" not in body


# --- server-side folder resolution (existing folders only) -------------------


class _FakeFolderState:
    def __init__(self, folders=None) -> None:
        self._folders = list(folders or [])


class _FakeRequest:
    def __init__(self, state, *, expected_store: str | None = None) -> None:
        self.app = {"state": state}
        self._attrs = {"workflow_expected_store": expected_store}

    def get(self, key, default=None):
        # Mirrors aiohttp's request mapping for the handful of request-scoped
        # values the resolver reads (``workflow_expected_store``: the verified
        # member store, or ``None`` for the owner / app caller).
        return self._attrs.get(key, default)


@pytest.mark.asyncio
async def test_resolver_empty_ref_files_nothing() -> None:
    # No folder given -> ("", None): the fallback stays unfiled (today's default).
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    fid, refusal = await _resolve_fallback_folder_id(_FakeRequest(_FakeFolderState()), "")
    assert (fid, refusal) == ("", None)


@pytest.mark.asyncio
async def test_resolver_existing_id_files_the_session() -> None:
    # An existing folder id resolves to itself.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState([{"id": "0123456789ab", "name": "MR"}]))
    fid, refusal = await _resolve_fallback_folder_id(req, "0123456789ab")
    assert (fid, refusal) == ("0123456789ab", None)


@pytest.mark.asyncio
async def test_resolver_existing_path_files_the_session() -> None:
    # An existing human path resolves to the folder's id — nothing created.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    folders = [
        {"id": "id-a", "name": "A", "parent_id": ""},
        {"id": "id-b", "name": "B", "parent_id": "id-a"},
    ]
    req = _FakeRequest(_FakeFolderState(folders))
    fid, refusal = await _resolve_fallback_folder_id(req, "A/B")
    assert (fid, refusal) == ("id-b", None)
    # The folder list is unchanged: resolution never writes.
    assert len(req.app["state"]._folders) == 2


@pytest.mark.asyncio
async def test_resolver_missing_folder_gives_caller_visible_400() -> None:
    # A folder that does not exist is refused with a 400, not silently dropped
    # and not created.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState([{"id": "id-a", "name": "A", "parent_id": ""}]))
    fid, refusal = await _resolve_fallback_folder_id(req, "Nope")
    assert fid == ""
    assert refusal is not None and refusal.status == 400
    assert len(req.app["state"]._folders) == 1  # nothing created


@pytest.mark.asyncio
async def test_resolver_unknown_id_gives_400() -> None:
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState())
    fid, refusal = await _resolve_fallback_folder_id(req, "0123456789ab")
    assert fid == ""
    assert refusal is not None and refusal.status == 400


@pytest.mark.asyncio
async def test_resolver_ambiguous_path_gives_400() -> None:
    # Two folders rendering the same path ``A`` is refused by the canonical
    # resolver's ambiguity check (pass the id instead) -> a caller-visible 400.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    folders = [
        {"id": "id-a1", "name": "A", "parent_id": ""},
        {"id": "id-a2", "name": "A", "parent_id": ""},
    ]
    req = _FakeRequest(_FakeFolderState(folders))
    fid, refusal = await _resolve_fallback_folder_id(req, "A")
    assert fid == ""
    assert refusal is not None and refusal.status == 400


# --- mate-home refusal (a fallback slot never files into a member's home) ----


def _mate_home_folders() -> list[dict]:
    """A member's home folder and a child folder inside it."""
    return [
        {
            "id": "home-1",
            "name": "Home",
            "parent_id": "",
            "mate_home": True,
            "owner_app": "member:store-x",
        },
        {"id": "child-1", "name": "Inside", "parent_id": "home-1"},
    ]


@pytest.mark.asyncio
async def test_resolver_refuses_mate_home_folder_by_id() -> None:
    # A member's home folder is refused with the shared 403; nothing is created.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState(_mate_home_folders()))
    fid, refusal = await _resolve_fallback_folder_id(req, "home-1")
    assert fid == ""
    assert refusal is not None and refusal.status == 403
    body = (
        refusal.body.decode() if isinstance(refusal.body, (bytes, bytearray)) else str(refusal.body)
    )
    assert "mate_home_locked" in body
    assert len(req.app["state"]._folders) == 2  # nothing created


@pytest.mark.asyncio
async def test_resolver_refuses_folder_inside_mate_home_by_id() -> None:
    # A folder nested inside a member's home is refused the same way.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState(_mate_home_folders()))
    fid, refusal = await _resolve_fallback_folder_id(req, "child-1")
    assert fid == ""
    assert refusal is not None and refusal.status == 403
    body = (
        refusal.body.decode() if isinstance(refusal.body, (bytes, bytearray)) else str(refusal.body)
    )
    assert "mate_home_locked" in body


@pytest.mark.asyncio
async def test_resolver_refuses_mate_home_folder_by_path() -> None:
    # The same refusal when the member's home is named by its human path.
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState(_mate_home_folders()))
    fid, refusal = await _resolve_fallback_folder_id(req, "Home")
    assert fid == ""
    assert refusal is not None and refusal.status == 403


@pytest.mark.asyncio
async def test_resolver_refuses_folder_inside_mate_home_by_path() -> None:
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState(_mate_home_folders()))
    fid, refusal = await _resolve_fallback_folder_id(req, "Home/Inside")
    assert fid == ""
    assert refusal is not None and refusal.status == 403


# --- member caller may not resolve against the person's sidebar tree --------


async def _member_refusal_body(ref: str, folders: list[dict]) -> tuple[int, dict]:
    """Resolve *ref* as a crew-member caller and return (status, body)."""
    import json

    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState(folders), expected_store="store-member")
    fid, refusal = await _resolve_fallback_folder_id(req, ref)
    assert fid == ""
    assert refusal is not None
    raw = refusal.body
    body = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    return refusal.status, body


@pytest.mark.asyncio
async def test_member_caller_folder_refusal_is_uniform_and_leaks_nothing() -> None:
    # A member caller gets the IDENTICAL 400 whether the person's folder exists,
    # is missing, or is ambiguous: the response reveals nothing of the person's
    # folder layout, and resolution never runs against their tree.
    existing = [{"id": "person-fld", "name": "Finance", "parent_id": ""}]
    ambiguous = [
        {"id": "a1", "name": "Finance", "parent_id": ""},
        {"id": "a2", "name": "Finance", "parent_id": ""},
    ]

    # By id, by path, a missing ref, and an ambiguous path all give one answer.
    answers = [
        await _member_refusal_body("person-fld", existing),  # exists, by id
        await _member_refusal_body("Finance", existing),  # exists, by path
        await _member_refusal_body("Nope", existing),  # missing
        await _member_refusal_body("Finance", ambiguous),  # ambiguous
    ]
    for status, body in answers:
        assert status == 400
        assert body == {
            "error": "folder is not available for this caller",
            "code": "folder_unresolvable",
        }
    # Every answer is byte-identical: nothing distinguishes exists / missing /
    # ambiguous, so the person's folder ids and paths cannot be probed.
    assert len({repr(a) for a in answers}) == 1


@pytest.mark.asyncio
async def test_member_caller_empty_folder_is_still_allowed() -> None:
    # The refusal is only for a NON-EMPTY folder; a member run that names no
    # folder is unaffected (the fallback stays unfiled, today's default).
    from kiro_crew.dashboard.handlers.workflows import _resolve_fallback_folder_id

    req = _FakeRequest(_FakeFolderState([{"id": "x"}]), expected_store="store-member")
    fid, refusal = await _resolve_fallback_folder_id(req, "")
    assert (fid, refusal) == ("", None)


@pytest.mark.asyncio
async def test_resolver_no_admission_slot_patched_on_member_refusal() -> None:
    # End-to-end: a member caller naming any folder gets 400 and admission is
    # never reached, so no slot is reserved. The owner path keeps working (its
    # existing tests cover a successful resolve).
    from unittest.mock import AsyncMock, patch

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import kiro_crew.dashboard.handlers.workflows as wf
    from kiro_crew.dashboard.handlers.workflows import api_workflow_run

    start = AsyncMock(return_value={"run_id": "wf_never"})
    app = web.Application()
    app["state"] = SimpleNamespace(
        workflow_service=SimpleNamespace(start=start), _folders=[{"id": "person-fld"}]
    )
    app.router.add_post("/api/workflows/run", api_workflow_run)
    admit = AsyncMock()

    async def _member_pass(request, _op):
        request["workflow_expected_store"] = "store-member"
        return None

    with (
        patch.object(wf, "_private_memory_refusal", _member_pass),
        patch.object(wf, "_admit_workflow_run", admit),
    ):
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/workflows/run", json={"source": "print('ok')", "folder": "person-fld"}
            )
            body = await response.json()
            assert response.status == 400
            assert body["code"] == "folder_unresolvable"

    admit.assert_not_awaited()
    start.assert_not_awaited()
