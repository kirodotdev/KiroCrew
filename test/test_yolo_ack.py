"""The owner's YOLO confirmation acknowledgement kept on the gateway.

``kiro_crew.yolo_ack`` holds the record, and ``GET``/``PUT /api/chat/yolo-ack``
(``dashboard.handlers.yolo_ack``) is its only reader and writer for the dashboard.
Only the machine owner reaches either verb; every other caller is refused and
nothing is written.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import sandbox_leaves, sandbox_precreate
from kiro_crew.security import paths_lists
from kiro_crew.shared_crew import teammate_access

OWNER = "owner-1"
_ABSENT = object()


def _store():
    # Imported by the tests, not here, so the module collects where the store is absent.
    return importlib.import_module("kiro_crew.yolo_ack")


def _handlers():
    return importlib.import_module("kiro_crew.dashboard.handlers.yolo_ack")


@pytest.fixture(name="home")
def home_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(name="audits")
def audits_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        _handlers(),
        "_audit",
        lambda request, outcome, detail, operation: events.append((outcome, detail, operation)),
    )
    return events


def _app(*, user: object = OWNER, app_claim: object = "") -> web.Application:
    """The two routes behind a stand-in for the auth middleware's request claims."""

    @web.middleware
    async def claims(request: web.Request, handler):
        if user is not _ABSENT:
            request["user"] = user
        if app_claim is not _ABSENT:
            request["app"] = app_claim
        return await handler(request)

    handlers = _handlers()
    app = web.Application(middlewares=[claims])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    app.router.add_get("/api/chat/yolo-ack", handlers.api_yolo_ack_get)
    app.router.add_put("/api/chat/yolo-ack", handlers.api_yolo_ack_put)
    return app


async def _call(app: web.Application, method: str, body: object = _ABSENT, raw: str = ""):
    async with TestClient(TestServer(app)) as client:
        if raw:
            resp = await client.request(
                method, "/api/chat/yolo-ack", data=raw, headers={"Content-Type": "application/json"}
            )
        elif body is _ABSENT:
            resp = await client.request(method, "/api/chat/yolo-ack")
        else:
            resp = await client.request(method, "/api/chat/yolo-ack", json=body)
        return resp.status, await resp.json()


class TestTheRecordReads:
    def test_a_missing_record_reads_not_acknowledged(self, home: Path) -> None:
        assert not (home / "yolo_ack.json").exists()
        assert _store().read_acknowledged() is False

    @pytest.mark.parametrize(
        "text",
        ["{}", "{not json", '{"acknowledged": "yes"}', '{"acknowledged": 1}', "[true]", ""],
    )
    def test_anything_but_a_json_true_reads_not_acknowledged(self, home: Path, text: str) -> None:
        (home / "yolo_ack.json").write_text(text, encoding="utf-8")
        assert _store().read_acknowledged() is False

    def test_a_json_true_reads_acknowledged(self, home: Path) -> None:
        (home / "yolo_ack.json").write_text('{"acknowledged": true}', encoding="utf-8")
        assert _store().read_acknowledged() is True


class TestTheOwnerRoutes:
    @pytest.mark.asyncio
    async def test_owner_put_true_then_false_keeps_the_inode(
        self, home: Path, audits: list[tuple[str, str, str]]
    ) -> None:
        record = home / "yolo_ack.json"
        # The sandbox pre-creates the leaf as ``{}``; the writer must keep that inode.
        record.write_text("{}", encoding="utf-8")
        inode = record.stat().st_ino
        app = _app()
        assert await _call(app, "GET") == (200, {"acknowledged": False})

        assert await _call(app, "PUT", {"acknowledged": True}) == (200, {"acknowledged": True})
        assert json.loads(record.read_text(encoding="utf-8")) == {"acknowledged": True}
        assert await _call(app, "GET") == (200, {"acknowledged": True})
        assert record.stat().st_ino == inode

        assert await _call(app, "PUT", {"acknowledged": False}) == (200, {"acknowledged": False})
        assert json.loads(record.read_text(encoding="utf-8")) == {"acknowledged": False}
        assert await _call(app, "GET") == (200, {"acknowledged": False})
        assert record.stat().st_ino == inode
        # Only the PUTs are audited.
        assert [outcome for outcome, _detail, _operation in audits] == ["acknowledged", "rearmed"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"acknowledged": "true"},
            {"acknowledged": 1},
            {"acknowledged": None},
            {"acknowledged": True, "extra": 1},
        ],
    )
    async def test_a_bad_body_is_400_and_writes_nothing(
        self, home: Path, audits: list[tuple[str, str, str]], body: dict
    ) -> None:
        status, answer = await _call(_app(), "PUT", body)
        assert (status, answer["code"]) == (400, "invalid_body")
        assert not (home / "yolo_ack.json").exists()

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_an_object_is_400_and_writes_nothing(
        self, home: Path, audits: list[tuple[str, str, str]]
    ) -> None:
        for raw in ("[true]", "{not json"):
            status, _answer = await _call(_app(), "PUT", raw=raw)
            assert status == 400
        assert not (home / "yolo_ack.json").exists()

    @pytest.mark.asyncio
    async def test_a_failed_write_is_500(
        self, home: Path, audits: list[tuple[str, str, str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(_value: bool) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(_store(), "write_acknowledged", _fail)
        status, answer = await _call(_app(), "PUT", {"acknowledged": True})
        assert (status, answer["code"]) == (500, "yolo_ack_write_failed")
        assert audits[-1][0] == "write_failed"


class TestEveryoneElseIsRefused:
    CALLERS = {
        "an app token": {"user": "app:notes", "app_claim": "notes"},
        "an internal-secret caller": {"user": _ABSENT, "app_claim": _ABSENT},
        "a non-owner dashboard subject": {"user": "slack:U123", "app_claim": ""},
        "a Shared Crew teammate": {"user": "ssm:invite-1", "app_claim": ""},
    }

    @pytest.fixture(name="teammate_passes_the_wider_gate")
    def teammate_passes_the_wider_gate_fixture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The teammate clears the wider owner gate, as on a shared Crew: this
        # route must still refuse them.
        monkeypatch.setattr(
            teammate_access,
            "teammate_passes_owner_gate",
            lambda request: request.get("user") == "ssm:invite-1",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller", sorted(CALLERS))
    @pytest.mark.usefixtures("teammate_passes_the_wider_gate")
    async def test_refused_with_403_and_nothing_written(
        self, home: Path, audits: list[tuple[str, str, str]], caller: str
    ) -> None:
        app = _app(**self.CALLERS[caller])
        record = home / "yolo_ack.json"
        record.write_text('{"acknowledged": false}', encoding="utf-8")

        status, answer = await _call(app, "PUT", {"acknowledged": True})
        assert (status, answer["code"]) == (403, "dashboard_owner_required")
        status, answer = await _call(app, "GET")
        assert (status, answer["code"]) == (403, "dashboard_owner_required")

        assert json.loads(record.read_text(encoding="utf-8")) == {"acknowledged": False}
        # Each refusal is audited under its own verb's operation.
        assert [(outcome, operation) for outcome, _detail, operation in audits] == [
            ("denied", "chat.yolo_ack.set"),
            ("denied", "chat.yolo_ack.read"),
        ]


def test_the_leaf_is_sealed_on_every_floor() -> None:
    assert "yolo_ack.json" in paths_lists._CREW_SECRET_LEAVES
    assert "yolo_ack.json" in sandbox_leaves._CREW_READONLY_LEAVES
    assert "yolo_ack.json" in sandbox_leaves._CREW_CHILD_READABLE_LEAVES
    assert "yolo_ack.json" in sandbox_precreate._CREW_PRECREATE_READONLY_FILE_LEAVES
