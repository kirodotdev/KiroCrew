"""The mirror contract's ratchet: no backend may leave a concern unanswered.

This file is the mechanism the folder alone cannot provide. ``providers/mirrors/``
makes a spec projection easy to find and easy to copy; it does not make anyone
write one. These tests do, by failing when a backend is registered with a concern
it has not ruled on -- which is exactly the failure that let the same
missing-tools defect ship on KAS and then again on claude-agent-acp.

Design: ``docs/request-for-change/rfc-agent-config-mirror.md``.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp_backends import ACP_BACKENDS_KNOWN
from kiro_crew.providers.mirrors import (
    MIRRORS,
    NO_MIRROR,
    Concern,
    Disposition,
    Ruling,
    mirror_for,
)


class TestEveryBackendIsAccountedFor:
    def test_every_known_backend_has_a_mirror_or_a_stated_reason(self):
        """The whole point: absence must be a statement, not an oversight.

        A backend in neither map is the shape of the original defect -- nobody
        declared it needed a projection, so nobody wrote one, and the session came
        up missing its tools with no error.
        """
        unaccounted = sorted(
            b for b in ACP_BACKENDS_KNOWN if b not in MIRRORS and b not in NO_MIRROR
        )
        assert not unaccounted, (
            f"backends with no mirror and no NO_MIRROR entry: {unaccounted}. "
            "Add a mirror in providers/mirrors/ or a NO_MIRROR entry with the reason."
        )

    def test_no_backend_is_in_both_maps(self):
        assert not (set(MIRRORS) & set(NO_MIRROR))

    def test_every_no_mirror_entry_states_a_real_reason(self):
        for backend, reason in NO_MIRROR.items():
            assert len(reason.strip()) > 40, f"{backend!r} needs a real reason, not a label"

    def test_an_unregistered_backend_raises_rather_than_returning_none(self):
        """Fail loud. Returning None would read as 'declared to need no mirror'."""
        with pytest.raises(KeyError, match="no agent-config mirror"):
            mirror_for("a-backend-nobody-registered")

    def test_mirror_for_returns_none_only_for_a_declared_no_mirror(self):
        for backend in NO_MIRROR:
            assert mirror_for(backend) is None
        for backend in MIRRORS:
            assert mirror_for(backend) is not None


class TestRulingsAreComplete:
    @pytest.mark.parametrize("backend", sorted(MIRRORS))
    def test_a_mirror_rules_on_every_concern(self, backend):
        """Completeness is the ratchet.

        Adding a Concern obliges every backend to answer it, which is what stops a
        new setting being delivered to one backend and silently dropped by the rest.
        """
        rulings = mirror_for(backend).rulings()
        missing = sorted(c.value for c in Concern if c not in rulings)
        assert not missing, f"{backend!r} has not ruled on: {missing}"

    @pytest.mark.parametrize("backend", sorted(MIRRORS))
    def test_the_mirror_declares_the_backend_it_serves(self, backend):
        mirror = mirror_for(backend)
        assert mirror.backend == backend
        assert mirror.backend in ACP_BACKENDS_KNOWN

    @pytest.mark.parametrize("backend", sorted(MIRRORS))
    def test_every_no_channel_ruling_names_its_destination(self, backend):
        """A no-channel gap is a backlog item, so it must have an address.

        An unaddressed gap is indistinguishable from a decision, and conflating
        those two is the documented cause of the hooks regression (see
        UNSUPPORTED_SPEC_KEYS in acp/kas_agents.py).
        """
        for concern, ruling in mirror_for(backend).rulings().items():
            if ruling.disposition is Disposition.NO_CHANNEL:
                assert (
                    ruling.channel.strip()
                ), f"{backend!r} {concern.value}: no-channel with no channel named"


class TestRulingRejectsAnIncoherentClaim:
    def test_a_reason_is_required(self):
        with pytest.raises(ValueError, match="needs a reason"):
            Ruling(Disposition.DELIVERED, "   ")

    def test_no_channel_without_a_channel_is_refused(self):
        with pytest.raises(ValueError, match="must name the channel"):
            Ruling(Disposition.NO_CHANNEL, "the backend supports it")

    def test_a_channel_on_any_other_disposition_is_refused(self):
        """A channel on a delivered ruling would read as a gap that is not one."""
        with pytest.raises(ValueError, match="only meaningful"):
            Ruling(Disposition.DELIVERED, "sent on the wire", channel="somewhere")


class TestClaudeCodeMirror:
    def test_hooks_is_the_one_open_gap_and_it_is_addressed(self):
        """Pins the state the RFC's hooks plan starts from.

        Claude Code runs hooks natively; nothing writes them today. When phase H2
        lands, this flips to delivered/translated and this test is what says so.
        """
        rulings = mirror_for("claude").rulings()
        hooks = rulings[Concern.HOOKS]
        assert hooks.disposition is Disposition.NO_CHANNEL
        assert "settings.local.json" in hooks.channel

    def test_auto_approve_is_withheld_not_missing(self):
        """The gate boundary is a decision, and must not read as an oversight."""
        ruling = mirror_for("claude").rulings()[Concern.AUTO_APPROVE]
        assert ruling.disposition is Disposition.WITHHELD
        assert "gate" in ruling.reason

    def test_the_wire_face_returns_the_mcp_servers_key(self, tmp_path):
        params = mirror_for("claude").session_params(None, permission_surface_owned=True)
        assert "mcpServers" in params
        assert isinstance(params["mcpServers"], list)

    def test_the_wire_face_withholds_everything_by_default(self):
        """The precondition defaults to withholding, so forgetting it fails closed.

        Delivering tools into a permission surface Crew does not own hands the
        session a capability nothing can withhold -- a pre-approved tool never
        sends ``session/request_permission``, so Crew's gate never fires. A caller
        that omits the flag therefore gets nothing rather than everything.
        """
        assert mirror_for("claude").session_params(None) == {"mcpServers": []}
        assert mirror_for("claude").session_params(None, permission_surface_owned=False) == {
            "mcpServers": []
        }

    def test_the_mcp_ruling_states_the_precondition(self):
        """The folder is the inventory, so the condition has to be readable there.

        A ruling that says only "delivered" would let the next backend copy the
        delivery and drop the condition that makes it safe.
        """
        ruling = mirror_for("claude").rulings()[Concern.MCP_SERVERS]
        assert ruling.disposition is Disposition.DELIVERED
        assert "settings.local.json" in ruling.reason
