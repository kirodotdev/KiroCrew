"""Whether an agent-written dashboard page may run script, decided by the ceiling.

The RFC amendment licenses a running owner-written page on four reasons, and the
fourth is that the owner agent already reaches the network, so a page it wrote
navigating its own frame opens no path that agent did not already have.

That reason holds only while the agent's egress is UNRESTRICTED. A host allowlist
is the case worth spelling out: the agent may fetch one host and nothing else,
but a frame that runs script can navigate ITSELF anywhere, so the page would
reach hosts the ceiling never permitted. "Some egress" is not the premise.

So scripts run only for an agent nothing restricts, and the three cases below
are the ones that decide it: unrestricted runs, an allowlist does not, deny-all
does not. The page is then static and its fields still fill, because a
`data-dashboard-field` binding is the HOST writing `textContent` into the
composed document and needs no script inside the frame.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.dashboard.handlers import member_dashboard as md
from kiro_crew.platform import governance as gov


def _ruleset(mode: str, allow: tuple[str, ...] = (), deny: tuple[str, ...] = ()) -> Any:
    return gov.ScopedRuleset(mode=mode, allow=allow, deny=deny, matcher="host")


def _ceiling_with(control: Any, *, governed: bool = True) -> Any:
    controls = {"network.egress": control} if governed else {}
    return gov.GovernanceCeiling(version=1, boot=gov.BootControls(), controls=controls)


@pytest.fixture
def ceiling(monkeypatch):
    """Install a ceiling, and a profile answer beside it.

    The grant is POLICY AND PROFILE, so a fixture that set only the ceiling would
    leave the profile half reading the developer's own platform context -- and every
    case below would then be asserting two things at once.
    """

    def _install(control: Any, *, governed: bool = True, profile_permits: bool = True) -> None:

        from kiro_crew.platform import governance_profiles as profiles_mod

        monkeypatch.setattr(
            gov, "last_composed_ceiling", lambda: _ceiling_with(control, governed=governed)
        )
        # The profile half asks the resolver for the active scope and then tests the
        # ruleset's ARCHETYPE, so the stub is a Profile and not a permit answer: a
        # permitted probe answers about one host, which is not the question.
        profile = (
            None
            if profile_permits
            else gov.Profile(
                name="dashboard",
                controls={"network.egress": _ruleset(gov.MODE_ALLOW, allow=("api.github.com",))},
            )
        )
        monkeypatch.setattr(profiles_mod, "resolve_active_scope", lambda *a, **k: profile)

    return _install


class TestTheThreeCasesThatDecideIt:
    """The ruling's own three cases, each asserted on its own."""

    def test_an_unrestricted_agent_gets_scripts(self, ceiling) -> None:
        """A deny-mode ruleset that denies NOTHING restricts nothing."""
        ceiling(_ruleset(gov.MODE_DENY, deny=()))
        assert md.owner_agent_egress_unrestricted() is True

    def test_a_host_allowlist_gets_NO_scripts(self, ceiling) -> None:
        """THE HOLE THIS CLOSES. The agent may reach `api.github.com` and nothing
        else, so a page that can navigate its own frame would reach hosts the
        ceiling never permitted. An allow-mode ruleset is a closed set, so it
        restricts even though it names a host the agent may use."""
        ceiling(_ruleset(gov.MODE_ALLOW, allow=("api.github.com",)))
        assert md.owner_agent_egress_unrestricted() is False

    def test_deny_all_gets_NO_scripts(self, ceiling) -> None:
        ceiling(_ruleset(gov.MODE_DENY, deny=("*",)))
        assert md.owner_agent_egress_unrestricted() is False


class TestTheProfileIsAskedTooNotJustThePolicy:
    """THE CEILING IS THE OUTER BOUND, NOT THE ANSWER.

    A profile narrows what the policy permits, and the two come apart in the ordinary
    case: an open policy with a profile that restricts ``network.egress`` is an agent
    whose real egress is restricted. Reading the ceiling alone called that agent
    unrestricted and handed its page ``allow-scripts``, so the page could navigate its
    own frame -- carrying the crewmate's fold values -- to a host the profile forbids.

    Asked through ``governance_permits``, the one place ceiling and profile are
    intersected, so this is not a second resolver with its own answer.
    """

    def test_an_open_policy_with_a_restricting_profile_gets_NO_scripts(self, ceiling) -> None:
        """THE REPORTED CASE. The policy restricts nothing and the profile does."""
        ceiling(_ruleset(gov.MODE_DENY, deny=()), profile_permits=False)
        assert md.owner_agent_egress_unrestricted("Atlas") is False

    def test_both_open_still_gets_scripts(self, ceiling) -> None:
        """The limit: the grant is unchanged where neither layer restricts."""
        ceiling(_ruleset(gov.MODE_DENY, deny=()), profile_permits=True)
        assert md.owner_agent_egress_unrestricted("Atlas") is True

    def test_an_ungoverned_policy_still_asks_the_profile(self, ceiling) -> None:
        """The scope's absence from the ceiling is not an answer about the profile.
        This is the standalone default, which is every install that configures no
        policy -- and a profile can still restrict egress there."""
        ceiling(None, governed=False, profile_permits=False)
        assert md.owner_agent_egress_unrestricted("Atlas") is False

    def test_a_profile_that_cannot_be_resolved_gets_NO_scripts(self, monkeypatch) -> None:
        """FAILS CLOSED on the profile half as it already did on the policy half: a
        page runs only when both were read and both restrict nothing."""
        from kiro_crew.platform import governance_profiles as profiles_mod

        monkeypatch.setattr(
            gov, "last_composed_ceiling", lambda: _ceiling_with(None, governed=False)
        )

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("the profile store is unreadable")

        monkeypatch.setattr(profiles_mod, "resolve_active_scope", _boom)
        assert md.owner_agent_egress_unrestricted("Atlas") is False

    def test_the_surfaces_own_key_is_what_is_asked_with(self, ceiling, monkeypatch) -> None:
        """A PROFILE IS BOUND TO A SURFACE, and the resolver infers the surface from the
        session key. Asking with none resolves the scope to "unknown", which matches no
        surface profile -- so the intersection falls back to policy alone and answers
        True for exactly the agent a `surface: dashboard` profile restricts.
        """

        from kiro_crew.platform import governance_profiles as profiles_mod

        asked: list[str] = []

        def _record(session_key: str, **_kw: Any) -> Any:
            asked.append(session_key)
            return None

        monkeypatch.setattr(
            gov, "last_composed_ceiling", lambda: _ceiling_with(None, governed=False)
        )
        monkeypatch.setattr(profiles_mod, "resolve_active_scope", _record)
        md.owner_agent_egress_unrestricted("Atlas", "chat-7-1791007589")
        assert asked == ["chat-7-1791007589"], (
            "the egress question carries no session key, so no surface-bound profile "
            "can be the one consulted"
        )

    def test_a_profile_denying_one_host_is_restricted(self, ceiling, monkeypatch) -> None:
        """THE PROBE THIS REPLACES. Asking whether one host is permitted answers only
        about that host: a profile that denies `github.com` permits every other name,
        so any probe it happens to allow reads as "unrestricted" while the restriction
        is still there. The question is whether the scope is RESTRICTED AT ALL, and a
        deny-mode ruleset naming anything is."""
        from kiro_crew.platform import governance_profiles as profiles_mod

        monkeypatch.setattr(
            gov, "last_composed_ceiling", lambda: _ceiling_with(None, governed=False)
        )
        monkeypatch.setattr(
            profiles_mod,
            "resolve_active_scope",
            lambda *a, **k: gov.Profile(
                name="task",
                controls={"network.egress": _ruleset(gov.MODE_DENY, deny=("github.com",))},
            ),
        )
        assert md.owner_agent_egress_unrestricted("Atlas", "chat-7-1791007589") is False

    def test_a_profile_that_governs_other_scopes_narrows_nothing_here(
        self, ceiling, monkeypatch
    ) -> None:
        """The limit: a profile is bound for its own reasons, and one that says nothing
        about egress restricts no egress."""
        from kiro_crew.platform import governance_profiles as profiles_mod

        monkeypatch.setattr(
            gov, "last_composed_ceiling", lambda: _ceiling_with(None, governed=False)
        )
        monkeypatch.setattr(
            profiles_mod,
            "resolve_active_scope",
            lambda *a, **k: gov.Profile(name="task", controls={"commands": object()}),
        )
        assert md.owner_agent_egress_unrestricted("Atlas", "chat-7-1791007589") is True

    def test_the_key_the_route_passes_resolves_to_the_dashboard_surface(self) -> None:
        """THE BOUNDARY, asked of the resolver rather than compared to a literal.

        A profile is bound to a surface and the surface is read off the key's prefix.
        A crewmate's slot is a bare ``member-*`` string, which the canonical classifier
        sends to its Slack fallback -- so a key built from the slot alone skips every
        dashboard-bound profile and the page runs script the operator restricted.
        """
        from kiro_crew.platform import governance_profiles as profiles_mod

        key = md.governance_session_key("member-atlas")
        assert profiles_mod._infer_surface(key) == "dashboard", (
            "the key the route resolves under is not this surface, so a profile bound "
            "to the dashboard is never the one consulted"
        )
        # And the bare slot is NOT this surface, which is what makes the helper load-
        # bearing rather than decorative.
        assert profiles_mod._infer_surface("member-atlas") != "dashboard"

    def test_the_route_builds_that_key_through_the_helper(self) -> None:
        """One spelling. The ``dashboard:`` prefix is inlined at about a dozen call
        sites across the product, so a surface whose key each caller assembles is one
        a caller eventually spells wrong -- this route has a single source for it."""
        import inspect

        src = inspect.getsource(md._serve_package)
        assert "governance_session_key(slot)" in src

    @pytest.mark.parametrize("surface", ["dashboard", ""])
    def test_a_profile_bound_to_any_surface_a_member_page_runs_on_restricts_it(
        self, monkeypatch, surface: str
    ) -> None:
        """EVERY SURFACE THIS PAGE CAN RUN ON, not just the one named today.

        The resolver is asked for the active scope and answers with whatever profile is
        bound -- a dashboard-bound one, or a default with no bind at all. Either way an
        open policy plus a restricting profile is a restricted agent, so the grant is
        withheld; the test is parameterised so adding a surface does not quietly leave
        one of them ungoverned.
        """
        from kiro_crew.platform import governance_profiles as profiles_mod

        bound = gov.Profile(
            name=surface or "default",
            controls={"network.egress": _ruleset(gov.MODE_ALLOW, allow=("api.github.com",))},
        )
        monkeypatch.setattr(
            gov, "last_composed_ceiling", lambda: _ceiling_with(None, governed=False)
        )
        monkeypatch.setattr(profiles_mod, "resolve_active_scope", lambda *a, **k: bound)
        key = md.governance_session_key("member-atlas")
        assert md.owner_agent_egress_unrestricted("Atlas", key) is False

    def test_the_grant_is_resolved_off_the_event_loop(self) -> None:
        """Resolving the profile makes the store check its directory's freshness -- an
        enumerate plus a stat per file, and a reload of any that moved. Read off the
        source because what is under test is WHERE it runs, which no value shows."""
        import inspect

        src = inspect.getsource(md._serve_package)
        # Matched on the call's SHAPE with whitespace collapsed, so reformatting it
        # does not read as moving it back onto the loop.
        import re as _re

        flat = " ".join(src.split())
        offloaded = _re.search(r"await _run\(\s*lambda:\s*_egress_decision\(", flat)
        assert offloaded, (
            "the grant is resolved inline, so a profile edit pauses every other "
            "session's chat and the heartbeat while the store re-reads its directory"
        )


class TestTheUngovernedDefaults:
    """Every install that configures no policy is unchanged."""

    def test_enterprise_unnamed_egress_is_the_baseline_and_gets_NO_scripts(
        self, ceiling, monkeypatch
    ) -> None:
        """In an enterprise build an unnamed ``network.egress`` is the built-in
        baseline (off), so the page stays static (RFC D3)."""
        monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")
        ceiling(None, governed=False)
        assert md.owner_agent_egress_unrestricted() is False

    def test_no_ceiling_gets_scripts(self, monkeypatch) -> None:
        monkeypatch.setattr(gov, "last_composed_ceiling", lambda: None)
        assert md.owner_agent_egress_unrestricted() is True

    def test_a_scope_the_ceiling_does_not_govern_gets_scripts(self, ceiling) -> None:
        ceiling(None, governed=False)
        assert md.owner_agent_egress_unrestricted() is True


class TestItFailsClosed:
    """A page runs only when the ceiling was READ and found to restrict nothing."""

    @pytest.mark.parametrize("pattern", ["*", "**", "evil.example", "*.internal"])
    def test_any_deny_pattern_withholds_scripts(self, ceiling, pattern: str) -> None:
        """One host denied is still a restriction: the premise is "no
        restriction", not "the agent can still reach something"."""
        ceiling(_ruleset(gov.MODE_DENY, deny=(pattern,)))
        assert md.owner_agent_egress_unrestricted() is False

    def test_an_empty_allowlist_withholds_scripts(self, ceiling) -> None:
        ceiling(_ruleset(gov.MODE_ALLOW, allow=()))
        assert md.owner_agent_egress_unrestricted() is False

    def test_an_archetype_this_handler_cannot_read_withholds_scripts(self, ceiling) -> None:
        """Something governs the scope and this code does not know its shape.
        Guessing "unrestricted" would hand out script on a ceiling nobody here
        understood."""
        ceiling(object())
        assert md.owner_agent_egress_unrestricted() is False

    def test_an_exception_withholds_scripts(self, monkeypatch) -> None:
        """The direction that cannot open an egress path."""

        def _boom() -> Any:
            raise RuntimeError("the ceiling could not be read")

        monkeypatch.setattr(gov, "last_composed_ceiling", _boom)
        assert md.owner_agent_egress_unrestricted() is False
