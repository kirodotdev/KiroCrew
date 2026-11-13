"""Every S3 request the runtime sends pins the task's own account as the bucket owner.

The bucket comes from an environment variable. If that name ever belongs to another
account, S3 must refuse the request instead of serving or storing a transcript there.
"""

from __future__ import annotations

import io
import sys
import types

import pytest
from container.common import objects
from container.front import transcript as t
from container.sidecar.store import S3ObjectStore

OWNER = "111122223333"


class _Body:
    def __init__(self, data: bytes) -> None:
        self._raw = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._raw.read(size)


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def put_object(self, **kw):
        kw["Body"].read()
        self.calls.append(("put_object", kw))
        return {"ETag": '"e"'}

    def get_object(self, **kw):
        self.calls.append(("get_object", kw))
        return {"ContentLength": 2, "Body": _Body(b"{}"), "ETag": '"e"'}


def test_the_sidecar_put_and_get_carry_the_expected_owner() -> None:
    client = _Client()
    store = S3ObjectStore("bkt", client=client, expected_owner=OWNER)
    store.put("k", io.BytesIO(b"ab"), 2)
    store.get("k", limit=10)
    assert [c[1]["ExpectedBucketOwner"] for c in client.calls] == [OWNER, OWNER]


def test_the_transcript_get_carries_the_expected_owner() -> None:
    client = _Client()
    reader = t.S3TranscriptReader("bkt", client=client, expected_owner=OWNER)
    reader.get("k")
    assert client.calls[0][1]["ExpectedBucketOwner"] == OWNER


@pytest.fixture
def _fake_sts(monkeypatch):
    """A boto3 whose STS answers with OWNER and counts the calls."""
    asked: list[str] = []

    class _Sts:
        def get_caller_identity(self):
            asked.append("sts")
            return {"Account": OWNER}

    fake = types.ModuleType("boto3")
    fake.client = lambda name, **_kw: _Sts()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setattr(objects, "_caller_account", None)
    return asked


def test_without_a_given_owner_the_account_comes_from_sts_once(_fake_sts) -> None:
    client = _Client()
    first = t.S3TranscriptReader("bkt", client=client)
    second = t.S3TranscriptReader("bkt", client=client)
    first.get("k")
    second.get("k")
    assert [c[1]["ExpectedBucketOwner"] for c in client.calls] == [OWNER, OWNER]
    assert _fake_sts == ["sts"]


def test_an_empty_sts_answer_is_refused_not_sent_as_an_empty_owner(monkeypatch) -> None:
    class _Sts:
        def get_caller_identity(self):
            return {}

    fake = types.ModuleType("boto3")
    fake.client = lambda name, **_kw: _Sts()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setattr(objects, "_caller_account", None)
    client = _Client()
    with pytest.raises(RuntimeError, match="no account"):
        t.S3TranscriptReader("bkt", client=client).get("k")
    assert client.calls == []
