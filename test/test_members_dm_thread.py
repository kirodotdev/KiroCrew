"""Crew Members DM threads: binding persistence, routes, and pin enforcement.

Covers spec task 2 of the Crew Members page:

* ``dm.json`` binding read/write in the existing per-member space
  (``$KIROCREW_HOME/members/<slug>/``, isolated per test by the autouse
  ``_isolate_kirocrew_home`` fixture).
* Route contract for ``GET /api/members`` and the idempotent
  ``POST /api/members/{slug}/thread``.
* Agent-pin enforcement at every reachable writer: the send-path slot config,
  the agent-switch endpoint, and the mid-turn ``EVENT_AGENT_SWITCHED`` veto.
  Each denial test also asserts the slot state did NOT move (the mutation
  check: removing the guard makes these fail by letting the write land).

The pin and the member-key reservation past the thread route (the mode writer, resume,
restart restore, the OpenAI-compatible completions path, binding drift) live in
``test_members_dm_thread_resume_and_reservation.py``; the activity and briefing routes,
the event-log service lifetime and the denial audits in
``test_members_dm_thread_activity_and_briefing.py``; and opening and reopening a thread
on a private store in ``test_members_dm_thread_private_store_reopen.py``. Shared
constants and fakes live in ``members_dm_thread_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from members_dm_thread_helpers import (
    CREW,
    OTHER,
    _fake_config,
    _make_members_app,
    _member_slot,
    _patched_config,
    _record_or_explain,
)

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.members import (
    DM_SLOT_MODE,
    MemberSlugError,
    dm_binding_path,
    member_dir,
    member_slot_key,
    read_dm_binding,
    slug_for_name,
    write_dm_binding,
)
from kiro_crew.testing.wait import await_until
from kiro_crew.validation import normalize_unicode


class TestDmBinding:
    def test_write_then_read_round_trips(self):
        write_dm_binding("code-reviewer", member=CREW, slot_key="member-code-reviewer")
        binding = read_dm_binding("code-reviewer")
        assert binding is not None
        assert binding["member"] == CREW
        assert binding["slug"] == "code-reviewer"
        assert binding["slot_key"] == "member-code-reviewer"
        assert binding["created_ts"]

    def test_binding_lives_inside_the_member_records_leaf(self):
        write_dm_binding("code-reviewer", member=CREW, slot_key="member-code-reviewer")
        path = dm_binding_path("code-reviewer")
        # Identity authority belongs under the gateway-only member-records/ leaf,
        # NOT inside the agent-writable member dir and not under trust/.
        assert "member-records" in path.parts
        assert "trust" not in path.parts
        assert not (member_dir("code-reviewer") / "dm.json").exists()
        assert path.is_file()
        # Plain JSON on disk (atomic_write leaves no temp siblings behind).
        assert json.loads(path.read_text(encoding="utf-8"))["member"] == CREW
        # atomic_write's temps are `*.tmp` siblings; assert on that pattern so
        # a leaked temp is actually observable (a `*.json` glob never sees one).
        assert not list(path.parent.glob("*.tmp"))
        assert list(path.parent.glob("*.json")) == [path]

    def test_read_missing_file_is_none(self):
        assert read_dm_binding("nobody-here") is None

    def test_read_bad_slug_is_none(self):
        assert read_dm_binding("Not A Slug!") is None

    @pytest.mark.parametrize(
        "payload",
        [
            "not json {",
            json.dumps(["a", "list"]),
            json.dumps({"member": CREW}),  # no slot_key
            json.dumps({"slot_key": ""}),  # empty slot_key
            json.dumps({"slot_key": "member-x"}),  # no member
            json.dumps({"slot_key": 7, "member": CREW}),  # non-string slot_key
        ],
    )
    def test_read_malformed_payload_is_none(self, payload):
        path = dm_binding_path("code-reviewer")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        assert read_dm_binding("code-reviewer") is None

    def test_read_invalid_utf8_is_none(self):
        """Invalid UTF-8 bytes are the same totality case as unreadable IO.

        A raise here would 500 every member API off one corrupt dm.json."""
        path = dm_binding_path("code-reviewer")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"member": "\xff\xfe broken')
        assert read_dm_binding("code-reviewer") is None

    def test_write_rejects_bad_slug(self):
        with pytest.raises(MemberSlugError):
            write_dm_binding("../escape", member=CREW, slot_key="member-x")

    def test_non_canonical_slot_key_reads_as_absent(self):
        """A binding whose slot_key is not the slug's derivation is unusable.

        dm.json pointing anywhere else would let a tampered or stale file mount
        an arbitrary session as the member's thread (the roster trusts `bound`
        rows enough that the page skips the create POST). Non-canonical reads
        as None, so the thread endpoint repairs it to the derived key.
        """
        path = dm_binding_path("code-reviewer")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"member": CREW, "slot_key": "dashboard:chat-7-123"}),
            encoding="utf-8",
        )
        assert read_dm_binding("code-reviewer") is None

    def test_slot_key_derivation(self):
        assert member_slot_key("code-reviewer") == "member-code-reviewer"

    def test_slot_key_rejects_bad_slug(self):
        with pytest.raises(MemberSlugError):
            member_slot_key("Bad Slug")


class TestRestrictedRestartMemberIdentity:
    """A restricted chat must not read a member's private store it cannot prove
    it ran as, after an alias change across a restart.

    A restricted (incognito/temporary) session persists no durable execution
    carrier, so on restart it re-selects the member from the alias its slot
    carries. Binding that member's PRIVATE store is sound only when a durable
    identity the session itself carries -- the slot KEY, never the agent-writable
    metadata line -- confirms the alias still names the same member. A DM slot
    key encodes the ``member_id``; an ordinary ``chat-N`` slot encodes none.
    Resolve the hazard by refusing (open a new conversation) rather than
    failing open: a DM slot whose encoded id DIFFERS from the resolved member
    (the thread belongs to a specific member), and a restricted ordinary slot
    whose alias resolves to a V2 member (reading its private store is a
    cross-member read, and substituting the default store reads Global memory
    the chat did not run as -- so neither is bound).
    """

    @staticmethod
    def _member(cfg, tmp_path, alias, member_id, store):
        from kiro_crew.config.sections import KiroCrewAgentConfig, MemoryStoreConfig
        from kiro_crew.vector_memory import create_member_database

        path = tmp_path / "memory_stores" / store / "memory.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        create_member_database(path, member_id=member_id, store_id=store)
        cfg.agents[alias] = KiroCrewAgentConfig(
            member_id=member_id, memory_store=store, kiro_agent="shared-template"
        )
        cfg.memory_stores[store] = MemoryStoreConfig(
            owner_member=alias, owner_member_id=member_id, memory_version=2
        )

    def test_dm_slot_refuses_after_alias_reassignment(self, tmp_path, monkeypatch):
        from kiro_crew import execution_context as execution
        from kiro_crew.config import loader
        from kiro_crew.dashboard.chat_runner import (
            _dm_member_binding_changed,
            _slot_member_slug,
        )

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        loader._invalidate_config_cache()
        cfg = SimpleNamespace(agents={}, memory_stores={}, default_agent="kirocrew")
        # The original member the incognito DM chat ran as. Its slug IS its id
        # (a MemV2 member always gets an id-based slug), so the slot key encodes
        # the immutable id.
        self._member(cfg, tmp_path, "assistant", "id-alpha", "member-alpha")
        monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))

        slot_key = member_slot_key("id-alpha")

        # While the chat is restarting, the alias "assistant" is reassigned to a
        # brand-new member (the original removed, a new one created under the
        # same name), minting a different immutable id and store.
        cfg.agents.clear()
        cfg.memory_stores.clear()
        self._member(cfg, tmp_path, "assistant", "id-beta", "member-beta")

        # The slot key still encodes the ORIGINAL id -- durable across the
        # original member's removal.
        assert _slot_member_slug(slot_key) == "id-alpha"
        resolved = execution.resolve_member_execution(
            cfg, "assistant", memory_mode="incognito", validate_memory_files=False
        )
        assert resolved.member_id == "id-beta"
        # A DM slot whose encoded id differs from the alias -> refuse.
        assert _dm_member_binding_changed(resolved.member_id, slot_key)

    def test_dm_slot_runs_normally_when_alias_is_unchanged(self, tmp_path, monkeypatch):
        from kiro_crew import execution_context as execution
        from kiro_crew.config import loader
        from kiro_crew.dashboard.chat_runner import (
            _dm_member_binding_changed,
            _slot_member_slug,
        )

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        loader._invalidate_config_cache()
        cfg = SimpleNamespace(agents={}, memory_stores={}, default_agent="kirocrew")
        self._member(cfg, tmp_path, "assistant", "id-alpha", "member-alpha")
        monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))

        slot_key = member_slot_key("id-alpha")
        resolved = execution.resolve_member_execution(
            cfg, "assistant", memory_mode="incognito", validate_memory_files=False
        )
        # No reassignment: the alias still resolves to the same immutable id, so
        # the refuse branch does not fire.
        assert _slot_member_slug(slot_key) == "id-alpha"
        assert resolved.member_id == "id-alpha"
        assert not _dm_member_binding_changed(resolved.member_id, slot_key)

    def test_legacy_member_without_persisted_id_touches_neither_branch(self, tmp_path, monkeypatch):
        from kiro_crew import execution_context as execution
        from kiro_crew.config import loader
        from kiro_crew.config.sections import KiroCrewAgentConfig
        from kiro_crew.dashboard.chat_runner import (
            _dm_member_binding_changed,
            _slot_member_slug,
        )

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        loader._invalidate_config_cache()
        # A legacy (MemV1) member has a name-derived slug and no persisted id, so
        # ``resolve_member_execution`` yields an empty ``member_id`` -- nothing
        # private is at stake, so the DM-mismatch branch does not fire on a
        # member slot key OR an ordinary one.
        slug = slug_for_name("legacy crew")
        slot_key = member_slot_key(slug)
        cfg = SimpleNamespace(
            agents={"legacy crew": KiroCrewAgentConfig(kiro_agent="kirocrew")},
            memory_stores={},
            default_agent="kirocrew",
        )
        monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        resolved = execution.resolve_member_execution(
            cfg, "legacy crew", memory_mode="incognito", validate_memory_files=False
        )
        assert resolved.member_id is None
        assert _slot_member_slug(slot_key) == slug
        assert slug != ""
        assert not _dm_member_binding_changed(resolved.member_id or "", slot_key)
        assert not _dm_member_binding_changed(resolved.member_id or "", "chat-7-f00d")

    def test_non_member_slot_key_has_no_slug(self):
        from kiro_crew.dashboard.chat_runner import _slot_member_slug

        assert _slot_member_slug("chat-1-abc") == ""


class TestMemberRoutes:
    @pytest.mark.asyncio
    async def test_roster_reuses_config_loaded_off_loop(self, tmp_path, monkeypatch):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context, reset_context, set_context

        cfg = _fake_config([CREW, OTHER])
        loop_thread = threading.get_ident()
        loads = []

        def load():
            loads.append(threading.get_ident())
            return cfg

        monkeypatch.setattr(KiroCrewConfig, "load", load)
        state = _make_state(tmp_path)
        set_context(build_default_context(cfg))
        loads.clear()
        try:
            async with TestClient(TestServer(_make_members_app(state))) as client:
                response = await client.get("/api/members")
                assert response.status == 200
                assert len((await response.json())["members"]) == 2
        finally:
            reset_context()
        assert loads and loop_thread not in loads
        assert len(loads) == 1

    @pytest.mark.asyncio
    async def test_roster_lists_global_crews_with_slugs(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW, "Docs_Writer"]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members")
                assert resp.status == 200
                data = await resp.json()
        rows = {r["name"]: r for r in data["members"]}
        assert rows[CREW]["slug"] == "code-reviewer"
        assert rows["Docs_Writer"]["slug"] == "docs-writer"
        assert rows[CREW]["slot_key"] == ""
        assert rows[CREW]["running"] is False
        # The row is an explicit allowlist: no dataclass spread, no `bound`
        # (the page never trusts it), no top-level default_agent.
        assert "bound" not in rows[CREW]
        assert "default_agent" not in data
        # The avatar override IS allowlisted (presentation-only, validated at
        # load) — without it every Members surface shows the name-derived face.
        assert rows[CREW]["avatar"] == {}
        # Unbound members have never talked: last activity reads as 0.
        assert rows[CREW]["last_active_ts"] == 0.0

    @pytest.mark.asyncio
    async def test_roster_hides_a_credential_shaped_legacy_name(self, tmp_path):
        from kiro_crew.external_text import external_text_requires_redaction

        name = "crew password=shortvalue"
        assert external_text_requires_redaction(name)
        state = _make_state(tmp_path)
        with _patched_config([name], default=name):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                response = await client.get("/api/members")
                assert response.status == 200
                assert (await response.json())["members"] == []

    def test_shared_redactor_applies_a_companion_policy_on_top_of_the_baseline(self):
        import dataclasses

        from kiro_crew import security
        from kiro_crew.external_text import (
            external_text_requires_redaction,
            redact_external_text,
        )
        from kiro_crew.platform import (
            PROFILE_ENTERPRISE,
            build_default_context,
            reset_context,
            set_context,
        )

        companion_shape = "SSO-COOKIE"
        baseline_shape = "AKIAIOSFODNN7EXAMPLE"

        class _Policy:
            def redact(self, text: str) -> str:
                return security.redact(text).replace(companion_shape, "[REDACTED-SSO]")

        set_context(
            dataclasses.replace(
                build_default_context(KiroCrewConfig(), profile=PROFILE_ENTERPRISE),
                credentials=_Policy(),
            )
        )
        try:
            assert external_text_requires_redaction(f"crew {companion_shape}")
            out = redact_external_text(f"crew {companion_shape} {baseline_shape} token=abc")
        finally:
            reset_context()
        assert companion_shape not in out
        assert baseline_shape not in out
        assert out.endswith("token=[REDACTED]")

    @pytest.mark.asyncio
    async def test_free_form_name_round_trips_roster_thread_and_activity(self, tmp_path):
        name = "dr. eggbot"
        state = _make_state(tmp_path)

        cfg = _fake_config([name], default=name)
        assert _record_or_explain(name, "dashboard_chat-1", "persistent", via="chat")
        with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                roster_response = await client.get("/api/members")
                assert roster_response.status == 200
                roster = await roster_response.json()
                assert [(row["name"], row["slug"]) for row in roster["members"]] == [
                    ("dr. eggbot", "dr-eggbot")
                ]

                thread_response = await client.post("/api/members/dr-eggbot/thread")
                assert thread_response.status == 200
                assert await thread_response.json() == {
                    "slot_key": "member-dr-eggbot",
                    "slug": "dr-eggbot",
                    "member": "dr. eggbot",
                }

                activity_response = await client.get(
                    "/api/members/dr-eggbot/activity", params={"member": name}
                )
                assert activity_response.status == 200
                activity = await activity_response.json()
                assert activity["member"] == "dr. eggbot"
                assert len(activity["entries"]) == 1

        assert read_dm_binding("dr-eggbot")["member"] == "dr. eggbot"
        assert state._slots["member-dr-eggbot"].agent == "dr. eggbot"

    @pytest.mark.asyncio
    async def test_nfd_legacy_member_survives_roster_and_thread_open(self, tmp_path):
        name = "Cafe\u0301"
        assert normalize_unicode(name) != name
        state = _make_state(tmp_path)
        cfg = _fake_config([name], default=name)
        assert _record_or_explain(name, "dashboard_chat-1", "persistent", via="chat")
        with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                roster_response = await client.get("/api/members")
                assert roster_response.status == 200
                roster = await roster_response.json()
                assert [(row["name"], row["slug"]) for row in roster["members"]] == [(name, "cafe")]

                thread_response = await client.post("/api/members/cafe/thread")
                assert thread_response.status == 200
                body = await thread_response.json()
                assert body == {"slot_key": "member-cafe", "slug": "cafe", "member": name}

                activity_response = await client.get(
                    "/api/members/cafe/activity", params={"member": name}
                )
                assert activity_response.status == 200
                activity = await activity_response.json()
                assert activity["member"] == name
                assert len(activity["entries"]) == 1

        assert read_dm_binding("cafe")["member"] == name
        assert state._slots["member-cafe"].agent == name

    @pytest.mark.asyncio
    async def test_roster_reports_last_activity_from_the_dm_transcript(self, tmp_path):
        """last_active_ts is the DM transcript's mtime — the roster's sort key.

        The transcript file is the one durable signal that survives restarts
        and covers live and dormant threads alike; a bound member with no
        transcript still reads 0 rather than erroring.
        """
        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        state.conversation_log.append(key, "user", "hello")
        with _patched_config([CREW, "Docs_Writer"]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members")
                assert resp.status == 200
                data = await resp.json()
        rows = {r["name"]: r for r in data["members"]}
        assert rows[CREW]["last_active_ts"] > 0
        # The row also carries the transcript's last-message preview — the
        # roster sub-line, same data a session row shows.
        assert rows[CREW]["last_message"] == "hello"
        # A member with no transcript stays at 0 — sorted last, never an error.
        assert rows["Docs_Writer"]["last_active_ts"] == 0.0
        assert rows["Docs_Writer"]["last_message"] == ""
        # No stop card in either thread, so the flag is absent (omitted when
        # false — see below) on both rows.
        assert "last_message_stopped" not in rows[CREW]
        assert "last_message_stopped" not in rows["Docs_Writer"]

    @pytest.mark.asyncio
    async def test_roster_flags_a_thread_whose_newest_event_is_a_stop(self, tmp_path):
        """A just-stopped thread carries last_message_stopped=True on the wire.

        The preview is the last CONVERSATIONAL line (the stop card's JSON is
        skipped), but that line reads as ongoing work on a thread the user has
        stopped. So the row also carries a locale-independent boolean the
        locale-aware client turns into a "Stopped" chip — never the word
        "Stopped" from here, where the client's locale is unknown. The flag is
        OMITTED (not False) when the newest event is not a stop, so the common
        row stays byte-for-byte what it is without it; a later real message
        leaves it absent.
        """
        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        state.conversation_log.append(key, "assistant", "Running the analysis now.")
        stop_payload = json.dumps({"kind": "stop_event", "id": "s1", "state": "stopped"})
        state.conversation_log.append(key, "system", stop_payload, cls=stop_payload)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members")
                assert resp.status == 200
                data = await resp.json()
        row = {r["name"]: r for r in data["members"]}[CREW]
        # Preview is the conversational line, not the stop JSON…
        assert row["last_message"] == "Running the analysis now."
        # …and the flag says the newest event is a stop, so the chip renders.
        assert row["last_message_stopped"] is True

        # The member speaks again: the newest real row is now that message, the
        # flag clears to absent, and the chip comes down.
        state.conversation_log.append(key, "user", "actually, hold on")
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members")
                data = await resp.json()
        row = {r["name"]: r for r in data["members"]}[CREW]
        assert row["last_message"] == "actually, hold on"
        assert "last_message_stopped" not in row

    @pytest.mark.asyncio
    async def test_roster_preview_redacts_before_truncation(self, tmp_path):
        """A credential straddling the preview's length cap never leaks.

        Redaction runs on the FULL text before the 120-char cap: truncating
        first splits the token, and the pattern-based redactors cannot match
        a partial credential — its raw prefix would reach /api/members.
        """
        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        # Padding places the AKIA token across the 120-char boundary.
        secret = "AKIAIOSFODNN7EXAMPLE"
        state.conversation_log.append(key, "assistant", "x" * 110 + " " + secret)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members")
                assert resp.status == 200
                data = await resp.json()
        preview = {r["name"]: r for r in data["members"]}[CREW]["last_message"]
        # Neither the full token nor any partial prefix of it survives.
        assert "AKIA" not in preview

    @pytest.mark.asyncio
    async def test_roster_preview_uses_shared_external_text_redaction(self, tmp_path):
        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        state.conversation_log.append(key, "assistant", "password=shortvalue")
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                response = await client.get("/api/members")
                assert response.status == 200
                data = await response.json()

        preview = {row["name"]: row for row in data["members"]}[CREW]["last_message"]
        assert preview == "password=[REDACTED]"

    @pytest.mark.asyncio
    async def test_roster_orders_by_message_ts_not_file_mtime(self, tmp_path, monkeypatch):
        """last_active_ts is the newest MESSAGE's own timestamp.

        Non-message writes (metadata, rehydration) bump the transcript file's
        mtime without any new message; ordering on mtime made rows reorder
        with no visible cause. Bumping the older thread's file mtime to the
        newest time must NOT promote it above the thread whose message is
        actually newer.
        """
        import datetime as _dt
        import os
        import time

        # append stamps each row via monotonic_transcript_ts, whose correction
        # only consults prior rows of the SAME file — two freshly created files
        # each get a raw clock read. On a coarse clock (Windows' ~15ms tick)
        # these back-to-back appends collide, and the strict `<` below fails on
        # equal timestamps. Drive a strictly-increasing clock so the
        # chronological order this test asserts is actually encoded in the
        # timestamps, on every OS. The stand-in must be tz-aware: append calls
        # .astimezone() on the result.
        _base = _dt.datetime(2026, 7, 25, 0, 0, 0, tzinfo=_dt.timezone.utc)
        _tick = {"n": 0}

        # Subclass keeps datetime classmethods (fromisoformat) available while
        # patched, so _parse_transcript_ts is not silently degraded.
        class _IncDateTime(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                _tick["n"] += 1
                return _base + _dt.timedelta(seconds=_tick["n"])

        monkeypatch.setattr("kiro_crew.history.datetime", _IncDateTime)

        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        write_dm_binding(
            "docs-writer", member="Docs_Writer", slot_key=member_slot_key("docs-writer")
        )
        old_key = f"dashboard:{member_slot_key(CREW)}"
        new_key = f"dashboard:{member_slot_key('docs-writer')}"
        state.conversation_log.append(old_key, "user", "older message")
        state.conversation_log.append(new_key, "user", "newer message")
        # Touch the OLDER thread's file so its mtime is the newest of the two.
        # Deliberately the REAL clock, far ahead of the fixed fake message
        # timestamps: the mtime-vs-ts contrast is the point of this test — do
        # not "align" the two clocks.
        old_path = state.conversation_log._path(old_key)
        now = time.time() + 60
        os.utime(old_path, (now, now))
        with _patched_config([CREW, "Docs_Writer"]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members")
                assert resp.status == 200
                data = await resp.json()
        rows = {r["name"]: r for r in data["members"]}
        assert rows[CREW]["last_active_ts"] < rows["Docs_Writer"]["last_active_ts"]

    @pytest.mark.asyncio
    async def test_thread_create_then_roster_reports_bound(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post("/api/members/code-reviewer/thread")
                assert resp.status == 200
                created = await resp.json()
                resp = await client.get("/api/members")
                data = await resp.json()
        assert created == {
            "slot_key": "member-code-reviewer",
            "slug": "code-reviewer",
            "member": CREW,
        }
        row = data["members"][0]
        assert row["slot_key"] == "member-code-reviewer"
        slot = state._slots["member-code-reviewer"]
        assert slot.agent == CREW
        assert slot.mode == DM_SLOT_MODE

    @pytest.mark.asyncio
    async def test_thread_is_titled_with_the_crewmate_name_as_a_final_title(self, tmp_path):
        """A member thread has no background title, so opening it names it."""
        state = _make_state(tmp_path)
        cfg = _fake_config([CREW])
        cfg.agents[CREW].display_name = "Code Reviewer"
        with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                assert (await client.post("/api/members/code-reviewer/thread")).status == 200
                slot = state._slots["member-code-reviewer"]
                assert slot.title == "Code Reviewer"
                assert slot.display_title == "Code Reviewer"
                assert slot._titled is True
                # Final: the refresh is locked out, and a second open changes nothing.
                assert slot._title_origin == "member"
                epoch = slot._title_epoch
                assert (await client.post("/api/members/code-reviewer/thread")).status == 200
                assert slot._title_epoch == epoch

    @pytest.mark.asyncio
    async def test_reopening_keeps_a_title_a_person_set(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _member_slot(state)
        slot.title = "My own name for this thread"
        slot._titled = True
        slot._title_origin = "user"
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                assert (await client.post("/api/members/code-reviewer/thread")).status == 200
        assert slot.title == "My own name for this thread"
        assert slot._title_origin == "user"

    @pytest.mark.asyncio
    async def test_reopening_replaces_an_old_generated_topic_with_the_name(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _member_slot(state)
        slot.title = "Crew page text colour check"
        slot._titled = True
        slot._title_origin = "auto"
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                assert (await client.post("/api/members/code-reviewer/thread")).status == 200
        assert slot.title == CREW
        assert slot._title_origin == "member"

    @pytest.mark.asyncio
    async def test_reopening_follows_a_renamed_crewmate(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = _fake_config([CREW])
        cfg.agents[CREW].display_name = "Code Reviewer"
        with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                assert (await client.post("/api/members/code-reviewer/thread")).status == 200
                slot = state._slots["member-code-reviewer"]
                assert slot.title == "Code Reviewer"
                cfg.agents[CREW].display_name = "Senior Reviewer"
                assert (await client.post("/api/members/code-reviewer/thread")).status == 200
        assert slot.title == "Senior Reviewer"
        assert slot._title_origin == "member"

    @pytest.mark.asyncio
    async def test_reopening_keeps_a_user_title_that_equals_the_slot_key(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _member_slot(state)
        slot.title = slot.key
        slot._titled = True
        slot._title_origin = "user"
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                assert (await client.post("/api/members/code-reviewer/thread")).status == 200
        assert slot.title == "member-code-reviewer"

    @pytest.mark.asyncio
    async def test_a_credential_shaped_display_name_is_redacted_before_it_is_stored_or_pushed(
        self, tmp_path
    ):
        state = _make_state(tmp_path)
        pushed = []
        state.push_slot_title = lambda key, title, **kw: pushed.append(title)  # type: ignore[method-assign]
        cfg = _fake_config([CREW])
        secret = "AKIAIOSFODNN7EXAMPLE"
        cfg.agents[CREW].display_name = f"Reviewer {secret}"
        with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                assert (await client.post("/api/members/code-reviewer/thread")).status == 200
        slot = state._slots["member-code-reviewer"]
        assert secret not in slot.title
        assert pushed and all(secret not in t for t in pushed)

    @pytest.mark.asyncio
    async def test_the_first_crewmate_without_a_display_name_is_titled_mate(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config(["mate"], default="mate"):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                assert (await client.post("/api/members/mate/thread")).status == 200
        assert state._slots["member-mate"].title == "Mate"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("private", [False, True])
    @pytest.mark.parametrize("workspace", ["team-a", "missing"])
    async def test_thread_create_honors_member_workspace_and_project(
        self, tmp_path, monkeypatch, private, workspace
    ):
        """A new member DM uses the configured workspace for cwd and project guides."""

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.sections import WorkspaceConfig
        from kiro_crew.memory_stores import provision_member_memory

        team_dir = tmp_path / "team-a-workspace"
        team_dir.mkdir()
        cfg = KiroCrewConfig.load()
        cfg.agents[CREW] = KiroCrewAgentConfig(kiro_agent=CREW, workspace=workspace)
        cfg.workspaces["team-a"] = WorkspaceConfig(dir=str(team_dir))
        cfg.default_workspace = "team-a"
        if private:
            pass  # Member routing does not depend on OS isolation.
            await asyncio.to_thread(provision_member_memory, cfg, CREW)
        await asyncio.to_thread(cfg.save)
        state = _make_state(tmp_path)
        frames = []
        monkeypatch.setattr(state, "_slots_broadcast_lock", None)
        monkeypatch.setattr(
            state,
            "_do_slots_broadcast",
            lambda: frames.append(
                [(slot.workspace, slot.project) for slot in state._slots.values()]
            ),
        )
        async with TestClient(TestServer(_make_members_app(state))) as client:
            resp = await client.post(f"/api/members/{CREW}/thread")
            assert resp.status == 200, await resp.text()
            data = await resp.json()
        slot = state._slots[data["slot_key"]]
        assert slot.workspace == "team-a"
        assert slot.project == str(team_dir.resolve())
        assert frames and frames[0] == [("team-a", str(team_dir.resolve()))]
        if private:
            assert slot.memory_store == cfg.agents[CREW].memory_store

    @pytest.mark.asyncio
    async def test_a_concurrent_opener_keeps_its_slot_and_converges_on_the_binding(self, tmp_path):
        """Two openers racing the same thread end on ONE slot, bound to the crew.

        The slot the other opener published is kept (never re-minted); its
        workspace and project are the crew's configured binding, which both
        openers derive from the same config, so the race cannot leave the
        thread on a directory the crew is not bound to.
        """
        state = _make_state(tmp_path)
        entered, release = threading.Event(), threading.Event()
        resolved = str(tmp_path / "resolved")

        def resolve(workspace):
            entered.set()
            assert release.wait(timeout=5)
            return resolved

        with (
            _patched_config([CREW]),
            patch("kiro_crew.dashboard.handlers.members.default_project_dir", resolve),
        ):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                pending = asyncio.create_task(client.post(f"/api/members/{CREW}/thread"))
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    slot = state.get_or_create_slot(
                        member_slot_key(CREW), agent=CREW, mode=DM_SLOT_MODE, workspace="chosen"
                    )
                    slot.project = str(tmp_path / "chosen")
                finally:
                    release.set()
                    response = await await_until(pending, what="the POST /thread response")
                assert response.status == 200
        assert state._slots[member_slot_key(CREW)] is slot
        assert slot.workspace == "default"
        assert slot.project == resolved

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prior", ["", "/somewhere/else"])
    async def test_reopening_live_member_binds_the_configured_workspace(
        self, tmp_path, monkeypatch, prior
    ):
        """A crewmate's project is its crew's workspace, derived from config on every open.

        Not a per-thread choice read back from the transcript: a thread restored
        with an empty project (written before member slots inherited one -- the
        pinned Files tab read "no project directory" for it) and a thread left on
        another directory by an in-session ``set_project`` both converge on the
        binding. Resolved through the member's workspace name, not a bare default.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(
            member_slot_key(CREW), agent=CREW, mode=DM_SLOT_MODE, workspace="chosen"
        )
        slot.project = prior
        slot.messages.append({"role": "user", "content": "hello"})
        slot._dirty = False
        pushes = []
        monkeypatch.setattr(state, "push_slots_update", lambda **kw: pushes.append(kw))
        resolved = str(tmp_path / "team-workspace")
        with (
            _patched_config([CREW]),
            patch(
                "kiro_crew.dashboard.handlers.members.default_project_dir",
                return_value=resolved,
            ) as resolve,
        ):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                response = await client.post(f"/api/members/{CREW}/thread")
                assert response.status == 200
        assert resolve.call_args.args == ("default",)
        assert slot.workspace == "default"
        assert slot.project == resolved
        # Reaches disk via the periodic flush and the client via a push.
        assert slot._dirty is True
        assert pushes
        # A warm provider keeps the cwd it was spawned in -- a thread that ran
        # with NO directory has one warm in the fallback work dir -- so every
        # directory change arms the deferred reset other project writers arm.
        assert slot._pending_reset_history_key == f"dashboard:{member_slot_key(CREW)}"

    @pytest.mark.asyncio
    async def test_reopening_an_already_bound_member_is_a_no_op(self, tmp_path, monkeypatch):
        """A thread already on its binding is neither re-marked dirty nor re-pushed."""
        state = _make_state(tmp_path)
        resolved = str(tmp_path / "team-workspace")
        slot = state.get_or_create_slot(
            member_slot_key(CREW), agent=CREW, mode=DM_SLOT_MODE, workspace="default"
        )
        slot.project = resolved
        slot.messages.append({"role": "user", "content": "hello"})
        slot._dirty = False
        pushes = []
        monkeypatch.setattr(state, "push_slots_update", lambda **kw: pushes.append(kw))
        with (
            _patched_config([CREW]),
            patch(
                "kiro_crew.dashboard.handlers.members.default_project_dir",
                return_value=resolved,
            ),
        ):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                assert (await client.post(f"/api/members/{CREW}/thread")).status == 200
        assert slot.project == resolved
        assert slot._dirty is False
        assert not pushes
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_a_missing_workspace_directory_leaves_the_thread_alone(self, tmp_path):
        """An unresolvable binding (dir missing or fenced) does not clear a visible directory."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(
            member_slot_key(CREW), agent=CREW, mode=DM_SLOT_MODE, workspace="chosen"
        )
        slot.project = str(tmp_path / "chosen")
        with (
            _patched_config([CREW]),
            patch("kiro_crew.dashboard.handlers.members.default_project_dir", return_value=""),
        ):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                assert (await client.post(f"/api/members/{CREW}/thread")).status == 200
        assert slot.workspace == "chosen"
        assert slot.project == str(tmp_path / "chosen")

    @pytest.mark.asyncio
    async def test_dormant_thread_without_a_project_rehydrates_onto_the_workspace(self, tmp_path):
        """The reporter's case: an old DM transcript with no ``project`` on its line.

        Rehydrate copies the project only when the line carries a non-empty one,
        so without the binding step every reopen of such a thread landed
        project-less. The history still comes back with it.
        """
        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        log = state.conversation_log
        log.append(key, "user", "remember the roadmap discussion")
        log.append(key, "assistant", "noted: roadmap discussion")
        log.update_metadata(key, {"agent": CREW, "mode": DM_SLOT_MODE})
        assert member_slot_key(CREW) not in state._slots
        resolved = str(tmp_path / "team-workspace")
        with (
            _patched_config([CREW]),
            patch(
                "kiro_crew.dashboard.handlers.members.default_project_dir",
                return_value=resolved,
            ),
        ):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post(f"/api/members/{CREW}/thread")
                assert resp.status == 200
        slot = state._slots[member_slot_key(CREW)]
        assert slot.project == resolved
        assert any("roadmap discussion" in str(m.get("content", "")) for m in slot.messages)

    @pytest.mark.asyncio
    async def test_thread_reopen_rehydrates_dormant_history(self, tmp_path):
        """A dormant thread's transcript comes back when the thread reopens.

        Gateway restart outside the restore window (or a ✕-closed thread)
        leaves the canonical transcript on disk with no live slot. Minting a
        bare slot would reopen the DM with EMPTY context — the next reply
        would run without the prior conversation.
        """
        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        log = state.conversation_log
        log.append(key, "user", "remember the roadmap discussion")
        log.append(key, "assistant", "noted: roadmap discussion")
        log.update_metadata(key, {"agent": CREW, "mode": DM_SLOT_MODE})
        assert member_slot_key(CREW) not in state._slots
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post(f"/api/members/{CREW}/thread")
                assert resp.status == 200
        slot = state._slots[member_slot_key(CREW)]
        assert slot.agent == CREW
        assert slot.mode == DM_SLOT_MODE
        # The reopened slot carries the prior DM context, not an empty pane.
        assert any(
            "roadmap discussion" in str(m.get("content", "")) for m in slot.messages
        ), "dormant history was not rehydrated into the reopened thread"

    @pytest.mark.asyncio
    async def test_thread_create_is_idempotent(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                first = await (await client.post("/api/members/code-reviewer/thread")).json()
                second = await (await client.post("/api/members/code-reviewer/thread")).json()
        assert first["slot_key"] == second["slot_key"]
        # Idempotent: the second open returns the same body — repair vs create
        # is not a client-visible distinction.
        assert first == second
        assert len([k for k in state._slots if k.startswith("member-")]) == 1

    @pytest.mark.asyncio
    async def test_thread_invalid_slug_is_400_with_code(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post("/api/members/Not-A-Slug/thread")
                assert resp.status == 400
                assert (await resp.json())["code"] == "invalid_member_slug"
        assert not state._slots

    @pytest.mark.asyncio
    async def test_thread_unknown_member_is_404_with_code(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post("/api/members/nobody-here/thread")
                assert resp.status == 404
                assert (await resp.json())["code"] == "member_not_found"

    @pytest.mark.asyncio
    async def test_thread_refuses_a_foreign_slot_on_the_derived_key(self, tmp_path):
        """A pre-existing non-member slot occupying member-<slug> is never adopted.

        The constructor now refuses to MINT such a squatter, so this simulates
        a legacy one (pre-reservation install) by inserting the slot directly —
        the endpoint must still refuse to adopt it.
        """
        from kiro_crew.dashboard.state import _ChatSlot

        state = _make_state(tmp_path)
        foreign = _ChatSlot("member-code-reviewer", agent="someone-else")
        state._slots[foreign.key] = foreign
        assert foreign.mode != DM_SLOT_MODE
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post("/api/members/code-reviewer/thread")
                assert resp.status == 409
                assert (await resp.json())["code"] == "member_slot_conflict"
        # Mutation check: the foreign slot was not converted or re-pinned.
        assert foreign.agent == "someone-else"
        assert read_dm_binding("code-reviewer") is None

    @pytest.mark.asyncio
    async def test_colliding_slug_stays_with_first_bound_member(self, tmp_path):
        """Two crew names deriving one slug: the binding's member wins."""
        state = _make_state(tmp_path)
        # Both fold to "review-agent"; config order makes Review_Agent first.
        with _patched_config(["Review_Agent", "review-agent"], default="Review_Agent"):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                first = await (await client.post("/api/members/review-agent/thread")).json()
                second = await (await client.post("/api/members/review-agent/thread")).json()
                roster = await (await client.get("/api/members")).json()
        assert first["member"] == "Review_Agent"
        assert second["member"] == "Review_Agent"
        bound_rows = [r for r in roster["members"] if r["slot_key"]]
        assert [r["name"] for r in bound_rows] == ["Review_Agent"]

    @pytest.mark.asyncio
    async def test_colliding_slug_honors_a_binding_naming_the_later_crew(self, tmp_path):
        """A corroborated binding OUTRANKS config order, it does not tie it.

        Binding the second of two colliding names is the only case where the
        bound member and the config-order fallback differ, so it is the only
        case that can observe which of the two the handler picks. Every other
        binding in this suite names the sole owner, where both answers agree.
        """
        state = _make_state(tmp_path)
        write_dm_binding(
            "review-agent", member="review-agent", slot_key=member_slot_key("review-agent")
        )
        with _patched_config(["Review_Agent", "review-agent"], default="Review_Agent"):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                opened = await (await client.post("/api/members/review-agent/thread")).json()
        # Config order answers Review_Agent; the binding answers review-agent.
        assert opened["member"] == "review-agent"
        assert read_dm_binding("review-agent")["member"] == "review-agent"

    @pytest.mark.asyncio
    async def test_app_tokens_are_denied(self, tmp_path):
        state = _make_state(tmp_path)

        @web.middleware
        async def _as_app(request: web.Request, handler):
            request["app"] = "some-app"
            return await handler(request)

        app = _make_members_app(state)
        app.middlewares.insert(0, _as_app)
        with _patched_config([CREW]):
            async with TestClient(TestServer(app)) as client:
                assert (await client.get("/api/members")).status == 404
                assert (await client.post("/api/members/code-reviewer/thread")).status == 404
                assert (await client.get("/api/members/code-reviewer/activity")).status == 404
                briefing = await client.get(
                    "/api/members/code-reviewer/briefing", params={"member": CREW}
                )
                assert briefing.status == 404
                assert (await briefing.json()) == {"error": "not found", "code": "not_found"}
        assert not state._slots

    @pytest.mark.asyncio
    async def test_member_slot_is_excluded_from_the_chat_surface(self, tmp_path):
        """mode="member" is what keeps the thread out of the Sessions list.

        The frontend's single ownership predicate (isChatPageSurface) admits
        only ''/'orchestrator'/'crew'; the serialized payload's mode/surface
        pair is the contract this pins.
        """
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                await client.post("/api/members/code-reviewer/thread")
        payload = state._slots["member-code-reviewer"].to_dict()
        assert payload["mode"] == DM_SLOT_MODE
        assert payload["surface"] == DM_SLOT_MODE


class TestPinEnforcement:
    @pytest.mark.asyncio
    async def test_agent_switch_endpoint_refuses_member_repin(self, tmp_path):
        from chat_test_helpers import _make_app_with_agent_routes

        state = _make_state(tmp_path)
        slot = _member_slot(state)
        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": OTHER})
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_thread_agent_pinned"
        # Mutation check: the pin held — nothing rebound the slot.
        assert slot.agent == CREW

    @pytest.mark.asyncio
    async def test_agent_switch_endpoint_allows_free_form_same_name(self, tmp_path):
        from chat_test_helpers import _make_app_with_agent_routes

        name = "dr. eggbot"
        state = _make_state(tmp_path)
        slot = _member_slot(state, key="member-dr-eggbot", agent=name)
        cfg = KiroCrewConfig()
        cfg.agents = {name: KiroCrewAgentConfig(kiro_agent="kirocrew")}
        cfg.default_agent = name
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
                resp = await client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": name})
                assert resp.status == 200, await resp.text()
        assert slot.agent == name

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["AKIAIOSFODNN7EXAMPLE", "crew password=shortvalue"])
    async def test_agent_switch_refuses_a_non_dispatchable_stored_pin(self, tmp_path, name):
        from chat_test_helpers import _make_app_with_agent_routes

        state = _make_state(tmp_path)
        slot = _member_slot(state, key="member-legacy-credential", agent=name)
        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            response = await client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": name})
            assert response.status == 409
            body = await response.json()

        assert body["code"] == "member_pin_mismatch"
        assert slot.agent == name

    @pytest.mark.asyncio
    @pytest.mark.parametrize("live", [False, True])
    async def test_app_send_hides_free_form_member_slot_existence(self, tmp_path, live):
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        if live:
            _member_slot(state, key="member-dr-eggbot", agent="dr. eggbot")

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = "some-app"
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, _as_app)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat",
                json={
                    "slot": "member-dr-eggbot",
                    "agent": "dr. eggbot",
                    "message": "hello",
                },
            )
            assert response.status == 404
            assert await response.json() == {"error": "not found", "code": "slot_not_found"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("requested", [None, "dr. otherbot"])
    async def test_agent_switch_rejects_invalid_or_nonmatching_free_form_name(
        self, tmp_path, requested
    ):
        from chat_test_helpers import _make_app_with_agent_routes

        state = _make_state(tmp_path)
        slot = _member_slot(state, key="member-dr-eggbot", agent="dr. eggbot")
        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            response = await client.post(
                f"/api/chat/slots/{slot.key}/agent", json={"agent": requested}
            )
            assert response.status == 400
        assert slot.agent == "dr. eggbot"

    @pytest.mark.asyncio
    async def test_send_path_refuses_member_agent_mismatch(self, tmp_path):
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        slot = _member_slot(state)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat", json={"slot": slot.key, "agent": OTHER, "message": "hi"}
                )
                assert resp.status == 409
                assert (await resp.json())["code"] == "member_thread_agent_pinned"
        assert slot.agent == CREW
        # The refused send dispatched nothing into the thread.
        assert not any(m.get("content") == "hi" for m in slot.messages)

    @pytest.mark.asyncio
    async def test_send_path_allows_matching_or_absent_agent(self, tmp_path):
        """The pin refuses MISMATCHES only.

        An empty message stops the request at the message-required check, which
        sits AFTER the pin guard — reaching that 400 instead of the pin's 409
        proves the guard fell through for a matching/absent agent, without
        dispatching a real turn.
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        slot = _member_slot(state)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_app(state))) as client:
                for body in (
                    {"slot": slot.key, "agent": CREW, "message": ""},
                    {"slot": slot.key, "message": ""},
                ):
                    resp = await client.post("/api/chat", json=body)
                    assert resp.status == 400
                    assert (await resp.json()).get("code") != "member_thread_agent_pinned"
        assert slot.agent == CREW

    @pytest.mark.asyncio
    async def test_send_path_allows_matching_free_form_member(self, tmp_path):
        from chat_test_helpers import _make_app

        name = "dr. eggbot"
        state = _make_state(tmp_path)
        slot = _member_slot(state, key="member-dr-eggbot", agent=name)
        cfg = _fake_config([name], default=name)
        with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat", json={"slot": slot.key, "agent": name, "message": ""}
                )
                assert resp.status == 400
                body = await resp.json()
                assert body == {"error": "message is required", "code": "message_required"}
        assert slot.agent == name

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["AKIAIOSFODNN7EXAMPLE", "crew password=shortvalue"])
    async def test_send_path_refuses_a_redaction_requiring_stored_member_pin(self, tmp_path, name):
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        slot = _member_slot(state, key="member-legacy-credential", agent=name)
        cfg = _fake_config([name], default=name)
        with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_make_app(state))) as client:
                for body in (
                    {"slot": slot.key, "agent": name, "message": ""},
                    {"slot": slot.key, "message": ""},
                ):
                    response = await client.post("/api/chat", json=body)
                    assert response.status == 409
                    assert (await response.json())["code"] == "member_pin_mismatch"
        assert slot.agent == name

    @pytest.mark.asyncio
    async def test_runner_refuses_a_redaction_requiring_stored_member_pin(self, tmp_path, caplog):
        from kiro_crew.dashboard.chat_runner import _run_chat
        from kiro_crew.eventlog.service import get_service

        name = "crew password=shortvalue"
        credential = "".join(["AKIA", "IOSFODNN7", "EXAMPLE"])
        slot_key = f"member-{credential}"
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.sessions.get_or_create = AsyncMock(
            side_effect=AssertionError("provider acquisition must not run")
        )
        state.context_builder = MagicMock()
        state.context_builder.build_message = MagicMock(
            side_effect=AssertionError("context construction must not run")
        )
        slot = state.get_or_create_slot(slot_key, agent=name, mode=DM_SLOT_MODE)
        get_service().ensure("legacy-credential", name)
        slot.append("user", "hello", "msg msg-u")
        autonudge = MagicMock()

        with (
            patch("kiro_crew.autonudge.get_instance", return_value=autonudge),
            caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"),
        ):
            await _run_chat(state, slot, "hello")

        errors = [message for message in slot.messages if message["role"] == "error"]
        assert len(errors) == 1
        assert name not in errors[0]["content"]
        logs = "\n".join(caplog.messages)
        assert credential not in logs
        assert "[REDACTED" in logs
        assert slot.messages[-1]["role"] == "done"
        assert any(call.args[0] == "chat_done" for call in state.broadcast_ws.call_args_list)
        assert not any(message["role"] == "assistant" for message in slot.messages)
        autonudge.notify_turn_complete.assert_called_once()
        assert autonudge.notify_turn_complete.call_args.args == (slot.key,)
        state.sessions.get_or_create.assert_not_called()
        state.context_builder.build_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_runner_refuses_a_reassigned_dm_member_after_restricted_restart(
        self, tmp_path, monkeypatch
    ):
        """Drive the real ``_run_chat`` guard: a DM member thread whose alias now
        resolves to a DIFFERENT immutable id (member removed, new one created
        under the same name) must be refused before any provider or context work,
        not bound to the replacement member's private store. Deleting the guard
        regresses this to a silent cross-member bind.
        """
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.sections import KiroCrewAgentConfig, MemoryStoreConfig
        from kiro_crew.dashboard.chat_runner import _run_chat
        from kiro_crew.vector_memory import create_member_database

        def _member(cfg, alias, member_id, store):
            path = tmp_path / "memory_stores" / store / "memory.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            create_member_database(path, member_id=member_id, store_id=store)
            cfg.agents[alias] = KiroCrewAgentConfig(
                member_id=member_id, memory_store=store, kiro_agent="kirocrew"
            )
            cfg.memory_stores[store] = MemoryStoreConfig(
                owner_member=alias, owner_member_id=member_id, memory_version=2
            )

        cfg = KiroCrewConfig.load()
        # The DM slot the incognito chat ran as encodes id-alpha; the alias is
        # then reassigned to a brand-new member before the restart.
        _member(cfg, "assistant", "id-beta", "member-beta")
        cfg.save()
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda c: cfg))

        slot_key = "member-id-alpha"
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.sessions.get_or_create = AsyncMock(
            side_effect=AssertionError("provider acquisition must not run")
        )
        state.context_builder = MagicMock()
        state.context_builder.build_message = MagicMock(
            side_effect=AssertionError("context construction must not run")
        )
        slot = state.get_or_create_slot(slot_key, agent="assistant", mode=DM_SLOT_MODE)
        slot.memory_mode = "incognito"
        state._restricted_keys.add(f"dashboard:{slot_key}")
        state.sessions.record_failure = AsyncMock()
        slot.append("user", "hello", "msg msg-u")

        with (
            patch("kiro_crew.autonudge.get_instance", return_value=MagicMock()),
            patch(
                "kiro_crew.dashboard.chat_runner.chat_done_payload",
                AsyncMock(return_value={}),
            ),
        ):
            await _run_chat(state, slot, "after restart")

        errors = [m for m in slot.messages if m["role"] == "error"]
        assert len(errors) == 1
        assert "open a new conversation" in errors[0]["content"]
        assert not any(m["role"] == "assistant" for m in slot.messages)
        state.sessions.get_or_create.assert_not_called()
        state.context_builder.build_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_runner_refuses_a_non_dispatchable_default_member_on_an_ordinary_slot(
        self, tmp_path, caplog
    ):
        from kiro_crew.dashboard.chat_runner import _run_chat

        name = "crew password=shortvalue"
        credential = "".join(["AKIA", "IOSFODNN7", "EXAMPLE"])
        slot_key = f"chat-{credential}"
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.sessions.get_or_create = AsyncMock(
            side_effect=AssertionError("provider acquisition must not run")
        )
        state.context_builder = MagicMock()
        state.context_builder.build_message = MagicMock(
            side_effect=AssertionError("context construction must not run")
        )
        slot = state.get_or_create_slot(slot_key)
        # PICKED on an ordinary slot: an agent-less slot is the default template
        # and inherits no member, dispatchable or not.
        slot.agent = name
        slot.append("user", "hello", "msg msg-u")
        cfg = KiroCrewConfig()
        cfg.agents = {name: KiroCrewAgentConfig(kiro_agent="kirocrew")}
        cfg.default_agent = name
        autonudge = MagicMock()

        with (
            patch("kiro_crew.dashboard.chat_runner.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.autonudge.get_instance", return_value=autonudge),
            caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"),
        ):
            await _run_chat(state, slot, "hello")

        errors = [message for message in slot.messages if message["role"] == "error"]
        assert len(errors) == 1
        assert (
            errors[0]["content"]
            == "This thread's crew name cannot be dispatched. Rename or recreate the Crew Member."
        )
        logs = "\n".join(caplog.messages)
        assert credential not in logs
        assert "[REDACTED" in logs
        assert slot.messages[-1]["role"] == "done"
        assert any(call.args[0] == "chat_done" for call in state.broadcast_ws.call_args_list)
        autonudge.notify_turn_complete.assert_called_once()
        assert autonudge.notify_turn_complete.call_args.args == (slot.key,)
        state.sessions.get_or_create.assert_not_called()
        state.context_builder.build_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_path_fails_closed_on_binding_drift(self, tmp_path):
        """A live member slot whose binding vanished must refuse the send.

        Binding deleted/corrupted while the tab stays open -> accepting the
        send would persist history that restore and thread-open both refuse,
        stranding the transcript the moment the slot dies. Empty message +
        409 (not the message-required 400) proves the guard ran first.
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        slot = _member_slot(state)
        # Binding vanishes out from under the live slot.
        dm_binding_path(CREW).unlink()
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat", json={"slot": slot.key, "message": ""})
                assert resp.status == 409
                assert (await resp.json())["code"] == "member_binding_missing"
        assert slot.agent == CREW

    @pytest.mark.asyncio
    async def test_send_path_fails_closed_on_registry_drift(self, tmp_path):
        """An agentless send must not dispatch for a crew the registry lost.

        Crew deleted while its thread stays open -> the resolver would fall
        back to the default agent and store the reply under the deleted
        member's identity. The guard fires BEFORE the message-required check,
        so an empty message reaching 409 (not 400) proves it ran first.
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        slot = _member_slot(state)
        # Registry holds only an unrelated crew, not CREW.
        with _patched_config([OTHER]):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat", json={"slot": slot.key, "message": ""})
                assert resp.status == 409
                assert (await resp.json())["code"] == "member_pin_mismatch"
        assert slot.agent == CREW

    def _runner_harness(self, tmp_path, monkeypatch, *, mode, private=True, switch_to=OTHER):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.chat_runner import _run_chat
        from kiro_crew.memory_stores import provision_member_memory

        cfg = KiroCrewConfig.load()
        cfg.agents[CREW] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        provision_member_memory(cfg, CREW)
        cfg.save()
        # No real provider or embedding process runs in this stream harness.
        # Keep private ownership and the protected session binding real.
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.title_then_refresh", AsyncMock())
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.generate_session_summary", AsyncMock())
        monkeypatch.setattr(
            "kiro_crew.config.loader._materialized_kiro_agent",
            lambda name, project_dir=None: name if name == switch_to else "",
        )
        context = SimpleNamespace(
            ensure_store=AsyncMock(return_value=object()),
            build_message=lambda text, *args, **kwargs: (text, None),
            conversation_log=None,
            hooks=SimpleNamespace(auto_approve_subagent_tools=False),
        )

        state = _make_state(tmp_path, context_builder=context)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        # Ordinary-mode control runs on an ordinary key: the constructor's
        # member-* reservation (correctly) refuses a bare member key.
        slot_key = "member-code-reviewer" if mode == DM_SLOT_MODE else "chat-1-100"
        agent = CREW if private else "default"
        slot = state.get_or_create_slot(slot_key, agent=agent, mode=mode)
        if private:
            # The turn only confirms a grant an owner-gated route wrote; the
            # harness stands in for that route.
            from kiro_crew.member_memory_auth import bind_private_session_store

            bind_private_session_store(f"dashboard:{slot_key}", cfg.agents[CREW].memory_store)
        slot.append("user", "hello", "msg msg-u")

        client = state.sessions.get_or_create.return_value[0]
        client.shutdown = AsyncMock()

        from kiro_crew.providers.base import (
            EVENT_AGENT_SWITCHED,
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_AGENT_SWITCHED, text=switch_to)
            # Anything after the switch executes as the FOREIGN agent — the
            # veto must stop consumption here, so this text must never land.
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="foreign agent output after switch")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream
        client.stream_command = _stream
        return state, slot, _run_chat

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [DM_SLOT_MODE, ""], ids=["member-dm", "ordinary-v2"])
    @pytest.mark.parametrize("switch_to", [OTHER, CREW], ids=["other-agent", "alias-collision"])
    async def test_mid_turn_agent_switch_is_vetoed_on_member_threads(
        self, tmp_path, monkeypatch, mode, switch_to
    ):
        state, slot, _run_chat = self._runner_harness(
            tmp_path, monkeypatch, mode=mode, switch_to=switch_to
        )

        await _run_chat(state, slot, "test message")
        await asyncio.gather(*state._background_tasks)

        # The pin held: agent unchanged, no switch advertised to the UI.
        assert slot.agent == CREW
        switch_broadcasts = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "slot_agent_switch"
        ]
        assert switch_broadcasts == []
        # The veto is VISIBLE: kiro-cli already switched, so the rest of the
        # turn runs as the foreign agent — the thread must say so.
        assert any(
            "Agent switch" in str(m.get("content", "")) and "pinned" in str(m.get("content", ""))
            for m in slot.messages
        ), "veto left no user-visible notice on the thread"
        # The stream was TERMINATED at the veto: kiro-cli had already switched,
        # so any later event would execute as the foreign agent — the text the
        # harness yields after the switch must never land on the thread.
        assert not any(
            "foreign agent output" in str(m.get("content", "")) for m in slot.messages
        ), "events after the vetoed switch were still consumed"
        # And it is CONSUMED: the finally block resets the session so the next
        # turn cold-starts on the pinned crew. Dropping needs_session_reset in
        # the veto branch fails this line.
        state.sessions.reset.assert_awaited()
        # The veto counts as VISIBLE OUTPUT: without that, the empty-response
        # recovery would silently requeue the prompt and REPLAY any
        # non-idempotent tool calls that completed before the switch event.
        assert slot._empty_response_retries == 0, (
            "vetoed turn triggered the empty-response requeue — completed "
            "tool side effects would replay"
        )

    @pytest.mark.asyncio
    async def test_mid_turn_agent_switch_still_lands_on_ordinary_slots(self, tmp_path, monkeypatch):
        """Control for the veto: the same event MOVES a non-member slot.

        Proves the event path executes in this harness, so the member test
        above passes because of the veto, not because the event never ran.
        """
        state, slot, _run_chat = self._runner_harness(tmp_path, monkeypatch, mode="", private=False)
        assert slot.agent == "default"

        await _run_chat(state, slot, "test message")
        await asyncio.gather(*state._background_tasks)

        assert slot.agent == OTHER
        switch_broadcasts = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "slot_agent_switch"
        ]
        assert len(switch_broadcasts) == 1
