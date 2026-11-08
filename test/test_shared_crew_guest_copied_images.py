"""A host image an assistant reply names is copied for the owner, never for a guest.

The two transcript writers copy every local image a non-user row names into
``sessions/<stem>.attachments`` (``chat_attachments.persist_inline_images``), and the
dashboard registers the same picture as an image artifact
(``image_artifacts.register_images``). A Shared Crew guest previews session copies
through ``GET /api/file-raw`` and artifact bytes through
``GET /api/artifacts/{slug}/asset``, with no tool call, so those copies must not make
an owner's host file readable to a guest. The stored name records where the bytes came
from (``<sha16>_<name>`` for a Crew-owned root, ``<sha16>-<name>`` for anything else),
an image artifact carries ``crew_origin``, and every guest reader admits only the
Crew-origin form. The owner keeps every copy they had.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_shared_crew_guest_parity import GUEST

from conftest import make_dir_link, requires_symlinks
from kiro_crew import chat_attachments, guest_prompt_origin, image_artifacts
from kiro_crew.acp import prompt_blocks
from kiro_crew.agent_scratch import scratch_root
from kiro_crew.artifact_store.records import parse_image_metadata
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.chat_attachments import (
    ImageBudget,
    is_crew_origin_name,
    persist_inline_images,
    same_text_modulo_images,
    store_image_file,
)
from kiro_crew.shared_crew import guest_scope
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY

#: A real 1x1 PNG: the file-raw route serves only sniffed raster types.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_OTHER_PNG = _PNG + b"\x00other"
_CAP = 1 << 20
_HOST_FORM = re.compile(r"[0-9a-f]{16}-.+")
_CREW_FORM = re.compile(r"[0-9a-f]{16}_.+")
#: A stored destination in a rewritten row, angle-wrapped or bare (tmp paths and
#: stored names hold no space, paren or angle bracket).
_STORED_DEST = re.compile(r"\]\(<?([^)\s<>]+\.attachments/[^)\s<>]+)>?\)")


@pytest.fixture(autouse=True)
def _preview_on(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A data home with its sessions and upload roots, the scratch tree, and a host directory."""
    data_home = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    (data_home / "sessions").mkdir(parents=True)
    (data_home / "uploads").mkdir()
    scratch_root().mkdir(parents=True, exist_ok=True)
    (tmp_path / "hostpics").mkdir()
    with patch("kiro_crew.sel.sel", return_value=MagicMock()):
        yield data_home


def _write(path: Path, data: bytes = _PNG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _stored(text: str) -> list[Path]:
    return [Path(dest) for dest in _STORED_DEST.findall(text)]


def _sessions(home: Path) -> Path:
    return home / "sessions"


def _app(*, guest: bool, store: ArtifactStore | None = None) -> web.Application:
    """The guest arms under test, behind the identity a guest's or the owner's tab carries."""
    from kiro_crew.dashboard.handlers import api_file_raw
    from kiro_crew.dashboard.handlers.artifacts_parts.library_reads import api_artifact_asset

    @web.middleware
    async def identity(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = "guest@example.test" if guest else "owner"
        request["is_dashboard_user"] = True
        if guest:
            request[ACTOR_REQUEST_KEY] = GUEST
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = MagicMock(owner_id="owner")
    app.router.add_get("/api/file-raw", api_file_raw)
    app.router.add_get("/api/artifacts/{slug}/asset", api_artifact_asset)
    return app


async def _file_raw(path: Path, *, guest: bool) -> tuple[int, bytes]:
    async with TestClient(TestServer(_app(guest=guest))) as client:
        resp = await client.get("/api/file-raw", params={"path": str(path)})
        return resp.status, await resp.read()


async def _asset(slug: str, *, guest: bool) -> tuple[int, bytes]:
    async with TestClient(TestServer(_app(guest=guest))) as client:
        resp = await client.get(f"/api/artifacts/{slug}/asset")
        return resp.status, await resp.read()


# ── the reviewer's scenario, through both transcript writers ──


def _persist_through(writer: str, sessions: Path, text: str, variant: str) -> list[str]:
    """Persist an assistant row the way *writer* does; the persisted texts it holds."""
    if writer == "conversation_log":
        from kiro_crew.history import ConversationLog

        ConversationLog(base_dir=sessions).append("s1", "assistant", text)
        rows = [
            json.loads(line)
            for line in (sessions / "s1.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        (row,) = [r for r in rows if r.get("_type") != "metadata"]
        return [row["content"]]
    from kiro_crew.dashboard.chat_persistence import _build_message_entry_uncached

    message = {
        "role": "assistant",
        "content": text,
        "ts": "t0",
        "variants": [{"content": text}, {"content": variant}],
        "variant_idx": 0,
    }
    first = _build_message_entry_uncached(message, attachments=(sessions, "s1"))
    assert first is not None
    # The window is re-serialized on every flush: the second pass is a fixed point.
    assert _build_message_entry_uncached(message, attachments=(sessions, "s1")) == first
    assert message["content"] == first["content"]
    return [first["content"], *(v["content"] for v in first["variants"])]


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["conversation_log", "slot_save"])
async def test_a_host_image_an_assistant_row_names_is_the_owners_copy_only(
    home: Path, tmp_path: Path, writer: str
) -> None:
    """REQUIRED: copied with today's ``-`` name; a guest is refused it, the owner reads it."""
    secret = _write(tmp_path / "hostpics" / "secret.png")
    other = _write(tmp_path / "hostpics" / "other.png", _OTHER_PNG)
    texts = _persist_through(writer, _sessions(home), f"here: ![x]({secret})", f"or: ![y]({other})")

    stored = {path for text in texts for path in _stored(text)}
    assert len(stored) == (2 if writer == "slot_save" else 1)
    for path in stored:
        assert path.parent == _sessions(home) / "s1.attachments"
        assert _HOST_FORM.fullmatch(path.name), path.name
        assert not is_crew_origin_name(path.name)
        assert guest_scope.attachment_read(str(path), _CAP) is None
        assert (await _file_raw(path, guest=True))[0] == 403
        assert await _file_raw(path, guest=False) == (200, path.read_bytes())
    assert str(secret) not in " ".join(texts)


# ── copies from a Crew-owned root ──


@pytest.mark.asyncio
async def test_a_scratch_image_is_copied_with_the_host_name_and_a_guest_is_refused_it(
    home: Path,
) -> None:
    """The scratch tree holds every runtime's allocation, so it is not a Crew root."""
    shot = _write(scratch_root() / "rt-1" / "shot.png")
    (stored,) = _stored(
        persist_inline_images(f"![s]({shot})", sessions_dir=_sessions(home), stem="s1")
    )
    assert _HOST_FORM.fullmatch(stored.name), stored.name
    assert guest_scope.attachment_read(str(stored), _CAP) is None
    assert (await _file_raw(stored, guest=True))[0] == 403
    assert await _file_raw(stored, guest=False) == (200, _PNG)


def test_an_upload_directory_image_is_copied_with_the_crew_name(home: Path) -> None:
    upload = _write(home / "uploads" / "abc_up.png")
    (stored,) = _stored(
        persist_inline_images(f"![u]({upload})", sessions_dir=_sessions(home), stem="s1")
    )
    assert _CREW_FORM.fullmatch(stored.name), stored.name
    assert guest_scope.attachment_read(str(stored), _CAP) is not None


def test_the_copier_vouches_for_the_upload_directory_the_guest_reader_serves(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writer and reader take the upload root from one function, so they cannot disagree."""
    from kiro_crew.dashboard.handlers import files

    moved = tmp_path / "moved-uploads"
    moved.mkdir()
    monkeypatch.setattr(files, "_UPLOAD_DIR", moved)
    upload = _write(moved / "abc_up.png")
    assert guest_scope.attachment_read(str(upload), _CAP) is not None
    (stored,) = _stored(
        persist_inline_images(f"![u]({upload})", sessions_dir=_sessions(home), stem="s1")
    )
    assert _CREW_FORM.fullmatch(stored.name), stored.name
    # ... and the default upload directory is then a Crew root for neither.
    default = _write(home / "uploads" / "abc_old.png", _OTHER_PNG)
    assert guest_scope.attachment_read(str(default), _CAP) is None
    (old,) = _stored(
        persist_inline_images(f"![o]({default})", sessions_dir=_sessions(home), stem="s1")
    )
    assert _HOST_FORM.fullmatch(old.name), old.name


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["subdirectory", "linked-subdirectory", "linked-upload-dir"])
async def test_an_upload_directory_linked_to_a_host_directory_gives_the_owner_copy_only(
    home: Path, tmp_path: Path, shape: str
) -> None:
    """Only a file directly inside the upload directory, read pinned there, is Crew origin.

    A subdirectory is not the depth a guest is served from, and a link (a linked
    subdirectory, or the upload directory itself replaced by a link) is judged on the
    descriptor the pinned read opened, not on the spelling under the upload directory.
    """
    uploads = home / "uploads"
    secret = _write(tmp_path / "hostpics" / "secret.png")
    if shape == "subdirectory":
        through = _write(uploads / "nested" / "secret.png")
    elif shape == "linked-subdirectory":
        make_dir_link(uploads / "linked", secret.parent)
        through = uploads / "linked" / "secret.png"
    else:
        uploads.rmdir()
        make_dir_link(uploads, secret.parent)
        through = uploads / "secret.png"
    (stored,) = _stored(
        persist_inline_images(f"![x]({through})", sessions_dir=_sessions(home), stem="s1")
    )
    assert _HOST_FORM.fullmatch(stored.name), stored.name
    assert stored.read_bytes() == _PNG
    assert guest_scope.attachment_read(str(stored), _CAP) is None
    assert (await _file_raw(stored, guest=True))[0] == 403
    assert await _file_raw(stored, guest=False) == (200, _PNG)


def test_a_picture_the_gateway_downloaded_is_stored_with_the_crew_name(
    home: Path, tmp_path: Path
) -> None:
    download = _write(tmp_path / "downloads" / "photo.png")
    markup = store_image_file(
        str(download), sessions_dir=_sessions(home), stem="s1", budget=ImageBudget()
    )
    assert markup is not None
    (stored,) = _stored(markup)
    assert _CREW_FORM.fullmatch(stored.name), stored.name
    assert guest_scope.attachment_read(str(stored), _CAP) == (str(stored), _PNG)


def test_a_stored_copy_keeps_its_origin_in_another_session(home: Path, tmp_path: Path) -> None:
    """A ``-`` copy cannot be laundered into a ``_`` one by naming it from another session."""
    host = _write(tmp_path / "hostpics" / "secret.png")
    shot = _write(home / "uploads" / "abc_shot.png", _OTHER_PNG)
    first = persist_inline_images(
        f"![h]({host}) ![s]({shot})", sessions_dir=_sessions(home), stem="a"
    )
    a_host, a_crew = _stored(first)
    assert _HOST_FORM.fullmatch(a_host.name) and _CREW_FORM.fullmatch(a_crew.name)

    second = persist_inline_images(
        f"![h]({a_host}) ![s]({a_crew})", sessions_dir=_sessions(home), stem="b"
    )
    b_host, b_crew = _stored(second)
    assert b_host.parent == b_crew.parent == _sessions(home) / "b.attachments"
    assert _HOST_FORM.fullmatch(b_host.name), b_host.name
    assert _CREW_FORM.fullmatch(b_crew.name), b_crew.name
    assert guest_scope.attachment_read(str(b_host), _CAP) is None
    assert guest_scope.attachment_read(str(b_crew), _CAP) is not None
    # Re-persisting a row that already names its own session's copies changes nothing.
    assert persist_inline_images(second, sessions_dir=_sessions(home), stem="b") == second


@pytest.mark.parametrize(
    "basename",
    ["0123456789abcdef_pic.png", "_x.png", "__a.png", "._b.png", "-x.png", "~x.png", "a b.png"],
)
def test_no_source_basename_can_forge_the_crew_name(
    home: Path, tmp_path: Path, basename: str
) -> None:
    host = _write(tmp_path / "hostpics" / basename)
    assert not chat_attachments._safe_name(basename).startswith("_")
    (stored,) = _stored(
        persist_inline_images(f"![x](<{host}>)", sessions_dir=_sessions(home), stem="s1")
    )
    assert _HOST_FORM.fullmatch(stored.name), stored.name
    assert not is_crew_origin_name(stored.name)
    assert guest_scope.attachment_read(str(stored), _CAP) is None


def test_both_name_forms_still_corroborate_an_id_matched_pair(home: Path, tmp_path: Path) -> None:
    host = _write(tmp_path / "hostpics" / "secret.png")
    shot = _write(home / "uploads" / "abc_shot.png", _OTHER_PNG)
    raw = f"![h]({host}) and ![s]({shot})"
    persisted = persist_inline_images(raw, sessions_dir=_sessions(home), stem="s1")
    assert [p.name[16] for p in _stored(persisted)] == ["-", "_"]
    assert same_text_modulo_images(raw, persisted, sessions_dir=_sessions(home), stem="s1")


# ── a legacy (``-``) name, at every guest reader ──


def _legacy_names() -> list[str]:
    return [
        "0123456789abcdef-pic.png",
        "0123_pic.png",
        "pic.png",
        "0123456789ABCDEF_pic.png",
    ]


@pytest.mark.parametrize("name", _legacy_names())
def test_a_legacy_session_copy_is_refused_by_the_guest_attachment_read(
    home: Path, name: str
) -> None:
    legacy = _write(_sessions(home) / "s1.attachments" / name)
    assert guest_scope.attachment_read(str(legacy), _CAP) is None
    assert not guest_scope._is_plain_store_file(str(legacy))


@requires_symlinks
def test_a_crew_name_linking_to_a_host_origin_copy_in_the_same_store_is_refused(
    home: Path,
) -> None:
    """A link stays inside the store yet names a ``-`` copy: the reader judges the entry, not
    where it leads, as the write checkpoint does."""
    store = _sessions(home) / "s1.attachments"
    secret = _write(store / "0123456789abcdef-secret.png")
    alias = store / "0123456789abcdef_alias.png"
    alias.symlink_to(secret.name)
    assert guest_scope.attachment_read(str(alias), _CAP) is None
    assert not guest_scope._is_plain_store_file(str(alias))


@pytest.mark.parametrize(
    ("name", "kinds"),
    [("0123456789abcdef-pic.png", ["text"]), ("0123456789abcdef_pic.png", ["text", "image"])],
)
def test_a_guest_prompt_attaches_only_a_crew_origin_session_copy(
    home: Path, name: str, kinds: list[str]
) -> None:
    kept = _write(_sessions(home) / "s1.attachments" / name)
    token = guest_prompt_origin.bind(True)
    try:
        blocks = prompt_blocks.build_prompt_blocks(f"look at {kept}")
    finally:
        guest_prompt_origin.reset(token)
    assert [b["type"] for b in blocks] == kinds


@pytest.mark.parametrize(
    ("accepts", "cap", "note"),
    [
        (True, 16, prompt_blocks.IMAGE_SIZE_NOTE),
        (False, None, prompt_blocks.IMAGE_UNSUPPORTED_NOTE),
    ],
    ids=["over-the-cap", "agent-takes-no-pictures"],
)
def test_a_guest_store_picture_left_out_of_the_prompt_says_why(
    home: Path, accepts: bool, cap: int | None, note: str
) -> None:
    """A store copy the guest may name but that is not attached gets the owner's note."""
    kept = _write(_sessions(home) / "s1.attachments" / "0123456789abcdef_pic.png")
    token = guest_prompt_origin.bind(True)
    try:
        kwargs: dict[str, Any] = {"agent_accepts_image": accepts}
        if cap is not None:
            kwargs["max_image_bytes"] = cap
        blocks = prompt_blocks.build_prompt_blocks(f"look at {kept}", **kwargs)
    finally:
        guest_prompt_origin.reset(token)
    assert [b["type"] for b in blocks] == ["text"]
    assert note in blocks[0]["text"]


def test_a_guest_prompt_naming_a_host_picture_gets_no_note(home: Path, tmp_path: Path) -> None:
    """A path outside the stores is not looked at, so nothing is said about it either."""
    host = _write(tmp_path / "host" / "pic.png")
    token = guest_prompt_origin.bind(True)
    try:
        blocks = prompt_blocks.build_prompt_blocks(f"look at {host}", max_image_bytes=16)
    finally:
        guest_prompt_origin.reset(token)
    assert [b["type"] for b in blocks] == ["text"]
    assert "[image not attached" not in blocks[0]["text"]


def _checkpoint_app(seen: list[Any]) -> web.Application:
    from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware

    @web.middleware
    async def identity(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = "guest@example.test"
        request["is_dashboard_user"] = True
        request[ACTOR_REQUEST_KEY] = GUEST
        return await handler(request)

    async def create_schedule(request: web.Request) -> web.Response:
        seen.append(await request.json())
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[identity, slot_ownership_middleware])
    app["state"] = MagicMock(owner_id="owner", _slots={})
    app.router.add_post("/api/crons", create_schedule)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "status"),
    [("0123456789abcdef-pic.png", 403), ("0123456789abcdef_pic.png", 200)],
)
async def test_a_guest_schedule_naming_a_host_origin_copy_is_refused_at_the_checkpoint(
    home: Path, name: str, status: int
) -> None:
    """A scheduled run reads its prompt's paths as the owner, so the checkpoint is the guard."""
    kept = _write(_sessions(home) / "s1.attachments" / name)
    body = {"name": "j", "message": f"every morning describe {kept}", "every": 3600}
    seen: list[Any] = []
    async with TestClient(TestServer(_checkpoint_app(seen))) as client:
        resp = await client.post("/api/crons", json=body)
        payload = await resp.json()
    assert resp.status == status
    if status == 403:
        assert payload["code"] == "owner_only"
        assert seen == []
    else:
        assert seen == [body]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "private.p\u200bng",  # the handler's sanitize_string drops the zero-width space
        "private\u2014image.png",  # the turn builder folds the em dash to "--"
        "private\u2011image.png",  # the member view maps the dash (Pd) to "-"
    ],
    ids=["zero-width-space", "em-dash", "non-breaking-hyphen"],
)
async def test_a_guest_schedule_naming_a_host_image_in_a_folded_spelling_is_refused(
    home: Path, name: str
) -> None:
    """The checkpoint judges the text as the readers that sanitize and fold it will."""
    # A native absolute path outside both stores, so the platform's own grammar names it.
    spelling = str(home.parent / "host" / name)
    body = {"name": "j", "message": f"every morning describe {spelling}", "every": 3600}
    seen: list[Any] = []
    async with TestClient(TestServer(_checkpoint_app(seen))) as client:
        resp = await client.post("/api/crons", json=body)
        payload = await resp.json()
    assert resp.status == 403
    assert payload["code"] == "owner_only"
    assert seen == []


@pytest.mark.asyncio
async def test_a_guest_body_is_judged_off_the_event_loop(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normalizing and path-scanning a large body must not stall every other request."""
    import threading

    threads: list[threading.Thread] = []
    real = guest_scope._values_name_outside_image

    def recording(values: list[Any]) -> bool:
        threads.append(threading.current_thread())
        return real(values)

    monkeypatch.setattr(guest_scope, "_values_name_outside_image", recording)
    host = str(home.parent / "host" / "x.png")
    body = {"name": "j", "message": f"every morning describe {host}", "every": 3600}
    seen: list[Any] = []
    async with TestClient(TestServer(_checkpoint_app(seen))) as client:
        resp = await client.post("/api/crons", json=body)
    assert resp.status == 403
    assert threads and all(t is not threading.main_thread() for t in threads)


@pytest.mark.asyncio
async def test_a_guest_body_with_more_image_text_than_one_check_scans_is_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guest_scope, "_IMAGE_SCAN_BUDGET_CHARS", 64)
    body = {"name": "j", "message": "see a.png " * 10, "every": 3600}
    seen: list[Any] = []
    async with TestClient(TestServer(_checkpoint_app(seen))) as client:
        resp = await client.post("/api/crons", json=body)
    assert resp.status == 403
    assert seen == []


@pytest.mark.asyncio
async def test_a_long_guest_body_that_spells_no_image_suffix_skips_the_path_scan(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slash runs that make the path scan slow are skipped when no suffix exists."""
    monkeypatch.setattr(guest_scope, "_IMAGE_SCAN_BUDGET_CHARS", 64)
    body = {"name": "j", "message": " /" * 200_000, "every": 3600}
    seen: list[Any] = []
    async with TestClient(TestServer(_checkpoint_app(seen))) as client:
        resp = await client.post("/api/crons", json=body)
    # Over the budget, but never counted against it: nothing in it can name an image.
    assert resp.status == 200
    assert seen == [body]


@pytest.mark.parametrize(
    "text",
    [
        "/srv/x.p\u200bng",
        "/srv/x\uff0epng",  # a fullwidth full stop, which NFKC folds to "."
        "/srv/X.PNG",
        "![a](b)",
    ],
)
def test_the_prefilter_keeps_every_suffix_a_reader_view_could_spell(text: str) -> None:
    assert guest_scope._may_name_an_image(text)


def test_the_prefilter_drops_text_that_spells_no_image() -> None:
    assert not guest_scope._may_name_an_image("plain words / and slashes / " * 50)


@pytest.mark.asyncio
async def test_a_guest_schedule_naming_a_store_copy_with_a_hidden_character_is_admitted(
    home: Path,
) -> None:
    """A fold that lands on a Crew-origin store file is still the guest's own picture."""
    kept = _write(_sessions(home) / "s1.attachments" / "0123456789abcdef_pic.png")
    spelled = str(kept)[:-3] + "p\u200bng"
    body = {"name": "j", "message": f"every morning describe {spelled}", "every": 3600}
    seen: list[Any] = []
    async with TestClient(TestServer(_checkpoint_app(seen))) as client:
        resp = await client.post("/api/crons", json=body)
    assert resp.status == 200
    assert seen == [body]


# ── image artifacts: the sibling copier ──


@pytest.fixture
def store(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    from kiro_crew.dashboard.handlers import artifacts as artifact_handlers

    artifacts = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(image_artifacts, "get_default_store", lambda: artifacts)
    monkeypatch.setattr(artifact_handlers, "get_default_store", lambda: artifacts)
    return artifacts


@pytest.mark.asyncio
async def test_a_host_image_artifact_is_served_to_the_owner_and_not_to_a_guest(
    store: ArtifactStore, tmp_path: Path
) -> None:
    secret = _write(tmp_path / "hostpics" / "secret.png")
    (slug,) = image_artifacts.register_images(f"![x]({secret})", "ts-host", "dashboard:s1")
    image = store.get(slug).image
    assert image is not None and image.crew_origin is False
    assert (await _asset(slug, guest=True))[0] == 404
    assert await _asset(slug, guest=False) == (200, _PNG)


@pytest.mark.asyncio
async def test_a_scratch_image_artifact_is_the_owners_only(store: ArtifactStore) -> None:
    shot = _write(scratch_root() / "rt-1" / "shot.png")
    (slug,) = image_artifacts.register_images(f"![s]({shot})", "ts-scratch", "dashboard:s1")
    image = store.get(slug).image
    assert image is not None and image.crew_origin is False
    assert (await _asset(slug, guest=True))[0] == 404
    assert await _asset(slug, guest=False) == (200, _PNG)


@pytest.mark.asyncio
async def test_an_upload_directory_image_artifact_is_served_to_a_guest(
    store: ArtifactStore, home: Path
) -> None:
    upload = _write(home / "uploads" / "abc_up.png")
    (slug,) = image_artifacts.register_images(f"![u]({upload})", "ts-upload", "dashboard:s1")
    image = store.get(slug).image
    assert image is not None and image.crew_origin is True
    assert await _asset(slug, guest=True) == (200, _PNG)
    assert await _asset(slug, guest=False) == (200, _PNG)


@pytest.mark.asyncio
async def test_an_image_artifact_written_before_the_flag_is_the_owners(
    store: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.handlers import artifacts as artifact_handlers

    art = store.create_image(name="Pic", image_bytes=_PNG, mime="image/png", crew_origin=True)
    meta_path = tmp_path / "artifacts" / art.slug / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["image"].pop("crew_origin") is True
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    # A fresh store, so nothing the first one remembers stands in for the file.
    legacy = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(artifact_handlers, "get_default_store", lambda: legacy)
    image = legacy.get(art.slug).image
    assert image is not None and image.crew_origin is False
    assert (await _asset(art.slug, guest=True))[0] == 404
    assert await _asset(art.slug, guest=False) == (200, _PNG)


@pytest.mark.parametrize("value", [None, "true", 1, "yes"])
def test_only_a_literal_true_marks_an_image_crew_origin(value: object) -> None:
    raw: dict[str, Any] = {"mime": "image/png"}
    if value is not None:
        raw["crew_origin"] = value
    image = parse_image_metadata(raw)
    assert image is not None and image.crew_origin is False
    marked = parse_image_metadata({"mime": "image/png", "crew_origin": True})
    assert marked is not None and marked.crew_origin is True
