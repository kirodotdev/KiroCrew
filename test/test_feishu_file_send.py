"""``file_send``'s Feishu leg: ``FeishuTransport.send_document`` and its client calls.

Driven through the real ``POST /api/channel/upload-file`` handler and the real
destination oracle, whose send-ladder step (``_resolve_mirror_target``) is
stubbed to answer the link and transport under test, with a real
:class:`FeishuTransport` over a real :class:`LarkClient`. The SDK is faked only
where the call reaches it: the upload and message-create request builders, and
the ``image``, ``file`` and ``message`` resources (the same seam
``test_feishu_client_images.py`` uses). The uploads exist only on the client
with the timeout, so an upload through the reply client fails the test.
"""

from __future__ import annotations

import json
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import upload_destination
from kiro_crew.dashboard.handlers.files import api_channel_upload_file
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.feishu.client import LarkClient
from kiro_crew.feishu.transport import FeishuTransport
from kiro_crew.messaging.link import ChannelLink

OWNER = "ou_owner"
GROUP = "oc_group"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
PDF = b"%PDF-1.4\n%%EOF\n"


class _Fields:
    """A request builder whose setters each record a field; ``build()`` returns them."""

    def __init__(self) -> None:
        self.fields: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        def _set(value: Any) -> "_Fields":
            self.fields[name] = value
            return self

        return _set

    def build(self) -> dict[str, Any]:
        return dict(self.fields)


class _Upload:
    """``im.v1.image`` or ``im.v1.file``: records uploads, answers a key or a refusal."""

    def __init__(self, key_field: str) -> None:
        self.key_field = key_field
        self.uploads: list[dict[str, Any]] = []
        self.threads: list[str] = []
        self.refusal: tuple[int, str] | None = None

    def create(self, req: dict[str, Any]) -> Any:
        self.uploads.append(req)
        self.threads.append(threading.current_thread().name)
        if self.refusal is not None:
            code, msg = self.refusal
            return types.SimpleNamespace(success=lambda: False, code=code, msg=msg, data=None)
        data = types.SimpleNamespace(**{self.key_field: f"{self.key_field}_1"})
        return types.SimpleNamespace(success=lambda: True, code=0, msg="success", data=data)


class _Message:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create(self, req: dict[str, Any]) -> Any:
        self.created.append(req)
        data = types.SimpleNamespace(message_id=f"om_{len(self.created)}")
        return types.SimpleNamespace(success=lambda: True, code=0, msg="success", data=data)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[LarkClient]:
    im_v1 = types.ModuleType("lark_oapi.api.im.v1")
    for name in (
        "CreateImageRequest",
        "CreateImageRequestBody",
        "CreateFileRequest",
        "CreateFileRequestBody",
        "CreateMessageRequest",
        "CreateMessageRequestBody",
    ):
        setattr(im_v1, name, types.SimpleNamespace(builder=_Fields))
    monkeypatch.setitem(sys.modules, "lark_oapi.api.im.v1", im_v1)
    # Built without __init__, which needs the real SDK to construct its clients.
    lark_client = object.__new__(LarkClient)
    lark_client._lark = types.SimpleNamespace(
        im=types.SimpleNamespace(v1=types.SimpleNamespace(message=_Message()))
    )
    lark_client._lark_files = types.SimpleNamespace(
        im=types.SimpleNamespace(
            v1=types.SimpleNamespace(image=_Upload("image_key"), file=_Upload("file_key"))
        )
    )
    lark_client._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="feishu-rest")
    lark_client._download_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="feishu-download"
    )
    yield lark_client
    lark_client._executor.shutdown(wait=True)
    lark_client._download_executor.shutdown(wait=True)


def _v1(client: LarkClient) -> Any:
    uploads = client._lark_files.im.v1
    return types.SimpleNamespace(
        image=uploads.image, file=uploads.file, message=client._lark.im.v1.message
    )


def _transport(client: LarkClient, **kwargs: Any) -> FeishuTransport:
    return FeishuTransport(client, allowed_open_ids=[OWNER], **kwargs)


@pytest.fixture
def outbox(tmp_path: Path) -> Iterator[Path]:
    box = tmp_path / "outbox"
    box.mkdir()
    (box / "shot.png").write_bytes(PNG)
    (box / "report.pdf").write_bytes(PDF)
    with (
        patch("kiro_crew.config.loader.outbox_dir", return_value=box),
        patch("kiro_crew.config.loader.workspace_root", return_value=tmp_path),
    ):
        yield box


async def _upload(
    transport: FeishuTransport,
    path: Path,
    *,
    link: ChannelLink | None = None,
    description: str = "",
) -> tuple[int, dict[str, Any]]:
    """POST the endpoint ``file_send`` calls, for a Feishu-native session."""
    state = MagicMock(spec=DashboardState)
    state.sessions = MagicMock()
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/channel/upload-file", api_channel_upload_file)
    with patch(
        "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
        return_value=(link or ChannelLink("feishu", channel_id=OWNER), transport),
    ):
        async with TestClient(TestServer(app)) as http:
            resp = await http.post(
                "/api/channel/upload-file",
                json={"file_path": str(path), "filename": path.name, "description": description},
                headers={"X-Session-Key": f"feishu:{OWNER}"},
            )
            return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_a_feishu_dm_resolves_to_the_transports_document_verb(client: LarkClient) -> None:
    transport = _transport(client)
    state = types.SimpleNamespace(sessions=MagicMock())
    with patch(
        "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
        return_value=(ChannelLink("feishu", channel_id=OWNER), transport),
    ):
        target = await upload_destination.resolve_channel(
            state, f"feishu:{OWNER}", persisted_probe=lambda _key: None
        )
    assert isinstance(target, upload_destination.ChannelTarget)
    assert target.deliver == transport.send_document


@pytest.mark.asyncio
async def test_a_png_is_sent_as_an_image_message(client: LarkClient, outbox: Path) -> None:
    status, body = await _upload(_transport(client), outbox / "shot.png")

    assert status == 200 and body["delivered"] is True and body["channel_type"] == "feishu"
    (upload,) = _v1(client).image.uploads
    assert upload["request_body"]["image_type"] == "message"
    assert upload["request_body"]["image"].read() == PNG
    # Off the reply workers: a stalled upload must not hold up replies.
    assert _v1(client).image.threads[0].startswith("feishu-download")
    assert _v1(client).file.uploads == []
    (create,) = _v1(client).message.created
    assert create["receive_id_type"] == "open_id"
    assert create["request_body"]["receive_id"] == OWNER
    assert create["request_body"]["msg_type"] == "image"
    assert json.loads(create["request_body"]["content"]) == {"image_key": "image_key_1"}


@pytest.mark.asyncio
async def test_a_pdf_is_sent_as_a_file_message_under_its_name(
    client: LarkClient, outbox: Path
) -> None:
    status, body = await _upload(_transport(client), outbox / "report.pdf")

    assert status == 200 and body["delivered"] is True
    (upload,) = _v1(client).file.uploads
    assert upload["request_body"]["file_type"] == "pdf"
    assert upload["request_body"]["file_name"] == "report.pdf"
    assert upload["request_body"]["file"].read() == PDF
    assert _v1(client).image.uploads == []
    (create,) = _v1(client).message.created
    assert create["request_body"]["msg_type"] == "file"
    assert json.loads(create["request_body"]["content"]) == {"file_key": "file_key_1"}


@pytest.mark.asyncio
async def test_an_image_over_the_image_limit_is_sent_as_a_file(
    client: LarkClient, outbox: Path
) -> None:
    from kiro_crew.messaging.outbound_files import ExtractLimits

    big = outbox / "big.png"
    big.write_bytes(PNG + b"\x00" * 64)
    # The image ceiling scaled down, so the test need not build a 10 MB file.
    with patch(
        "kiro_crew.feishu.transport.FEISHU_UPLOAD_LIMITS",
        ExtractLimits(max_file_bytes=len(PNG)),
    ):
        status, _ = await _upload(_transport(client), big)

    assert status == 200
    assert _v1(client).image.uploads == []
    (upload,) = _v1(client).file.uploads
    assert upload["request_body"]["file_type"] == "stream"


@pytest.mark.asyncio
async def test_text_that_opens_with_a_raster_magic_is_sent_as_a_file(
    client: LarkClient, outbox: Path
) -> None:
    # "BM" is the whole BMP signature, so only the file's name tells this CSV apart.
    csv = outbox / "bmi.csv"
    csv.write_bytes(b"BMI,weight\n21,70\n")
    status, _ = await _upload(_transport(client), csv)

    assert status == 200
    assert _v1(client).image.uploads == []
    (upload,) = _v1(client).file.uploads
    assert upload["request_body"]["file_type"] == "stream"
    assert upload["request_body"]["file_name"] == "bmi.csv"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data, limit, reason",
    [
        pytest.param(b"", 1024, "empty file", id="empty"),
        pytest.param(PDF, len(PDF) - 1, "30 MB", id="over-the-file-limit"),
    ],
)
async def test_a_file_feishu_would_refuse_uploads_nothing(
    client: LarkClient, outbox: Path, data: bytes, limit: int, reason: str
) -> None:
    doc = outbox / "notes.txt"
    doc.write_bytes(data)
    with patch("kiro_crew.feishu.transport.FEISHU_FILE_MAX_BYTES", limit):
        status, body = await _upload(_transport(client), doc)

    assert status == 502 and reason in body["error"]
    assert _v1(client).file.uploads == [] and _v1(client).image.uploads == []
    assert _v1(client).message.created == []


@pytest.mark.asyncio
async def test_the_caption_follows_the_file_as_text(client: LarkClient, outbox: Path) -> None:
    status, _ = await _upload(_transport(client), outbox / "report.pdf", description="Q3 numbers")

    assert status == 200
    file_msg, caption = _v1(client).message.created
    assert file_msg["request_body"]["msg_type"] == "file"
    assert caption["request_body"]["msg_type"] == "text"
    assert json.loads(caption["request_body"]["content"]) == {"text": "Q3 numbers"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revoked_in, status, reason, created",
    [
        pytest.param("file", 502, "allow-list", [], id="during-the-upload"),
        pytest.param("message", 200, '"delivered": true', ["file"], id="before-the-caption"),
    ],
)
async def test_a_dm_revoked_mid_send_receives_nothing_after_the_revocation(
    client: LarkClient,
    outbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    revoked_in: str,
    status: int,
    reason: str,
    created: list[str],
) -> None:
    # A live reload empties the roster inside the SDK call, so the next delivery
    # (the file message, or the caption) must re-decide the DM rather than reuse
    # the decision taken before the upload.
    transport = _transport(client)
    resource = getattr(_v1(client), revoked_in)
    create = resource.create

    def _create_then_revoke(req: dict[str, Any]) -> Any:
        answer = create(req)
        transport.reconfigure(
            types.SimpleNamespace(allowed_open_ids=[], allowed_group_ids=[], allow_group=False)
        )
        return answer

    monkeypatch.setattr(resource, "create", _create_then_revoke)
    got, body = await _upload(transport, outbox / "report.pdf", description="Q3 numbers")

    assert got == status and reason in json.dumps(body)
    assert len(_v1(client).file.uploads) == 1
    assert [m["request_body"]["msg_type"] for m in _v1(client).message.created] == created


@pytest.mark.asyncio
async def test_a_restricted_session_is_skipped_before_the_verb(client: LarkClient) -> None:
    transport = _transport(client)
    # A live incognito slot answers the real shared predicate.
    state = types.SimpleNamespace(
        sessions=MagicMock(), get_slot=lambda _name: types.SimpleNamespace(is_restricted=True)
    )
    with patch(
        "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
        return_value=(ChannelLink("feishu", channel_id=OWNER), transport),
    ):
        target = await upload_destination.resolve_channel(
            state, "dashboard:s1", persisted_probe=lambda _key: None
        )
    assert target == upload_destination.Skip("restricted_session")
    assert _v1(client).image.uploads == [] and _v1(client).message.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link",
    [
        pytest.param(ChannelLink("feishu", channel_id=GROUP), id="allowed-group"),
        pytest.param(ChannelLink("feishu", channel_id="ou_revoked"), id="off-the-roster"),
    ],
)
async def test_anything_but_an_allow_listed_dm_uploads_nothing(
    client: LarkClient, outbox: Path, link: ChannelLink
) -> None:
    # The group is allowed for chat, so only the send boundary's DM rule refuses it;
    # the id off the roster is the link the oracle admitted before a roster reload.
    transport = _transport(client, allow_group=True, allowed_group_ids=[GROUP])
    status, body = await _upload(transport, outbox / "report.pdf", link=link)

    assert status == 502
    assert "direct message" in body["error"]
    assert _v1(client).file.uploads == [] and _v1(client).image.uploads == []
    assert _v1(client).message.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name, kind", [("shot.png", "image"), ("report.pdf", "file")])
async def test_a_missing_upload_grant_names_im_resource(
    client: LarkClient, outbox: Path, name: str, kind: str
) -> None:
    getattr(_v1(client), kind).refusal = (99991672, "Access denied")
    status, body = await _upload(_transport(client), outbox / name)

    assert status == 502
    assert "99991672" in body["error"] and "im:resource" in body["error"]
    assert _v1(client).message.created == []
