"""A save to the Web Push subscription store never replaces a file it could not read.

``PushSubscriptionStore.add`` rewrites the whole ``subscriptions.json`` from
memory, and a load that cannot read or parse the file leaves memory empty. These
tests pin what happens to the devices already saved in that file: a leading
UTF-8 byte-order mark reads normally, a file that is not usable JSON is kept at
``subscriptions.json.corrupt-<utc>`` before the save, and a read that fails
leaves the file as it is and the save raises.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from kiro_crew.notifications.push_store import PushSubscriptionStore

_SAVED = {
    "https://push.example.com/phone": {
        "endpoint": "https://push.example.com/phone",
        "keys": {"p256dh": "phone-p256dh", "auth": "phone-auth"},
    },
    "https://push.example.com/laptop": {
        "endpoint": "https://push.example.com/laptop",
        "keys": {"p256dh": "laptop-p256dh", "auth": "laptop-auth"},
    },
}

_NEW = {
    "endpoint": "https://push.example.com/tablet",
    "keys": {"p256dh": "tablet-p256dh", "auth": "tablet-auth"},
}


@pytest.fixture
def store_dir(monkeypatch, tmp_path):
    """A data home whose subscriptions file the tests write, with public DNS."""
    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "kiro_crew.link_unfurl._default_resolve", lambda _host, _port: ["93.184.216.34"]
    )
    directory = tmp_path / "web-push"
    directory.mkdir()
    return directory


def _store_file(store_dir: pathlib.Path) -> pathlib.Path:
    return store_dir / "subscriptions.json"


def _raw(path: pathlib.Path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _corrupt_copies(store_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted(store_dir.glob("subscriptions.json.corrupt-*"))


def test_a_bom_file_keeps_every_saved_device_on_the_next_add(store_dir) -> None:
    body = json.dumps({"subscriptions": _SAVED}).encode("utf-8")
    _store_file(store_dir).write_bytes(b"\xef\xbb\xbf" + body)

    store = PushSubscriptionStore()
    assert {entry["endpoint"] for entry in store.all()} == set(_SAVED)
    store.add(_NEW)

    on_disk = json.loads(_store_file(store_dir).read_text(encoding="utf-8"))
    assert set(on_disk["subscriptions"]) == set(_SAVED) | {_NEW["endpoint"]}
    assert _corrupt_copies(store_dir) == []


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[]", b'{"subscriptions": []}', b"\xff\xfe not utf-8"],
    ids=["not-json", "a-list", "subscriptions-not-an-object", "not-utf8"],
)
def test_an_unusable_file_is_kept_aside_before_the_add_saves(store_dir, content) -> None:
    _store_file(store_dir).write_bytes(content)

    store = PushSubscriptionStore()
    assert store.all() == []
    store.add(_NEW)

    copies = _corrupt_copies(store_dir)
    assert len(copies) == 1
    assert _raw(copies[0]) == content
    on_disk = json.loads(_store_file(store_dir).read_text(encoding="utf-8"))
    assert list(on_disk["subscriptions"]) == [_NEW["endpoint"]]


def test_a_failed_read_refuses_the_add_and_leaves_the_file(store_dir, monkeypatch) -> None:
    path = _store_file(store_dir)
    path.write_text(json.dumps({"subscriptions": _SAVED}), encoding="utf-8")
    before = _raw(path)
    real = pathlib.Path.read_text

    def unreadable(self, *args, **kwargs):
        if self.name == "subscriptions.json":
            raise PermissionError("EACCES: held by a scanner")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", unreadable)
    store = PushSubscriptionStore()
    assert store.all() == []

    with pytest.raises(OSError, match="not saving over it"):
        store.add(_NEW)

    assert _raw(path) == before
    assert _corrupt_copies(store_dir) == []
    monkeypatch.setattr(pathlib.Path, "read_text", real)
    assert {entry["endpoint"] for entry in PushSubscriptionStore().all()} == set(_SAVED)


def test_an_add_after_a_read_that_recovers_keeps_every_saved_device(store_dir, monkeypatch) -> None:
    path = _store_file(store_dir)
    path.write_text(json.dumps({"subscriptions": _SAVED}), encoding="utf-8")
    real = pathlib.Path.read_text
    armed = {"on": True}

    def unreadable_once(self, *args, **kwargs):
        if self.name == "subscriptions.json" and armed.pop("on", False):
            raise OSError("EIO: transient")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", unreadable_once)
    store = PushSubscriptionStore()
    assert store.all() == []

    store.add(_NEW)

    on_disk = json.loads(_raw(path).decode("utf-8"))
    assert set(on_disk["subscriptions"]) == set(_SAVED) | {_NEW["endpoint"]}
    assert _corrupt_copies(store_dir) == []
