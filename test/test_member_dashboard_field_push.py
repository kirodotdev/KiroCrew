"""The controller for a package's page: only the model's fields, only what moved.

Three claims, and each is the one a reader of a live dashboard depends on without ever
being told it:

1. A field the model does not declare never reaches the page. The model is the
   allowlist, so a fold carrying a value the owner did not ask for is a value the
   browser never sees -- which is what lets a page bind one field of the work ledger
   without being handed the whole ledger.
2. A fold that advanced for a reason this page does not read sends nothing. A work
   ledger moves on every worker report; a page binding one field of it must not redraw
   on each one.
3. Every patch steps the version by exactly one, and a full read keeps the counter.
   The page refuses a patch it cannot place in order and asks for a whole read, so a
   counter that reset would make the next patch look like a gap and the page would ask
   again forever.

The feed is driven here by publishing on the crew-log bus, not by calling the callback:
the subscription, the baseline and the revision ordering are the half of this that is
easy to get wrong, and a test that bypasses them proves the diff and nothing else.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.crew_log import bus as crew_log_bus
from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

SLOT = "chat-1-1790000000"


def package(types: dict[str, Any] | None = None, html: str = "") -> dict[str, Any]:
    """A canonical package whose view is a page and whose model names two fields.

    With *types* overridden the page binds NOTHING: the write gate refuses a binding the
    model does not declare, so a fixture carrying the default page would be refused for
    the gate's own correct reason and tell a reader nothing about the push.
    """
    if html == "" and types is not None:
        html = "<canvas id=t></canvas>"
    return dp.validate_package(
        {
            "kind": "dashboard",
            "bound_to": "crewmate:mate",
            "model": {
                "types": (
                    types
                    if types is not None
                    else {
                        "goal": {
                            "type": "text",
                            "source": {"fold": "work", "path": "conductor.goal"},
                        },
                        "omitted": {
                            "type": "number",
                            "source": {"fold": "work", "path": "omitted"},
                        },
                    }
                )
            },
            "view": {"html": html or '<p data-dashboard-field="goal"></p>'},
            "theme": {"tokens": {}},
        }
    )


#: Revisions start well above any the eager folder has already minted for this slot.
#:
#: The feed subscribes with a BASELINE, so the fold's real current value arrives before
#: the first publish here -- and the cache drops any event whose revision is not higher
#: than the one it holds. A test publishing from 1 therefore has its values silently
#: ignored and reads the slot's real empty fold instead, which looks exactly like a push
#: that was correctly suppressed.
_REVISION_BASE = 1_000_000


def publish(fold: str, value: dict[str, Any], revision: int, seq: int = 1) -> None:
    """Advance one slot fold on the bus, the way the folder does."""
    revision += _REVISION_BASE
    crew_log_bus.publish(
        crew_log_bus.FOLD_ADVANCED,
        crew_log_bus.FoldAdvanced(
            scope=crew_log_bus.SCOPE_SLOT,
            key=SLOT,
            fold=fold,
            revision=revision,
            seq=seq,
            value=value,
        ),
    )


@pytest.fixture()
def page() -> Any:
    """An armed page over an empty slot, with every frame it sends recorded."""
    sent: list[dict[str, Any]] = []
    live = mdf.LivePage(SLOT, SLOT, package(), sent.append, layout_version=3)
    first = live.arm()
    try:
        yield live, first, sent
    finally:
        live.disarm()


class TestOnlyTheModelsFields:
    def test_a_fold_key_the_model_does_not_declare_is_never_sent(self, page: Any) -> None:
        """The model is the allowlist. A page binding one field of the work ledger must
        not be handed the ledger: the fold's value carries every item, every goal and
        every worker's report, and only the declared paths are resolved out of it."""
        live, _first, sent = page
        publish(
            "work", {"conductor": {"goal": "atlas"}, "omitted": 0, "items": ["secret"]}, revision=1
        )
        assert sent, "the fold moved and nothing was sent"
        assert set(sent[-1]["fields"]) <= {"goal", "omitted"}
        assert "items" not in sent[-1]["fields"]

    def test_the_subscriptions_are_the_folds_the_model_names(self, page: Any) -> None:
        live, _first, _sent = page
        assert live.folds() == frozenset({"work"})

    def test_a_fold_no_field_reads_is_not_subscribed(self) -> None:
        """A subscription this page does not read is a callback on the fold worker's
        thread for every advance of a log nothing on the page shows."""
        live = mdf.LivePage(SLOT, SLOT, package(), lambda frame: None)
        live.arm()
        try:
            assert "ledger" not in live.folds()
        finally:
            live.disarm()


class TestOnlyWhatMoved:
    def test_the_first_change_is_sent(self, page: Any) -> None:
        live, _first, sent = page
        publish("work", {"conductor": {"goal": "atlas"}, "omitted": 0}, revision=1)
        assert sent[-1]["fields"] == {"goal": "atlas"}
        assert sent[-1]["patch"] is True

    def test_a_fold_that_moved_with_no_field_changing_sends_nothing(self, page: Any) -> None:
        """THE RULE THAT MAKES THIS AFFORDABLE. A work ledger advances on every worker
        report; a page binding one field of it redraws only when that field differs."""
        live, _first, sent = page
        publish("work", {"conductor": {"goal": "atlas"}, "omitted": 0}, revision=1)
        before = len(sent)
        publish(
            "work", {"conductor": {"goal": "atlas"}, "omitted": 0, "items": ["new"]}, revision=2
        )
        assert len(sent) == before, "a frame was sent for a fold whose bound fields did not move"

    def test_a_patch_carries_only_the_field_that_changed(self, page: Any) -> None:
        live, _first, sent = page
        publish("work", {"conductor": {"goal": "atlas"}, "omitted": 0}, revision=1)
        publish("work", {"conductor": {"goal": "nyx"}, "omitted": 0}, revision=2)
        assert sent[-1]["fields"] == {"goal": "nyx"}

    def test_a_field_that_stopped_resolving_is_sent_even_though_no_value_moved(
        self, page: Any
    ) -> None:
        """A FIELD THAT STOPPED RESOLVING IS NEWS, and it moves no value.

        ``DashboardFeed.read`` DROPS a field whose fold path does not resolve, so its
        last value stays in the sent table and nothing compares unequal. A comparison
        of values alone therefore sends nothing, and the page keeps drawing that value
        undimmed -- reporting a number as current at the moment the product cannot
        read it.
        """
        live, _first, sent = page
        publish("work", {"conductor": {"goal": "atlas"}, "omitted": 0}, revision=1)
        before = len(sent)
        # The bound path is gone. The value this page holds for it does not change --
        # the feed simply stops answering for it.
        publish("work", {"omitted": 0}, revision=2)
        assert len(sent) > before, (
            "a field stopped resolving and the page was told nothing, so it is still "
            "showing the last value undimmed"
        )
        assert "goal" in (sent[-1].get("missing") or [])

    def test_a_value_returning_to_an_earlier_one_is_a_change(self, page: Any) -> None:
        """The comparison is against what was SENT, not against a history: a field that
        went atlas -> nyx -> atlas has to be sent three times, because the page is
        showing nyx."""
        live, _first, sent = page
        publish("work", {"conductor": {"goal": "atlas"}, "omitted": 0}, revision=1)
        publish("work", {"conductor": {"goal": "nyx"}, "omitted": 0}, revision=2)
        publish("work", {"conductor": {"goal": "atlas"}, "omitted": 0}, revision=3)
        assert [f["fields"] for f in sent[-2:]] == [{"goal": "nyx"}, {"goal": "atlas"}]


class TestTheVersionAndTheRefetch:
    def test_the_first_read_is_not_a_patch(self, page: Any) -> None:
        """The first paint replaces nothing and merges nothing -- it IS the page's whole
        read, so a merge marker on it would be a claim about a previous read that does
        not exist."""
        _live, first, _sent = page
        assert "patch" not in first
        # A FULL READ MINTS ITS OWN VERSION rather than reporting the counter's current
        # value. Reusing it let a fold advance between the read and the stamp, so an
        # older full read could carry a number a newer patch had already used and the
        # page would accept it, discarding the newer update.
        assert first["version"] == 1

    def test_each_patch_steps_the_version_by_one(self, page: Any) -> None:
        live, _first, sent = page
        for n, who in enumerate(("a", "b", "c"), start=1):
            publish("work", {"conductor": {"goal": who}, "omitted": 0}, revision=n)
        # CONSECUTIVE FROM ONE, counted rather than listed: the eager folder may have
        # moved the slot's own fold before this test published anything, and a literal
        # list would then fail for a frame the page was right to send.
        # From wherever arming left the counter, consecutively. Arming mints one, so
        # the first patch is not 1.
        versions = [f["version"] for f in sent]
        assert versions == list(range(versions[0], versions[0] + len(versions)))
        assert len(sent) >= 3

    def test_a_refetch_answers_at_the_current_version_not_above_it(self, page: Any) -> None:
        """A PATCH NUMBER IS SPENT ON A PATCH, and a full read is not one.

        A patch reaches every owner socket; a refetch answer reaches only the tab that
        asked. Minting a number here spends one the other tabs never see, so each of
        them reads the next push as a gap and refetches -- and each refetch puts the
        others behind, re-reading the tree folds under the page lock every time.
        Two tabs on one package, or one tab plus the fallback read that re-arms the
        page, is enough to start it.

        A full read REPLACES whatever the page holds rather than following it, so it
        needs no number of its own: it carries the current one, and the next patch is
        still the successor of what every tab last saw. The counter is never reset --
        that would make the next patch look like a gap and the page would ask forever.
        """
        live, _first, sent = page
        publish("work", {"conductor": {"goal": "atlas"}, "omitted": 7}, revision=1)
        whole = live.refetch()
        assert whole["fields"] == {"goal": "atlas", "omitted": 7}
        assert "patch" not in whole
        assert whole["version"] == sent[-1]["version"], (
            "the refetch spent a patch number, so every other tab on this package now "
            "reads the next push as a gap"
        )
        publish("work", {"conductor": {"goal": "nyx"}, "omitted": 7}, revision=2)
        assert sent[-1]["version"] == whole["version"] + 1

    def test_the_frame_names_the_layout_it_was_computed_under(self, page: Any) -> None:
        """A page rendered under an older layout must not have these values applied: the
        field set itself may have changed, so the frame says which layout it belongs
        to rather than leaving the page to assume."""
        live, _first, _sent = page
        assert live.layout_version == 3


class TestRedactionCannotBeSkipped:
    def test_every_value_crosses_the_redactor_on_both_paths(self) -> None:
        """One function produces values for the first paint AND for every patch, so a
        route that hands in a masking pass cannot have it apply to one and not the
        other."""
        seen: list[Any] = []
        frames: list[dict[str, Any]] = []

        def redact(value: Any) -> Any:
            seen.append(value)
            # MARKED, not flattened: a redactor that returned one constant for every
            # string would make two different values compare equal, so no patch would
            # ever be sent and this test would pass for the wrong reason.
            return f"<{value}>" if isinstance(value, str) else value

        live = mdf.LivePage(SLOT, SLOT, package(), frames.append, redact=redact)
        live.arm()
        try:
            publish("work", {"conductor": {"goal": "atlas"}, "omitted": 0}, revision=1)
            assert frames[-1]["fields"]["goal"] == "<atlas>"
            assert "atlas" in seen
        finally:
            live.disarm()


class TestTheModelBecomesTheManifest:
    def test_a_fold_field_keeps_its_fold_and_path(self) -> None:
        manifest = mdf.package_manifest(package())
        spec = manifest.fields["goal"]
        assert (spec.fold, spec.path, spec.agentic) == ("work", "conductor.goal", False)

    def test_a_custom_field_is_the_owners_own(self) -> None:
        manifest = mdf.package_manifest(
            package(
                types={
                    "verdict": {"type": "text", "source": {"agentic": True}},
                }
            )
        )
        spec = manifest.fields["verdict"]
        assert spec.agentic is True
        assert (spec.fold, spec.path) == (None, None)

    def test_the_translated_manifest_is_not_a_renderable_source(self) -> None:
        """``_trusted_page`` gates the TEMPLATE render path on this field. A renderable
        source here would be this translation vouching for a page the repository did not
        ship -- and a package's page is trusted for a different reason entirely."""
        from kiro_crew.dashboard_templates import instance as instance_store

        assert mdf.package_manifest(package()).source not in instance_store.RENDERABLE_SOURCES

    def test_every_package_data_type_maps_to_a_manifest_type(self) -> None:
        """Enumerated from the catalog rather than from a list copied out of it, so a
        type the data line adds fails here instead of silently becoming a string."""
        from kiro_crew.dashboard_templates.manifest import FIELD_TYPES

        for name in dp.data_type_catalog():
            assert name in mdf._TYPE_MAP, f"the data type {name!r} has no manifest shape"
            assert mdf._TYPE_MAP[name] in FIELD_TYPES


class TestACustomValueRidesTheSameSubscription:
    def test_the_agentic_fold_is_subscribed_when_the_model_names_a_custom_field(self) -> None:
        """No field reads a path into it, so it has to be asked for by name. Reading it
        off disk per change instead would put file I/O on the fold worker's thread,
        which is the one thread the feed may not block."""
        live = mdf.LivePage(
            SLOT,
            SLOT,
            package(types={"verdict": {"type": "text", "source": {"agentic": True}}}),
            lambda frame: None,
        )
        live.arm()
        try:
            assert mdf.AGENTIC_FOLD in live.folds()
        finally:
            live.disarm()

    def test_the_two_spellings_of_the_agentic_fold_agree(self) -> None:
        """The feed names this fold too, because it is the one fold a caller asks for
        by flag rather than through the manifest -- and this module cannot import the
        feed at module scope, since the gateway imports it before the socket binds. So
        the two spellings are pinned to each other instead: a rename on one side would
        otherwise leave a page subscribed to a fold nothing publishes, silently."""
        from kiro_crew import dashboard_feed

        assert mdf.AGENTIC_FOLD == dashboard_feed.AGENTIC_FOLD

    def test_a_custom_value_the_owner_wrote_is_pushed(self) -> None:
        frames: list[dict[str, Any]] = []
        live = mdf.LivePage(
            SLOT,
            SLOT,
            package(types={"verdict": {"type": "text", "source": {"agentic": True}}}),
            frames.append,
        )
        live.arm()
        try:
            publish(
                mdf.AGENTIC_FOLD,
                {"fields": {"verdict": {"value": "ship it", "at": "2026-10-11T00:00:00+00:00"}}},
                revision=1,
            )
            assert frames[-1]["fields"] == {"verdict": "ship it"}
            assert frames[-1]["agentic"] == ["verdict"]
        finally:
            live.disarm()

    def test_an_unwritten_custom_field_is_missing_and_no_stale_flag_is_sent(self) -> None:
        """A cell the owner has not filled is ABSENT, not older than the record: it is
        dimmed through `missing`, and the read carries no staleness flag at all."""
        live = mdf.LivePage(
            SLOT,
            SLOT,
            package(types={"verdict": {"type": "text", "source": {"agentic": True}}}),
            lambda frame: None,
        )
        first = live.arm()
        try:
            assert "stale" not in first
            assert first["missing"] == ["verdict"]
        finally:
            live.disarm()
