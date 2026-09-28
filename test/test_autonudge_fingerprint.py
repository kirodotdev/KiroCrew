"""The stale-baseline refusal and the echoed-projection guards.

The service-layer half of the goal-token change -- load-time rotation, the in-lock
baseline compare, and the two echo guards in ``autonudge_authz``. The wire projection
is covered separately.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from kiro_crew import autonudge_authz as authz
from kiro_crew.autonudge import AutoNudgeService, AutoNudgeStaleBaseline


class TestTheRedactedProjectionCannotOverwriteTheStoredMessage:
    """A submitted message equal to the stored text's scrubbed projection is UNCHANGED.

    ``svc.add`` stores a message without the PATCH path's redaction, so the popover can
    load a projection that differs from the stored value and Save it straight back. The
    guard compares with the very same ``scrub_loop_text`` the projection uses, so the two
    cannot drift apart. These are its non-regression arms.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @pytest.fixture()
    def audits(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        """Capture SEL events rather than writing them (mirrors the authz suite)."""
        events: list[dict] = []
        monkeypatch.setattr(
            authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
        )
        return events

    @pytest.fixture()
    def svc(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        yield service
        service.stop()

    async def _armed(self, svc):
        """Arm through ``svc.add``, the path that does NOT redact on the way in."""
        original = f"deploy using key {self.SECRET} and report back"
        loop = await svc.add(slot_key="chat-1-123", message=original, idle_secs=300)
        assert loop.message == original, "svc.add unexpectedly redacted on the way in"
        return original, loop.id

    @pytest.mark.asyncio
    async def test_a_genuinely_different_message_still_replaces_and_is_redacted(
        self, svc, audits
    ) -> None:
        """Preserved: a real edit still lands, and inbound redaction still applies."""
        _, loop_id = await self._armed(svc)

        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id=loop_id,
            message=f"completely new instruction {self.SECRET}",
            source="test",
        )
        assert status == 200, f"a genuine edit was refused: {error}"
        assert loop.message.startswith("completely new instruction")
        assert self.SECRET not in loop.message, "inbound redaction was lost"

    @pytest.mark.asyncio
    async def test_a_submitted_empty_string_still_clears_as_it_does_today(
        self, svc, audits
    ) -> None:
        """Preserved: '' is not the projection of a non-empty message, so it applies."""
        _, loop_id = await self._armed(svc)

        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc, loop_id=loop_id, message="", source="test"
        )
        assert status == 200, f"the empty-string update was refused: {error}"
        assert loop.message == "", "'' stopped being applied"

    @pytest.mark.asyncio
    async def test_a_message_with_nothing_to_scrub_is_still_updatable(self, svc, audits) -> None:
        """Preserved: when projection == stored, re-saving it is a genuine no-op.

        A benign message projects to itself, so the new predicate treats a re-save as
        unchanged -- which is correct, because applying it would store the identical
        value. Pinned so the predicate cannot be read as breaking benign saves.
        """
        benign = await svc.add(slot_key="chat-2-456", message="just do it")
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc, loop_id=benign.id, message="just do it", idle_secs=900, source="test"
        )
        assert status == 200, f"a benign re-save was refused: {error}"
        assert loop.message == "just do it"
        assert loop.idle_secs == 900

    @pytest.mark.asyncio
    async def test_an_unedited_save_of_the_raw_stored_text_is_not_redacted_over(
        self, svc, audits
    ) -> None:
        """``GET`` serves the message RAW, so an unedited Save sends back the stored text.

        That text is not its own scrubbed projection, so a projection-only guard let the
        inbound redaction replace an instruction the operator never edited -- with no
        error, no warning and no copy of the original left anywhere.
        """
        original, loop_id = await self._armed(svc)

        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc, loop_id=loop_id, message=original, source="test"
        )

        assert status == 200, f"an unedited save was refused: {error}"
        assert (
            loop.message == original
        ), "the unedited resubmit was redacted over the stored message: " + repr(loop.message)

    @pytest.mark.asyncio
    async def test_an_unknown_loop_still_produces_the_existing_404(self, svc, audits) -> None:
        """Preserved: the pre-read must not invent a second 404 path."""
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc, loop_id="no-such-loop", message="anything", source="test"
        )
        assert status == 404, f"the existing not-found path changed: {status} {error}"
        assert loop is None
        assert error == "loop not found"


class TestAnEchoedBannerDoesNotOverwriteTheStoredOne:
    """FP: the echoed-projection guard covered ``message`` only.

    ``GET`` serves a scrubbed banner, so a client that PATCHes back what it was served
    replaced the operator's stored banner with its own redaction -- irreversibly.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    class _Svc:
        def __init__(self, row: object) -> None:
            self.row = row
            self.applied: dict[str, object] = {}

        def get_by_id(self, _loop_id: str) -> object:
            return self.row

        async def update(self, _loop_id: str, **fields: object) -> object:
            self.applied = fields
            return self.row

    def _row(self, banner: str) -> object:
        return SimpleNamespace(
            id="lp-1", slot_key="dashboard:chat-1", message="goal", banner=banner
        )

    @pytest.mark.asyncio
    async def test_the_served_projection_is_not_written_back(self) -> None:
        from kiro_crew.autonudge import scrub_loop_text
        from kiro_crew.autonudge_authz import authorize_and_update_nudge

        stored = "watching " + self.SECRET
        served = scrub_loop_text(stored)
        assert served != stored, "the fixture banner was not scrubbed, so it proves nothing"
        svc = self._Svc(self._row(stored))

        _loop, error, _status = await authorize_and_update_nudge(
            svc=svc, loop_id="lp-1", banner=served, source="dashboard"
        )

        assert error is None, error
        assert svc.applied.get("banner") is None, (
            "the echoed projection was written back, so the stored banner is now its own "
            "redaction: " + repr(svc.applied.get("banner"))
        )

    @pytest.mark.asyncio
    async def test_a_genuinely_edited_banner_is_still_stored(self) -> None:
        from kiro_crew.autonudge_authz import authorize_and_update_nudge

        svc = self._Svc(self._row("watching " + self.SECRET))

        _loop, error, _status = await authorize_and_update_nudge(
            svc=svc, loop_id="lp-1", banner="watching CI instead", source="dashboard"
        )

        assert error is None, error
        assert svc.applied.get("banner") == "watching CI instead"

    @pytest.mark.asyncio
    async def test_an_unedited_raw_banner_is_not_redacted_over(self) -> None:
        """The banner twin of the message arm: an unedited resubmit must not be applied."""
        from kiro_crew.autonudge_authz import authorize_and_update_nudge

        stored = "watching " + self.SECRET
        svc = self._Svc(self._row(stored))

        _loop, error, _status = await authorize_and_update_nudge(
            svc=svc, loop_id="lp-1", banner=stored, source="dashboard"
        )

        assert error is None, error
        assert svc.applied.get("banner") is None, (
            "an unedited banner resubmit was written back, so the stored banner is now "
            "its own redaction: " + repr(svc.applied.get("banner"))
        )

    @pytest.mark.asyncio
    async def test_re_sending_an_unscrubbed_banner_unchanged_still_writes_it(self) -> None:
        """A clean banner's projection IS the stored text, so an equal value is no echo."""
        from kiro_crew.autonudge_authz import authorize_and_update_nudge

        svc = self._Svc(self._row("cycle ran"))

        _loop, error, _status = await authorize_and_update_nudge(
            svc=svc, loop_id="lp-1", banner="cycle ran", source="dashboard"
        )

        assert error is None, error
        assert svc.applied.get("banner") == "cycle ran", (
            "an idempotent set was mistaken for a destructive echo, so PATCH can no longer "
            "quiet a running loop: " + repr(svc.applied.get("banner"))
        )


class TestAConfirmedGoalIsNotOverwrittenByAStaleClient:
    """The PATCH must compare the caller's baseline INSIDE the store's mutation lock.

    The popover re-checks the stored goal synchronously and then awaits the PATCH, so a
    second client committing in that window had its goal silently overwritten -- the
    endpoint was last-write-wins with no baseline at all. A check anywhere outside the
    lock only narrows that window, so these pin it to the lock itself.
    """

    @pytest.fixture()
    def audits(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        events: list[dict] = []
        monkeypatch.setattr(
            authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
        )
        return events

    @pytest.mark.asyncio
    async def test_a_rotated_secret_variant_is_not_mistaken_for_the_goal_already_seen(
        self, tmp_path, audits
    ) -> None:
        """The two goals differ ONLY inside the span redaction masks.

        Normalising a projection baseline to the stored text let these two collapse: the
        client had seen the OLD goal, the store already held the rotated one, and both
        render the same scrubbed projection -- so the stale write authorised itself.
        """
        # One key-shaped literal, and a distinct same-shape variant derived from it, so
        # the two goals differ only inside the mask.
        key_seen = "AKIAIOSFODNN7EXAMPLE"
        seen = f"deploy with {key_seen} now"
        rotated = f"deploy with {key_seen[:-1] + 'F'} now"
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            armed = await svc.add(slot_key="chat-9-996", message=seen)
            seen_token = armed.goal_token
            await svc.update(armed.id, message=rotated)
            assert (
                armed.goal_token != seen_token
            ), "CONTROL: the write must mint a new identity, or nothing is detectable"

            _loop, error, status = await authz.authorize_and_update_nudge(
                svc=svc,
                loop_id=armed.id,
                message="clobbering goal",
                expect_fingerprint=seen_token,
                source="dashboard",
            )
            assert status == 409, f"a masked baseline authorised a stale write ({status}, {error})"
            assert (
                svc.get_by_id(armed.id).message == rotated
            ), "the rotated goal was overwritten by a client that had only seen the old one"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_current_fingerprint_still_succeeds(self, tmp_path, audits) -> None:
        """POSITIVE CONTROL: the token must not refuse an ordinary save."""
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            armed = await svc.add(slot_key="chat-9-995", message="deploy with AKIAIOSFODNN7EXAMPLE")
            _loop, error, status = await authz.authorize_and_update_nudge(
                svc=svc,
                loop_id=armed.id,
                message="an edited goal",
                expect_fingerprint=armed.goal_token,
                source="dashboard",
            )
            assert status == 200, f"an ordinary save was refused ({status}, {error})"
            assert svc.get_by_id(armed.id).message == "an edited goal"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_stale_token_is_refused_under_the_lock(self, tmp_path) -> None:
        """The refusal is raised by the STORE, under its own mutation lock."""
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            armed = await svc.add(slot_key="chat-9-999", message="original goal")
            with pytest.raises(AutoNudgeStaleBaseline):
                await svc.update(
                    armed.id,
                    message="clobbering goal",
                    expect_fingerprint="a token from a goal since replaced",
                )
            assert (
                svc.get_by_id(armed.id).message == "original goal"
            ), "a stale-baseline write overwrote the goal it never saw"
        finally:
            svc.stop()


class TestTheGoalTokenIsRotatedOnEveryLoad:
    """A token this load did not issue must not authorise a write.

    Nothing the service can read tells it the store file was untouched while the
    process was down, so a pre-restart fingerprint is not honoured.
    """

    def test_the_token_is_never_written_to_disk(self, tmp_path) -> None:
        """A legacy row loads CLEAN, and the rotation is not persisted.

        The rotation is UNCONDITIONAL, so a persisted token could never be honoured, and
        dirtying the store to write one broke the repo's clean-load-no-rewrite invariant.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "lp-1",
                            "slot_key": "chat-1-1",
                            "message": "ship it",
                            "idle_secs": 300,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._loops["lp-1"].goal_token, "precondition: a token was minted in memory"
            assert (
                svc._store_dirty is False
            ), "a legacy row dirtied a clean store to persist a token the next load discards"
            payload = svc._serialize_state()
            assert "goal_token" not in payload["loops"][0], (
                "the token reached the store payload, so a pre-restart value survives on "
                "disk and can authorise a write against a goal edited while we were down"
            )
        finally:
            svc.stop()

    def test_a_persisted_token_that_is_not_generator_shaped_is_re_minted(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING, security): the fingerprint routed around the egress scrub.

        ``goal_token`` is served verbatim as ``message_fingerprint``, and the load path only
        minted one when the field was EMPTY -- so a credential-shaped value hand-written or
        migrated into the store reached every REST and websocket client untouched, past the
        scrub this change exists to add. ADDRESSING_FIELDS are exempt, and this field was
        effectively a third one nobody declared.
        """
        planted = "AKIAIOSFODNN7EXAMPLE"
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "lp-1",
                            "slot_key": "chat-1-1",
                            "message": "ship it",
                            "idle_secs": 300,
                            "goal_token": planted,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loop = svc._loops["lp-1"]
            assert loop.goal_token != planted, (
                "a non-generated token survived the load, so it is served verbatim as "
                "message_fingerprint and bypasses the scrub entirely"
            )
            assert re.fullmatch(
                r"[0-9a-f]{32}", loop.goal_token
            ), f"re-minted token is not generator-shaped: {loop.goal_token!r}"
        finally:
            svc.stop()

    def test_a_valid_token_is_rotated_on_every_load(self, tmp_path) -> None:
        """INVERTED: a valid token surviving the load unchanged is the defect, not the rule.

        GPT 5.6 blocked on that: while the process is down a human can hand-edit the goal
        in the store without touching its token, so a fingerprint issued BEFORE the restart
        still authorised overwriting the edit. The token is an authorisation, and nothing
        this service can read tells it the file was untouched, so it stops honouring one it
        did not issue this load. The cost is a 409 on the first baselined save after a
        restart, which is a refetch, not a lost write.

        Rotated IN MEMORY only: the comparison reads ``loop.goal_token``, so forcing a
        rewrite per boot would buy nothing and break the clean-load-no-rewrite invariant.
        """
        good = "0123456789abcdef0123456789abcdef"
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "lp-1",
                            "slot_key": "chat-1-1",
                            "message": "ship it",
                            "idle_secs": 300,
                            "goal_token": good,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            token = svc._loops["lp-1"].goal_token
            assert token != good, (
                "the pre-restart token survived, so a fingerprint issued before this load "
                "still authorises a write against a goal that may have been hand-edited"
            )
            assert re.fullmatch(r"[0-9a-f]{32}", token), f"rotated to a bad shape: {token!r}"
            assert (
                svc._store_dirty is False
            ), "a clean load must not be rewritten just to persist the rotation"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_pre_restart_token_cannot_overwrite_a_hand_edited_goal(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): the restart + hand-edit interleaving, end to end.

        A client reads the goal and holds its token. The service stops. A human edits the
        goal in the store file and leaves the token alone -- nothing else could, since the
        token is opaque. On restart the client's stale token must not authorise the
        write, or the human's edit is silently replaced.
        """
        store = tmp_path / "autonudge.json"
        issued = "0123456789abcdef0123456789abcdef"
        store.write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "lp-1",
                            "slot_key": "chat-1-1",
                            "message": "the goal the client read",
                            "idle_secs": 300,
                            "goal_token": issued,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        before = AutoNudgeService(base_dir=tmp_path)
        try:
            before._load()
        finally:
            before.stop()

        # The HUMAN edit, with the token deliberately left as it was.
        raw = json.loads(store.read_text(encoding="utf-8"))
        raw["loops"][0]["message"] = "the goal a human typed while we were down"
        raw["loops"][0]["goal_token"] = issued
        store.write_text(json.dumps(raw), encoding="utf-8")

        after = AutoNudgeService(base_dir=tmp_path)
        try:
            after._load()
            with pytest.raises(AutoNudgeStaleBaseline):
                await after.update(
                    "lp-1", message="what the client had typed", expect_fingerprint=issued
                )
            assert after._loops["lp-1"].message == (
                "the goal a human typed while we were down"
            ), "the hand-edited goal was overwritten by a pre-restart authorisation"
        finally:
            after.stop()


class TestTheStaleBaselineRefusalIsNotLoggedAsAFault:
    """A concurrent-edit refusal is an answer the caller surfaces, not an error."""

    @pytest.mark.asyncio
    async def test_a_refused_baseline_is_not_logged_as_a_detached_failure(
        self, tmp_path, caplog
    ) -> None:
        """Opus 4.8: the expected 409 read to an operator as an internal fault.

        ``AutoNudgeStaleBaseline`` is the concurrent-edit refusal the HTTP layer answers
        with 409, so a routine conflict must not emit a WARNING traceback. The second arm
        holds the other half: an exception that is NOT expected still logs as it did.
        """
        import asyncio

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="first goal", idle_secs=300)

            with caplog.at_level("WARNING"):
                with pytest.raises(AutoNudgeStaleBaseline):
                    await svc.update(
                        loop.id, message="second goal", expect_fingerprint="not-the-token"
                    )
                await asyncio.sleep(0)
            assert "detached update() failed" not in caplog.text, (
                "an ordinary concurrent-edit conflict logged a WARNING traceback, so a 409 "
                "the caller already surfaces reads to an operator as an internal failure"
            )

            caplog.clear()

            async def _boom(*_a, **_k):
                raise RuntimeError("genuinely unexpected")

            svc._update_unserialized = _boom
            with caplog.at_level("WARNING"):
                with pytest.raises(RuntimeError):
                    await svc.update(loop.id, message="third goal")
                await asyncio.sleep(0)
            assert "detached update() failed" in caplog.text, (
                "a real failure stopped logging, so the fix suppressed the whole sink rather "
                "than the one expected exception"
            )
        finally:
            svc.stop()
