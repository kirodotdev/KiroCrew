"""Writes to a stored artifact.

Covers the JSON body gate the create and update routes read through (a non-JSON media type
refused 415, the byte cap bounding what is buffered), ``PATCH`` (version bumps and
snapshots, metadata-only saves, the theme-contrast warning), ``clear_tags`` sent from the
``artifact_update`` tool, the document type control, webapp metadata, the
optimistic-concurrency content token (with the reads kept off the event loop, the detail
read assigning a pre-token artifact its token) and the ``bound_to`` targets a dashboard package may name. MagicMock
requests and a real store from ``artifacts_handlers_helpers.py``; the tag cases run the
real tool against a loopback test server.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from artifacts_handlers_helpers import (  # noqa: F401 -- fixtures are requested by name
    _json_body,
    _request,
    isolated_store_fixture,
    patch_restricted_fixture,
)
from body_stream_helpers import attach_json_body

from kiro_crew import artifacts as art_mod
from kiro_crew import mcp_core
from kiro_crew.dashboard.handlers.artifacts import (
    api_artifact_delete,
    api_artifact_detail,
    api_artifact_events,
    api_artifact_update,
    api_artifacts_create,
)


@pytest.fixture
def sequential_tokens(monkeypatch) -> None:
    """Hand out distinct content tokens in order, so no comparison rests on chance."""
    counter = itertools.count(1)
    monkeypatch.setattr(art_mod, "_new_content_token", lambda: f"{next(counter):064x}")


class TestJsonBodyGate:
    """``_read_json_body`` reads through ``read_bounded_json``.

    A present body that does not declare JSON is refused 415 before it is read,
    on every route the helper serves; and the byte cap bounds what is buffered,
    not only what is accepted.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content_type", ["text/plain", "application/octet-stream"])
    async def test_create_refuses_a_non_json_media_type_415(
        self, isolated_store, patch_restricted, content_type
    ) -> None:
        req = _request(body={"name": "Hello", "content": "<p>hi</p>"})
        req.content_type = content_type
        with pytest.raises(web.HTTPUnsupportedMediaType) as exc:
            await api_artifacts_create(req)
        assert exc.value.status == 415
        assert json.loads(exc.value.text)["code"] == "unsupported_media_type"
        with pytest.raises(art_mod.ArtifactNotFoundError):
            isolated_store.get("hello")

    @pytest.mark.asyncio
    async def test_update_refuses_a_non_json_media_type_415(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="page", content="<p>ok</p>", slug="page")
        req = _request(body={"name": "Renamed"}, match={"slug": "page"})
        req.content_type = "text/plain"
        with pytest.raises(web.HTTPUnsupportedMediaType):
            await api_artifact_update(req)
        assert isolated_store.get("page").name == "page"

    @pytest.mark.asyncio
    async def test_an_oversize_chunked_body_stops_streaming_at_the_cap(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        cap = 1024
        monkeypatch.setattr(art_handlers, "_MAX_BODY_BYTES", cap)
        raw = json.dumps({"name": "big", "content": "x" * (cap * 50)}).encode()
        req = _request(body=None)
        attach_json_body(req, raw)
        req.content_length = None  # chunked: no declared length to refuse on
        served: list[int] = []
        drain = req.content.iter_chunked

        async def _counting(n: int):
            async for chunk in drain(n):
                served.append(len(chunk))
                yield chunk

        req.content.iter_chunked = _counting
        resp = await api_artifacts_create(req)
        assert resp.status == 400
        assert "exceeds" in _json_body(resp)["error"]
        # Stopped one chunk past the cap rather than draining the whole body.
        assert sum(served) <= cap + 8192
        assert sum(served) < len(raw)
        with pytest.raises(art_mod.ArtifactNotFoundError):
            isolated_store.get("big")

    @pytest.mark.asyncio
    async def test_an_absent_body_is_still_an_empty_object(
        self, isolated_store, patch_restricted
    ) -> None:
        resp = await api_artifacts_create(_request(body=None))
        # {} reaches the route's own validation, not a body refusal.
        assert resp.status == 400
        assert "JSON" not in _json_body(resp)["error"]


class TestUpdate:
    @pytest.mark.asyncio
    async def test_content_update_carries_theme_contrast_warning(
        self, isolated_store, patch_restricted
    ) -> None:
        # The CLI's PATCH is exactly how the motivating incident's script
        # pushed its hardcoded-palette page — the verdict must ride the
        # update response, not just the MCP tool's local hint.
        isolated_store.create(name="page", content="<p>ok</p>", slug="page")
        resp = await api_artifact_update(
            _request(body={"content": '<body style="color:#111">x</body>'}, match={"slug": "page"})
        )
        assert resp.status == 200
        assert _json_body(resp)["theme_contrast_warning"] is True

    @pytest.mark.asyncio
    async def test_metadata_only_update_stamps_warning_false(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="page", content='<p style="color:#111">x</p>', slug="page")
        resp = await api_artifact_update(_request(body={"name": "Renamed"}, match={"slug": "page"}))
        assert resp.status == 200
        assert _json_body(resp)["theme_contrast_warning"] is False

    @pytest.mark.asyncio
    async def test_content_change_with_snapshot_bumps_version(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(body={"content": "v2", "snapshot": True}, match={"slug": "x"})
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["version"] == 2
        assert body["content"] == "v2"

    @pytest.mark.asyncio
    async def test_dashboard_save_without_snapshot_keeps_version(
        self, isolated_store, patch_restricted
    ) -> None:
        # Explicit-snapshot model: a dashboard PATCH with no snapshot flag
        # updates the live state but does NOT bump version. Versioning is
        # deliberate.
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(_request(body={"content": "v2"}, match={"slug": "x"}))
        assert resp.status == 200
        body = _json_body(resp)
        assert body["version"] == 1  # version unchanged
        assert body["content"] == "v2"  # live state updated

    @pytest.mark.asyncio
    async def test_mcp_update_auto_snapshots(self, isolated_store, patch_restricted) -> None:
        # MCP-originated requests (X-Internal-Secret header) auto-snapshot
        # because each agent iteration is a meaningful state change worth
        # versioning.
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(
                body={"content": "v2"},
                match={"slug": "x"},
                extra_headers={"X-Internal-Secret": "fake-secret"},
            )
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["version"] == 2  # MCP call → auto-snapshot

    @pytest.mark.asyncio
    async def test_metadata_change_no_version_bump(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(body={"description": "updated"}, match={"slug": "x"})
        )
        body = _json_body(resp)
        assert body["version"] == 1
        assert body["description"] == "updated"

    @pytest.mark.asyncio
    async def test_missing_returns_404(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifact_update(_request(body={"content": "x"}, match={"slug": "nope"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_restricted_session_denied(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(body={"content": "v2"}, match={"slug": "x"}, restricted=True)
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_invalid_field_returns_400(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        # over-long description
        resp = await api_artifact_update(
            _request(body={"description": "z" * 5_000}, match={"slug": "x"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_artifact_error_fallback_returns_500(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        # store.update() raising the base ArtifactError (e.g. a sensitive-path
        # refusal from _write_text) must be caught explicitly and audited as an
        # error, not escape the handler as an unhandled 500 with no audit trail.
        from kiro_crew.artifacts import ArtifactError

        isolated_store.create(name="x", content="v1", slug="x")

        def _boom(*_a, **_kw):
            raise ArtifactError("refusing to write sensitive path: ~/.aws/credentials")

        monkeypatch.setattr(isolated_store, "update", _boom)
        resp = await api_artifact_update(_request(body={"content": "v2"}, match={"slug": "x"}))
        assert resp.status == 500
        assert "sensitive path" in _json_body(resp)["error"]


class TestClearTagsFromTheToolReachesTheStore:
    """``artifact_update clear_tags: true`` empties the stored tags.

    A client that drops empty lists never delivers ``tags: []``, so the tool takes
    ``clear_tags: true`` and sends the route the empty list itself. The tool runs from
    its own entry (schema validation and handler) with its ``_patch`` bridged onto a
    loopback test server that holds the real update route and the real store.
    """

    @staticmethod
    def _tool_calls(monkeypatch, *calls: dict[str, Any]) -> tuple[list[str], list[Any]]:
        sent: list[Any] = []
        replies: list[str] = []

        @web.middleware
        async def internal(request: web.Request, handler):
            # What the gateway's auth layer leaves on the tool's internal-secret call.
            request["internal_auth"] = True
            return await handler(request)

        async def main() -> None:
            app = web.Application(middlewares=[internal])
            state = MagicMock()
            state.get_slot.return_value = None
            app["state"] = state
            app.router.add_patch("/api/artifacts/{slug}", api_artifact_update)
            async with TestClient(TestServer(app)) as client:
                addrs = [a[0] for a in client.server.runner.addresses]
                assert addrs and all(a == "127.0.0.1" for a in addrs), addrs
                loop = asyncio.get_running_loop()

                def patch(path: str, body: Any = None, *, session_key: Any = None) -> Any:
                    async def send() -> Any:
                        sent.append(body)
                        headers = {"X-Internal-Secret": "test", "X-Session-Key": "dashboard:t"}
                        response = await client.patch(path, json=body, headers=headers)
                        return await response.json()

                    return asyncio.run_coroutine_threadsafe(send(), loop).result(timeout=30)

                monkeypatch.setattr(mcp_core, "_patch", patch)
                for args in calls:
                    replies.append(
                        await asyncio.wait_for(
                            asyncio.to_thread(mcp_core._call_tool, "artifact_update", args),
                            timeout=30,
                        )
                    )

        asyncio.run(main())
        return replies, sent

    @pytest.mark.parametrize(
        "args",
        [
            # What a client that drops empty lists delivers for ``tags: []`` beside
            # the flag: the flag alone.
            {"clear_tags": True},
            {"clear_tags": True, "tags": []},
            # Beside another field, the case where a dropped ``[]`` was silently lost.
            {"clear_tags": True, "name": "Final"},
        ],
    )
    def test_clear_tags_empties_the_tags(
        self, isolated_store, patch_restricted, monkeypatch, args
    ) -> None:
        isolated_store.create(name="Report", content="# notes", slug="report", tags=["q3", "draft"])

        (reply,), sent = self._tool_calls(monkeypatch, {"slug": "report", **args})

        assert reply.startswith("Updated artifact"), reply
        assert sent == [{"tags": [], **({"name": "Final"} if "name" in args else {})}]
        stored = isolated_store.get("report")
        assert stored.tags == []
        assert stored.name == ("Final" if "name" in args else "Report")

    @pytest.mark.parametrize("flag", [{}, {"clear_tags": False}])
    def test_a_tag_list_still_replaces_the_tags(
        self, isolated_store, patch_restricted, monkeypatch, flag
    ) -> None:
        isolated_store.create(name="Report", content="# notes", slug="report", tags=["q3", "draft"])

        (reply,), sent = self._tool_calls(
            monkeypatch, {"slug": "report", "tags": ["final"], **flag}
        )

        assert reply.startswith("Updated artifact"), reply
        assert sent == [{"tags": ["final"]}]
        assert isolated_store.get("report").tags == ["final"]

    def test_clear_tags_beside_tags_is_refused_before_anything_is_sent(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        isolated_store.create(name="Report", content="# notes", slug="report", tags=["q3", "draft"])

        (reply,), sent = self._tool_calls(
            monkeypatch, {"slug": "report", "tags": ["final"], "clear_tags": True}
        )

        assert reply.startswith("Error: clear_tags"), reply
        assert sent == []
        assert isolated_store.get("report").tags == ["q3", "draft"]


class TestUpdateDocumentType:
    """The artifact page's type control -- ``PATCH {"kind": ...}``.

    The control exists because some types cannot be inferred from content:
    markdown and plain text accept identical bytes and differ only in whether
    ``#`` means "heading" or a literal hash. That is intent, so it has to be
    stated rather than sniffed.
    """

    @pytest.mark.asyncio
    async def test_accepts_a_selectable_kind(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="# hi", slug="x", kind="markdown")
        resp = await api_artifact_update(_request(body={"kind": "text"}, match={"slug": "x"}))
        assert resp.status == 200
        assert _json_body(resp)["kind"] == "text"
        assert isolated_store.get("x").kind == "text"

    @pytest.mark.asyncio
    async def test_choosing_a_kind_does_not_bump_the_version(
        self, isolated_store, patch_restricted
    ) -> None:
        """Re-typing is a render change, not a content edit."""
        isolated_store.create(name="x", content="# hi", slug="x", kind="markdown")
        resp = await api_artifact_update(_request(body={"kind": "text"}, match={"slug": "x"}))
        assert _json_body(resp)["version"] == 1

    @pytest.mark.asyncio
    async def test_choosing_a_kind_pins_it_against_auto_detect(
        self, isolated_store, patch_restricted
    ) -> None:
        """The whole point of the control: a stated type must stick.

        A blank document is born auto-typed, so without pinning the next save
        would re-detect and silently overrule what the user chose.
        """
        isolated_store.create(name="x", content="", slug="x")
        assert isolated_store.get("x").kind_auto is True
        await api_artifact_update(_request(body={"kind": "text"}, match={"slug": "x"}))
        assert isolated_store.get("x").kind_auto is False
        # JSON content would normally re-type an auto-typed document.
        isolated_store.update("x", content='{"a": 1}')
        assert isolated_store.get("x").kind == "text"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["widget", "html", "webapp"])
    async def test_refuses_kinds_with_no_editor(
        self, isolated_store, patch_restricted, kind: str
    ) -> None:
        """``widget`` / ``html`` render in a sandboxed iframe with no editor, and
        ``webapp`` carries deploy metadata -- selecting one would strand the
        document the user is typing in."""
        isolated_store.create(name="x", content="# hi", slug="x", kind="markdown")
        resp = await api_artifact_update(_request(body={"kind": kind}, match={"slug": "x"}))
        assert resp.status == 400
        assert isolated_store.get("x").kind == "markdown"

    @pytest.mark.asyncio
    async def test_refuses_a_kind_that_is_not_a_kind(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="# hi", slug="x", kind="markdown")
        resp = await api_artifact_update(_request(body={"kind": "../etc"}, match={"slug": "x"}))
        assert resp.status == 400
        assert isolated_store.get("x").kind == "markdown"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", [[], {}, 7, True])
    async def test_refuses_a_non_string_kind_without_crashing(
        self, isolated_store, patch_restricted, kind
    ) -> None:
        """A JSON body can carry any type here. An unhashable one raises
        TypeError on the set lookup -- a 500 where the caller deserves a 400."""
        isolated_store.create(name="x", content="# hi", slug="x", kind="markdown")
        resp = await api_artifact_update(_request(body={"kind": kind}, match={"slug": "x"}))
        assert resp.status == 400
        assert isolated_store.get("x").kind == "markdown"

    @pytest.mark.asyncio
    async def test_omitting_kind_leaves_it_alone(self, isolated_store, patch_restricted) -> None:
        """A content save must not disturb the type, or every editor keystroke
        would fight the control."""
        isolated_store.create(name="x", content="# hi", slug="x", kind="markdown")
        resp = await api_artifact_update(_request(body={"content": "# bye"}, match={"slug": "x"}))
        assert resp.status == 200
        assert _json_body(resp)["kind"] == "markdown"


class TestUpdateWebappMetadata:
    """artifact_update accepts validated webapp_metadata (round-15)."""

    @pytest.mark.asyncio
    async def test_update_sets_webapp_metadata(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="app", content="draft app", slug="app", kind="webapp")
        resp = await api_artifact_update(
            _request(
                body={
                    "webapp_metadata": {
                        "deploy_target": {
                            "provider": "aws",
                            "profile": "deploy-mvp",
                            "public_url": "https://d.cloudfront.net/app/",
                        },
                        "lifecycle": {"status": "live"},
                    }
                },
                match={"slug": "app"},
            )
        )
        assert resp.status == 200
        art = isolated_store.get("app")
        assert art.webapp_metadata is not None
        assert art.webapp_metadata.deploy_target.profile == "deploy-mvp"
        assert art.webapp_metadata.lifecycle.status == "live"

    @pytest.mark.asyncio
    async def test_update_rejects_invalid_metadata_url(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="app2", content="draft", slug="app2", kind="webapp")
        resp = await api_artifact_update(
            _request(
                body={
                    "webapp_metadata": {
                        "deploy_target": {"public_url": "javascript:alert(1)"},
                    }
                },
                match={"slug": "app2"},
            )
        )
        # Bounded validator rejects non-http(s) URLs.
        assert resp.status == 400


# ── optimistic-concurrency content token ────────────────────────────────────


@pytest.mark.usefixtures("sequential_tokens")
class TestUpdateConflictToken:
    @pytest.mark.asyncio
    async def test_detail_assigns_a_pre_token_artifact_its_token(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        path = isolated_store.root / "x" / "meta.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        del meta["content_token"]
        path.write_text(json.dumps(meta), encoding="utf-8")
        body = _json_body(await api_artifact_detail(_request(match={"slug": "x"})))
        assert body["content_token"]
        assert (
            json.loads(path.read_text(encoding="utf-8"))["content_token"] == body["content_token"]
        )

    @pytest.mark.asyncio
    async def test_detail_carries_the_persisted_token(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        body = _json_body(await api_artifact_detail(_request(match={"slug": "x"})))
        assert body["content_token"] == isolated_store.get("x").content_token

    @pytest.mark.asyncio
    async def test_matching_token_saves_and_returns_the_next_token(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        token = isolated_store.get("x").content_token
        resp = await api_artifact_update(
            _request(body={"content": "v2", "expected_token": token}, match={"slug": "x"})
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["content"] == "v2"
        assert body["content_token"] == isolated_store.get("x").content_token != token

    @pytest.mark.asyncio
    async def test_stale_token_answers_409_with_the_rebase_token(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        stale = isolated_store.get("x").content_token
        isolated_store.update("x", content="newer")
        resp = await api_artifact_update(
            _request(body={"content": "stale", "expected_token": stale}, match={"slug": "x"})
        )
        assert resp.status == 409
        body = _json_body(resp)
        assert body["code"] == "artifact_conflict"
        assert body["current_token"] == isolated_store.get("x").content_token
        assert "version" not in body
        assert isolated_store.get("x").content == "newer"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["short", 42, ["x"]])
    async def test_malformed_token_is_a_400(self, isolated_store, patch_restricted, bad) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(body={"content": "v2", "expected_token": bad}, match={"slug": "x"})
        )
        assert resp.status == 400
        assert isolated_store.get("x").content == "v1"


# ── current reads off the event loop ────────────────────────────────────────


class TestCurrentReadsOffTheLoop:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler", [api_artifact_detail, api_artifact_events, api_artifact_delete]
    )
    async def test_untokened_read_runs_in_the_executor(
        self, isolated_store, patch_restricted, monkeypatch, handler
    ) -> None:
        # These reads run in the executor; the detail read of a pre-token
        # artifact persists its token to meta.json.
        isolated_store.create(name="x", content="v1", slug="x")
        path = isolated_store.root / "x" / "meta.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        del meta["content_token"]
        path.write_text(json.dumps(meta), encoding="utf-8")
        real_get = isolated_store.get
        threads: list[int] = []

        def _get(*args, **kwargs):
            threads.append(threading.get_ident())
            return real_get(*args, **kwargs)

        monkeypatch.setattr(isolated_store, "get", _get)
        resp = await handler(_request(match={"slug": "x"}))
        assert resp.status == 200
        assert threads and threading.get_ident() not in threads


# ── dashboard package bindings ──────────────────────────────────────────────
#
# A ``bound_to`` the write path accepts and no reader can ever resolve. The three
# refusals below were all 200s, found by an exploratory click-through run; the two
# acceptances beside them are what must keep working, and the layout-edit case is the
# rule for a target that dies after its binding was written.


def _package(bound_to: str) -> str:
    """The smallest valid dashboard package, bound where the caller says."""
    return json.dumps(
        {
            "kind": "dashboard",
            "bound_to": bound_to,
            "model": {
                "types": {
                    "open_prs": {
                        "type": "number",
                        "source": {"fold": "work", "path": "summary.open"},
                    }
                }
            },
            "view": {"html": '<p data-dashboard-field="open_prs"></p>'},
            "theme": {"tokens": {}},
        }
    )


@pytest.fixture
def roster(monkeypatch):
    """A roster holding exactly one crewmate, ``Atlas`` -> slug ``atlas``.

    Only config LOADING is stubbed. ``_member_names_for_slug`` -- the same helper the
    serve path asks -- runs for real, so what these tests exercise is the real
    resolution and not a stand-in for it.
    """
    from types import SimpleNamespace

    from kiro_crew.config import loader as loader_mod

    cfg = SimpleNamespace(agents={"Atlas": SimpleNamespace(member_id="")})
    monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    return cfg


def _bound_request(slug: str, bound_to: str, *, slot: object = None) -> MagicMock:
    req = _request(body={"content": _package(bound_to)}, match={"slug": slug})
    req.app["state"].get_slot.return_value = slot
    return req


@pytest.mark.usefixtures("patch_restricted", "roster")
class TestDashboardBindingTargets:
    @pytest.fixture
    def bound(self, isolated_store) -> str:
        """A stored package bound to the one crewmate the roster holds."""
        isolated_store.create(
            name="Atlas board",
            slug="board",
            kind="dashboard",
            content=_package("crewmate:atlas"),
        )
        return "board"

    @pytest.mark.asyncio
    async def test_a_rebind_to_a_crewmate_that_does_not_exist_is_refused(
        self, isolated_store, bound
    ) -> None:
        resp = await api_artifact_update(_bound_request(bound, "crewmate:someone-else"))
        assert resp.status == 400
        assert b"names no crew member" in resp.body
        # And the stored binding is untouched: a refused write stores nothing.
        content = isolated_store.get(bound).content or ""
        assert json.loads(content)["bound_to"] == "crewmate:atlas"

    @pytest.mark.asyncio
    async def test_a_rebind_to_a_session_this_gateway_does_not_know_is_refused(
        self, isolated_store, bound
    ) -> None:
        # ``get_slot`` answers None, which is the live state's own "no such session".
        resp = await api_artifact_update(_bound_request(bound, "session:does-not-exist"))
        assert resp.status == 400
        assert b"names no live session" in resp.body

    @pytest.mark.asyncio
    async def test_a_rebind_to_a_dispatched_session_is_refused(
        self, isolated_store, bound, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import card_lifecycle

        monkeypatch.setattr(card_lifecycle, "is_root_session", lambda slot: False)
        resp = await api_artifact_update(
            _bound_request(bound, "session:chat-9-1791", slot=MagicMock())
        )
        assert resp.status == 400
        assert b"dispatched or adopted" in resp.body

    @pytest.mark.asyncio
    async def test_a_slug_two_crew_members_share_is_refused_rather_than_picked(
        self, isolated_store, bound, monkeypatch
    ) -> None:
        # Slugification is lossy, so a hand-edited roster holding both spellings
        # derives ONE slug from TWO names. Accepting the binding because something
        # matched stores a page every read refuses with 409 dashboard_slug_ambiguous,
        # and picking one of the two hands a crewmate the other's page.
        from types import SimpleNamespace

        from kiro_crew.config import loader as loader_mod

        cfg = SimpleNamespace(
            agents={
                "Atlas": SimpleNamespace(member_id=""),
                "atlas": SimpleNamespace(member_id=""),
            }
        )
        monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        # Stored bound ELSEWHERE, so this write really changes the binding: a write
        # that keeps the binding it had is deliberately never re-asked.
        isolated_store.create(
            name="Shared", slug="shared", kind="dashboard", content=_package("crewmate:nyx")
        )
        resp = await api_artifact_update(_bound_request("shared", "crewmate:atlas"))
        assert resp.status == 400
        assert b"ambiguous" in resp.body
        assert b"dashboard_slug_ambiguous" in resp.body

    @pytest.mark.asyncio
    async def test_a_rebind_to_the_crewmate_on_the_roster_is_accepted(
        self, isolated_store, bound
    ) -> None:
        isolated_store.create(
            name="Second", slug="second", kind="dashboard", content=_package("crewmate:atlas")
        )
        resp = await api_artifact_update(_bound_request("second", "crewmate:atlas"))
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_rebind_to_a_root_session_is_accepted(
        self, isolated_store, bound, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import card_lifecycle

        monkeypatch.setattr(card_lifecycle, "is_root_session", lambda slot: True)
        resp = await api_artifact_update(
            _bound_request(bound, "session:chat-9-1791", slot=MagicMock())
        )
        assert resp.status == 200
        content = isolated_store.get(bound).content or ""
        assert json.loads(content)["bound_to"] == "session:chat-9-1791"

    @pytest.mark.asyncio
    async def test_a_layout_edit_still_lands_when_the_binding_is_already_dead(
        self, isolated_store
    ) -> None:
        # The ordering rule. This record was written before the check existed and names
        # a crewmate the roster does not hold -- as one whose crewmate was renamed or
        # deleted would. A write that does not CHANGE the binding is not re-asked, so
        # the page can still be re-themed and re-versioned rather than frozen.
        isolated_store.create(
            name="Orphan", slug="orphan", kind="dashboard", content=_package("crewmate:departed")
        )
        payload = json.loads(_package("crewmate:departed"))
        payload["theme"]["tokens"] = {"--panel-bg": "black"}
        resp = await api_artifact_update(
            _request(body={"content": json.dumps(payload)}, match={"slug": "orphan"})
        )
        assert resp.status == 200
        stored = json.loads(isolated_store.get("orphan").content or "")
        assert stored["theme"]["tokens"] == {"--panel-bg": "black"}
        assert stored["bound_to"] == "crewmate:departed"

    @pytest.mark.asyncio
    async def test_a_json_document_carrying_a_bound_to_field_is_not_a_binding(
        self, isolated_store
    ) -> None:
        # The check is gated on the artifact's KIND, which is also what makes the store
        # read the content as a package. A plain JSON document is nobody's dashboard.
        isolated_store.create(name="Notes", slug="notes", kind="json", content='{"a": 1}')
        resp = await api_artifact_update(
            _request(
                body={"content": json.dumps({"bound_to": "crewmate:someone-else"})},
                match={"slug": "notes"},
            )
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_create_naming_a_crewmate_that_does_not_exist_is_refused(
        self, isolated_store
    ) -> None:
        # A create declares a binding for the first time, so there is no previous one
        # to compare against and it is always checked.
        resp = await api_artifacts_create(
            _request(
                body={
                    "name": "New board",
                    "kind": "dashboard",
                    "content": _package("crewmate:someone-else"),
                }
            )
        )
        assert resp.status == 400
        assert b"names no crew member" in resp.body

    @pytest.mark.asyncio
    async def test_a_create_naming_the_crewmate_on_the_roster_is_accepted(
        self, isolated_store
    ) -> None:
        resp = await api_artifacts_create(
            _request(
                body={
                    "name": "Atlas board",
                    "kind": "dashboard",
                    "content": _package("crewmate:atlas"),
                }
            )
        )
        assert resp.status in (200, 201)

    @pytest.mark.asyncio
    async def test_a_revert_past_a_rebind_is_not_read_as_a_rebind(
        self, isolated_store, bound
    ) -> None:
        # The revert flow submits a PAST version's whole package, and
        # ``revert_package`` discards the binding in it and keeps the live one -- so a
        # rollback whose old version names a dead target must still land. Reading the
        # submitted value as a rebind would refuse a rollback over a binding the store
        # was never going to store.
        # Under the record's own token, as the route's write does: a dashboard
        # record is never written without the token of the read it was authorized
        # against, so a setup standing in for the owner's write names one too.
        isolated_store.update(
            bound,
            content=_package("crewmate:atlas"),
            snapshot=True,
            snapshot_token=isolated_store.get(bound, assign_token=True).content_token,
        )
        resp = await api_artifact_update(
            _request(
                body={
                    "content": _package("crewmate:departed"),
                    "event_type": "reverted",
                    "from_version": 1,
                },
                match={"slug": bound},
            )
        )
        assert resp.status == 200
        # And the LIVE binding is what survived, which is the store's own rule.
        stored = json.loads(isolated_store.get(bound).content or "")
        assert stored["bound_to"] == "crewmate:atlas"

    @pytest.mark.asyncio
    async def test_deeply_nested_json_in_an_opaque_text_write_does_not_crash_the_probe(
        self, isolated_store
    ) -> None:
        # The binding probe runs on EVERY update carrying content, before the record's
        # kind is known. Content that is valid JSON nested past the interpreter's limit
        # raises RecursionError out of json.loads, and RecursionError is a RuntimeError
        # rather than a ValueError -- so letting it out turned a perfectly good
        # plain-text write into an HTTP 500. The write must simply land.
        isolated_store.create(name="Opaque", slug="opaque", kind="text", content="v1")
        payload = "[" * 10000 + "0" + "]" * 10000
        resp = await api_artifact_update(
            _request(body={"content": payload}, match={"slug": "opaque"})
        )
        assert resp.status == 200
        assert isolated_store.get("opaque").content == payload

    @pytest.mark.asyncio
    async def test_the_probe_reads_no_binding_out_of_unparseable_depth(self) -> None:
        # The unit behind the route case above, so the regression is pinned at the
        # function too: the answer is "no binding here", not an exception.
        from kiro_crew.dashboard.handlers.artifacts import _package_binding

        assert _package_binding("[" * 10000 + "0" + "]" * 10000) is None

    @pytest.mark.asyncio
    async def test_a_credential_shaped_binding_is_not_echoed_back_in_the_refusal(
        self, isolated_store, bound
    ) -> None:
        # The refusal names the target so the author can see which binding was wrong,
        # and an agent can put anything in a bound_to slug. A credential-shaped one
        # must not come back verbatim in the 400 body.
        resp = await api_artifact_update(_bound_request(bound, "crewmate:AKIAIOSFODNN7EXAMPLE"))
        assert resp.status == 400
        assert b"AKIAIOSFODNN7EXAMPLE" not in resp.body
        assert b"REDACTED" in resp.body

    @pytest.mark.asyncio
    async def test_the_session_scope_refusal_is_redacted_too(self, isolated_store, bound) -> None:
        # Both scopes build their sentences through the same helper, so neither can
        # return one that was never redacted.
        resp = await api_artifact_update(_bound_request(bound, "session:AKIAIOSFODNN7EXAMPLE"))
        assert resp.status == 400
        assert b"AKIAIOSFODNN7EXAMPLE" not in resp.body
        assert b"REDACTED" in resp.body

    @pytest.mark.asyncio
    async def test_an_ordinary_slug_still_reads_back_so_the_author_sees_the_typo(
        self, isolated_store, bound
    ) -> None:
        # The control beside the two above. Redaction rewrites credential-shaped spans,
        # not identifiers, so a refusal that named no slug at all would mean the
        # redaction had eaten the message rather than the credential.
        resp = await api_artifact_update(_bound_request(bound, "crewmate:someone-else"))
        assert resp.status == 400
        assert b"someone-else" in resp.body
        assert b"REDACTED" not in resp.body

    @pytest.mark.asyncio
    async def test_converting_a_record_into_a_dashboard_is_not_a_reachable_write(
        self, isolated_store
    ) -> None:
        # The update path checks the binding only while the record STAYS a package, so
        # a markdown->dashboard PATCH would declare a binding for the first time and
        # skip the check -- if the route allowed that conversion. It does not:
        # `dashboard` is deliberately absent from USER_SELECTABLE_KINDS, so the kind
        # gate refuses it before the store is reached. Pinned here rather than argued,
        # so adding `dashboard` to that set turns THIS test red and whoever does it
        # sees that the binding check needs the no-prior-binding case first.
        from kiro_crew.artifacts import USER_SELECTABLE_KINDS

        assert "dashboard" not in USER_SELECTABLE_KINDS
        isolated_store.create(name="Prose", slug="prose", kind="markdown", content="# notes")
        resp = await api_artifact_update(
            _request(
                body={"kind": "dashboard", "content": _package("crewmate:someone-else")},
                match={"slug": "prose"},
            )
        )
        assert resp.status == 400
        assert b"kind must be one of" in resp.body
        # And the record is still prose, so no unchecked package was stored.
        assert isolated_store.get("prose").kind == "markdown"
