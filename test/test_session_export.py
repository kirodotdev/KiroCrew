"""Tests for the session file export (``GET /api/chat/slots/{slot}/export``).

The emphasis is on the invariants a reviewer would want pinned, which are all
compatibility or egress claims rather than "the feature works":

* **``bundle_version`` stays 2** and the ``source`` record is additive, so an
  instance that has not updated still receives a bundle from one that has —
  proven by round-tripping an export through the importer's own validator;
* **the tunnel's bundle is unchanged**, so an existing working flow does not
  start putting new keys on the wire;
* **recorded, never applied** — nothing in the record survives validation, and
  ``approval_policy`` least of all;
* **the filename comes from the REDACTED title**, because a filename is listed
  by whatever holds the file and is therefore its own egress surface;
* **an incognito or temporary session cannot be exported at all.**
"""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import session_export as se
from kiro_crew.dashboard.session_transfer import (
    _SUPPORTED_BUNDLE_VERSIONS,
    BUNDLE_VERSION,
    _validate_bundle,
    build_source_record,
    build_transfer_bundle_async,
)


class _FakeLog:
    def __init__(self, messages):
        self._messages = messages

    def read_messages_chained(self, _key):
        return list(self._messages)


def _slot(messages, *, title="My session", memory_mode="persistent", app="", **over):
    """A slot carrying every field the provenance record reads."""
    slot = SimpleNamespace(
        key="slot-1",
        title=title,
        _titled=True,
        agent="",
        model="claude-opus-5",
        reasoning_effort="high",
        mode="orchestrator",
        autocompact_pct=75.0,
        workspace="default",
        project="/home/me/checkout",
        messages=list(messages),
        _dirty=False,
        _resumed_count=len(messages),
        _disk_window_len=len(messages),
        _disk_older_count=0,
        _pending_rewrite=False,
        _dirty_gen=0,
        memory_mode=memory_mode,
        running=False,
        _in_stage_execution=False,
        _app=app,
    )
    for k, v in over.items():
        setattr(slot, k, v)
    return slot


class _FakeSessions:
    """Stands in for the SessionManager's live registry.

    Keyed on the SESSION key (``dashboard:<slot>``), not the slot key, so these
    tests also pin that the record addresses the session a slot's turns run on
    rather than the slot or its transcript — the three differ for a
    channel-bound slot.
    """

    def __init__(self, policies):
        self._policies = policies

    def has_session(self, key):
        return key in self._policies

    def get_approval_policy(self, key):
        return self._policies.get(key, "")


#: The session key ``effective_session_key`` resolves ``_slot()`` to.
SESSION_KEY = "dashboard:slot-1"


def _state(messages, *, sessions=None, slots=None):
    state = SimpleNamespace(conversation_log=_FakeLog(messages), _slots=slots or {})
    if sessions is not None:
        state.sessions = sessions
    return state


def _request(state, slot_key="slot-1", app=""):
    return SimpleNamespace(
        app={"state": state},
        match_info={"slot": slot_key},
        get=lambda k, default="": app if k == "app" else default,
    )


MSGS = [
    {"role": "user", "content": "how does the tunnel work?", "ts": "t1"},
    {"role": "assistant", "content": "it forwards loopback", "ts": "t2"},
]


# ── the compatibility contract ───────────────────────────────────────────


def test_bundle_version_is_not_bumped():
    """The load-bearing compatibility decision, as a gate.

    ``_validate_bundle`` refuses an unrecognised version OUTRIGHT, so bumping to
    3 would stop every instance that has not updated from receiving anything —
    while an unknown KEY is dropped silently and costs it nothing. The record
    below is therefore additive on version 2, and this pins that nobody
    "tidies" it into a bump later.
    """
    assert BUNDLE_VERSION == 2
    assert _SUPPORTED_BUNDLE_VERSIONS == (1, 2)


@pytest.mark.asyncio
async def test_a_peer_that_ignores_the_new_keys_imports_exactly_as_before():
    """Phase 1's exit criterion: the export round-trips through the tunnel's
    own importer unchanged.

    ``_validate_bundle`` IS the peer here — it is what an instance without this
    change runs — so validating an exported bundle and a plain one and comparing
    the results is the strongest available statement that the new keys are inert
    on the receiving side.
    """
    exported = await build_transfer_bundle_async(
        _state(MSGS), _slot(MSGS), origin="mac", with_source=True
    )
    plain = await build_transfer_bundle_async(_state(MSGS), _slot(MSGS), origin="mac")

    assert "source" in exported
    validated_export, err_export = _validate_bundle(exported)
    validated_plain, err_plain = _validate_bundle(plain)

    assert err_export is None and err_plain is None
    # Not merely "it was accepted": the two validated payloads are the SAME, so
    # no downstream write can differ because of the record.
    assert validated_export == validated_plain
    # And the record does not survive validation, so no import path can read it.
    assert "source" not in validated_export


@pytest.mark.asyncio
async def test_the_tunnel_bundle_does_not_gain_the_record():
    """The tunnel is an existing working flow and this feature does not change
    what it puts on the wire. ``with_source`` defaults off for that reason."""
    bundle = await build_transfer_bundle_async(_state(MSGS), _slot(MSGS), origin="mac")
    assert "source" not in bundle


# ── the provenance record ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_source_record_carries_what_the_session_ran_under():
    sessions = _FakeSessions({SESSION_KEY: "auto"})
    bundle = await build_transfer_bundle_async(
        _state(MSGS, sessions=sessions), _slot(MSGS), origin="mac", with_source=True
    )
    source = bundle["source"]

    assert source["model"] == "claude-opus-5"
    assert source["reasoning_effort"] == "high"
    assert source["workspace"] == "default"
    assert source["project"] == "/home/me/checkout"
    assert source["exported_at"]
    assert source["producer"].startswith("kirocrew/")
    # origin and agent already live at the top level; duplicating them would give
    # a reader two places to look and a way for the two to disagree.
    assert "origin" not in source
    assert "agent" not in source
    # The reader is a HUMAN inspecting the file, and every field has to earn its
    # place against that reader. ``mode`` and ``autocompact_pct`` are re-derived
    # per turn, so they are pointless to apply and there is nothing for a person
    # to do with them either -- they stay out.
    assert "mode" not in source
    assert "autocompact_pct" not in source


@pytest.mark.asyncio
async def test_approval_policy_is_read_off_the_live_session():
    """It has no durable copy anywhere: the session object is its only home."""
    sessions = _FakeSessions({SESSION_KEY: "auto"})
    bundle = await build_transfer_bundle_async(
        _state(MSGS, sessions=sessions), _slot(MSGS), origin="mac", with_source=True
    )
    assert bundle["source"]["approval_policy"] == "auto"


@pytest.mark.asyncio
async def test_an_interactive_session_is_distinguishable_from_an_unknown_one():
    """``""`` is a VALUE (interactive), absence means "could not be read".

    Collapsing the two would make the field's only interesting reading — that a
    transcript was produced under auto-approval — indistinguishable from a
    gateway with nothing to report, which is exactly the misinformation the
    record exists to avoid.
    """
    live = await build_transfer_bundle_async(
        _state(MSGS, sessions=_FakeSessions({SESSION_KEY: ""})),
        _slot(MSGS),
        with_source=True,
    )
    assert live["source"]["approval_policy"] == ""

    # No live session object — evicted, or not re-opened since a restart.
    gone = await build_transfer_bundle_async(
        _state(MSGS, sessions=_FakeSessions({})), _slot(MSGS), with_source=True
    )
    assert "approval_policy" not in gone["source"]

    # No registry at all must degrade the same way rather than raising.
    headless = await build_transfer_bundle_async(_state(MSGS), _slot(MSGS), with_source=True)
    assert "approval_policy" not in headless["source"]


def test_every_field_is_optional():
    """§7.1a: no field is ever required in this format, so a record built from
    nothing still validates as a record rather than carrying empty claims."""
    source = build_source_record()
    assert set(source) == {"exported_at", "producer"}


def test_an_unreadable_registry_does_not_fail_the_export():
    """Provenance is never worth failing an export for."""

    class _Boom:
        def has_session(self, _key):
            raise RuntimeError("registry is wedged")

    state = _state(MSGS, sessions=_Boom())
    from kiro_crew.dashboard.session_transfer import _snapshot_source_record

    source = _snapshot_source_record(state, _slot(MSGS), "dashboard:slot-1")
    assert "approval_policy" not in source
    assert source["producer"].startswith("kirocrew/")


def test_the_policy_is_read_for_the_pinned_session_not_the_slots_current_one():
    """A rebind mid-assembly must not relabel the transcript's provenance.

    The transcript key is pinned before the pre-bundle flush, so the session the
    shipped turns ran on is already decided. A cron injection landing in that await
    rebinds the slot's ``linked_session_key``; if the policy were read off a freshly
    resolved key, the file would claim a policy belonging to a session its messages
    never ran under -- and a downloaded file cannot correct itself later.
    """
    asked: list[str] = []

    class _Registry:
        def has_session(self, key):
            asked.append(key)
            return True

        def get_approval_policy(self, key):
            return "auto" if key == "dashboard:slot-1" else "never"

    state = _state(MSGS, sessions=_Registry())
    slot = _slot(MSGS)
    # The rebind that a cron injection performs, landing after the pin.
    slot.linked_session_key = "cron:job-7"

    from kiro_crew.dashboard.session_transfer import _snapshot_source_record

    source = _snapshot_source_record(state, slot, "dashboard:slot-1")

    assert asked == ["dashboard:slot-1"]
    assert source["approval_policy"] == "auto"


# ── the filename is an egress surface ────────────────────────────────────


def test_the_slug_comes_from_the_redacted_title():
    """A filename is displayed by whatever holds the file, so a credential in a
    title must not reach it. The bundle's title is already redacted; this pins
    that the slug is built from that copy and not from a raw one."""
    from kiro_crew.security import redact_credentials

    raw = "debug ghp_0123456789abcdefghijklmnopqrstuvwxyz now"
    redacted, _ = redact_credentials(raw)
    assert redacted != raw, "the fixture must actually be redactable"

    name = se.export_filename(redacted)
    assert "ghp-0123456789abcdefghijklmnopqrstuvwxyz" not in name
    assert "0123456789abcdefghijklmnopqrstuvwxyz" not in name


def test_the_filename_carries_the_slug_stamp_and_suffix():
    name = se.export_filename("Design chat", stamp="20260909T083244Z")
    assert name == "design-chat-20260909T083244Z.kcsession.json.gz"


def test_a_non_latin_title_keeps_its_name_hint():
    """The name hint survives for the readers most likely to need it.

    Reducing the slug to ASCII would throw the whole hint away for a CJK,
    Cyrillic or accented title. The header carries it percent-encoded instead,
    which is the spelling every other download handler here already ships.
    """
    # Escaped so this file itself stays ASCII: U+4F1A U+8BDD is "session" in
    # Chinese, and U+00E9 is an accented Latin letter.
    name = se.export_filename("\u4f1a\u8bdd \u00e9", stamp="S")
    assert name.startswith("\u4f1a\u8bdd-\u00e9")
    assert name.endswith(".kcsession.json.gz")


def test_the_disposition_header_is_percent_encoded_and_injection_proof():
    header = se.content_disposition(se.export_filename("\u4f1a\u8bdd", stamp="S"))
    assert header.startswith("attachment; filename*=UTF-8''")
    assert header.isascii(), "a header must be ASCII on the wire"

    # A title cannot contribute a quote, a semicolon, a CR or an LF: the slug
    # drops them and percent-encoding would neutralise whatever survived.
    hostile = se.content_disposition(
        se.export_filename('evil\r\nX-Injected: 1 "quoted"', stamp="S")
    )
    for forbidden in ("\r", "\n", '"'):
        assert forbidden not in hostile
    assert hostile.count(";") == 1, "only the disposition's own separator"


def test_a_title_with_no_usable_characters_still_yields_a_name():
    assert se.export_filename("", stamp="S") == "session-S.kcsession.json.gz"
    assert se.export_filename("!!! ---", stamp="S") == "session-S.kcsession.json.gz"


def test_the_slug_is_length_capped():
    slug = se.slugify_title("word " * 200)
    assert len(slug) <= 60
    assert not slug.endswith("-")


def test_the_stamp_is_filename_safe():
    stamp = se.export_stamp()
    assert ":" not in stamp
    assert stamp.endswith("Z")
    assert stamp.isascii()


# ── the endpoint ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_streams_a_gzipped_bundle():
    slot = _slot(MSGS, title="Design chat")
    state = _state(MSGS, sessions=_FakeSessions({SESSION_KEY: "auto"}), slots={"slot-1": slot})

    resp = await se.api_chat_slot_export(_request(state))

    assert resp.status == 200
    assert resp.content_type == "application/gzip"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "design-chat-" in resp.headers["Content-Disposition"]
    assert resp.headers["Content-Disposition"].startswith("attachment; filename*=UTF-8''")
    assert resp.headers["Content-Disposition"].endswith(".kcsession.json.gz")

    document = json.loads(gzip.decompress(resp.body))
    assert document["bundle_version"] == 2
    assert [m["content"] for m in document["messages"]] == [
        "how does the tunnel work?",
        "it forwards loopback",
    ]
    assert document["source"]["approval_policy"] == "auto"


@pytest.mark.asyncio
async def test_incognito_and_temporary_sessions_are_refused():
    """Those transcripts exist under a promise that nothing is kept; writing one
    into a file the user then stores somewhere is the opposite of that."""
    for mode in ("incognito", "temporary"):
        slot = _slot(MSGS, memory_mode=mode)
        state = _state(MSGS, slots={"slot-1": slot})

        resp = await se.api_chat_slot_export(_request(state))

        assert resp.status == 400, mode
        assert json.loads(resp.body)["code"] == "export_slot_not_persistent"


@pytest.mark.asyncio
async def test_an_unknown_slot_is_a_404():
    resp = await se.api_chat_slot_export(_request(_state(MSGS), slot_key="nope"))
    assert resp.status == 404
    assert json.loads(resp.body)["code"] == "export_slot_not_found"


@pytest.mark.asyncio
async def test_an_app_cannot_export_a_slot_it_does_not_own():
    """The app sandbox boundary: without this an app token could name another
    slot's key and download that transcript.

    The status is 404 and not 403 on purpose — a slot owned by somebody else has
    to be indistinguishable from one that does not exist, or the code itself
    enumerates slots across the boundary.
    """
    slot = _slot(MSGS, app="other-app")
    state = _state(MSGS, slots={"slot-1": slot})

    resp = await se.api_chat_slot_export(_request(state, app="my-app"))

    assert resp.status == 404
    assert json.loads(resp.body)["code"] == "export_slot_not_found"


@pytest.mark.asyncio
async def test_an_app_can_export_its_own_slot():
    slot = _slot(MSGS, app="my-app")
    state = _state(MSGS, slots={"slot-1": slot})

    resp = await se.api_chat_slot_export(_request(state, app="my-app"))

    assert resp.status == 200


@pytest.mark.asyncio
async def test_an_empty_session_is_refused_rather_than_handed_over():
    """The importer's floor requires a non-empty ``messages`` array, so an empty
    file would download cleanly and be rejected wherever it was taken."""
    slot = _slot([])
    state = _state([], slots={"slot-1": slot})

    resp = await se.api_chat_slot_export(_request(state))

    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "export_bundle_empty"


@pytest.mark.asyncio
async def test_an_unstable_snapshot_is_a_retryable_503(monkeypatch):
    from kiro_crew.dashboard.session_transfer import SnapshotUnstable

    async def _unstable(*_a, **_k):
        raise SnapshotUnstable("a pending rewrite means the on-disk transcript is stale")

    monkeypatch.setattr(se, "build_transfer_bundle_async", _unstable)
    state = _state(MSGS, slots={"slot-1": _slot(MSGS)})

    resp = await se.api_chat_slot_export(_request(state))

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "export_snapshot_unstable"


@pytest.mark.asyncio
async def test_any_other_build_failure_is_an_audited_500(monkeypatch):
    """Every exit from the handler records an SEL outcome.

    An unhandled exception would leave the one case an operator most wants to
    find as the only export with no audit line at all.
    """

    async def _boom(*_a, **_k):
        raise OSError("the transcript would not read")

    monkeypatch.setattr(se, "build_transfer_bundle_async", _boom)
    state = _state(MSGS, slots={"slot-1": _slot(MSGS)})

    resp = await se.api_chat_slot_export(_request(state))

    assert resp.status == 500
    assert json.loads(resp.body)["code"] == "export_failed"


# ── Layer B does not travel in a file ────────────────────────────────────


@pytest.mark.asyncio
async def test_an_export_carries_no_layer_b_and_says_so(monkeypatch):
    """The destination rule, as a gate.

    Layer B ships byte-exact and UNREDACTED, which is forced: the thinking-block
    signatures inside it are validated on replay, so redacting and transplanting
    cannot both hold. What makes byte-exact acceptable is the destination -- the
    operator's own authenticated peer, stored 0600. A file has no destination, so
    the context stays behind, and the bundle must SAY it withheld context rather
    than leave a reader to infer it from an absent key.
    """
    import kiro_crew.dashboard.session_transfer as st

    # A session that genuinely HAS resumable context; the refusal must be by
    # destination, not by there being nothing to carry.
    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *_a, **_k: "a-real-sid")
    # Faithful to the real reader, which returns None for an empty sid -- a stub
    # that answered for "" would hide the very gate under test.
    monkeypatch.setattr(
        st,
        "_read_layer_b",
        lambda sid: {"sid": sid, "envelope": {}, "events": "{}"} if sid else None,
    )

    over_tunnel = await build_transfer_bundle_async(_state(MSGS), _slot(MSGS), origin="mac")
    assert "layer_b" in over_tunnel, "the tunnel must still carry context"

    slot = _slot(MSGS)
    state = _state(MSGS, slots={"slot-1": slot})
    resp = await se.api_chat_slot_export(_request(state))
    assert resp.status == 200

    document = json.loads(gzip.decompress(resp.body))
    assert "layer_b" not in document
    assert document["layer_b_skipped"] is True
    assert "a-real-sid" not in json.dumps(document)


@pytest.mark.asyncio
async def test_the_builder_refuses_layer_b_when_asked(monkeypatch):
    import kiro_crew.dashboard.session_transfer as st

    monkeypatch.setattr(st, "_resolve_layer_b_sid", lambda *_a, **_k: "a-real-sid")
    # Faithful to the real reader, which returns None for an empty sid -- a stub
    # that answered for "" would hide the very gate under test.
    monkeypatch.setattr(
        st,
        "_read_layer_b",
        lambda sid: {"sid": sid, "envelope": {}, "events": "{}"} if sid else None,
    )

    bundle = await build_transfer_bundle_async(
        _state(MSGS), _slot(MSGS), origin="mac", include_layer_b=False
    )

    assert "layer_b" not in bundle
    assert bundle["layer_b_skipped"] is True


@pytest.mark.asyncio
async def test_a_session_that_never_had_context_is_not_flagged_as_degraded():
    """``layer_b_skipped`` means "this session HAD context and gave it up".

    The importer appends a "transcript only" suffix to the tab title on the
    strength of that flag, so setting it for a session that never opened a
    kiro-cli context would label an undegraded copy as degraded -- on every such
    export, not in some corner.
    """
    slot = _slot(MSGS)
    state = _state(MSGS, slots={"slot-1": slot})

    resp = await se.api_chat_slot_export(_request(state))
    assert resp.status == 200

    document = json.loads(gzip.decompress(resp.body))
    assert "layer_b" not in document
    assert "layer_b_skipped" not in document


def test_the_free_text_provenance_fields_are_redacted():
    """``workspace`` and ``project`` are the only free text in the record, and the
    document is an egress boundary, so they go through the same scan as the
    title."""
    from kiro_crew.security import redact_credentials

    raw = "/home/me/ghp_0123456789abcdefghijklmnopqrstuvwxyz/checkout"
    assert redact_credentials(raw)[0] != raw, "the fixture must actually be redactable"

    source = build_source_record(project=raw, workspace=raw)

    assert "0123456789abcdefghijklmnopqrstuvwxyz" not in source["project"]
    assert "0123456789abcdefghijklmnopqrstuvwxyz" not in source["workspace"]


@pytest.mark.asyncio
async def test_a_bundle_the_importer_would_reject_is_not_handed_over(monkeypatch):
    """A producer must not emit a document its own reader refuses.

    Past the importer's bounds the file would download cleanly, cost the user a
    download, and then be rejected wherever they took it. The bounds are consulted
    through the validator itself rather than restated, so the two cannot drift.
    """
    oversized = {
        "bundle_version": 2,
        "origin": "mac",
        "title": "huge",
        "agent": "",
        "messages": [{"role": "user", "content": "x", "ts": ""} for _ in range(5001)],
    }

    async def _huge(*_a, **_k):
        return oversized

    monkeypatch.setattr(se, "build_transfer_bundle_async", _huge)
    state = _state(MSGS, slots={"slot-1": _slot(MSGS)})

    resp = await se.api_chat_slot_export(_request(state))

    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["code"] == "export_bundle_rejected"
    # Which bound was hit is carried in prose and in the audit record, not as a
    # separate machine-readable field: nothing reads one off the wire.
    assert "too many messages" in body["error"]
    assert "importer_code" not in body


def test_the_rejection_reason_comes_from_the_importer_itself():
    from kiro_crew.dashboard.session_transfer import bundle_rejection_reason

    ok = {
        "bundle_version": 2,
        "messages": [{"role": "user", "content": "hi", "ts": ""}],
    }
    assert bundle_rejection_reason(ok) == ("", "")

    reason, code = bundle_rejection_reason({"bundle_version": 2, "messages": []})
    assert code == "transfer_bundle_empty"
    assert reason


@pytest.mark.asyncio
async def test_an_app_cannot_export_a_channel_linked_slot_it_owns():
    """Owning the SLOT is not owning the TRANSCRIPT.

    A channel-linked slot displays a conversation that lives on the channel's own
    session, and ``get_or_create_slot`` auto-binds that link from a channel-shaped
    NAME the creating caller supplies. So an app can hold a slot it legitimately
    owns whose transcript is a channel's. The boundary refuses rather than
    reasoning about the binding.
    """
    slot = _slot(MSGS, app="my-app")
    slot.linked_session_key = "slack:1700000000.000100"
    state = _state(MSGS, slots={"slot-1": slot})

    resp = await se.api_chat_slot_export(_request(state, app="my-app"))

    assert resp.status == 404
    # Indistinguishable from an unknown slot: a separate code would let an app
    # learn which of its slots carry a channel link.
    assert json.loads(resp.body)["code"] == "export_slot_not_found"


@pytest.mark.asyncio
async def test_the_dashboard_owner_can_still_export_a_channel_linked_slot():
    """The refusal is the APP boundary, not a general restriction: the owner is
    entitled to both the slot and the channel transcript behind it."""
    slot = _slot(MSGS)
    slot.linked_session_key = "slack:1700000000.000100"
    state = _state(MSGS, slots={"slot-1": slot})

    resp = await se.api_chat_slot_export(_request(state))

    assert resp.status == 200


def test_a_lone_surrogate_does_not_crash_serialisation():
    """A transcript can legitimately hold one.

    ``_validate_bundle`` accepts any ``str`` content and ``json.loads('"\\ud800"')``
    yields a lone surrogate, so an imported conversation can persist one. Encoding
    that as UTF-8 raises unless JSON's ASCII escaping is left on -- which would turn
    a readable session into a 500 at export time.
    """
    lone = json.loads('"\\ud800"')
    document = {
        "bundle_version": 2,
        "messages": [{"role": "user", "content": lone, "ts": ""}],
    }

    raw = se.gzip_bundle(document)

    assert json.loads(gzip.decompress(raw))["messages"][0]["content"] == lone


def test_gzip_is_deterministic_for_one_document():
    """``mtime=0``: the export instant is already inside the document, so a
    second copy in the gzip header would only make identical exports differ."""
    document = {"bundle_version": 2, "messages": [{"role": "user", "content": "hi", "ts": ""}]}
    assert se.gzip_bundle(document) == se.gzip_bundle(document)
    assert json.loads(gzip.decompress(se.gzip_bundle(document))) == document
