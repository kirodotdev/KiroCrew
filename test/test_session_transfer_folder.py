"""A sent folder: the sending side of a folder's *Send a copy to*.

The bundle carries the session's folder NAME only when ``send-session`` is asked
with ``with_folder: true``, egress-redacted like the title and read from this
instance's committed folder tree by id. The receiving side (filing under
``Imported`` / ``from <sender>`` / ``<folder>``) is in
``test_session_transfer_landing.py``; the rest of the transfer in
``test_session_transfer.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from session_transfer_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _async_value,
    _isolate_import_tmp,
    _OwnerReq,
    _PersistentLine,
    _slot,
)


def test_a_bundle_without_a_folder_has_no_folder_key():
    """Byte-identity for every caller that does not ask: a single-session send
    puts exactly the bundle it always did on the wire."""
    from kiro_crew.dashboard import session_transfer as st

    assert "folder" not in st._assemble_bundle([], "t", "", "mac")


def test_a_folder_name_is_egress_redacted_like_the_title():
    from kiro_crew.dashboard import session_transfer as st

    bundle = st._assemble_bundle([], "t", "", "mac", None, False, None, "Payments")
    assert bundle["folder"] == "Payments"
    leaky = st._assemble_bundle([], "t", "", "mac", None, False, None, "/Users/someone/src/app")
    assert "someone" not in leaky.get("folder", "")


@pytest.mark.asyncio
async def test_the_folder_name_is_read_from_this_instances_committed_tree_by_id():
    from types import SimpleNamespace

    from kiro_crew.dashboard import session_transfer as st

    committed = [{"id": "abc", "name": "Payments"}]

    async def read_folders(read):
        return read(committed)

    # The live list holds a rename whose write is still in flight (and may roll
    # back): the bundle must carry the committed name, never the provisional one.
    state = SimpleNamespace(
        _folders=[{"id": "abc", "name": "Payments (renaming)"}], read_folders=read_folders
    )
    assert await st._committed_folder_name(state, "abc") == "Payments"
    assert await st._committed_folder_name(state, "") == ""
    assert await st._committed_folder_name(state, "gone") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body_extra", "expected"),
    [({"with_folder": True}, "Payments"), ({}, None), ({"with_folder": "yes"}, None)],
)
async def test_send_session_carries_the_folder_only_when_asked(monkeypatch, body_extra, expected):
    """``with_folder: true`` puts the session's folder NAME on the wire; an absent
    or non-boolean flag sends exactly today's bundle."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )
    msg = {"role": "user", "content": "hi", "ts": ""}

    class _Log(_PersistentLine):
        def read_messages_chained(self, _key):
            return [msg]

    slot = _slot([msg], dirty=False)
    slot.messages = [msg]
    slot._disk_window_len = 1
    slot.key = "slot-1"
    slot.folder_id = "f1"

    async def _save(_state, s, best_effort=True):
        return True

    monkeypatch.setattr(st, "save_slot_off_loop", _save)
    captured: dict = {}

    class _Mgr:
        async def send_session_bundle(self, _id, bundle, **_kw):
            captured["bundle"] = bundle
            return True, {"key": "remote-1"}

    folders = [{"id": "f1", "name": "Payments"}]

    async def _read_folders(read):
        return read(folders)

    state = SimpleNamespace(
        _slots={"slot-1": slot},
        _folders=folders,
        read_folders=_read_folders,
        conversation_log=_Log(),
        instances_manager=_Mgr(),
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
        owner_id="owner",
    )
    request = _OwnerReq(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        json=_async_value({"slot": "slot-1", **body_extra}),
    )

    resp = await hi.api_instances_send_session(request)

    assert resp.status == 200, resp.body
    assert captured["bundle"].get("folder") == expected
