"""The two origin vocabularies on the Telemetry page, pinned apart.

The page derives "where did this session come from" twice, and the two
derivations answer different questions:

* Spend's **Origin** column is ``session_category(slot)`` -- which surface OWNS
  the session, read from the session key.
* Context's **Turn surface** column is the token row's own ``surface`` field --
  which code path RAN one turn.

The ruling these tests exist to protect: a trusted ``surface`` may NOT introduce
Spend attribution values that do not exist today, and monitor spend stays booked
to the conversation it nudged. A monitor nudge serves exactly one conversation,
so moving its credits into a separate ``monitor`` bucket would make that
conversation under-report what it cost.

The consequence is that the two vocabularies are permanently different sets --
``monitor`` and ``webhook`` are written as row surfaces and are not members of
``TELEMETRY_CHANNELS`` -- and unifying them is a product decision that has been
made and declined. Anything that folds one set into the other has to change a
test here, which is the point: the next author confronts the ruling instead of
discovering it. See ``docs/system-specs/modules/metrics.md``, "Two origin
columns, deliberately".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.messaging.link import TELEMETRY_CHANNELS, telemetry_channel_of

#: Row surfaces written by the product that are NOT channel-taxonomy members:
#: ``monitor`` (``slack/gateway.py``) and ``webhook`` (``handlers/hooks.py``).
#: Held as data so a test that grows a third value says which one it is.
OUT_OF_DOMAIN_SURFACES = ("monitor", "webhook")


def _row(*, slot, surface, credits=1.0, model="m"):
    ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    return {
        "_type": "tokens",
        "ts": ts.isoformat(),
        "slot": slot,
        "model": model,
        "credits": credits,
        "context_used": 0,
        "context_window": 1_000_000,
        "surface": surface,
        "agent": "kirocrew",
        "provider": "acp",
    }


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the aggregator at a throwaway row store."""

    def _write(rows):
        d = tmp_path / "usage" / ("to" + "kens")
        d.mkdir(parents=True, exist_ok=True)
        shard = d / "shard.jsonl"
        shard.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        monkeypatch.setattr(usage_mod, "_shards_in_window", lambda days: [shard])
        # The 30s memo would otherwise serve a previous test's answer.
        monkeypatch.setattr(usage_mod, "_COST_CACHE", None)
        monkeypatch.setattr(usage_mod, "_COST_CACHE_KEY", None)
        return shard

    return _write


class TestTheTwoVocabulariesAreDifferentSets:
    @pytest.mark.parametrize("surface", OUT_OF_DOMAIN_SURFACES)
    def test_a_written_row_surface_is_not_a_channel_taxonomy_member(self, surface):
        """The overlap is empty, and that is the deliberate state.

        Adding either name to ``TELEMETRY_CHANNELS`` is what "unify the two
        taxonomies" means in practice, and it mints a Spend bucket that does not
        exist today.
        """
        assert surface not in TELEMETRY_CHANNELS

    @pytest.mark.parametrize("surface", OUT_OF_DOMAIN_SURFACES)
    def test_the_read_path_leaves_an_out_of_domain_surface_alone(self, surface):
        """No normalisation folds these into a channel name.

        ``_canonical_telemetry_surface`` maps one legacy spelling and nothing
        else, so the Context column keeps reporting the lane that ran the turn.
        """
        assert usage_mod._canonical_telemetry_surface(surface) == surface

    def test_bare_workflow_is_a_member_so_a_stage_turn_is_not_unclassified(self):
        """The near-miss that a taxonomy member closes.

        A workflow stage's own key classifies as ``workflow`` and collapses into
        the background bucket, rather than pooling with unrecognised key shapes.
        """
        assert "workflow" in TELEMETRY_CHANNELS
        assert telemetry_channel_of("wf:run9:1") == "workflow"
        assert usage_mod.session_category("wf:run9:1") == "bg"


class TestSpendKeepsTheSessionKeyAuthoritative:
    def test_a_row_claiming_monitor_stays_booked_to_the_conversation_it_nudged(self, store):
        """The ruling's sharpest case, as an assertion.

        A monitor nudge persists its row against the slot of the conversation it
        serves. The spend therefore belongs to that conversation's bucket, and a
        ``monitor`` bucket must not appear beside it.
        """
        store([_row(slot="chat-41-1785445181", surface="monitor", credits=5.0)])

        out = usage_mod.cost_breakdown(days=1)
        names = [row["name"] for row in out["by_category"]]

        assert names == ["dashboard"]
        assert "monitor" not in names

    def test_a_webhook_row_classifies_by_its_key_shape(self, store):
        """The current answer, pinned including the part that is unflattering.

        A ``hook:``-shaped key is not a taxonomy prefix, so this spend reports
        under the catch-all rather than under a ``webhook`` bucket. Giving it one
        is a taxonomy change, not a relabel.
        """
        store([_row(slot="hook:review-42", surface="webhook", credits=3.0)])

        out = usage_mod.cost_breakdown(days=1)
        names = [row["name"] for row in out["by_category"]]

        assert "webhook" not in names
        assert names == ["other"]

    def test_one_unattended_session_reads_two_different_values_by_design(self):
        """The reading that looks like a bug and is not.

        Spend groups every unattended lane into one bucket; Context keeps the
        lane that ran the turn. Two values for one session is the two questions
        being answered, which is why each column header carries its own tip.
        """
        assert usage_mod.session_category("_hb") == "bg"
        assert telemetry_channel_of("_hb") == "heartbeat"
        assert usage_mod._canonical_telemetry_surface("heartbeat") == "heartbeat"
