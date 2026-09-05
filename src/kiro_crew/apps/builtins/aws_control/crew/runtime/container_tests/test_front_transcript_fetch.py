"""The on-demand transcript fetch: the other half of the ephemeral memory change.

Boot restores no transcripts, so THIS is where a returning customer's
conversation comes back (``EPHEMERAL-CONTRACT.md``). Every test here pins a rule
whose violation is silent in production:

1. The object is ``dashboard_<slot>.jsonl``, not ``<slot>.jsonl``. Getting this
   wrong fetches nothing and every customer looks new, with no error anywhere.
2. Exactly ONE key is requested, ever. A list undoes the isolation the change
   exists for.
3. A transcript already on disk is never re-fetched and never overwritten. The
   local copy leads S3 by up to one backup interval, so an overwrite rolls a
   customer's conversation backwards.
4. Absence is normal; a FAILURE fails the turn on both transports, before the
   body reaches the backend.
5. Contents are never logged.

No AWS: the reader is a fake with a ``get``. The backend is the same fake HTTP
surface ``test_front_proxy.py`` uses, imported rather than rebuilt so the two
files cannot model different backends.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from container import common
from container.backup import layout
from container.front import transcript
from container.front.app import build_app

from .test_front_proxy import _free_port, backend, env  # noqa: F401 - fixtures

TURN = "/c/crew/v1/chat/completions"

# A body long enough that a truncating bug is visible, and carrying a distinctive
# string so a test can prove it never reaches a log line.
SECRET_LINE = "customer-said-something-private"
TRANSCRIPT_BODY = (
    json.dumps({"_type": "metadata", "title": "Order 8831"})
    + "\n"
    + json.dumps({"role": "user", "content": SECRET_LINE})
    + "\n"
).encode("utf-8")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
@dataclass
class FakeReader:
    """A read-only store with a ``get`` and nothing else.

    Records every key asked for, so a test can assert the count as well as the
    value: "fetched the right object" and "fetched exactly one object" are
    different properties and both matter here.
    """

    objects: dict[str, bytes] = field(default_factory=dict)
    gets: list[str] = field(default_factory=list)
    fail_with: Exception | None = None
    delay: float = 0.0

    def get(self, key: str) -> bytes:
        self.gets.append(key)
        if self.delay:
            import time

            time.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        try:
            return self.objects[key]
        except KeyError:
            raise _client_error("NoSuchKey", 404) from None


def _client_error(code: str, status: int) -> Exception:
    """An exception shaped like botocore's ``ClientError``, without botocore."""

    class ClientError(Exception):
        pass

    exc = ClientError(f"{code} ({status})")
    exc.response = {  # type: ignore[attr-defined]
        "Error": {"Code": code},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return exc


def make_settings(backend_env, *, bucket: str | None = "smc-bucket", prefix: str = "crews"):
    """Settings for the HTTP-path tests, with a data home of their own.

    The data home is unique per call on purpose. Sharing one would make a slot id
    reused by a later test find the earlier test's transcript already on disk, and
    the fetch it meant to exercise would be skipped rather than failed: a test
    that passes for the wrong reason, which is the failure this whole file is
    about.
    """
    run_dir = backend_env["run_dir"]
    data_home = run_dir.parent / f"data-{uuid4().hex[:8]}"
    return common.Settings(
        backend_port=backend_env["port"],
        backend_run_dir=run_dir,
        front_port=8080,
        route_prefix="/c/crew",
        control_secret=None,
        data_home=data_home,
        config_dir=data_home,
        crew_name="crew",
        backup_bucket=bucket,
        backup_prefix=prefix,
        backup_interval_secs=30,
    )


def local_settings(tmp_path: Path, *, bucket: str | None = "smc-bucket") -> common.Settings:
    """Settings for the tests that need no HTTP at all."""
    return common.Settings(
        backend_port=1,
        backend_run_dir=tmp_path / "run",
        front_port=8080,
        route_prefix="",
        control_secret=None,
        data_home=tmp_path / "home",
        config_dir=tmp_path / "home",
        crew_name="crew",
        backup_bucket=bucket,
        backup_prefix="crews",
        backup_interval_secs=30,
    )


async def drive(settings, reader, payload: dict) -> httpx.Response:
    app = build_app(settings, transcript_reader=reader)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://front")
    try:
        return await client.post(TURN, json=payload)
    finally:
        await client.aclose()
        backend_client = getattr(app.state, "backend_client", None)
        if backend_client is not None:
            await backend_client.aclose()


async def drive_stream(settings, reader, payload: dict) -> tuple[int, str]:
    app = build_app(settings, transcript_reader=reader)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://front")
    try:
        async with client.stream("POST", TURN, json=payload) as resp:
            body = b"".join([chunk async for chunk in resp.aiter_bytes()])
            return resp.status_code, body.decode("utf-8")
    finally:
        await client.aclose()
        backend_client = getattr(app.state, "backend_client", None)
        if backend_client is not None:
            await backend_client.aclose()


# --------------------------------------------------------------------------- #
# 1. the filename mapping
# --------------------------------------------------------------------------- #
def test_the_stem_carries_the_dashboard_thread_prefix() -> None:
    """``<thread>_<slot>``, not ``<slot>``.

    Pinned to the two sources that establish it: the wheel's
    ``_normalize_slot_key`` docstring invariant
    (``_safe_key(_history_key_for(key)) == f"dashboard_{key}"``) and
    ``control/observe.py resolve_open_slots``, whose own example is
    ``smc-verify`` -> ``dashboard_smc-verify.jsonl``.
    """
    assert transcript.transcript_stem("cust-8831") == "dashboard_cust-8831"
    assert transcript.transcript_stem("smc-verify") == "dashboard_smc-verify"


def test_a_bare_slot_name_is_not_the_object_name(tmp_path: Path) -> None:
    """A store holding ``<slot>.jsonl`` is a MISS. This is the mutation test.

    Point the code at ``<slot>.jsonl`` and this reddens: the object is found and
    written, which in production is the same shape as the defect (the fetch reads
    the wrong key) only inverted, so it is the sharpest available witness.
    """
    settings = local_settings(tmp_path)
    reader = FakeReader(objects={"crews/crew/data/sessions/cust-8831.jsonl": TRANSCRIPT_BODY})

    outcome = asyncio.run(transcript.ensure_local_transcript(settings, "cust-8831", reader=reader))

    assert outcome.action == "absent"
    assert reader.gets == ["crews/crew/data/sessions/dashboard_cust-8831.jsonl"]
    assert not (settings.sessions_dir / "dashboard_cust-8831.jsonl").exists()


@pytest.mark.parametrize("spelling", ["cust-1", "dashboard_cust-1", "dashboard:cust-1"])
def test_every_spelling_of_one_slot_resolves_to_one_object(spelling: str) -> None:
    """The backend folds all three onto ONE slot, so all three must fetch one key.

    ``_normalize_slot_key`` strips a ``dashboard:``/``dashboard_`` prefix before
    the slot is looked up, so a fetch that skipped the strip would miss the object
    for two of these three and hand those callers an empty conversation.
    """
    assert transcript.transcript_stem(spelling) == "dashboard_cust-1"


def test_a_slot_id_cannot_escape_the_sessions_directory(tmp_path: Path) -> None:
    settings = local_settings(tmp_path)
    stem = transcript.transcript_stem("../../etc/passwd")
    assert "/" not in stem
    path = transcript.local_transcript_path(settings, stem)
    assert path is not None
    assert path.parent == settings.sessions_dir


def test_key_agrees_with_the_backup_layout(tmp_path: Path) -> None:
    """The reader and the sidecar must name the same object.

    ``container/backup/layout.py`` owns the key shape and is not imported at
    runtime (the container contract keeps track internals private, and that file
    is under active change), so the agreement is asserted here instead. If layout
    moves the namespace or the prefix join, this reddens rather than the fetch
    silently missing every object.
    """
    for prefix in ("crews", "", "crews/nested/"):
        settings = local_settings(tmp_path)
        settings = common.Settings(**{**settings.__dict__, "backup_prefix": prefix})
        rel = layout.sessions_prefix(settings) + "dashboard_cust-1.jsonl"
        assert transcript.object_key(settings, "dashboard_cust-1") == layout.full_key(settings, rel)
        # And the sidecar/restore classifier agrees it is a transcript.
        assert layout.is_transcript(settings, rel)


def test_the_reader_cannot_list_or_write() -> None:
    """Structural, not conventional: the surface has no way to do either.

    A list at the turn undoes the change the whole track exists for, and the
    sidecar must remain the only writer. Both are absent from the type rather
    than merely unused by it.
    """
    assert not hasattr(transcript.S3TranscriptReader, "list")
    assert not hasattr(transcript.S3TranscriptReader, "put")
    assert not hasattr(transcript.S3TranscriptReader, "delete")
    declared = {name for name in vars(transcript.TranscriptReader) if not name.startswith("_")}
    assert declared == {"get"}


# --------------------------------------------------------------------------- #
# 2. already on disk: never re-fetch, never overwrite
# --------------------------------------------------------------------------- #
def test_a_transcript_on_disk_is_neither_refetched_nor_overwritten(tmp_path: Path) -> None:
    """THE mutation test for the do-not-overwrite rule.

    The local copy is newer than S3 by up to one backup interval. Here S3 holds a
    SHORTER, older object, which is a real state (a transcript is atomically
    replaced and can shrink), so an overwrite silently rolls the customer's
    conversation backwards. Remove the on-disk short-circuit and this reddens.
    """
    settings = local_settings(tmp_path)
    settings.sessions_dir.mkdir(parents=True)
    path = settings.sessions_dir / "dashboard_cust-1.jsonl"
    newer = TRANSCRIPT_BODY + json.dumps({"role": "assistant", "content": "later"}).encode() + b"\n"
    path.write_bytes(newer)

    reader = FakeReader(
        objects={"crews/crew/data/sessions/dashboard_cust-1.jsonl": b"stale-and-shorter\n"}
    )
    outcome = asyncio.run(transcript.ensure_local_transcript(settings, "cust-1", reader=reader))

    # Ordered by what a customer would lose: the bytes first, then the wasted
    # GET, then the label. A mutation should redden on the harm, not on a name.
    assert path.read_bytes() == newer, "the newer local transcript was rolled backwards"
    assert reader.gets == [], "S3 was consulted for a conversation already on disk"
    assert outcome.action == "present"


def test_a_transcript_that_appears_mid_fetch_is_not_clobbered(tmp_path: Path) -> None:
    """The write refuses an existing target, so the rule holds against the race.

    ``ensure_local_transcript`` checks and then writes; between those the backend
    could create the file. The write is a link onto the target, which fails if it
    exists, so the newer copy survives without a second check to forget.
    """
    settings = local_settings(tmp_path)
    settings.sessions_dir.mkdir(parents=True)
    path = settings.sessions_dir / "dashboard_cust-1.jsonl"
    path.write_bytes(b"created-by-the-backend\n")

    transcript._write_without_clobbering(path, b"from-s3\n")

    assert path.read_bytes() == b"created-by-the-backend\n"


def test_a_fetch_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    settings = local_settings(tmp_path)
    reader = FakeReader(
        objects={"crews/crew/data/sessions/dashboard_cust-1.jsonl": TRANSCRIPT_BODY}
    )
    outcome = asyncio.run(transcript.ensure_local_transcript(settings, "cust-1", reader=reader))
    assert outcome.action == "fetched" and outcome.bytes_written == len(TRANSCRIPT_BODY)
    assert (settings.sessions_dir / "dashboard_cust-1.jsonl").read_bytes() == TRANSCRIPT_BODY
    assert list(settings.sessions_dir.glob(".smc-fetch-*")) == []


# --------------------------------------------------------------------------- #
# 3. absence, and no bucket
# --------------------------------------------------------------------------- #
def test_absent_in_s3_is_normal_and_writes_nothing(tmp_path: Path) -> None:
    settings = local_settings(tmp_path)
    reader = FakeReader()
    outcome = asyncio.run(transcript.ensure_local_transcript(settings, "brand-new", reader=reader))
    assert outcome.action == "absent"
    assert not settings.sessions_dir.exists() or list(settings.sessions_dir.iterdir()) == []


def test_no_bucket_configured_is_not_a_failed_fetch(tmp_path: Path) -> None:
    """A crew with no bucket must still serve turns.

    Nothing was ever uploaded, so nothing is missing: this is a crew without
    durable conversations, not a restore that failed. Reporting it as a failure
    here would refuse every turn such a crew ever receives.
    """
    settings = local_settings(tmp_path, bucket=None)
    outcome = asyncio.run(transcript.ensure_local_transcript(settings, "cust-1", reader=None))
    assert outcome.action == "no_store"


def test_a_turn_with_no_slot_id_fetches_nothing(tmp_path: Path) -> None:
    """An id-less turn is ephemeral: the backend mints a slot, so there is no
    conversation to restore and nothing to look up."""
    settings = local_settings(tmp_path)
    reader = FakeReader()
    outcome = asyncio.run(transcript.ensure_local_transcript(settings, "", reader=reader))
    assert outcome.action == "no_slot"
    assert reader.gets == []


# --------------------------------------------------------------------------- #
# 4. a failure fails the turn, on both transports
# --------------------------------------------------------------------------- #
def test_access_denied_is_a_failure_not_an_absence(tmp_path: Path) -> None:
    """403 must not read as "new conversation".

    Absence and denial are different answers and only one of them may proceed.
    Reading a denial as absence is the silent path to serving an empty history
    and then overwriting the real one at the next backup cycle.
    """
    settings = local_settings(tmp_path)
    reader = FakeReader(fail_with=_client_error("AccessDenied", 403))
    with pytest.raises(transcript.TranscriptUnavailable):
        asyncio.run(transcript.ensure_local_transcript(settings, "cust-1", reader=reader))


def test_a_transport_error_is_a_failure(tmp_path: Path) -> None:
    settings = local_settings(tmp_path)
    reader = FakeReader(fail_with=OSError("connection reset"))
    with pytest.raises(transcript.TranscriptUnavailable):
        asyncio.run(transcript.ensure_local_transcript(settings, "cust-1", reader=reader))


@pytest.mark.asyncio
async def test_a_failed_fetch_refuses_the_turn_before_the_backend_sees_it(env) -> None:
    reader = FakeReader(fail_with=_client_error("AccessDenied", 403))
    resp = await drive(
        make_settings(env), reader, {"model": "crew", "id": "cust-1", "messages": []}
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "transcript_unavailable"
    assert env["fake"].requests == [], "the turn reached the backend with no history"


@pytest.mark.asyncio
async def test_a_failed_fetch_refuses_a_streamed_turn_too(env) -> None:
    """The streaming path must fail as well, with the same code.

    SSE commits 200 before the body, so the refusal is an error FRAME. What
    matters is that the customer is told the turn failed rather than shown an
    empty conversation, and that the backend never received the turn.
    """
    reader = FakeReader(fail_with=_client_error("AccessDenied", 403))
    status, body = await drive_stream(
        make_settings(env),
        reader,
        {"model": "crew", "id": "cust-1", "messages": [], "stream": True},
    )
    assert status == 200
    assert "transcript_unavailable" in body
    assert "[DONE]" in body
    assert env["fake"].requests == []


@pytest.mark.asyncio
async def test_a_streamed_turn_fetches_the_transcript_before_forwarding(env) -> None:
    """The streaming path gets the same fetch, from the same scope.

    Both transports enter ``prepared_turn``; only the entry point differs. This
    is the test that would redden if the streaming branch were left on the bare
    slot lock.
    """
    settings = make_settings(env)
    key = "crews/crew/data/sessions/dashboard_cust-7.jsonl"
    reader = FakeReader(objects={key: TRANSCRIPT_BODY})
    env["fake"].stream_chunks = [
        b'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"hi"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    status, body = await drive_stream(
        settings, reader, {"model": "crew", "id": "cust-7", "messages": [], "stream": True}
    )

    assert status == 200 and "[DONE]" in body
    assert reader.gets == [key]
    assert (settings.sessions_dir / "dashboard_cust-7.jsonl").read_bytes() == TRANSCRIPT_BODY
    assert len(env["fake"].requests) == 1


@pytest.mark.asyncio
async def test_a_non_streamed_turn_fetches_then_forwards(env) -> None:
    settings = make_settings(env)
    key = "crews/crew/data/sessions/dashboard_cust-8831.jsonl"
    reader = FakeReader(objects={key: TRANSCRIPT_BODY})

    resp = await drive(settings, reader, {"model": "crew", "id": "cust-8831", "messages": []})

    assert resp.status_code == 200
    assert reader.gets == [key]
    assert (settings.sessions_dir / "dashboard_cust-8831.jsonl").read_bytes() == TRANSCRIPT_BODY


@pytest.mark.asyncio
async def test_a_refused_turn_never_reaches_the_fetch(env) -> None:
    """The fetch sits AFTER ``judge_addressed_crew``, so a refusal costs no GET.

    An unaddressed or wrongly addressed request must not be able to make the
    container fetch objects, or the refusals become a way to probe the bucket.
    """
    reader = FakeReader()
    payloads: tuple[dict, ...] = (
        {"messages": []},  # names nobody
        {"model": "other-crew", "id": "cust-1", "messages": []},  # wrong crew
    )
    for payload in payloads:
        resp = await drive(make_settings(env), reader, payload)
        assert resp.status_code in (400, 404)
    assert reader.gets == []


# --------------------------------------------------------------------------- #
# 5. one turn at a time, one fetch
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_two_concurrent_turns_on_one_slot_fetch_once(env) -> None:
    """The fetch is inside the per-slot lock, so the second turn finds the file.

    Outside the lock both turns would fetch the same object concurrently and one
    of them would write over the other's file. One recorded GET is the witness
    that the fetch and the lock are the same scope.
    """
    settings = make_settings(env)
    key = "crews/crew/data/sessions/dashboard_cust-9.jsonl"
    reader = FakeReader(objects={key: TRANSCRIPT_BODY}, delay=0.05)
    env["fake"].turn_delay = 0.05

    app = build_app(settings, transcript_reader=reader)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://front")
    try:
        r1, r2 = await asyncio.gather(
            client.post(TURN, json={"model": "crew", "id": "cust-9", "messages": []}),
            client.post(TURN, json={"model": "crew", "id": "cust-9", "messages": []}),
        )
    finally:
        await client.aclose()
        backend_client = getattr(app.state, "backend_client", None)
        if backend_client is not None:
            await backend_client.aclose()

    assert r1.status_code == 200 and r2.status_code == 200
    assert reader.gets == [key], f"fetched {len(reader.gets)} times, expected once"
    assert env["fake"].saw_409 == 0


# --------------------------------------------------------------------------- #
# 6. logging
# --------------------------------------------------------------------------- #
def test_the_log_carries_the_sid_and_the_byte_count_and_no_contents(tmp_path: Path, caplog) -> None:
    settings = local_settings(tmp_path)
    reader = FakeReader(
        objects={"crews/crew/data/sessions/dashboard_cust-1.jsonl": TRANSCRIPT_BODY}
    )
    with caplog.at_level(logging.DEBUG, logger="smc.front.transcript"):
        asyncio.run(transcript.ensure_local_transcript(settings, "cust-1", reader=reader))

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "dashboard_cust-1" in text
    assert str(len(TRANSCRIPT_BODY)) in text
    assert SECRET_LINE not in text
    assert "Order 8831" not in text
