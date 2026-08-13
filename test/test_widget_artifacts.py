"""Tests for :mod:`kiro_crew.widget_artifacts` — chat-widget auto-registration."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from kiro_crew import widget_artifacts
from kiro_crew.artifacts import ArtifactComment, ArtifactPublication, ArtifactStore
from kiro_crew.widget_slug import derive_widget_slug


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    """A tmp-rooted store installed as the module's default."""
    s = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(widget_artifacts, "get_default_store", lambda: s)
    return s


WIDGET_MSG = 'Here:\n<mcwidget title="Chart">\n<div>hi</div>\n</mcwidget>'


class TestRegisterWidgets:
    def test_registers_widget_unpinned_and_auto_flagged(self, store: ArtifactStore) -> None:
        slugs = widget_artifacts.register_widgets(WIDGET_MSG, "ts-1", "chat-1")
        assert slugs == [derive_widget_slug("ts-1", 0)]
        art = store.get(slugs[0])
        assert art.kind == "widget"
        assert art.name == "Chart"
        assert art.content == "<div>hi</div>"
        # Unpinned by default — registration is a record, not a library entry.
        assert art.pinned is False
        assert art.auto_registered is True
        # Stored verbatim — the caller passes the bare slot key, which is what
        # the in-session tab queries with.
        assert art.session_key == "chat-1"

    def test_slug_matches_frontend_derivation(self, store: ArtifactStore) -> None:
        """The whole scheme rests on this: the frontend must find what we wrote."""
        widget_artifacts.register_widgets(WIDGET_MSG, "1779995123.456789", "chat-1")
        assert store.get("4dc7b6b89ccdb068").name == "Chart"

    def test_registered_widget_is_findable_by_the_session_query(self, store: ArtifactStore) -> None:
        """The in-session Artifacts tab must actually find what we registered.

        The tab sends ``?session=<activeSlot>`` — the BARE slot key — and
        ``ArtifactStore.list`` compares ``session_key`` exactly (no prefix
        folding, unlike the session-docs scan). Storing a decorated key here
        (e.g. ``dashboard:<key>``) leaves the tab permanently empty of widgets
        while every unit test on the write side still passes, so assert the
        round-trip rather than the stored string.
        """
        slot_key = "chat-1-1779995123"
        slugs = widget_artifacts.register_widgets(WIDGET_MSG, "ts-session", slot_key)
        assert slugs
        found = store.list(session_key=slot_key)
        assert [a.slug for a in found] == slugs

    def test_two_widgets_get_distinct_slugs(self, store: ArtifactStore) -> None:
        text = '<mcwidget title="A">1</mcwidget>\n<mcwidget title="B">2</mcwidget>'
        slugs = widget_artifacts.register_widgets(text, "ts-2", "chat-1")
        assert slugs == [derive_widget_slug("ts-2", 0), derive_widget_slug("ts-2", 1)]
        assert store.get(slugs[0]).name == "A"
        assert store.get(slugs[1]).name == "B"

    def test_idempotent_across_replays(self, store: ArtifactStore) -> None:
        """A re-finalized/rehydrated message must not duplicate or clobber."""
        first = widget_artifacts.register_widgets(WIDGET_MSG, "ts-3", "chat-1")
        assert first
        again = widget_artifacts.register_widgets(WIDGET_MSG, "ts-3", "chat-1")
        assert again == []  # nothing newly created
        assert len(store.list()) == 1

    def test_replay_does_not_overwrite_user_edits(self, store: ArtifactStore) -> None:
        """The user may have iterated on the artifact since it was registered."""
        slug = widget_artifacts.register_widgets(WIDGET_MSG, "ts-4", "chat-1")[0]
        store.update(slug, content="<div>edited by user</div>")
        widget_artifacts.register_widgets(WIDGET_MSG, "ts-4", "chat-1")
        assert store.get(slug).content == "<div>edited by user</div>"

    def test_explicit_slug_attribute_is_skipped(self, store: ArtifactStore) -> None:
        """A re-emission names an existing artifact — re-emitting is not authoring."""
        text = '<mcwidget title="Saved" slug="cr-queue">body</mcwidget>'
        assert widget_artifacts.register_widgets(text, "ts-5", "chat-1") == []
        assert store.list() == []

    def test_missing_message_ts_registers_nothing(self, store: ArtifactStore) -> None:
        """Without a stable ts there is no slug the frontend could look up."""
        assert widget_artifacts.register_widgets(WIDGET_MSG, "", "chat-1") == []
        assert store.list() == []

    def test_empty_widget_body_skipped(self, store: ArtifactStore) -> None:
        assert widget_artifacts.register_widgets("<mcwidget></mcwidget>", "ts-6", "chat-1") == []
        assert store.list() == []

    def test_no_widgets_is_a_noop(self, store: ArtifactStore) -> None:
        assert widget_artifacts.register_widgets("just prose", "ts-7", "chat-1") == []
        assert store.list() == []

    def test_backtick_quoted_tag_is_not_registered(self, store: ArtifactStore) -> None:
        text = 'Use `<mcwidget title="X">html</mcwidget>` to render one.'
        assert widget_artifacts.register_widgets(text, "ts-8", "chat-1") == []
        assert store.list() == []

    def test_store_failure_does_not_raise(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registration failure must never break the chat turn that caused it."""

        def boom(*a: object, **k: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store, "create", boom)
        assert widget_artifacts.register_widgets(WIDGET_MSG, "ts-9", "chat-1") == []

    def test_parser_failure_does_not_raise(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_text: str) -> None:
            raise RuntimeError("parser bug")

        monkeypatch.setattr(widget_artifacts, "parse_widgets", boom)
        assert widget_artifacts.register_widgets(WIDGET_MSG, "ts-10", "chat-1") == []


class TestRetentionSweep:
    def test_prunes_oldest_unpinned_past_cap(self, store: ArtifactStore) -> None:
        for i in range(6):
            store.create(
                name=f"w{i}",
                content=f"<div>{i}</div>",
                slug=f"auto-{i}",
                kind="widget",
                auto_registered=True,
            )
        assert store.prune_auto_widgets(keep=4) == 2
        # Assert WHICH survived, not merely how many: artifacts created inside
        # one microsecond tie on ``updated_at``, so a count-only assertion passes
        # regardless of which two were destroyed. The sweep breaks ties on slug,
        # so the boundary is deterministic.
        assert {a.slug for a in store.list()} == {"auto-5", "auto-4", "auto-3", "auto-2"}

    def test_pinned_auto_widgets_are_exempt(self, store: ArtifactStore) -> None:
        """Starring is the user's 'keep this' signal — the sweep must honor it."""
        for i in range(5):
            store.create(
                name=f"w{i}",
                content=f"<div>{i}</div>",
                slug=f"auto-{i}",
                kind="widget",
                auto_registered=True,
            )
        store.set_pinned("auto-0", True)  # the OLDEST — first in line to be swept
        store.prune_auto_widgets(keep=1)
        assert store.get("auto-0").pinned is True
        remaining = {a.slug for a in store.list()}
        assert "auto-0" in remaining

    def test_explicit_saves_are_never_swept(self, store: ArtifactStore) -> None:
        for i in range(5):
            store.create(name=f"manual{i}", content=f"<div>{i}</div>", slug=f"manual-{i}")
        assert store.prune_auto_widgets(keep=1) == 0
        assert len(store.list()) == 5

    def test_under_cap_is_a_noop(self, store: ArtifactStore) -> None:
        store.create(name="w", content="<div>x</div>", slug="auto-0", auto_registered=True)
        assert store.prune_auto_widgets(keep=10) == 0
        assert len(store.list()) == 1

    # ── Claim signals: every one of these must exempt a record ──────────────
    #
    # The sweep DELETES data, so any sign of human/agent investment other than a
    # star must also protect the artifact. Each case below is a way a user can
    # invest in a widget without starring it.

    def _make_autos(self, store: ArtifactStore, n: int = 4) -> None:
        for i in range(n):
            store.create(
                name=f"w{i}",
                content=f"<div>{i}</div>",
                slug=f"auto-{i}",
                kind="widget",
                auto_registered=True,
            )

    def test_filed_into_a_folder_is_exempt(self, store: ArtifactStore) -> None:
        """Filing is curation — the user deliberately organized this."""
        self._make_autos(store)
        store.set_folder("auto-0", "some-folder-id")
        store.prune_auto_widgets(keep=1)
        assert "auto-0" in {a.slug for a in store.list()}

    def test_silently_edited_widget_is_exempt(self, store: ArtifactStore) -> None:
        """A content save WITHOUT ``snapshot=True`` must still exempt.

        This is the common agent-iteration path, and it does NOT bump ``version``
        (see ``update``) — so a ``version > 1`` test would happily delete a widget
        whose body the agent had just rewritten. Keyed on ``updated_at`` instead.
        """
        self._make_autos(store)
        store.update("auto-0", content="<div>refined by the agent</div>")
        assert store.get("auto-0").version == 1, "guards the premise of this test"
        store.prune_auto_widgets(keep=0)
        assert "auto-0" in {a.slug for a in store.list()}

    def test_snapshotted_edit_is_exempt(self, store: ArtifactStore) -> None:
        """The version-bumping edit path is exempt too."""
        self._make_autos(store)
        store.update("auto-0", content="<div>v2</div>", snapshot=True)
        assert store.get("auto-0").version == 2
        store.prune_auto_widgets(keep=0)
        assert "auto-0" in {a.slug for a in store.list()}

    def test_published_widget_is_exempt(self, store: ArtifactStore) -> None:
        """A live share URL points at it — deleting breaks someone else's link."""
        self._make_autos(store)
        art = store.get("auto-0")
        art.publication = ArtifactPublication(
            artifact_id="ext-1", view_url="https://example.invalid/a/ext-1"
        )
        store._write_meta(art)
        store.prune_auto_widgets(keep=1)
        assert "auto-0" in {a.slug for a in store.list()}

    def test_tagged_is_exempt(self, store: ArtifactStore) -> None:
        self._make_autos(store)
        store.update("auto-0", tags=["keep"])
        store.prune_auto_widgets(keep=0)
        assert "auto-0" in {a.slug for a in store.list()}

    def test_described_is_exempt(self, store: ArtifactStore) -> None:
        self._make_autos(store)
        store.update("auto-0", description="worth keeping")
        store.prune_auto_widgets(keep=0)
        assert "auto-0" in {a.slug for a in store.list()}

    def test_untouched_widget_is_still_swept(self, store: ArtifactStore) -> None:
        """The exemptions must not accidentally exempt everything."""
        self._make_autos(store)
        assert store.prune_auto_widgets(keep=1) == 3
        assert len(store.list()) == 1

    def test_star_between_snapshot_and_delete_is_honored(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TOCTOU: a star landing after the candidate snapshot must save the artifact.

        The snapshot is taken unlocked, so without a re-check the sweep would
        delete an artifact the user starred a moment earlier — silent data loss
        with no way to notice. Hooks ``_load_meta`` (inside the sweep's lock) so
        the star lands strictly between the snapshot and the eligibility re-check.
        """
        self._make_autos(store)
        real_load = store._load_meta
        fired: list[str] = []

        def racing_load(slug: str):  # type: ignore[no-untyped-def]
            # The user stars auto-1 just before the sweep re-reads it.
            if slug == "auto-1" and not fired:
                fired.append(slug)
                store._write_meta(  # bypass set_pinned to avoid re-entering the lock
                    dataclasses.replace(real_load(slug), pinned=True)
                )
            return real_load(slug)

        monkeypatch.setattr(store, "_load_meta", racing_load)
        store.prune_auto_widgets(keep=0)
        remaining = {a.slug for a in store.list()}
        assert "auto-1" in remaining, "a widget starred mid-sweep must survive"

    def test_eligibility_recheck_and_removal_share_one_lock(self, store: ArtifactStore) -> None:
        """The re-check and the directory removal must be ONE critical section.

        Re-checking under the lock and then delegating to ``delete()`` (which
        re-acquires) reopens the very window the re-check closes: a pin landing
        between the two acquisitions loses to a delete acting on a stale verdict.
        Asserted structurally — the sweep must not call ``delete()`` at all.
        """
        self._make_autos(store)
        calls: list[str] = []
        real_delete = store.delete

        def tracking_delete(slug: str) -> None:
            calls.append(slug)
            real_delete(slug)

        store.delete = tracking_delete  # type: ignore[method-assign]
        try:
            assert store.prune_auto_widgets(keep=1) == 3
        finally:
            del store.delete  # type: ignore[attr-defined]
        assert calls == [], "prune must remove inline under its own lock, not via delete()"
        assert len(store.list()) == 1

    def test_commented_widget_is_exempt(self, store: ArtifactStore) -> None:
        """Commenting is investment — and it is invisible to every other signal.

        ``add_comment`` writes only the ``comments.json`` sidecar; it does not
        touch ``meta.json``, so ``updated_at``/``version``/``tags`` all still look
        untouched. Without an explicit check the sweep deletes the artifact AND
        the user's comments with it.
        """
        self._make_autos(store)
        store.add_comment("auto-0", ArtifactComment(id="c1", body="worth keeping", author="user"))
        art = store.get("auto-0")
        assert art.updated_at == art.created_at, "guards the premise: meta is untouched"
        store.prune_auto_widgets(keep=0)
        assert "auto-0" in {a.slug for a in store.list()}

    def test_sweep_runs_after_registration(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(widget_artifacts, "MAX_AUTO_WIDGET_ARTIFACTS", 1)
        widget_artifacts.register_widgets(WIDGET_MSG, "ts-a", "chat-1")
        widget_artifacts.register_widgets(WIDGET_MSG, "ts-b", "chat-1")
        # Cap of 1 keeps only the newest.
        assert len(store.list()) == 1

    def test_prune_failure_does_not_break_registration(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(**_k: object) -> None:
            raise OSError("cannot stat")

        monkeypatch.setattr(store, "prune_auto_widgets", boom)
        assert widget_artifacts.register_widgets(WIDGET_MSG, "ts-c", "chat-1")


class TestOffLoopWrapper:
    @pytest.mark.asyncio
    async def test_off_loop_registers(self, store: ArtifactStore) -> None:
        """The async path is what chat_runner uses — it must not block the loop."""
        slugs = await widget_artifacts.register_widgets_off_loop(WIDGET_MSG, "ts-async", "chat-1")
        assert slugs == [derive_widget_slug("ts-async", 0)]
        assert store.get(slugs[0]).name == "Chart"
