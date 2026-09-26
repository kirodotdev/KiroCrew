"""What the restore step refuses, and why refusing is the safe direction.

The backend's own readers ignore an authority file they cannot parse and carry on with
an empty result. That is right for them and wrong here: bytes written by this step are
read once as "no conversations" and are then replaced by the backend's next flush, so a
malformed object would look like a restore that worked and leave the customer's list
empty. Refusing to boot turns that silent loss into a message an operator gets before
the task serves a turn.

Absence is the one case that is NOT a failure. A crew's first task finds nothing in the
bucket, which is a first boot. Every other reason a read does not return bytes -- a
denial above all -- is a failure, because reading a denial as absence is the route to
booting with an empty slot table and flushing it over the real one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from container.common import keys
from container.sidecar import restore as restore_mod
from container.sidecar.store import ObjectAbsent

from ._settings_helper import make_settings


class _DictStore:
    """Serves the bytes it is given; absence is the store's own exception."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def put(self, key: str, body, size: int) -> None:  # pragma: no cover - unused here
        self.objects[key] = body.read(size)

    def get(self, key: str, *, limit: int) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise ObjectAbsent(key) from None


class _DeniedStore:
    """Every read fails for a reason that is not absence."""

    def put(self, key: str, body, size: int) -> None:  # pragma: no cover - unused here
        raise AssertionError("the restore step does not put")

    def get(self, key: str, *, limit: int) -> bytes:
        raise RuntimeError("AccessDenied")


def _settings(tmp_path: Path):
    return make_settings(tmp_path, crew="crew-31", prefix="crews")


def _bucket_with(settings, **files: bytes) -> _DictStore:
    return _DictStore({keys.authority_key(settings, name): raw for name, raw in files.items()})


def test_bytes_that_are_not_utf8_refuse_the_boot(tmp_path):
    settings = _settings(tmp_path)
    store = _bucket_with(settings, **{"session_map.json": b"\xff\xfe not text"})

    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.restore_authority(settings, store)

    assert "session_map.json" in str(caught.value)
    assert not (settings.config_dir / "session_map.json").exists()


def test_bytes_that_are_not_json_refuse_the_boot(tmp_path):
    settings = _settings(tmp_path)
    store = _bucket_with(settings, **{"session_map.json": b"{not json"})

    with pytest.raises(restore_mod.RestoreFailed):
        restore_mod.restore_authority(settings, store)


def test_json_that_is_not_an_object_refuses_the_boot(tmp_path):
    """The backend requires an object at the top level and ignores anything else."""
    settings = _settings(tmp_path)
    store = _bucket_with(settings, **{"session_map.json": b'["cust-1"]'})

    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.restore_authority(settings, store)

    assert "not an object" in str(caught.value)


def test_open_slots_with_a_keys_field_that_is_not_a_list_refuses_the_boot(tmp_path):
    settings = _settings(tmp_path)
    store = _bucket_with(
        settings,
        **{"session_map.json": b"{}", "open_slots.json": b'{"keys": "cust-1"}'},
    )

    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.restore_authority(settings, store)

    assert "open_slots.json" in str(caught.value)


def test_open_slots_without_a_keys_field_is_legal(tmp_path):
    """An absent ``keys`` means no open slots, which is a state the backend writes."""
    settings = _settings(tmp_path)
    store = _bucket_with(
        settings, **{"session_map.json": b"{}", "open_slots.json": b'{"version": 2}'}
    )

    result = restore_mod.restore_authority(settings, store)

    assert sorted(result.restored) == sorted(keys.AUTHORITY_NAMES)
    assert (settings.config_dir / "open_slots.json").read_bytes() == b'{"version": 2}'


def test_a_denied_read_is_not_read_as_absence(tmp_path):
    settings = _settings(tmp_path)

    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.restore_authority(settings, _DeniedStore())

    assert "not the same" in str(caught.value)


def test_an_empty_bucket_is_a_first_boot_and_not_a_failure(tmp_path):
    settings = _settings(tmp_path)

    result = restore_mod.restore_authority(settings, _DictStore({}))

    assert result.restored == []
    assert sorted(result.absent) == sorted(keys.AUTHORITY_NAMES)


def test_a_symlinked_config_directory_refuses_the_boot(tmp_path):
    """Writing through a link would put this task's conversation index outside its home.

    Seeded with BOTH authority files, because a half-published pair refuses earlier and for
    a different reason, and this test is about the write rather than about the pair.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "data").symlink_to(elsewhere, target_is_directory=True)
    settings = make_settings(root, crew="crew-31", prefix="crews")
    store = _bucket_with(
        settings, **{"session_map.json": b"{}", "open_slots.json": b'{"keys": []}'}
    )

    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.restore_authority(settings, store)

    assert "symlink" in str(caught.value)


def test_validate_accepts_what_the_backend_accepts(tmp_path):
    """The check is exactly as strict as the readers require, and no stricter."""
    restore_mod.validate_authority("session_map.json", b'{"cust-1": "sess-1"}')
    restore_mod.validate_authority("open_slots.json", b'{"keys": []}')
    restore_mod.validate_authority("open_slots.json", b"{}")
    # The entry shapes the session-map loader really keeps: the legacy plain string, the
    # object carrying a sid, and the object whose sid was deliberately CLEARED when the
    # session stopped being resumable -- that last one is a live entry, not a fault.
    restore_mod.validate_authority(
        "session_map.json",
        b'{"a": "sess-1", "b": {"sid": "sess-2"}, "c": {"sid": "", "discarded_sid": "s3"}}',
    )
    restore_mod.validate_authority("open_slots.json", b'{"keys": ["dashboard:1"], "ts": 7}')


@pytest.mark.parametrize(
    "body",
    [
        b'{"cust-1": {"slack_channel_id": "C1"}}',
        b'{"cust-1": null}',
        b'{"cust-1": 7}',
        b'{"cust-1": ["sess-1"]}',
        b'{"good": "sess-1", "cust-1": {"no_sid": true}}',
        b'{"cust-1": {"sid": []}}',
        b'{"cust-1": {"sid": null}}',
        b'{"cust-1": {"sid": 5}}',
        b'{"cust-1": {"sid": true}}',
        b'{"cust-1": {"sid": {"id": "sess-1"}}}',
    ],
)
def test_a_session_map_entry_the_backend_skips_refuses_the_boot(body):
    """The loader drops these with a bare ``continue`` and the next flush deletes them.

    Nothing logs it, so accepting the file spends the pointer silently. Refusing leaves the
    bytes in the bucket, which is the only one of the two outcomes a repair can undo. The
    fifth case carries a healthy entry beside the bad one, because surviving siblings do not
    make the dropped one recoverable.

    The ``sid`` cases reach the same loss by the other door: the loader's test is that the
    key is PRESENT, so these pass it, and the startup prune's is that the value is truthy.
    An empty container reads as empty and the entry is collected as stale; a number reads as
    truthy and sends the prune looking for a session file named after it, which is absent,
    so it is collected too.
    """
    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.validate_authority("session_map.json", body)

    assert "cust-1" in str(caught.value)


def test_an_empty_string_sid_is_accepted_because_the_backend_writes_one():
    """The over-strict direction: refusing this would refuse a bucket the backend produced.

    The backend stashes a session id that stopped resolving and leaves ``sid`` empty on an
    entry it keeps, so an empty STRING is a state a correct writer reaches. Only a non-string
    is unreachable, which is why the type is the test rather than the truthiness.
    """
    restore_mod.validate_authority(
        "session_map.json", b'{"cust-1": {"sid": "", "discarded_sid": "sess-9"}}'
    )


@pytest.mark.parametrize(
    "bad_sid", [b"[]", b"null", b"5", b"true", b"{}", b'{"id": "s"}', b'["sess-1"]', b"1.5"]
)
def test_a_non_string_sid_refuses_the_boot_and_writes_nothing(tmp_path, bad_sid):
    """The CONDITION, not the one type: refuse every non-string, and leave the bytes alone.

    The refusal is only worth anything if it happens before anything is written. Validation
    runs over every fetched object ahead of the write phase, so one bad entry stops the whole
    restore -- the bucket copy stays, the healthy sibling file is not written either, and a
    repair still has everything it needs. A refusal that had already written would have spent
    the state it was protecting.
    """
    settings = _settings(tmp_path)
    store = _bucket_with(
        settings,
        **{
            "session_map.json": b'{"cust-1": {"sid": ' + bad_sid + b"}}",
            "open_slots.json": b'{"keys": ["dashboard:1"]}',
        },
    )
    before = dict(store.objects)

    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.restore_authority(settings, store)

    assert "cust-1" in str(caught.value)
    assert store.objects == before
    assert sorted(p.name for p in settings.config_dir.glob("*.json")) == []

    # Control: the same probe sees a write when the entry is well formed, so the empty
    # result above is the refusal and not a path that never holds these files.
    healthy = _bucket_with(
        settings,
        **{
            "session_map.json": b'{"cust-1": {"sid": "sess-1"}}',
            "open_slots.json": b'{"keys": ["dashboard:1"]}',
        },
    )
    restore_mod.restore_authority(settings, healthy)

    assert sorted(p.name for p in settings.config_dir.glob("*.json")) == [
        "open_slots.json",
        "session_map.json",
    ]


@pytest.mark.parametrize("member", [b"null", b"7", b'""', b"[]", b'{"sid": "s"}'])
def test_an_open_slot_member_the_backend_drops_refuses_the_boot(member):
    """The backend's screen folds a non-string and an empty string to nothing, unlogged."""
    with pytest.raises(restore_mod.RestoreFailed) as caught:
        restore_mod.validate_authority("open_slots.json", b'{"keys": [' + member + b"]}")

    assert "open slots" in str(caught.value)


@pytest.mark.parametrize("member", ["../../etc/passwd", "a/b", "a\\b"])
def test_an_open_slot_key_with_path_separators_is_left_to_the_backend_screen(member):
    """The one entry the backend drops that this deliberately does not refuse.

    Its screen rejects a separator-bearing key as an escape from the sessions directory and
    WARNS, so removing it is the security outcome rather than a loss. Refusing here would
    let one planted key in the bucket deny every replacement task permanently, which is a
    worse answer than the screen the backend already applies.
    """
    restore_mod.validate_authority(
        "open_slots.json", json.dumps({"keys": [member]}).encode("utf-8")
    )
