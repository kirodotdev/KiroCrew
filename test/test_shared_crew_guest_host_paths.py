"""A Shared Crew guest never names a location on the host's disk.

One checkpoint (``guest_scope.guest_host_path_denial``, run by the slot-ownership
middleware) refuses a guest write whose query or body holds a field of
``guest_scope.HOST_PATH_FIELDS``, and a sweep fails when a guest-open write handler
reads a path-like field that is neither in that list nor reviewed here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_shared_crew_guest_parity import GUEST

from kiro_crew.shared_crew import guest_scope
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY

# ── host-path fields: one checkpoint for the class ──


@pytest.fixture(autouse=True)
def _preview_on(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")


def _guest_checkpoint_app(*, guest: bool, seen: dict[str, Any]) -> web.Application:
    from kiro_crew.dashboard.handlers._shared_request_body import read_bounded_json
    from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware

    @web.middleware
    async def mw(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = "guest@example.test"
        request["is_dashboard_user"] = True
        if guest:
            request[ACTOR_REQUEST_KEY] = GUEST
        else:
            request["internal_auth"] = True
        return await handler(request)

    async def bounded(request: web.Request) -> web.Response:
        body, err = await read_bounded_json(request, max_bytes=1 << 20)
        seen["bounded"] = body
        return err or web.json_response({"ok": True})

    async def whole(request: web.Request) -> web.Response:
        seen["json"] = await request.json()
        return web.json_response({"ok": True})

    async def upload(request: web.Request) -> web.Response:
        seen["upload"] = [part.name async for part in await request.multipart()]
        return web.json_response({"ok": True})

    async def post_fields(request: web.Request) -> web.Response:
        form = await request.post()
        seen["post"] = {k: v if isinstance(v, str) else "<file>" for k, v in form.items()}
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[mw, slot_ownership_middleware])
    app["state"] = MagicMock(owner_id="owner", _slots={})
    app.router.add_post("/api/x/post", post_fields)
    app.router.add_post("/api/x/bounded", bounded)
    app.router.add_post("/api/x/whole", whole)
    app.router.add_put("/api/x/whole", whole)
    app.router.add_post("/api/x/upload", upload)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"source_path": "/srv/owner/notes.md", "content": "x"},
        {"spec": "/srv/owner/TASK.md"},
        {"spec": "//attacker/share/TASK.md", "source": "text", "input": "hi"},
        {"workspace_dir": "\\\\attacker\\share\\work"},
        {"workspace_dir": "/srv/owner"},
        {"path": "/srv/owner/.ssh/config"},
        {"project_dir": "/srv/owner/code"},
        {"project": "/srv/owner/private-repo"},
        {"cwd": "/"},
        {"Source_Path": "/etc/hosts"},
        {"steering_dirs": ["/srv/owner/steer"]},
    ],
)
async def test_a_guest_write_naming_a_host_path_is_refused_at_the_checkpoint(
    body: dict[str, Any],
) -> None:
    """The class, once: whatever the route, a host-path field is owner-only."""
    seen: dict[str, Any] = {}
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        for route in ("/api/x/bounded", "/api/x/whole"):
            resp = await client.post(route, json=body)
            payload = await resp.json()
            assert (resp.status, payload["code"]) == (403, "owner_only"), (route, body)
    assert seen == {}, "the handler must not run"


@pytest.mark.asyncio
async def test_a_guest_host_path_in_the_query_or_a_form_or_a_plain_text_body_is_refused() -> None:
    seen: dict[str, Any] = {}
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        responses = [
            await client.post("/api/x/bounded?workspace_dir=/srv/owner", json={}),
            await client.put("/api/x/whole?path=/etc/hosts", data=b""),
            await client.post("/api/x/whole", data={"source_path": "/etc/hosts"}),
            await client.post(
                "/api/x/whole",
                data=b'{"spec": "/etc/hosts"}',
                headers={"Content-Type": "text/plain"},
            ),
        ]
    assert [r.status for r in responses] == [403, 403, 403, 403]
    assert seen == {}


@pytest.mark.asyncio
async def test_a_guest_keeps_typing_task_text_and_uploading_attachments() -> None:
    """Content is not a location: inline specs, task text, attachment objects and uploads pass."""
    seen: dict[str, Any] = {}
    bodies = [
        {"spec": "__inline__:# Task\nwrite /srv/owner/x", "input": "hi", "workspace_dir": ""},
        {"input": "read /etc/hosts", "source": "text", "workspace_dir": None, "path": ""},
        {"text": "see this", "attachments": [{"path": "/tmp/uploads/a.png", "name": "a.png"}]},
        ["not", "an", "object"],
    ]
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        for body in bodies:
            seen.clear()
            resp = await client.post("/api/x/bounded", json=body)
            assert resp.status in (200, 400), body
            seen.clear()
            resp = await client.post("/api/x/whole", json=body)
            assert resp.status == 200, body
            # The handler reads the body the checkpoint already read, unchanged.
            assert seen["json"] == body
        seen.clear()
        form = aiohttp.FormData()
        form.add_field("file", b"png-bytes", filename="a.png", content_type="image/png")
        resp = await client.post("/api/x/upload", data=form)
        assert resp.status == 200
        assert seen["upload"] == ["file"]
        seen.clear()
        resp = await client.post("/api/x/bounded", json={"input": "t"})
        assert (resp.status, seen["bounded"]) == (200, {"input": "t"})


@pytest.mark.asyncio
async def test_the_owner_is_not_held_to_the_host_path_fields() -> None:
    seen: dict[str, Any] = {}
    async with TestClient(TestServer(_guest_checkpoint_app(guest=False, seen=seen))) as client:
        resp = await client.post("/api/x/whole", json={"source_path": "/srv/owner/n.md"})
        assert resp.status == 200
    assert seen["json"] == {"source_path": "/srv/owner/n.md"}


@pytest.mark.asyncio
async def test_a_guest_body_over_the_apps_size_limit_is_answered_413() -> None:
    seen: dict[str, Any] = {}
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        resp = await client.post(
            "/api/x/whole",
            data=b'{"pad": "' + b"a" * (2 * 1024 * 1024) + b'"}',
            headers={"Content-Type": "application/json"},
        )
    assert resp.status == 413
    assert seen == {}


def test_the_host_path_list_names_the_fields_the_findings_were_about() -> None:
    assert {
        "source_path",
        "spec",
        "spec_path",
        "workspace_dir",
        "project_dir",
        "path",
        "cwd",
    } <= guest_scope.HOST_PATH_FIELDS


#: Path-like fields a guest-open write handler reads that are NOT host locations, each
#: with the reason. Anything else path-like must be in ``HOST_PATH_FIELDS``.
_REVIEWED_NOT_HOST_PATHS = {
    "filename": "a download's suggested basename; the handler writes nothing from it",
    "source_uri": "an identifier recorded on a knowledge document; never opened",
    "workspace": "a registered workspace's NAME, looked up in the owner's registry; never opened",
    "repo_scope": "a lesson's scope label in the memory store; never opened as a path",
    "include_project": "a boolean flag on a spawn; carries no location",
}

_PATH_TOKENS = frozenset(
    {
        "path",
        "paths",
        "dir",
        "dirs",
        "directory",
        "file",
        "files",
        "filename",
        "root",
        "cwd",
        "spec",
        "yaml",
        "workdir",
        "uri",
        "location",
        "project",
        "repo",
        "workspace",
        "home",
    }
)
_FIELD_READ = re.compile(
    r"(?:\b(?:body|data|payload|params|j|req|raw)\b|query)(?:\.get\(|\[)\s*[\"']([A-Za-z_]+)[\"']"
)


def _is_path_like(name: str) -> bool:
    return bool(_PATH_TOKENS & set(name.lower().split("_")))


@pytest.mark.asyncio
async def test_every_guest_open_write_handler_field_is_classified_as_host_path_or_reviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Walk every guest-open write route's handler for the fields it reads.

    A path-like field must be refused at the checkpoint (``HOST_PATH_FIELDS``) or be
    listed above with the reason it names no location. A new route that reads
    ``body["log_dir"]`` fails here until someone decides which.
    """
    import inspect

    from test_dashboard_server_startup_coverage import _dashboard

    unclassified: dict[str, set[str]] = {}
    walked = 0
    async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
        for route in runner.app.router.routes():
            if route.resource is None or route.method in ("HEAD", "OPTIONS", "GET"):
                continue
            path = route.resource.canonical
            if guest_scope.is_owner_only_path(path, route.method):
                continue
            handler = route.handler
            while hasattr(handler, "__wrapped__"):
                handler = handler.__wrapped__
            try:
                source = inspect.getsource(handler)
            except (OSError, TypeError):
                continue
            walked += 1
            names = {n.lower() for n in _FIELD_READ.findall(source) if _is_path_like(n)}
            names -= guest_scope.HOST_PATH_FIELDS | set(_REVIEWED_NOT_HOST_PATHS)
            if names:
                unclassified[f"{route.method} {path}"] = names
    assert walked > 100
    assert unclassified == {}


def test_the_reviewed_not_host_path_fields_are_not_also_refused() -> None:
    assert not set(_REVIEWED_NOT_HOST_PATHS) & guest_scope.HOST_PATH_FIELDS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    ["image/png", "audio/mpeg", "video/mp4", "application/octet-stream", "text/plain"],
)
async def test_a_guest_host_path_in_a_body_labelled_with_any_type_is_refused(
    content_type: str,
) -> None:
    """A handler's ``request.json()`` parses whatever the client labelled the body."""
    seen: dict[str, Any] = {}
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        resp = await client.post(
            "/api/x/whole",
            data=b'{"project_dir": "/srv/owner/code"}',
            headers={"Content-Type": content_type},
        )
        payload = await resp.json()
        assert (resp.status, payload["code"]) == (403, "owner_only")
        resp = await client.post(
            "/api/x/whole",
            data=b'{"input": "hello"}',
            headers={"Content-Type": content_type},
        )
        assert resp.status == 200
    assert seen["json"] == {"input": "hello"}


@pytest.mark.parametrize(
    "field", ["root", "project_dir", "cwd", "paths", "file", "source_path", "workspace_dir"]
)
def test_the_host_path_list_covers_every_field_the_findings_named(field: str) -> None:
    assert field in guest_scope.HOST_PATH_FIELDS


def test_an_upload_directory_swapped_for_a_symlink_serves_a_guest_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store root is lstat-ed unresolved: a link there is refused, not followed."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "sessions").mkdir(parents=True)
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "pic.png").write_bytes(b"png")
    uploads = tmp_path / "home" / "uploads"
    uploads.mkdir()
    (uploads / "pic.png").write_bytes(b"png")
    assert guest_scope.attachment_read(str(uploads / "pic.png"), 1024) is not None

    (uploads / "pic.png").unlink()
    uploads.rmdir()
    uploads.symlink_to(real, target_is_directory=True)
    assert guest_scope.attachment_read(str(uploads / "pic.png"), 1024) is None
    assert guest_scope.attachment_read(str(real / "pic.png"), 1024) is None


def test_a_missing_upload_directory_does_not_hide_a_valid_session_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upload directory exists only after the first upload; the other store still serves."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    attachments = tmp_path / "home" / "sessions" / "s1.attachments"
    attachments.mkdir(parents=True)
    (attachments / "pic.png").write_bytes(b"png")
    assert not (tmp_path / "home" / "uploads").exists()
    result = guest_scope.attachment_read(str(attachments / "pic.png"), 1024)
    assert result is not None and result[1] == b"png"


def test_a_replaced_store_directory_is_skipped_and_the_other_store_still_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    elsewhere = tmp_path / "elsewhere" / "s1.attachments"
    elsewhere.mkdir(parents=True)
    (elsewhere / "pic.png").write_bytes(b"png")
    uploads = tmp_path / "home" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "pic.png").write_bytes(b"png")
    # sessions/ replaced by a link to another tree: nothing under it is served.
    (tmp_path / "home" / "sessions").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    assert guest_scope.attachment_read(str(elsewhere / "pic.png"), 1024) is None
    assert (
        guest_scope.attachment_read(
            str(tmp_path / "home" / "sessions" / "s1.attachments" / "pic.png"), 1024
        )
        is None
    )
    # ... while the intact upload directory keeps serving.
    assert guest_scope.attachment_read(str(uploads / "pic.png"), 1024) is not None


def _bad_attachment_shapes(data_home: Path) -> list[str]:
    uploads = str(data_home / "uploads")
    sessions = str(data_home / "sessions")
    return [
        f"{uploads}/../x.png",
        f"{uploads}/./x.png",
        f"{uploads}//x.png",
        f"{uploads}/",
        f"{uploads}/sub/x.png",
        f"{uploads}\\..\\x.png",
        f"{uploads}/x.png:stream",
        f"{uploads}/\x01.png",
        f"{sessions}/s1.attachments/../x.png",
        f"{sessions}/s1/x.png",
        f"{sessions}/.attachments/x.png",
        f"{sessions}/s1.attachments/sub/x.png",
        f"{sessions}/s1.attachments",
        "C:/Windows/win.ini",
        "C:\\Windows\\win.ini",
        "/etc/hosts",
        "x.png",
        "~/x.png",
        "x\x00.png",
        "//attacker/share/x.png",
        "\\\\?\\UNC\\host\\share\\x.png",
    ]


def test_a_guest_attachment_name_of_a_bad_shape_is_refused_without_touching_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_home = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    shapes = _bad_attachment_shapes(data_home)
    server_known = {data_home, data_home / "uploads", data_home / "sessions", *data_home.parents}
    touched: list[Path] = []

    def recording(real: Any) -> Any:
        def call(path: Any, *a: Any, **kw: Any) -> Any:
            touched.append(Path(path))
            return real(path, *a, **kw)

        return call

    with (
        patch.object(guest_scope.os, "lstat", recording(guest_scope.os.lstat)),
        patch.object(guest_scope.os, "open", recording(guest_scope.os.open)),
        patch.object(guest_scope.os.path, "realpath", recording(guest_scope.os.path.realpath)),
    ):
        for raw in shapes:
            assert guest_scope.attachment_read(raw, 1024) is None, raw
    # The server resolves its own data home; no name the guest sent is looked at.
    assert set(touched) <= server_known


def test_a_plain_store_name_still_previews(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "uploads").mkdir(parents=True)
    (tmp_path / "home" / "sessions" / "s1.attachments").mkdir(parents=True)
    upload = tmp_path / "home" / "uploads" / "abc_pic.png"
    kept = tmp_path / "home" / "sessions" / "s1.attachments" / "pic.png"
    for path in (upload, kept):
        path.write_bytes(b"png")
        assert guest_scope.attachment_read(str(path), 1024) == (str(path), b"png")


def _multipart(boundary: str, *parts: tuple[str, str | None, bytes]) -> bytes:
    """A multipart body: ``(field name, filename or None, content)`` parts."""
    out = b""
    for name, filename, content in parts:
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        out += f"--{boundary}\r\nContent-Disposition: {disposition}\r\n\r\n".encode()
        out += content + b"\r\n"
    return out + f"--{boundary}--\r\n".encode()


_JSON_PATH = b'{"project_dir": "/srv/owner/code"}'
_UPLOAD = _multipart("XbX", ("file", "a.png", b"\x89PNG" + b"0" * 5000))
_HIDDEN = _multipart("XbX", ("file", "a.png", b"png"), ("project_dir", None, b"/srv/owner"))
_NO_CONTENT_TYPE: dict[str, str] = {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (_JSON_PATH, "multipart/form-data; boundary=XbX"),
        (_JSON_PATH, "multipart/form-data"),
        (_JSON_PATH, None),
        (_HIDDEN, "multipart/form-data; boundary=XbX"),
        # Labelled with one boundary, split on another further in.
        (
            _multipart("Y", ("file", "a.png", b"junk\r\n" + _HIDDEN)),
            "multipart/form-data; boundary=XbX",
        ),
        # A path sent as a "file" part under a name that is not an upload's.
        (
            _multipart("XbX", ("workspace_dir", "x.txt", b"/srv")),
            "multipart/form-data; boundary=XbX",
        ),
    ],
)
async def test_a_host_path_the_handlers_parser_reads_is_refused_whatever_the_label(
    body: bytes, content_type: str | None
) -> None:
    seen: dict[str, Any] = {}
    headers = {"Content-Type": content_type} if content_type else {}
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        resp = await client.post(
            "/api/x/whole",
            data=body,
            headers=headers,
            skip_auto_headers=() if content_type else ("Content-Type",),
        )
        payload = await resp.json()
    assert (resp.status, payload["code"]) == (403, "owner_only")
    assert seen == {}


@pytest.mark.asyncio
async def test_a_real_file_upload_still_passes() -> None:
    seen: dict[str, Any] = {}
    big = _multipart("XbX", ("file", "big.bin", b"\0" * (900 * 1024)))
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        resp = await client.post(
            "/api/x/upload",
            data=_UPLOAD,
            headers={"Content-Type": "multipart/form-data; boundary=XbX"},
        )
        assert resp.status == 200
        assert seen["upload"] == ["file"]
        seen.clear()
        resp = await client.post(
            "/api/x/upload", data=big, headers={"Content-Type": "multipart/form-data; boundary=XbX"}
        )
        assert resp.status == 200
        # A text field that is empty is not a location.
        seen.clear()
        empty = _multipart("XbX", ("file", "a.png", b"png"), ("project_dir", None, b""))
        resp = await client.post(
            "/api/x/upload",
            data=empty,
            headers={"Content-Type": "multipart/form-data; boundary=XbX"},
        )
        assert resp.status == 200
        assert seen["upload"] == ["file", "project_dir"]


@pytest.mark.asyncio
async def test_a_body_that_parses_as_nothing_is_let_through() -> None:
    seen: dict[str, Any] = {}
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        resp = await client.post(
            "/api/x/bounded",
            data=b"\x00\x01\x02 not a document",
            headers={"Content-Type": "image/png"},
        )
    assert resp.status in (400, 415)  # the handler's own refusal, not ours


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("charset", "encode"),
    [
        ("utf-8-sig", lambda text: b"\xef\xbb\xbf" + text.encode("utf-8")),
        ("utf-16", lambda text: text.encode("utf-16")),
        ("utf-16-le", lambda text: text.encode("utf-16-le")),
        ("utf-16-be", lambda text: text.encode("utf-16-be")),
        ("utf-8", lambda text: text.encode("utf-8")),
    ],
)
async def test_a_json_body_in_any_encoding_the_handler_parses_is_judged_the_same(
    charset: str, encode: Any
) -> None:
    """The checkpoint calls ``request.json()``: whatever the handler can parse, it sees."""
    body = encode('{"project_dir": "/srv/owner/code"}')
    headers = {"Content-Type": f"application/json; charset={charset}"}
    seen: dict[str, Any] = {}
    # The owner's handler parses it (so a guest's handler would too) ...
    async with TestClient(TestServer(_guest_checkpoint_app(guest=False, seen=seen))) as client:
        assert (await client.post("/api/x/whole", data=body, headers=headers)).status == 200
    assert seen["json"] == {"project_dir": "/srv/owner/code"}
    # ... so the checkpoint refuses it for a guest.
    seen.clear()
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        resp = await client.post("/api/x/whole", data=body, headers=headers)
        assert (resp.status, (await resp.json())["code"]) == (403, "owner_only")
    assert seen == {}


@pytest.mark.asyncio
async def test_the_handler_still_receives_a_body_the_checkpoint_has_read() -> None:
    """Every way a handler reads a request gets the same bytes after the checkpoint."""
    seen: dict[str, Any] = {}
    note = {"input": "hello", "attachments": [{"path": "/tmp/uploads/a.png"}]}
    form = _multipart("XbX", ("input", None, b"hello"), ("file", "a.png", b"png-bytes"))
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        assert (await client.post("/api/x/whole", json=note)).status == 200
        assert (await client.post("/api/x/bounded", json=note)).status == 200
        assert (
            await client.post(
                "/api/x/post",
                data=form,
                headers={"Content-Type": "multipart/form-data; boundary=XbX"},
            )
        ).status == 200
        assert (
            await client.post(
                "/api/x/upload",
                data=form,
                headers={"Content-Type": "multipart/form-data; boundary=XbX"},
            )
        ).status == 200
        assert (await client.post("/api/x/post", data={"input": "hello", "n": "1"})).status == 200
    assert seen["json"] == note
    assert seen["bounded"] == note
    assert seen["upload"] == ["input", "file"]
    assert seen["post"] == {"input": "hello", "n": "1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(guest_scope._STREAMED_UPLOAD_PATHS))
async def test_a_guest_upload_past_the_apps_size_limit_is_not_buffered_at_the_checkpoint(
    route: str,
) -> None:
    """A streaming route carries more than ``client_max_size`` (a 512 MiB video)."""
    from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware

    seen: dict[str, Any] = {}

    @web.middleware
    async def mw(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = "guest@example.test"
        request["is_dashboard_user"] = True
        request[ACTOR_REQUEST_KEY] = GUEST
        return await handler(request)

    async def streaming(request: web.Request) -> web.Response:
        seen["bytes"] = sum([len(chunk) async for chunk in request.content.iter_chunked(8192)])
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[mw, slot_ownership_middleware], client_max_size=2048)
    app["state"] = MagicMock(owner_id="owner", _slots={})
    app.router.add_post(route, streaming)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(route, data=b"\0" * 20000)
        assert resp.status == 200
        # The query is still judged on these routes.
        refused = await client.post(route + "?project_dir=/srv/owner", data=b"x")
        assert refused.status == 403
    assert seen["bytes"] == 20000


@pytest.mark.asyncio
async def test_every_guest_open_streaming_handler_is_listed_as_streamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guest-open handler that streams its body must not be buffered at the checkpoint."""
    import inspect

    from test_dashboard_server_startup_coverage import _dashboard

    streaming = re.compile(r"\.multipart\(\)|request\.content\b")
    found: set[str] = set()
    registered: set[str] = set()
    async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
        for route in runner.app.router.routes():
            if route.resource is None or route.method != "POST":
                continue
            path = route.resource.canonical
            registered.add(path)
            if guest_scope.is_owner_only_path(path, route.method):
                continue
            handler = route.handler
            while hasattr(handler, "__wrapped__"):
                handler = handler.__wrapped__
            try:
                source = inspect.getsource(handler)
            except (OSError, TypeError):
                continue
            if streaming.search(source):
                found.add(path)
    assert found <= guest_scope._STREAMED_UPLOAD_PATHS
    assert guest_scope._STREAMED_UPLOAD_PATHS <= registered


@pytest.mark.asyncio
@pytest.mark.parametrize("guest", [True, False])
@pytest.mark.parametrize("shape", ["plain", "nested", "variant_text"])
async def test_a_guest_import_copies_no_host_image_into_the_session_store(
    monkeypatch: pytest.MonkeyPatch,
    _floor_monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    guest: bool,
    shape: str,
) -> None:
    """Persisting a row copies the file an image reference names into the session's store.

    A guest's imported rows hold no ``![``, so neither the import nor any later flush of
    the window (which persists the same rows again) copies a host image, including from
    markup nested so that stripping the inner reference would leave a live one.
    """
    from session_transfer_helpers import _make_request, _stub_state, _valid

    from kiro_crew.chat_attachments import attachments_dir, persist_inline_images
    from kiro_crew.dashboard import session_transfer as st

    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    host_image = tmp_path / "host" / "private.png"
    other = tmp_path / "host" / "c.png"
    host_image.parent.mkdir()
    for image in (host_image, other):
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    state = _stub_state(st, monkeypatch)
    text = {
        "plain": f"see ![pic]({host_image}) and ![web](https://example.test/a.png) ok",
        "nested": f"![![z]({other})]({host_image})",
        "variant_text": f"![a]({other}) ![![b]({other})]({host_image}) ![{host_image}]({host_image})",
    }[shape]
    body = _valid(messages=[{"role": "assistant", "content": text, "ts": ""}])
    request = _make_request(state, body)
    items: dict[str, Any] = {"app": "", "user": "g@example.test", "is_dashboard_user": True}
    if guest:
        items[ACTOR_REQUEST_KEY] = GUEST
    request.get = lambda key, default=None: items.get(key, default)
    resp = await st.api_chat_slot_import(request)
    assert resp.status == 200
    imported = state._imported_slot.messages[-1]["content"]
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    # Flushed twice, as a window is: the second pass must find nothing to copy either.
    persisted = persist_inline_images(imported, sessions_dir=sessions, stem="s1")
    persisted = persist_inline_images(persisted, sessions_dir=sessions, stem="s1")
    copied = attachments_dir(sessions, "s1").exists()
    if guest:
        assert "![" not in imported
        assert not copied and persisted == imported
    else:
        assert str(host_image) in imported
        assert copied, "the control: an owner's row does copy it"


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An upload directory and a session attachments directory, each holding a picture."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    uploads = tmp_path / "home" / "uploads"
    attachments = tmp_path / "home" / "sessions" / "s1.attachments"
    for directory in (uploads, attachments):
        directory.mkdir(parents=True)
        (directory / "pic.png").write_bytes(b"png")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "shot.png").write_bytes(b"png")
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "where",
    ["top", "nested", "list", "variant"],
)
async def test_a_guest_text_naming_a_host_image_outside_the_stores_is_refused(
    stores: Path, where: str
) -> None:
    """A row's ``![alt](/abs/path)`` is copied into the session when it is persisted."""
    markdown = f"look ![x]({stores / 'private' / 'shot.png'})"
    body = {
        "top": {"content": markdown},
        "nested": {"turn": {"parts": [{"text": markdown}]}},
        "list": {"messages": ["hi", markdown]},
        "variant": {"input": "ok", "note": f"![a](https://example.test/a.png) {markdown}"},
    }[where]
    seen: dict[str, Any] = {}
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        resp = await client.post("/api/x/whole", json=body)
        assert (resp.status, (await resp.json())["code"]) == (403, "owner_only")
        form = await client.post("/api/x/post", data={"content": markdown})
        assert form.status == 403
    assert seen == {}


@pytest.mark.asyncio
async def test_a_guest_text_may_reference_a_picture_already_in_a_store(stores: Path) -> None:
    upload = stores / "home" / "uploads" / "pic.png"
    attached = stores / "home" / "sessions" / "s1.attachments" / "pic.png"
    seen: dict[str, Any] = {}
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        for text in (
            f"see ![x]({upload}) and ![y]({attached})",
            "a remote ![z](https://example.test/a.png) and plain text",
            "no image at all",
        ):
            seen.clear()
            resp = await client.post("/api/x/whole", json={"content": text})
            assert resp.status == 200, text
            assert seen["json"] == {"content": text}


@pytest.mark.asyncio
async def test_a_guest_text_naming_a_network_image_is_never_resolved() -> None:
    """Four backslashes in markdown are the two that open a UNC path (each pair is one).

    Where the copier reads such a path (Windows), the checkpoint refuses it unresolved;
    where it does not (a relative path here), the copier leaves it alone and so do we.
    """
    from unittest.mock import patch

    seen: dict[str, Any] = {}
    sep = "\\" * 2  # one escaped pair in markdown, so the destination begins ``\\``
    markdown = "![x](" + sep * 2 + "attacker" + sep + "share" + sep + "x.png)"
    real = guest_scope.os.path.realpath
    resolved: list[str] = []

    def spy(path: Any, *args: Any, **kwargs: Any) -> str:
        resolved.append(str(path))
        return real(path, *args, **kwargs)

    with patch.object(guest_scope.os.path, "realpath", spy):
        async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
            resp = await client.post("/api/x/whole", json={"content": markdown})
    assert resp.status in (200, 403)
    assert not [path for path in resolved if "attacker" in path]


@pytest.mark.asyncio
async def test_the_owner_may_send_text_naming_any_image(stores: Path) -> None:
    seen: dict[str, Any] = {}
    markdown = f"![x]({stores / 'private' / 'shot.png'})"
    async with TestClient(TestServer(_guest_checkpoint_app(guest=False, seen=seen))) as client:
        resp = await client.post("/api/x/whole", json={"content": markdown})
        assert resp.status == 200
    assert seen["json"] == {"content": markdown}


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["#/../../uploads/pic.png", "?/../../uploads/pic.png"])
async def test_a_fragment_or_query_cannot_make_the_checked_path_differ_from_the_copied_one(
    stores: Path, suffix: str
) -> None:
    """The copier strips ``#``/``?`` and reads what is left, so that is what is judged."""
    markdown = f"![x]({stores / 'private' / 'shot.png'}{suffix})"
    seen: dict[str, Any] = {}
    async with TestClient(TestServer(_guest_checkpoint_app(guest=True, seen=seen))) as client:
        resp = await client.post("/api/x/whole", json={"content": markdown})
    assert resp.status == 403
    assert seen == {}
