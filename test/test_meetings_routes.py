"""HTTP route tests: the enable gate and the config, dictionary, rename, attachment and agent routes.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

Covers the request contract every frontend call depends on, plus the input
validation and redaction the AUTOSDE ``backend-security-controls`` rule requires.
Agent dispatch always goes through the fake session manager; nothing spawns a
process or opens a socket.

The meeting lifecycle routes and the start/teardown invariants live in
``test_meetings_routes_lifecycle.py``; the task and calendar routes and the store's
off-loop and serialization rules in ``test_meetings_routes_tasks.py``. The route base
path and the meeting-start calls they share live in ``meetings_routes_helpers.py``.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from aiohttp import web
from meetings_helpers import (  # noqa: F401
    app_fixture,
    client_for,
    enabled_fixture,
    fake_sessions_fixture,
    make_app,
    reset_module_state_fixture,
    root_fixture,
)
from meetings_routes_helpers import BASE, _start, _start_and_get_session

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain import session as sess
from kiro_crew.apps.builtins.meetings.backend.routes import _common
from kiro_crew.testing.wait import await_until

# Repo-root anchor for tests that read a checked-in source file. A relative
# literal resolves against the process CWD, which no test owns: another test
# chdir-ing away makes the read fail, and under `-n auto` the loser is decided
# by worker scheduling rather than by anything in this file.
_REPO_ROOT = Path(__file__).resolve().parents[1]


class TestAuthorizationGate:
    @pytest.mark.asyncio
    async def test_disabled_app_denies_every_route(self, root: Path, monkeypatch):
        """Deny-by-default: routes are registered at startup, so a
        default-disabled app must refuse at request time."""
        monkeypatch.setattr(_common, "is_app_enabled", lambda _name: False)
        async with client_for(make_app(root)) as client:
            for method, path in (
                ("get", f"{BASE}/config"),
                ("get", f"{BASE}/meetings"),
                ("get", f"{BASE}/status"),
                ("post", f"{BASE}/meetings/x/init"),
                ("patch", f"{BASE}/meetings/x"),
                ("post", f"{BASE}/calendar/sync"),
            ):
                resp = await getattr(client, method)(path, json={})
                assert resp.status == 403, f"{method} {path} was not denied"
                assert "disabled" in (await resp.json())["error"]


class TestConfigRoutes:
    @pytest.mark.asyncio
    async def test_get_config_includes_provider_catalogs(self, app: web.Application):
        async with client_for(app) as client:
            resp = await client.get(f"{BASE}/config")
            assert resp.status == 200
            body = await resp.json()
            assert body["config"]["task_provider"] == k.TASK_PROVIDER_LOCAL
            assert {r["id"] for r in body["task_providers"]} >= {k.TASK_PROVIDER_LOCAL}
            assert {r["id"] for r in body["calendar_providers"]} >= {k.CALENDAR_PROVIDER_ICS}

    @pytest.mark.asyncio
    async def test_put_config_roundtrips_allowed_fields(self, app, root: Path):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config",
                json={
                    "config": {
                        "task_provider": k.TASK_PROVIDER_LOCAL,
                        "calendar": {"provider": k.CALENDAR_PROVIDER_ICS, "source": "/tmp/c.ics"},
                        "poll_interval_active": 2500,
                        "meeting_agents": [
                            {"id": "note-taker", "name": "Notes", "widget_type": "markdown"}
                        ],
                    }
                },
            )
            assert resp.status == 200
            saved = (await resp.json())["config"]
            assert saved["calendar"]["provider"] == k.CALENDAR_PROVIDER_ICS
            assert saved["poll_interval_active"] == 2500
        assert store.read_config(root)["calendar"]["source"] == "/tmp/c.ics"

    @pytest.mark.asyncio
    async def test_put_config_rejects_unknown_providers(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config",
                json={
                    "config": {
                        "task_provider": "corporate-tracker",
                        "calendar": {"provider": "corporate-calendar"},
                    }
                },
            )
            saved = (await resp.json())["config"]
            # An unregistered id would name a provider that cannot resolve; it is
            # collapsed to the default rather than persisted.
            assert saved["task_provider"] == k.TASK_PROVIDER_LOCAL
            assert saved["calendar"]["provider"] == k.CALENDAR_PROVIDER_NONE

    @pytest.mark.asyncio
    async def test_put_config_drops_agent_with_unsafe_id(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config",
                json={
                    "config": {
                        "meeting_agents": [
                            {"id": "../../evil", "name": "Evil"},
                            {"id": "note-taker", "name": "Notes"},
                        ]
                    }
                },
            )
            ids = [a["id"] for a in (await resp.json())["config"]["meeting_agents"]]
            assert ids == ["note-taker"]

    @pytest.mark.asyncio
    async def test_put_config_sanitizes_agent_reference(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config",
                json={
                    "config": {
                        "meeting_agents": [
                            {"id": "note-taker", "agent": "../../../etc/passwd"},
                        ]
                    }
                },
            )
            assert (await resp.json())["config"]["meeting_agents"][0]["agent"] == ""

    @pytest.mark.asyncio
    async def test_put_config_empty_agents_falls_back_to_defaults(self, app):
        async with client_for(app) as client:
            resp = await client.put(f"{BASE}/config", json={"config": {"meeting_agents": []}})
            ids = [a["id"] for a in (await resp.json())["config"]["meeting_agents"]]
            assert ids == ["note-taker", "sketch-artist"]

    @pytest.mark.asyncio
    async def test_put_config_rejects_non_object(self, app):
        async with client_for(app) as client:
            resp = await client.put(f"{BASE}/config", json={"config": "nope"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_config_clamps_poll_intervals(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config",
                json={"config": {"poll_interval_active": 1, "poll_interval_idle": 99_999_999}},
            )
            saved = (await resp.json())["config"]
            assert saved["poll_interval_active"] == 1000
            assert saved["poll_interval_idle"] == 600_000

    @pytest.mark.asyncio
    async def test_put_config_drops_default_preset_that_does_not_exist(self, app):
        async with client_for(app) as client:
            resp = await client.put(f"{BASE}/config", json={"config": {"default_preset": "ghost"}})
            assert (await resp.json())["config"]["default_preset"] == ""

    @pytest.mark.asyncio
    async def test_put_config_invalid_json_is_400(self, app):
        async with client_for(app) as client:
            resp = await client.put(
                f"{BASE}/config",
                data="{not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400


class TestDictionaryRoutes:
    @pytest.mark.asyncio
    async def test_add_list_remove(self, app, root: Path):
        async with client_for(app) as client:
            resp = await client.post(
                f"{BASE}/dictionary", json={"correct": "DynamoDB", "aliases": ["dynamo db"]}
            )
            assert resp.status == 200
            # Added alongside the seeded starter terms, not replacing them.
            assert "DynamoDB" in {t["correct"] for t in (await resp.json())["terms"]}

            resp = await client.get(f"{BASE}/dictionary")
            assert any(t["correct"] == "DynamoDB" for t in (await resp.json())["terms"])

            resp = await client.post(f"{BASE}/dictionary/remove", json={"correct": "DynamoDB"})
            assert resp.status == 200
            assert "DynamoDB" not in {t["correct"] for t in (await resp.json())["terms"]}

        assert "DynamoDB" not in store.dictionary_path(root).read_text()

    @pytest.mark.asyncio
    async def test_add_requires_correct_and_aliases(self, app):
        async with client_for(app) as client:
            assert (await client.post(f"{BASE}/dictionary", json={"aliases": ["x"]})).status == 400
            assert (
                await client.post(f"{BASE}/dictionary", json={"correct": "X", "aliases": []})
            ).status == 400

    @pytest.mark.asyncio
    async def test_a_refused_surrogate_term_never_reaches_the_shared_dictionary(
        self, app, root: Path
    ):
        """``"\\ud800"`` is legal JSON but has no UTF-8 encoding.

        Sent as raw bytes rather than through ``json=``: the client's own encoder
        would refuse it, while a real HTTP client puts the escape on the wire and
        the SERVER's ``json.loads`` is what materializes the surrogate.

        The status was already 400 before the fix — ``UnicodeEncodeError`` is a
        ``ValueError``, so the write blew up into the handler's own 400 branch and
        wore a codec message. What that hid is the ordering: ``add_term`` had
        already replaced the terms on the PROCESS-WIDE dictionary
        (``domain.session._dictionary``, which every transcript line is corrected
        against at ``session.py``'s ``_dictionary.correct``) and only then failed to
        write, leaving live corrections running against a term no file holds.
        """
        async with client_for(app) as client:
            resp = await client.post(
                f"{BASE}/dictionary", json={"correct": "DynamoDB", "aliases": ["dynamo db"]}
            )
            assert resp.status == 200
            before = store.dictionary_path(root).read_bytes()

            resp = await client.post(
                f"{BASE}/dictionary",
                data=b'{"correct": "\\ud800", "aliases": ["lone half"]}',
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            # Asserted BEFORE any further dictionary route: every one of them
            # reloads from disk first, which would wash the bad term back out.
            assert all("\ud800" not in c for c, _ in sess.shared_dictionary().terms)
            # A validation message, not whatever the codec happened to say.
            assert "surrogate" in (await resp.json())["error"]
            assert "codec" not in (await resp.json())["error"]

            resp = await client.get(f"{BASE}/dictionary")
            assert any(t["correct"] == "DynamoDB" for t in (await resp.json())["terms"])

        # ...and nothing was written.
        assert store.dictionary_path(root).read_bytes() == before

    @pytest.mark.asyncio
    async def test_remove_unknown_is_404(self, app):
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/dictionary/remove", json={"correct": "Ghost"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reload_reports_count(self, app, root: Path):
        store.dictionary_path(root).write_text('[[term]]\ncorrect = "X"\naliases = ["ex"]\n')
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/dictionary/reload")
            assert (await resp.json())["count"] == 1


class TestRenameRoute:
    @pytest.mark.asyncio
    async def test_rename_trims_and_persists(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "Standup"})
            resp = await client.patch(f"{BASE}/meetings/standup", json={"title": "  Retro  "})
            assert resp.status == 200
            assert (await resp.json())["meta"]["title"] == "Retro"
            got = await (await client.get(f"{BASE}/meetings/standup")).json()
            assert got["meta"]["title"] == "Retro"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("title", ["", "   ", None, 7])
    async def test_empty_or_missing_title_is_400(self, app, title):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "Standup"})
            resp = await client.patch(f"{BASE}/meetings/standup", json={"title": title})
            assert resp.status == 400
            got = await (await client.get(f"{BASE}/meetings/standup")).json()
            assert got["meta"]["title"] == "Standup"

    @pytest.mark.asyncio
    async def test_too_long_title_is_400(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "Standup"})
            ok = await client.patch(
                f"{BASE}/meetings/standup", json={"title": "x" * k.MAX_TITLE_LEN}
            )
            assert ok.status == 200
            resp = await client.patch(
                f"{BASE}/meetings/standup", json={"title": "x" * (k.MAX_TITLE_LEN + 1)}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_meeting_is_404_and_creates_nothing(self, app, root: Path):
        async with client_for(app) as client:
            resp = await client.patch(f"{BASE}/meetings/ghost", json={"title": "Ghost"})
            assert resp.status == 404
            assert (await resp.json())["code"] == "meeting_not_found"
        assert not store.meeting_dir("ghost", root).exists()


class TestAttachmentRoutes:
    @pytest.mark.asyncio
    async def test_add_and_remove(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/attachments",
                json={
                    "action": "add",
                    "attachments": [
                        {"type": "url", "url": "https://example.test/doc", "label": "Doc"}
                    ],
                },
            )
            assert len((await resp.json())["attachments"]) == 1
            resp = await client.post(
                f"{BASE}/meetings/standup/attachments", json={"action": "remove", "index": 0}
            )
            assert (await resp.json())["attachments"] == []

    @pytest.mark.asyncio
    async def test_drops_a_dangerous_url_scheme(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/attachments",
                json={
                    "action": "add",
                    "attachments": [
                        {"type": "url", "url": "file:///etc/passwd", "label": "Bad"},
                        {"type": "url", "url": "javascript:alert(1)", "label": "Worse"},
                        {"type": "url", "url": "https://example.test/ok", "label": "Fine"},
                    ],
                },
            )
            attachments = (await resp.json())["attachments"]
            assert [a["label"] for a in attachments] == ["Fine"]

    @pytest.mark.asyncio
    async def test_drops_an_unknown_type(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/attachments",
                json={"action": "add", "attachments": [{"type": "artifact", "slug": "x"}]},
            )
            assert (await resp.json())["attachments"] == []

    @pytest.mark.asyncio
    async def test_enforces_the_cap(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/attachments",
                json={
                    "action": "add",
                    "attachments": [
                        {"type": "url", "url": f"https://example.test/{i}", "label": str(i)}
                        for i in range(k.MAX_ATTACHMENTS + 20)
                    ],
                },
            )
            assert len((await resp.json())["attachments"]) == k.MAX_ATTACHMENTS

    @pytest.mark.asyncio
    async def test_rejects_a_bad_action_and_index(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            assert (
                await client.post(f"{BASE}/meetings/standup/attachments", json={"action": "nuke"})
            ).status == 400
            assert (
                await client.post(
                    f"{BASE}/meetings/standup/attachments",
                    json={"action": "remove", "index": "one"},
                )
            ).status == 400

    @pytest.mark.asyncio
    async def test_unknown_meeting_is_404(self, app):
        async with client_for(app) as client:
            resp = await client.post(
                f"{BASE}/meetings/ghost/attachments", json={"action": "add", "attachments": []}
            )
            assert resp.status == 404


class TestAgentRoutes:
    @pytest.mark.asyncio
    async def test_get_agents(self, app):
        async with client_for(app) as client:
            body = await (await client.get(f"{BASE}/agents")).json()
            assert [a["id"] for a in body["agents"]] == ["note-taker", "sketch-artist"]
            assert body["task_extractor_id"] == k.TASK_EXTRACTOR_ID

    @pytest.mark.asyncio
    async def test_disabling_one_default_agent_keeps_the_others(self, app):
        """An ABSENT `agents_enabled` means "the defaults", not "none".

        The toggle seeded its list with `meta.get("agents_enabled") or []`, so the
        FIRST toggle on a fresh meeting destroyed the roster: disabling one default
        agent persisted `[]`, which `get_enabled_agents` reads as an explicit empty
        roster rather than "use the defaults". The meeting then started with no
        agents at all and silently produced no notes and no diagrams.
        """
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/agents",
                json={"agent_id": "sketch-artist", "enable": False},
            )
            assert resp.status == 200
            remaining = (await resp.json())["agents_enabled"]
        # The one we turned off is gone; the other default survives.
        assert "sketch-artist" not in remaining
        assert "note-taker" in remaining, "disabling one default agent must not disable the rest"

    @pytest.mark.asyncio
    async def test_an_explicit_empty_roster_is_preserved(self, app):
        """Turning everything off is a real state, not a value to re-seed."""
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            await client.post(f"{BASE}/meetings/standup/start", json={"agents_enabled": []})
            resp = await client.post(
                f"{BASE}/meetings/standup/agents",
                json={"agent_id": "note-taker", "enable": True},
            )
            assert resp.status == 200
            # Only the agent just enabled — the defaults are NOT re-added.
            assert (await resp.json())["agents_enabled"] == ["note-taker"]

    def _default_output_names(self, root: Path) -> set[str]:
        """Output filenames the default roster would seed."""
        config = store.read_config(root)
        return {store.agent_output_filename(a) for a in sess.get_enabled_agents(config, None)}

    @pytest.mark.asyncio
    async def test_init_preserves_an_explicit_empty_roster(self, app, root):
        """On init too, `[]` means "no agents" — not "use the defaults".

        Init seeded its roster with ``field_str_list(...) or meta.get(...)``, and
        ``or`` is falsy on ``[]``, so an explicitly empty roster fell through to
        the default set: every default agent got an output file seeded and then
        ran on the meeting. ``field_str_list`` returns None for absent precisely
        so the two stay distinguishable.
        """
        expected_defaults = self._default_output_names(root)
        assert expected_defaults, "fixture config must define at least one default agent"

        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/meetings/standup/init", json={"agents_enabled": []})
            assert resp.status == 200

        seeded = {p.name for p in store.meeting_dir("standup", root).iterdir() if p.is_file()}
        assert not (
            seeded & expected_defaults
        ), "an explicitly empty roster must seed no agent output files"

    @pytest.mark.asyncio
    async def test_init_without_a_roster_still_seeds_the_defaults(self, app, root):
        """The other direction: absent must keep meaning "use the defaults"."""
        expected_defaults = self._default_output_names(root)

        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/meetings/standup/init", json={})
            assert resp.status == 200

        seeded = {p.name for p in store.meeting_dir("standup", root).iterdir() if p.is_file()}
        assert expected_defaults <= seeded

    @pytest.mark.asyncio
    async def test_an_illegal_status_transition_is_refused(self, app, fake_sessions):
        """The dashboard greys these out; the SERVER has to refuse them.

        The endpoint accepted any member of `VALID_STATUSES`, so an authenticated
        `POST status=idle` against an ACTIVE meeting persisted "idle" while the live
        session stayed installed — transcription stopped feeding a meeting the UI
        still showed as running, and starting another answered 409 because `ACTIVE`
        was still held. A state reachable through the API that the UI can neither
        produce nor explain.
        """
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(f"{BASE}/meetings/standup/status", json={"status": "idle"})
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "invalid_transition"
            # And the meeting is still active — the refusal changed nothing.
            meta = (await (await client.get(f"{BASE}/meetings/standup")).json())["meta"]
            assert meta["status"] == k.STATUS_ACTIVE

    @pytest.mark.asyncio
    async def test_a_legal_transition_still_works(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(f"{BASE}/meetings/standup/status", json={"status": "paused"})
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_repeating_the_current_status_is_accepted(self, app, fake_sessions):
        """An idempotent retry of a request whose response was lost must not fail."""
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(f"{BASE}/meetings/standup/status", json={"status": "active"})
            assert resp.status == 200

    def test_the_server_and_client_transition_tables_agree(self):
        """Two copies of one rule, so drift is the risk worth pinning.

        The client's is a UI affordance and the server's is enforcement; if they
        disagree the UI either offers a button the API refuses or hides one it
        allows. Parsed from the TS source rather than duplicated here, so the
        assertion breaks when either side changes alone.
        """
        import re

        source = (_REPO_ROOT / "website/src/apps/meetings/hooks/useMeetingSession.ts").read_text()
        block = re.search(
            r"ALLOWED_TRANSITIONS: Record<MeetingStatus, MeetingStatus\[\]> = \{(.*?)\n\}",
            source,
            re.S,
        )
        assert block is not None, "the client transition table was not found"
        client_rule = {
            row.group(1): set(re.findall(r"'([a-z]+)'", row.group(2)))
            for row in re.finditer(r"(\w+): \[([^\]]*)\]", block.group(1))
        }
        server_rule = {key: set(value) for key, value in k.ALLOWED_TRANSITIONS.items()}
        assert client_rule == server_rule

    @pytest.mark.asyncio
    async def test_status_idle_shape(self, app):
        async with client_for(app) as client:
            body = await (await client.get(f"{BASE}/status")).json()
            assert body == {
                "active_meeting": None,
                "muted_agents": [],
                "agents": {},
                "agents_paused": False,
                "expired": False,
            }

    @pytest.mark.asyncio
    async def test_status_live_shape(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            body = await (await client.get(f"{BASE}/status")).json()
            assert body["active_meeting"] == "standup"
            assert set(body["agents"]) == {"note-taker", "sketch-artist", k.TASK_EXTRACTOR_ID}

    @pytest.mark.asyncio
    async def test_dispatch_broadcasts_and_redacts(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch",
                json={"text": "rotate AKIAIOSFODNN7EXAMPLE today"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["dispatched"] == 3
            assert "AKIAIOSFODNN7EXAMPLE" not in body["text"]
            assert body["segment"]["source"] == k.TRANSCRIPT_SOURCE_SPEECH
            assert "AKIAIOSFODNN7EXAMPLE" not in body["segment"]["text"]

            transcript = await client.get(f"{BASE}/meetings/standup/transcript")
            assert transcript.status == 200
            transcript_body = await transcript.json()
            assert transcript_body["segments"] == [body["segment"]]
            assert transcript_body["next_cursor"] > 0

            second = await client.post(
                f"{BASE}/meetings/standup/dispatch",
                json={"text": "second line"},
            )
            assert second.status == 200
            page = await client.get(
                f"{BASE}/meetings/standup/transcript",
                params={"cursor": transcript_body["next_cursor"]},
            )
            page_body = await page.json()
            assert [segment["text"] for segment in page_body["segments"]] == ["second line"]
            assert page_body["next_cursor"] > transcript_body["next_cursor"]

    @pytest.mark.asyncio
    async def test_dispatch_marks_a_chat_line(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch",
                json={"text": "actually the owner is Bob", "chat": True},
            )
            body = await resp.json()
            assert body["text"].startswith(k.CHAT_PREFIX)
            assert body["segment"]["source"] == k.TRANSCRIPT_SOURCE_TYPED
            assert body["segment"]["text"] == "actually the owner is Bob"

    @pytest.mark.asyncio
    async def test_transcript_for_a_legacy_meeting_is_empty(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/legacy/init", json={})
            resp = await client.get(f"{BASE}/meetings/legacy/transcript")
            assert resp.status == 200
            assert await resp.json() == {"segments": [], "next_cursor": 0}

    @pytest.mark.asyncio
    async def test_transcript_for_an_unknown_meeting_is_404(self, app):
        async with client_for(app) as client:
            resp = await client.get(f"{BASE}/meetings/missing/transcript")
            assert resp.status == 404
            assert (await resp.json())["code"] == "meeting_not_found"

    @pytest.mark.asyncio
    async def test_capacity_failure_happens_before_agent_fanout(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_for(app) as client:
            session = await _start_and_get_session(client)
            broadcasted: list[str] = []
            monkeypatch.setattr(session, "broadcast", lambda line: broadcasted.append(line))
            monkeypatch.setattr(store, "append_transcript", lambda *_args: None)

            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "must not fan out"}
            )
            assert resp.status == 413
            assert (await resp.json())["code"] == "transcript_too_large"
            assert broadcasted == []

    @pytest.mark.asyncio
    async def test_stop_waits_for_an_in_flight_transcript_append(
        self, app, root: Path, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        from kiro_crew.apps.builtins.meetings.backend.routes import (
            agents,
            meeting_lifecycle,
        )

        class ObservedLock:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.waiter = asyncio.Event()

            async def __aenter__(self):
                if self.lock.locked():
                    self.waiter.set()
                await self.lock.acquire()
                return self

            async def __aexit__(self, *_args):
                self.lock.release()

        async with client_for(app) as client:
            await _start(client)
            observed_lock = ObservedLock()
            append_entered = threading.Event()
            release_append = threading.Event()
            real_append = store.append_transcript

            def blocked_append(*args, **kwargs):
                append_entered.set()
                assert release_append.wait(timeout=5)
                return real_append(*args, **kwargs)

            # The dispatch transaction lives in `_common.dispatch_admission`, which
            # reads its own module global; the lifecycle handlers read their
            # from-imported binding. Patch both so contention is observable.
            monkeypatch.setattr(_common, "DISPATCH_LOCK", observed_lock)
            monkeypatch.setattr(agents, "DISPATCH_LOCK", observed_lock)
            monkeypatch.setattr(meeting_lifecycle, "DISPATCH_LOCK", observed_lock)
            monkeypatch.setattr(store, "append_transcript", blocked_append)

            dispatch_request = asyncio.create_task(
                client.post(f"{BASE}/meetings/standup/dispatch", json={"text": "kept"})
            )
            assert await asyncio.to_thread(append_entered.wait, 5)

            stop_request = asyncio.create_task(client.post(f"{BASE}/meetings/standup/stop"))
            await await_until(observed_lock.waiter)
            release_append.set()

            assert (await dispatch_request).status == 200
            assert (await stop_request).status == 200
            assert (await client.delete(f"{BASE}/meetings/standup")).status == 204

        assert not store.meeting_dir("standup", root).exists()

    @pytest.mark.asyncio
    async def test_review_status_waits_for_an_in_flight_transcript_append(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        from kiro_crew.apps.builtins.meetings.backend.routes import (
            agents,
            meeting_lifecycle,
        )

        class ObservedLock:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.waiter = asyncio.Event()

            async def __aenter__(self):
                if self.lock.locked():
                    self.waiter.set()
                await self.lock.acquire()
                return self

            async def __aexit__(self, *_args):
                self.lock.release()

        async with client_for(app) as client:
            await _start(client)
            fake_sessions.calls.clear()

            observed_lock = ObservedLock()
            append_entered = threading.Event()
            release_append = threading.Event()
            real_append = store.append_transcript

            def blocked_append(*args, **kwargs):
                append_entered.set()
                assert release_append.wait(timeout=5)
                return real_append(*args, **kwargs)

            # The dispatch transaction lives in `_common.dispatch_admission`, which
            # reads its own module global; the lifecycle handlers read their
            # from-imported binding. Patch both so contention is observable.
            monkeypatch.setattr(_common, "DISPATCH_LOCK", observed_lock)
            monkeypatch.setattr(agents, "DISPATCH_LOCK", observed_lock)
            monkeypatch.setattr(meeting_lifecycle, "DISPATCH_LOCK", observed_lock)
            monkeypatch.setattr(store, "append_transcript", blocked_append)

            dispatch_request = asyncio.create_task(
                client.post(f"{BASE}/meetings/standup/dispatch", json={"text": "kept"})
            )
            assert await asyncio.to_thread(append_entered.wait, 5)

            review_request = asyncio.create_task(
                client.post(
                    f"{BASE}/meetings/standup/status",
                    json={"status": k.STATUS_REVIEWING},
                )
            )
            await await_until(observed_lock.waiter)
            release_append.set()

            assert (await dispatch_request).status == 200
            assert (await review_request).status == 200

        assert any("kept" in message for _key, _agent, message in fake_sessions.calls)

    @pytest.mark.asyncio
    async def test_stop_closes_dispatch_admission_before_a_slow_agent_flush(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_for(app) as client:
            session = await _start_and_get_session(client)
            flush_entered = asyncio.Event()
            release_flush = asyncio.Event()

            async def slow_flush() -> None:
                flush_entered.set()
                await release_flush.wait()

            monkeypatch.setattr(session, "flush_all", slow_flush)
            stop_request = asyncio.create_task(client.post(f"{BASE}/meetings/standup/stop"))
            await await_until(flush_entered)

            dispatch = await await_until(
                asyncio.ensure_future(
                    client.post(
                        f"{BASE}/meetings/standup/dispatch",
                        json={"text": "too late"},
                    )
                ),
                timeout=1,
            )
            assert dispatch.status == 409
            assert (await dispatch.json())["code"] == "no_active_meeting"

            release_flush.set()
            assert (await stop_request).status == 200

        assert _common.ACTIVE.get() is None

    @pytest.mark.asyncio
    async def test_stopping_an_inactive_meeting_keeps_active_dispatch_open(
        self, app, fake_sessions
    ):
        async with client_for(app) as client:
            active = await _start_and_get_session(client, "active")
            await client.post(f"{BASE}/meetings/stale/init", json={})

            stopped = await client.post(f"{BASE}/meetings/stale/stop")
            assert stopped.status == 200
            assert _common.ACTIVE.get("active") is active

            dispatch = await client.post(
                f"{BASE}/meetings/active/dispatch",
                json={"text": "still accepted"},
            )
            assert dispatch.status == 200

    @pytest.mark.asyncio
    async def test_dispatch_without_an_active_meeting_is_409(self, app):
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/meetings/standup/dispatch", json={"text": "hello"})
            assert resp.status == 409

    @pytest.mark.asyncio
    async def test_dispatch_requires_text(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            assert (await client.post(f"{BASE}/meetings/standup/dispatch", json={})).status == 400

    @pytest.mark.asyncio
    async def test_dispatch_on_an_expired_session_is_410(self, app, fake_sessions):
        import time

        async with client_for(app) as client:
            await _start(client)
            session = _common.ACTIVE.get()
            assert session is not None
            session.started_at = time.time() - (k.MAX_SESSION_DURATION + 1)
            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "still talking"}
            )
            assert resp.status == 410

    @pytest.mark.asyncio
    async def test_dispatch_drops_noise_without_erroring(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(f"{BASE}/meetings/standup/dispatch", json={"text": "I I"})
            assert resp.status == 200
            assert (await resp.json())["dispatched"] == 0

    @pytest.mark.asyncio
    async def test_mute_persists_without_a_live_session(self, app, root: Path):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/mute",
                json={"agent_id": "note-taker", "muted": True},
            )
            assert (await resp.json())["muted_agents"] == ["note-taker"]
        meta = store.read_meeting_meta("standup", root)
        assert meta is not None and meta["muted_agents"] == ["note-taker"]

    @pytest.mark.asyncio
    async def test_mute_applies_to_the_live_session(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            await client.post(
                f"{BASE}/meetings/standup/mute",
                json={"agent_id": "note-taker", "muted": True},
            )
            session = _common.ACTIVE.get()
            assert session is not None and "note-taker" in session.muted_agents
            resp = await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "the build is green"}
            )
            assert (await resp.json())["dispatched"] == 2

    @pytest.mark.asyncio
    async def test_mute_string_false_is_not_treated_as_true(self, app):
        """A type slip must not silently invert a mute decision: bool("false")
        is True, so the field reader is strict and falls back to the default."""
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/mute",
                json={"agent_id": "note-taker", "muted": "false"},
            )
            # Non-bool → the documented default (True), never a coerced truthy.
            assert (await resp.json())["muted_agents"] == ["note-taker"]

    @pytest.mark.asyncio
    async def test_mute_rejects_a_bad_agent_id(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/mute", json={"agent_id": "../../etc"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_toggle_agent_on_and_off(self, app, root: Path, fake_sessions):
        async with client_for(app) as client:
            await _start(client, agents_enabled=["note-taker"])
            fake_sessions.calls.clear()
            resp = await client.post(
                f"{BASE}/meetings/standup/agents",
                json={"agent_id": "sketch-artist", "enable": True},
            )
            assert resp.status == 200
            assert "sketch-artist" in (await resp.json())["agents_enabled"]
            assert store.agent_output_path("standup", "sketch-artist.html", root).is_file()
            kickoff = next(msg for _k, _a, msg in fake_sessions.calls if "mid-meeting" in msg)
            assert "end this turn" in kickoff.lower()
            assert "wait for transcription" not in kickoff.lower()

            resp = await client.post(
                f"{BASE}/meetings/standup/agents",
                json={"agent_id": "sketch-artist", "enable": False},
            )
            assert "sketch-artist" not in (await resp.json())["agents_enabled"]

    @pytest.mark.asyncio
    async def test_toggle_unknown_agent_is_404(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(f"{BASE}/meetings/standup/agents", json={"agent_id": "ghost"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reset_resumes_paused_queues(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            session = _common.ACTIVE.get()
            assert session is not None
            session.agents["note-taker"]._fail_count = k.MAX_DISPATCH_FAILURES
            resp = await client.post(f"{BASE}/meetings/standup/reset")
            assert (await resp.json())["resumed"] == ["note-taker"]
            assert session.agents_paused is False

    @pytest.mark.asyncio
    async def test_reset_without_an_active_meeting_is_409(self, app):
        async with client_for(app) as client:
            assert (await client.post(f"{BASE}/meetings/standup/reset")).status == 409

    @pytest.mark.asyncio
    async def test_agent_message_flushes_immediately(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client)
            fake_sessions.calls.clear()
            resp = await client.post(
                f"{BASE}/meetings/standup/message",
                json={"agent_id": "note-taker", "text": "please add the decision log"},
            )
            assert resp.status == 200
        prompts = fake_sessions.prompts_for("note-taker")
        assert prompts and prompts[-1].startswith(k.CHAT_PREFIX)

    @pytest.mark.asyncio
    async def test_agent_message_to_an_absent_agent_is_404(self, app, fake_sessions):
        async with client_for(app) as client:
            await _start(client, agents_enabled=["note-taker"])
            resp = await client.post(
                f"{BASE}/meetings/standup/message",
                json={"agent_id": "sketch-artist", "text": "hi"},
            )
            assert resp.status == 404


def _held_lines(session) -> list[str]:
    """Just the text of an init hold, dropping each entry's recipient snapshot."""
    return [line for line, _recipients in session.init_buffer]


class TestSpeechDuringAgentInitIsHeldNotRefused:
    """The opening of a meeting must survive agent initialization.

    ``handle_start_meeting`` persists ``active``, installs the session, then awaits
    ``init_agents`` — a sequence of model turns measured at ~46s. Ingress is shut
    for that whole span, so every line spoken into it was answered 409 and lost:
    the notes and tasks began partway through the first topic with nothing to show
    a turn had been dropped.

    Speech in that window is now HELD and replayed in arrival order. The hold is
    bounded by line count, overflows by dropping the OLDEST, and announces a drop
    to both readers — the agents and the durable transcript.
    """

    @staticmethod
    async def _start_paused_in_init(
        client, monkeypatch: pytest.MonkeyPatch, meeting_id: str = "standup"
    ):
        """Begin a start that blocks INSIDE ``init_agents``.

        Returns ``(start_task, release)``: the in-flight request, and the event that
        lets initialization finish. Mirrors the real window — the session is
        installed and the meeting reads ``active``, but no agent knows its output
        file yet.
        """
        entered = asyncio.Event()
        release = asyncio.Event()
        real_init_agents = sess.init_agents

        async def blocking_init(session, meta, root=None):
            entered.set()
            await release.wait()
            await real_init_agents(session, meta, root)

        monkeypatch.setattr(sess, "init_agents", blocking_init)
        await client.post(f"{BASE}/meetings/{meeting_id}/init", json={"title": "Standup"})
        start = asyncio.create_task(client.post(f"{BASE}/meetings/{meeting_id}/start", json={}))
        await await_until(entered)
        return start, release

    @staticmethod
    async def _wait_for_agent_flush(session, agent_id: str) -> None:
        task = session.agents[agent_id]._flush_task
        assert task is not None
        await await_until(task)

    @pytest.mark.asyncio
    async def test_speech_mid_init_is_buffered_and_delivered_in_order(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """The core regression: three lines spoken during init reach the agents.

        RED on origin/main — each dispatch answers 409 ``no_active_meeting`` and the
        lines never enter a queue at all.
        """
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)

            for line in ("first the agenda", "then the blockers", "and the owners"):
                resp = await client.post(f"{BASE}/meetings/standup/dispatch", json={"text": line})
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                # Held, not fanned out — and already durable either way. The hold is
                # observed on the session below; the response says only that nothing
                # reached an agent yet.
                assert body["dispatched"] == 0
                assert body["segment"]["source"] == k.TRANSCRIPT_SOURCE_SPEECH

            session = _common.ACTIVE.get("standup")
            assert session is not None
            assert _held_lines(session) == [
                "first the agenda",
                "then the blockers",
                "and the owners",
            ]

            release.set()
            assert (await start).status == 200

            # Drained and immediately flushed to every unmuted agent, in the order
            # spoken. The opening does not wait through the normal batch interval.
            assert session.init_buffer == []
            for agent_id in ("note-taker", "sketch-artist", k.TASK_EXTRACTOR_ID):
                await self._wait_for_agent_flush(session, agent_id)
                prompts = fake_sessions.prompts_for(agent_id)
                assert prompts[-1] == ("first the agenda\n\nthen the blockers\n\nand the owners")
                assert session.agents[agent_id].queue == []

    @pytest.mark.asyncio
    async def test_start_does_not_wait_for_the_opening_transcript_turn(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """The lifecycle lock is released while the ordinary turn is still live."""
        entered_turn = asyncio.Event()
        release_turn = asyncio.Event()
        real_dispatch = sess.dispatch_to_agent

        async def blocking_dispatch(sessions, key, text, agent="", **kwargs):
            if text == "opening while agents initialize":
                entered_turn.set()
                await release_turn.wait()
                return
            await real_dispatch(sessions, key, text, agent, **kwargs)

        monkeypatch.setattr(sess, "dispatch_to_agent", blocking_dispatch)
        async with client_for(app) as client:
            start, release_init = await self._start_paused_in_init(client, monkeypatch)
            await client.post(
                f"{BASE}/meetings/standup/dispatch",
                json={"text": "opening while agents initialize"},
            )
            session = _common.ACTIVE.get("standup")
            assert session is not None

            release_init.set()
            try:
                assert (await await_until(start)).status == 200
                await await_until(entered_turn)
                assert any(queue.busy for queue in session.agents.values())
            finally:
                release_turn.set()
                tasks = [
                    queue._flush_task
                    for queue in session.agents.values()
                    if queue._flush_task is not None
                ]
                await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_the_transcript_holds_every_line_spoken_during_init(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """The durable record is written at ARRIVAL, so a reader loses nothing."""
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            await client.post(f"{BASE}/meetings/standup/dispatch", json={"text": "opening remarks"})
            # Readable BEFORE initialization finishes — the user sees their own words.
            body = await (await client.get(f"{BASE}/meetings/standup/transcript")).json()
            assert [s["text"] for s in body["segments"]] == ["opening remarks"]

            release.set()
            assert (await start).status == 200

    @pytest.mark.asyncio
    async def test_a_typed_line_mid_init_keeps_its_chat_marking(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """A held line goes through the same recorder as a live one."""
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            body = await (
                await client.post(
                    f"{BASE}/meetings/standup/dispatch",
                    json={"text": "the owner is Bob", "chat": True},
                )
            ).json()
            assert body["dispatched"] == 0
            assert body["text"].startswith(k.CHAT_PREFIX)
            assert body["segment"]["source"] == k.TRANSCRIPT_SOURCE_TYPED

            release.set()
            assert (await start).status == 200

    @pytest.mark.asyncio
    async def test_a_held_line_is_still_redacted(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """The hold must not become a way for a credential to reach an agent unredacted."""
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            body = await (
                await client.post(
                    f"{BASE}/meetings/standup/dispatch",
                    json={"text": "rotate AKIAIOSFODNN7EXAMPLE today"},
                )
            ).json()
            assert "AKIAIOSFODNN7EXAMPLE" not in body["text"]
            assert "AKIAIOSFODNN7EXAMPLE" not in body["segment"]["text"]

            session = _common.ACTIVE.get("standup")
            assert session is not None
            assert not any("AKIAIOSFODNN7EXAMPLE" in line for line in _held_lines(session))

            release.set()
            assert (await start).status == 200

    @pytest.mark.asyncio
    async def test_the_hold_is_bounded_and_drops_the_oldest(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """The cap holds, and overflow discards the OLDEST lines — never the newest.

        The bound is the security property: ``POST .../dispatch`` takes untrusted
        text, so an unbounded hold on that path would be a memory-exhaustion lever.
        """
        monkeypatch.setattr(k, "MAX_INIT_BUFFER_LINES", 3)
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)

            for index in range(5):
                resp = await client.post(
                    f"{BASE}/meetings/standup/dispatch", json={"text": f"line number {index}"}
                )
                assert resp.status == 200

            session = _common.ACTIVE.get("standup")
            assert session is not None
            # Never more than the cap, and it is the TAIL that survived.
            assert len(session.init_buffer) == 3
            assert _held_lines(session) == ["line number 2", "line number 3", "line number 4"]
            assert session.init_dropped == 2

            release.set()
            assert (await start).status == 200

    @pytest.mark.asyncio
    async def test_an_overflow_marks_the_gap_for_the_agents_and_the_transcript(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """A drop is announced to BOTH readers — a silent truncation is the real bug.

        The marker leads the drained lines, because drop-oldest puts the gap at the
        head of what survived.
        """
        monkeypatch.setattr(k, "MAX_INIT_BUFFER_LINES", 2)
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            for index in range(4):
                await client.post(
                    f"{BASE}/meetings/standup/dispatch", json={"text": f"line number {index}"}
                )

            session = _common.ACTIVE.get("standup")
            assert session is not None
            release.set()
            assert (await start).status == 200

            expected_marker = k.SYSTEM_INIT_BUFFER_OVERFLOW.format(count=2, limit=2)
            # The agents are told first, then given what survived, in the immediate
            # post-init dispatch.
            await self._wait_for_agent_flush(session, "note-taker")
            assert fake_sessions.prompts_for("note-taker")[-1] == (
                f"{expected_marker}\n\nline number 2\n\nline number 3"
            )
            assert session.agents["note-taker"].queue == []
            assert session.init_dropped == 0  # the tally is consumed by the drain

            # And the human transcript states it too, under a source the reader
            # keeps: `read_transcript_page` drops unrecognized sources, so a marker
            # written outside `VALID_TRANSCRIPT_SOURCES` would vanish on read.
            body = await (await client.get(f"{BASE}/meetings/standup/transcript")).json()
            markers = [s for s in body["segments"] if s["source"] == k.TRANSCRIPT_SOURCE_SYSTEM]
            assert len(markers) == 1
            assert markers[0]["text"] == expected_marker
            # Every spoken line is still in the transcript — only the AGENTS lost two.
            assert [s["text"] for s in body["segments"] if s["source"] != "system"] == [
                "line number 0",
                "line number 1",
                "line number 2",
                "line number 3",
            ]

    @pytest.mark.asyncio
    async def test_no_marker_when_the_hold_did_not_overflow(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """A meeting that fits inside the bound gets no system noise at all."""
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "just the one line"}
            )
            release.set()
            assert (await start).status == 200

            body = await (await client.get(f"{BASE}/meetings/standup/transcript")).json()
            assert [s["source"] for s in body["segments"]] == [k.TRANSCRIPT_SOURCE_SPEECH]

    @pytest.mark.asyncio
    async def test_the_poll_reports_the_hold_so_the_microphone_can_open(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """``buffering_dispatches`` is the flag the dashboard's mic gate needs.

        Without it the client keeps the microphone shut for the whole window (its
        gate reads ``accepting_dispatches``), so nothing would ever reach the hold
        and the server-side fix would be unreachable.
        """
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            live = (await (await client.get(f"{BASE}/meetings/standup")).json())["live"]
            # Direct fan-out is shut, but speech LANDS — held.
            assert live["accepting_dispatches"] is False
            assert live["buffering_dispatches"] is True

            release.set()
            assert (await start).status == 200

            live = (await (await client.get(f"{BASE}/meetings/standup")).json())["live"]
            assert live["accepting_dispatches"] is True
            assert live["buffering_dispatches"] is False

    @pytest.mark.asyncio
    async def test_a_reviewing_meeting_still_refuses_instead_of_buffering(self, app, fake_sessions):
        """The existing gate is untouched: only INITIALIZATION holds a line.

        A reviewing meeting has nowhere to put the line — its agents were told to
        finalize — so it must keep answering 409 rather than quietly accumulating
        speech nothing will drain.
        """
        async with client_for(app) as client:
            await _start(client)
            status = await client.post(
                f"{BASE}/meetings/standup/status", json={"status": k.STATUS_REVIEWING}
            )
            assert status.status == 200

            resp = await client.post(f"{BASE}/meetings/standup/dispatch", json={"text": "too late"})
            assert resp.status == 409
            assert (await resp.json())["code"] == "no_active_meeting"

            live = (await (await client.get(f"{BASE}/meetings/standup")).json())["live"]
            assert live["accepting_dispatches"] is False
            assert live["buffering_dispatches"] is False

    @pytest.mark.asyncio
    async def test_an_outgoing_session_being_replaced_does_not_buffer(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """Replacing a lapsed meeting suspends the OUTGOING session to refuse, not hold.

        Its transcript is being flushed and its session dropped; a line held against
        it would be drained by nobody.
        """
        async with client_for(app) as client:
            await _start(client, "the-old-one")
            outgoing = _common.ACTIVE.get("the-old-one")
            assert outgoing is not None
            monkeypatch.setattr(type(outgoing), "expired", property(lambda _self: True))

            start, release = await self._start_paused_in_init(client, monkeypatch, "the-new-one")
            # The replaced meeting refuses; only the starting one holds.
            stale = await client.post(
                f"{BASE}/meetings/the-old-one/dispatch", json={"text": "orphan line"}
            )
            assert stale.status == 409
            held = await client.post(
                f"{BASE}/meetings/the-new-one/dispatch", json={"text": "live line"}
            )
            assert held.status == 200
            starting = _common.ACTIVE.get("the-new-one")
            assert starting is not None
            assert _held_lines(starting) == ["live line"]

            release.set()
            assert (await start).status == 200

    @pytest.mark.asyncio
    async def test_filler_does_not_consume_a_slot_and_evict_real_speech(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """Recognizer filler must not push the opening out of a bounded hold.

        Filtering ran at DRAIN, so ``"uh"`` occupied a slot until initialization
        finished and only then was discarded — meaning a burst of filler could
        evict the genuine speech the cap was supposed to be protecting. A live
        line is filtered the instant it arrives; a held one must be too.
        """
        monkeypatch.setattr(k, "MAX_INIT_BUFFER_LINES", 3)
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)

            await client.post(f"{BASE}/meetings/standup/dispatch", json={"text": "the real agenda"})
            for filler in ("uh", "um", "ok so uh", "hmm"):
                resp = await client.post(f"{BASE}/meetings/standup/dispatch", json={"text": filler})
                assert resp.status == 200

            session = _common.ACTIVE.get("standup")
            assert session is not None
            # Four filler lines went in and none of them displaced anything.
            assert _held_lines(session) == ["the real agenda"]
            assert session.init_dropped == 0

            release.set()
            assert (await start).status == 200
            await self._wait_for_agent_flush(session, "note-taker")
            assert fake_sessions.prompts_for("note-taker")[-1] == "the real agenda"
            assert session.agents["note-taker"].queue == []

    @pytest.mark.asyncio
    async def test_a_mute_during_init_does_not_rob_earlier_speech(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        """Recipients are the ones the line HAD, not the ones it finds at drain.

        Resolving recipients at drain let a mute applied mid-initialization reach
        backwards: a line spoken while the agent was listening silently vanished
        from its output. The live path cannot produce that, because it fans out
        before the mute exists.
        """
        async with client_for(app) as client:
            start, release = await self._start_paused_in_init(client, monkeypatch)
            await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "said while listening"}
            )

            mute = await client.post(
                f"{BASE}/meetings/standup/mute",
                json={"agent_id": "note-taker", "muted": True},
            )
            assert mute.status == 200

            await client.post(
                f"{BASE}/meetings/standup/dispatch", json={"text": "said after the mute"}
            )

            session = _common.ACTIVE.get("standup")
            assert session is not None
            release.set()
            assert (await start).status == 200

            # The note-taker keeps what it was addressed, and gains nothing after.
            await self._wait_for_agent_flush(session, "note-taker")
            await self._wait_for_agent_flush(session, "sketch-artist")
            assert fake_sessions.prompts_for("note-taker")[-1] == "said while listening"
            # An agent unmuted throughout got both lines.
            assert fake_sessions.prompts_for("sketch-artist")[-1] == (
                "said while listening\n\nsaid after the mute"
            )


class TestMuteCannotLandInsideADispatch:
    """A mute may not change the audience of a line already being recorded.

    `handle_mute_agent` wrote `session.muted_agents` with no lock, while both
    dispatch paths resolve their recipients from that set AFTER an awaited
    transcript write. A mute arriving inside that window re-addressed a line to
    the mute state of a moment after it was spoken — delivered to the wrong agent
    set on the live path, and recorded-then-replayed to the wrong set on the
    initialization hold.
    """

    @pytest.mark.asyncio
    async def test_a_mute_racing_the_transcript_write_keeps_the_line_s_audience(
        self, app, fake_sessions, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_for(app) as client:
            start, release = await TestSpeechDuringAgentInitIsHeldNotRefused._start_paused_in_init(
                client, monkeypatch
            )
            session = _common.ACTIVE.get("standup")
            assert session is not None

            entered_append = threading.Event()
            allow_append = threading.Event()
            real_append = store.append_transcript

            def blocking_append(*args, **kwargs):
                """Hold the worker thread inside the transcript write."""
                entered_append.set()
                allow_append.wait(5)
                return real_append(*args, **kwargs)

            monkeypatch.setattr(store, "append_transcript", blocking_append)

            dispatch = asyncio.create_task(
                client.post(
                    f"{BASE}/meetings/standup/dispatch",
                    json={"text": "said while the note-taker was listening"},
                )
            )
            # The dispatch now holds DISPATCH_LOCK and is parked in the append.
            await asyncio.to_thread(entered_append.wait, 5)

            mute = asyncio.create_task(
                client.post(
                    f"{BASE}/meetings/standup/mute",
                    json={"agent_id": "note-taker", "muted": True},
                )
            )
            # It must not be able to apply while the dispatch holds admission.
            await asyncio.sleep(0.05)
            assert not mute.done(), "the mute applied inside the dispatch's window"

            allow_append.set()
            assert (await dispatch).status == 200
            assert (await mute).status == 200

            # The held line kept the audience it had when it was spoken.
            recipients = [names for _line, names in session.init_buffer]
            assert len(recipients) == 1
            assert "note-taker" in recipients[0]

            release.set()
            assert (await start).status == 200
