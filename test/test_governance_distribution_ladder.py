"""Policy distribution: a refresh replaces one rung, not the whole ladder.

A live refresh re-runs the tier ladder boot uses instead of installing the fetched
document alone, the environment's document stays in the declaration peek chain,
composition keeps the fetch channel, the ``declared`` predicate tells a tier that said
nothing from one that published only a cadence, and two quiet polls after a boot
through the loader leave the governance generation unchanged.

One topic of the ``test_governance_distribution*.py`` family; the constants, document
builders, fake fetchers and fixtures the family shares come from
``governance_distribution_helpers.py``. Nothing here touches the network either: see
``test_governance_distribution.py``'s docstring for how the transport is faked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from governance_distribution_helpers import _hermetic_governance_globals  # noqa: F401
from governance_distribution_helpers import install_ceiling_fixture  # noqa: F401
from governance_distribution_helpers import transport_fixture  # noqa: F401
from governance_distribution_helpers import (
    _TEST_SCHEME,
    _body,
    _doc,
    _static_fetcher,
    _write_policy,
)

from kiro_crew.platform import governance
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError, current_context

# ──────────────────────────────────────────────────────────────────────────
# A refresh replaces one rung, not the whole ladder
# ──────────────────────────────────────────────────────────────────────────


class TestARefreshReComposesTheWholeLadder:
    """A live refresh must re-run the ladder, not install the fetched document alone.

    Installing the fetch by itself dropped every local restriction BELOW the central
    tier, so a host tightened at boot found that tightening silently gone at the first
    successful poll -- a ceiling that loosens itself on a timer, and the one direction
    this tier must never move on its own.
    ``apply_ceiling`` therefore routes through ``governance.compose_installed_ceiling``,
    which re-runs the SAME ``compose_tier_ladder`` boot uses, and validates the COMPOSED
    result so the floor gates judge what will actually govern.

    The subordinate here always DENIES something the fetched document does not, because
    that is the only assertion that can tell composition from replacement: an issuer
    check alone passes under both, since the central document is the authority either
    way and keeps its identity.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the class's absent tiers, moved verbatim; tracked for a de-flake pass
    def _only_the_tiers_a_test_declares(self, monkeypatch, tmp_path):
        """Pin every tier this class does not set to ABSENT.

        The last bundled document outlives a test -- any earlier
        ``load_security_policy(bundled_loader=...)`` on this worker leaves one in the
        process state, and ``compose_installed_ceiling`` reads it -- so a test declaring
        the home tier could silently compose against a stale bundled one instead. The
        home path is pinned for the ordinary reason: a real file on the host running
        the suite must not decide the answer.
        """
        governance.reset_process_state()
        monkeypatch.setattr(governance, "_policy_home_path", lambda: tmp_path / "absent-home.json")

    @staticmethod
    def _central(marker: str = "pushed", **extra: object):
        """A fetched document, as the loader's central rung carries it.

        ``signature_state`` is spelled out because the central tier is the AUTHORITY:
        ``_intersect_ceilings`` keeps the authority's provenance, so a rung built with
        ``parse_policy``'s ``UNCHECKED`` default would make the boot fold and the
        re-fold structurally unequal on that field alone -- and every quiet poll would
        read that as "a tier moved". ``parse_distributed_policy`` reports an unsigned
        document as ``UNSIGNED``, which is what a real refresh compares against.
        """
        from dataclasses import replace

        parsed = governance.parse_policy(
            _doc(marker, **extra), signature_state=governance.SIGNATURE_UNSIGNED
        )
        return replace(parsed, tier=governance.TIER_CENTRAL)

    # ── the tier BELOW: a local restriction must survive the poll ──────────

    def test_apply_ceiling_keeps_an_env_subordinates_denial(
        self, install_ceiling, monkeypatch, tmp_path
    ):
        """The regression itself, at the narrowest seam that can show it."""
        install_ceiling(governance.parse_policy(_doc("running")))
        monkeypatch.setenv(
            "KIROCREW_SECURITY_POLICY",
            str(
                _write_policy(
                    tmp_path / "local.json",
                    "local-file",
                    tools={"mode": "deny", "deny": ["danger"]},
                )
            ),
        )

        pd.apply_ceiling(self._central())

        installed = current_context().governance
        assert installed.identity_issuer == "pushed", "central is still the authority"
        assert "danger" in installed.controls["tools"].deny

    def test_apply_ceiling_keeps_a_home_subordinates_denial(
        self, install_ceiling, monkeypatch, tmp_path
    ):
        """The same contract one tier along, since the ladder is shared, not per-tier."""
        install_ceiling(governance.parse_policy(_doc("running")))
        home = _write_policy(
            tmp_path / "home.json", "home-file", tools={"mode": "deny", "deny": ["danger"]}
        )
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)

        pd.apply_ceiling(self._central())

        installed = current_context().governance
        assert installed.identity_issuer == "pushed"
        assert "danger" in installed.controls["tools"].deny

    def test_a_local_boot_restriction_survives_a_refresh_too(
        self, install_ceiling, monkeypatch, tmp_path
    ):
        """Composition is not only about scope controls.

        Both documents state ``allow_terminal`` explicitly, so the answer is the
        intersection rather than a default: the fetched document permits a terminal and
        the local one does not, and the local answer has to win.
        """
        install_ceiling(governance.parse_policy(_doc("running")))
        home = _write_policy(
            tmp_path / "home.json", "home-file", boot={"fail_closed": True, "allow_terminal": False}
        )
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)

        pd.apply_ceiling(self._central(boot={"fail_closed": True, "allow_terminal": True}))

        assert current_context().governance.boot.allow_terminal is False

    def test_an_env_subordinates_denial_survives_a_live_refresh(
        self, transport, install_ceiling, monkeypatch, tmp_path
    ):
        """End to end through ``refresh_now``, which is where a real host loses it.

        The boot-time tightening is not re-read by the poller from anywhere else, so if
        the install path does not recompose, the first successful poll is the moment the
        restriction disappears.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"))))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd._record_installed(_body("running", distribution={"source": source}))
        monkeypatch.setenv(
            "KIROCREW_SECURITY_POLICY",
            str(
                _write_policy(
                    tmp_path / "local.json",
                    "local-file",
                    tools={"mode": "deny", "deny": ["danger"]},
                )
            ),
        )

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_APPLIED
        installed = current_context().governance
        assert installed.identity_issuer == "pushed"
        assert "danger" in installed.controls["tools"].deny

    def test_a_home_subordinates_denial_survives_a_live_refresh(
        self, transport, install_ceiling, monkeypatch, tmp_path
    ):
        source = transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"))))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        pd._record_installed(_body("running", distribution={"source": source}))
        home = _write_policy(
            tmp_path / "home.json", "home-file", tools={"mode": "deny", "deny": ["danger"]}
        )
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_APPLIED
        installed = current_context().governance
        assert installed.identity_issuer == "pushed"
        assert "danger" in installed.controls["tools"].deny

    # ── a rung BELOW moves while the endpoint is quiet ─────────────────────

    def test_a_local_tightening_reaches_a_poll_the_endpoint_answered_304(
        self, transport, install_ceiling, monkeypatch, tmp_path
    ):
        """The local tiers are authored on THEIR own schedule, not the endpoint's.

        A 304 proves only that the central document is the one installed. A tightening
        a local document landed between two central publishes must not wait for the
        endpoint to publish something before it governs here: the running ceiling would
        stay above its own floor for as long as the endpoint stayed quiet. The 304 path
        therefore re-folds the ladder and adopts the fold when it differs from what is
        installed.
        """
        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        body = _body("running", distribution={"source": source})
        # Boot: no home file yet; the central document governs alone.
        home = tmp_path / "home.json"
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        install_ceiling(self._central("running", distribution={"source": source}))
        pd.write_cache(body, source=source, etag="v1")
        pd._record_installed(body)
        # Then the operator lands a tightening, and the endpoint has nothing newer.
        _write_policy(home, "home-file", tools={"mode": "deny", "deny": ["danger"]})

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_UNCHANGED, "the DOCUMENT is unchanged"
        installed = current_context().governance
        assert installed.tier == governance.TIER_CENTRAL, "but the ladder was re-run"
        assert "danger" in installed.controls["tools"].deny

    def test_a_304_with_no_tier_moved_does_not_reinstall(
        self, transport, install_ceiling, monkeypatch, tmp_path
    ):
        """The control: a genuinely unchanged poll leaves the installed object alone.

        Identity, not equality -- a re-install bumps the governance generation and
        invalidates every cache keyed on it, so "nothing moved" has to mean no install.
        """
        from kiro_crew.platform.context import governance_generation

        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        body = _body("running", distribution={"source": source})
        home = _write_policy(
            tmp_path / "home.json", "home-file", tools={"mode": "deny", "deny": ["danger"]}
        )
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        # Boot composed central-over-home; install exactly that fold.
        running = install_ceiling(
            governance.compose_installed_ceiling(
                self._central("running", distribution={"source": source})
            )
        )
        pd.write_cache(body, source=source, etag="v1")
        pd._record_installed(body)
        generation = governance_generation()

        outcome = pd.refresh_now()

        assert outcome.status == pd.REFRESH_UNCHANGED
        assert current_context().governance is running
        assert governance_generation() == generation

    def test_two_quiet_polls_do_not_churn_the_generation_when_boot_retagged_the_object(
        self, transport, install_ceiling, monkeypatch, tmp_path
    ):
        """The comparison basis is the loader's fold, not the context object.

        Boot installs whatever ``bootstrap`` / the companion put into the context after
        ``load_security_policy`` returned. Any ``replace()`` on that path -- here, a
        re-tag of ``identity_signature`` -- leaves an object that is the same ceiling but
        not structurally equal to the fold. Compared against the CONTEXT, every 304 would
        re-install and bump the generation once per interval. Compared against the
        recorded fold, two consecutive quiet polls change nothing.
        """
        from dataclasses import replace as _replace

        from kiro_crew.platform.context import governance_generation

        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        body = _body("running", distribution={"source": source})
        home = _write_policy(
            tmp_path / "home.json", "home-file", tools={"mode": "deny", "deny": ["danger"]}
        )
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        fold = governance.compose_installed_ceiling(
            self._central("running", distribution={"source": source})
        )
        # Boot's loader records the fold it returned (this is what load_security_policy
        # does at its return); the boot path then installs a re-tagged twin of it: same
        # ceiling, not ``==``.
        governance.record_composed_ceiling(fold)
        retagged = _replace(fold, identity_signature="tagged-by-boot")
        assert retagged != fold
        running = install_ceiling(retagged)
        pd.write_cache(body, source=source, etag="v1")
        pd._record_installed(body)
        generation = governance_generation()

        first = pd.refresh_now()
        second = pd.refresh_now()

        assert first.status == pd.REFRESH_UNCHANGED
        assert second.status == pd.REFRESH_UNCHANGED
        assert current_context().governance is running, "no re-install on a quiet poll"
        assert governance_generation() == generation

    def test_a_tier_moving_still_reaches_a_poll_when_boot_retagged_the_object(
        self, transport, install_ceiling, monkeypatch, tmp_path
    ):
        """The positive control for the test above: a REAL change still binds.

        Comparing against the fold rather than the context must not turn the re-fold
        into a no-op -- a local tightening landing after boot still differs from the
        recorded fold, so it is installed exactly once.
        """
        from dataclasses import replace as _replace

        from kiro_crew.platform.context import governance_generation

        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        body = _body("running", distribution={"source": source})
        home = tmp_path / "home.json"
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        fold = governance.compose_installed_ceiling(
            self._central("running", distribution={"source": source})
        )
        governance.record_composed_ceiling(fold)
        install_ceiling(_replace(fold, identity_signature="tagged-by-boot"))
        pd.write_cache(body, source=source, etag="v1")
        pd._record_installed(body)
        generation = governance_generation()
        # Then the operator lands a tightening.
        _write_policy(home, "home-file", tools={"mode": "deny", "deny": ["danger"]})

        first = pd.refresh_now()
        after_first = governance_generation()
        second = pd.refresh_now()

        assert first.status == pd.REFRESH_UNCHANGED and second.status == pd.REFRESH_UNCHANGED
        assert after_first == generation + 1, "the tightening was installed once"
        assert governance_generation() == after_first, "and the next quiet poll did nothing"
        assert "danger" in current_context().governance.controls["tools"].deny

    def test_a_rejected_refold_is_retried_on_the_next_poll(
        self, transport, install_ceiling, monkeypatch, tmp_path
    ):
        """A fold the floor gates refused must not become the comparison basis.

        304, a local tightening lands, the profile floor rejects the fold: nothing is
        installed and the poll reports REJECTED. If that rejected fold were recorded as
        "last composed", the next poll would fold the same thing, compare equal, and
        skip -- the looser ceiling would stay in effect after the operator fixed the
        profile, until a restart. The basis is what LANDED, so the retry installs.
        """
        from kiro_crew.platform.context import governance_generation

        source = transport(_static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True)))
        body = _body("running", distribution={"source": source})
        home = tmp_path / "home.json"
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        boot_fold = governance.compose_installed_ceiling(
            self._central("running", distribution={"source": source})
        )
        governance.record_composed_ceiling(boot_fold)
        install_ceiling(boot_fold)
        pd.write_cache(body, source=source, etag="v1")
        pd._record_installed(body)
        generation = governance_generation()
        _write_policy(home, "home-file", tools={"mode": "deny", "deny": ["danger"]})
        # The profile floor refuses the fold -- once.
        real_validate = pd.validate_ceiling
        refusals = {"left": 1}

        def refuse_once(ceiling):
            if refusals["left"]:
                refusals["left"] -= 1
                raise PlatformCompositionError("profile floor: simulated refusal")
            real_validate(ceiling)

        monkeypatch.setattr(pd, "validate_ceiling", refuse_once)

        first = pd.refresh_now()
        assert first.status == pd.REFRESH_REJECTED
        assert governance_generation() == generation, "nothing installed on the refusal"
        assert "tools" not in current_context().governance.controls, "still the boot fold"

        second = pd.refresh_now()  # the operator fixed the profile; the poll retries
        assert second.status == pd.REFRESH_UNCHANGED
        assert governance_generation() == generation + 1, "the tightening installed"
        assert "danger" in current_context().governance.controls["tools"].deny

    # ── and the unconfigured host pays nothing ─────────────────────────────

    def test_compose_installed_ceiling_returns_the_central_document_untouched_when_alone(self):
        """No other tier means no composition -- not a rebuilt equal-looking ceiling.

        Identity rather than equality, because "unchanged" is the promise made to every
        standalone host: the fetched document IS the installed one.
        """
        central = self._central()
        assert governance.compose_installed_ceiling(central) is central

    def test_apply_ceiling_installs_the_central_document_unchanged_when_alone(
        self, install_ceiling
    ):
        install_ceiling(governance.parse_policy(_doc("running")))
        central = self._central()
        pd.apply_ceiling(central)
        assert current_context().governance is central


class TestTheEnvDocumentIsInTheDeclarationPeekChain:
    """The peek must follow the same order the tiers do: env -> bundled -> home.

    The env document was missing from that chain, so an env-declared
    ``distribution.source`` was silently ignored and the central tier never loaded
    from it -- a fleet that bootstrapped through ``KIROCREW_SECURITY_POLICY`` got no
    central ceiling at all, with nothing anywhere reporting why.

    A tier naming a source is the operator choosing where their own ceiling comes
    from, which is theirs to choose.
    """

    def _source(self, host: str) -> str:
        """A URL on the fake scheme, distinct per *host* so the fetch can be named."""
        return f"{_TEST_SCHEME}://{host}.example/security_policy.json"

    def _fetcher(self, marker: str, seen: list):
        return _static_fetcher(pd.FetchedPolicy(body=_body(marker)), seen)

    def test_an_env_declared_source_drives_the_central_fetch(
        self, monkeypatch, tmp_path, transport
    ):
        seen: list = []
        transport(self._fetcher("central-push", seen))
        source = self._source("env-declared")
        monkeypatch.setenv(
            "KIROCREW_SECURITY_POLICY",
            str(_write_policy(tmp_path / "env.json", "bootstrap", distribution={"source": source})),
        )
        ceiling = governance.load_security_policy()
        assert ceiling is not None
        # Fetched from the address the env document named ...
        assert [request.url for request in seen] == [source]
        # ... and the fetched document is the authority, with the env tier beneath it.
        assert ceiling.identity_issuer == "central-push"
        assert ceiling.tier == governance.TIER_CENTRAL

    def test_the_env_declaration_outranks_a_bundled_and_a_home_one(
        self, monkeypatch, tmp_path, transport
    ):
        seen: list = []
        transport(self._fetcher("central-push", seen))
        env_source = self._source("from-env")
        home = _write_policy(
            tmp_path / "home.json", "home-decl", distribution={"source": self._source("from-home")}
        )
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        monkeypatch.setenv(
            "KIROCREW_SECURITY_POLICY",
            str(
                _write_policy(
                    tmp_path / "env.json", "env-decl", distribution={"source": env_source}
                )
            ),
        )
        ceiling = governance.load_security_policy(
            bundled_loader=lambda: _doc(
                "bundled-decl", distribution={"source": self._source("from-bundled")}
            )
        )
        assert ceiling is not None
        assert [request.url for request in seen] == [env_source]

    def test_the_env_tier_itself_still_outranks_bundled_and_home(self, monkeypatch, tmp_path):
        # Hoisting the read must not have changed WHERE the tier sits. No source is
        # declared anywhere, so the central tier stays inert and the subordinate is
        # decided by first-present-wins.
        home = _write_policy(tmp_path / "home.json", "operator")
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        monkeypatch.setenv(
            "KIROCREW_SECURITY_POLICY", str(_write_policy(tmp_path / "env.json", "local-env"))
        )
        ceiling = governance.load_security_policy(bundled_loader=lambda: _doc("companion"))
        assert ceiling is not None
        assert ceiling.tier == governance.TIER_ENV
        assert ceiling.identity_issuer == "local-env"

    def test_the_env_document_is_read_exactly_once_per_load(self, monkeypatch, tmp_path):
        """One read, so the peek and the tier cannot see different bytes.

        Two reads is not merely wasteful: a document rewritten between them would let
        the ``distribution`` block that chose the central source come from one version
        while the ceiling installed as the env tier came from another. Counted at
        ``_read_json_file`` rather than by patching a caller, so a future second read
        added anywhere in the chain trips this.
        """
        env_path = _write_policy(tmp_path / "env.json", "local-env", distribution={"source": ""})
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(env_path))
        real_read = governance._read_json_file
        reads: list = []

        def counting(path):  # type: ignore[no-untyped-def]
            reads.append(Path(path))
            return real_read(path)

        monkeypatch.setattr(governance, "_read_json_file", counting)
        ceiling = governance.load_security_policy()
        assert ceiling is not None
        assert ceiling.tier == governance.TIER_ENV
        assert [p for p in reads if p == env_path] == [env_path]

    def test_subordinate_ceiling_with_its_original_four_arguments_still_reads_the_env_tier(
        self, monkeypatch, tmp_path
    ):
        """``env_data``/``env_path`` are an optimisation, not an opt-in.

        Every other caller passes the original four positional arguments, so the
        function has to keep self-reading: had the parameters been made required in
        spirit -- read only when handed over -- those callers would have silently lost
        the env tier, which is a WIDENING (the tier that was governing disappears).
        """
        monkeypatch.setenv(
            "KIROCREW_SECURITY_POLICY", str(_write_policy(tmp_path / "env.json", "local-env"))
        )
        ceiling = governance._subordinate_ceiling(None, None, None, tmp_path / "absent-home.json")
        assert ceiling is not None
        assert ceiling.tier == governance.TIER_ENV
        assert ceiling.identity_issuer == "local-env"

    def test_the_four_argument_form_still_prefers_the_env_document_over_bundled(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv(
            "KIROCREW_SECURITY_POLICY", str(_write_policy(tmp_path / "env.json", "local-env"))
        )
        ceiling = governance._subordinate_ceiling(
            _doc("companion"), None, None, tmp_path / "absent-home.json"
        )
        assert ceiling is not None
        assert ceiling.identity_issuer == "local-env"

    def test_an_unreadable_env_path_still_raises_naming_the_variable(self, monkeypatch, tmp_path):
        """The read moved earlier; its disposition did not.

        The env tier outranks bundled and home, so there is no lower tier whose own
        failure could take precedence -- a fleet that pointed this variable at a file
        it cannot read is misconfigured, and the message has to name the variable or
        the operator has no way to find which of several governance files is meant.
        """
        broken = tmp_path / "env.json"
        broken.write_text("{ not json", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(broken))
        with pytest.raises(PlatformCompositionError, match="from KIROCREW_SECURITY_POLICY"):
            governance.load_security_policy()

    def test_an_unreadable_env_path_raises_before_the_central_tier_answers(
        self, monkeypatch, tmp_path, transport
    ):
        # Hoisting the read moved the refusal EARLIER than the central fetch, which is
        # the one behavioural difference: a host misconfigured this way must not have
        # its boot decided by whether a poll happened to succeed.
        seen: list = []
        transport(self._fetcher("central-push", seen))
        home = _write_policy(
            tmp_path / "home.json", "home-decl", distribution={"source": self._source("from-home")}
        )
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        broken = tmp_path / "env.json"
        broken.write_text("{ not json", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(broken))
        with pytest.raises(PlatformCompositionError, match="from KIROCREW_SECURITY_POLICY"):
            governance.load_security_policy()
        assert seen == []

    def test_an_unreadable_env_path_still_raises_at_the_reader(self, monkeypatch, tmp_path):
        broken = tmp_path / "env.json"
        broken.write_text("{ not json", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(broken))
        with pytest.raises(PlatformCompositionError, match="from KIROCREW_SECURITY_POLICY"):
            governance._read_env_policy()

    def test_an_unset_variable_reads_as_no_env_document(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
        assert governance._read_env_policy() == (None, None)


class TestCompositionPreservesTheFetchChannel:
    """The distribution pins must survive EVERY path that rebuilds a ceiling.

    They ride outside ``controls``, so the per-scope compose the fold uses does not
    carry them and each rebuild has to do it explicitly.  One path did not: an
    authority that declared no pins discarded the pins of the tier beneath it.  That
    switches the central tier off, which drops every centrally supplied restriction -- the
    looser-ceiling failure reached through the ladder that exists to prevent it.

    Asserted on ``compose_tier_ladder`` directly because it is the single
    implementation of precedence that both boot and a live refresh call.
    """

    _SOURCE = "https://fleet.example/policy.json"
    _OTHER = "https://local.example/other.json"

    @staticmethod
    def _tier(tier: str, marker: str, **extra: object) -> object:
        return governance.replace(governance.parse_policy(_doc(marker, **extra)), tier=tier)

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the class's accepting SEL, moved verbatim; tracked for a de-flake pass
    def _working_sel(self, monkeypatch):
        """A SEL that accepts every write, so a tier audit cannot refuse.

        The critical audit on the override path is fail-closed by design; stubbing it
        keeps these tests about the pins rather than about SEL availability.
        """

        class Stub:
            def log_api_access(self, **kw):
                return None

        monkeypatch.setattr(governance, "sel", lambda: Stub())

    def test_an_authority_with_no_pins_keeps_the_pins_beneath_it(self):
        """A fetched document may govern controls and say nothing about distribution.

        Keeping "the authority's" pins in that shape discarded the pins of the tier
        beneath it rather than preserving a choice, because there was no choice to
        preserve: the host silently lost the channel it fetches its ceiling from.
        """
        authority = self._tier(governance.TIER_CENTRAL, "authority")
        lower = self._tier(
            governance.TIER_HOME,
            "bootstrap",
            distribution={"source": self._SOURCE, "refresh_interval_secs": 3600},
        )
        assert not authority.distribution.declared

        composed = governance.compose_tier_ladder(authority, lower)

        assert composed is not None
        assert composed.tier == governance.TIER_CENTRAL, "the authority still governs"
        assert composed.distribution.source == self._SOURCE
        assert composed.distribution.refresh_interval_secs == 3600

    def test_a_lower_tier_cannot_redirect_a_source_its_authority_chose(self):
        """The security property the pins exist for, unchanged by the fallback.

        A tier may only supply pins when NOTHING above it declared any; it may never
        replace pins that are already there.
        """
        authority = self._tier(
            governance.TIER_CENTRAL, "authority", distribution={"source": self._SOURCE}
        )
        lower = self._tier(governance.TIER_ENV, "lower", distribution={"source": self._OTHER})

        composed = governance.compose_tier_ladder(authority, lower)

        assert composed is not None
        assert composed.distribution.source == self._SOURCE, "the authority's document wins"

    def test_a_single_tier_composes_byte_identically(self):
        """The re-apply must not perturb the one-document case."""
        only = self._tier(governance.TIER_HOME, "only", distribution={"source": self._SOURCE})

        composed = governance.compose_tier_ladder(only)

        assert composed == only


class TestTheDeclaredPredicate:
    """``declared`` asks whether a tier expressed distribution AT ALL.

    Separate from ``enabled``, which asks only whether a source is set: a document may
    publish the cadence and leave the address to whatever provisions the host, and the
    ladder needs to tell "said nothing" from "said no source".
    """

    def test_a_default_value_declared_nothing(self):
        assert governance.PolicyDistribution().declared is False

    def test_a_source_alone_counts_as_declared(self):
        dist = governance.PolicyDistribution(source="https://fleet.example/p.json")
        assert dist.declared is True
        assert dist.enabled is True

    def test_a_cadence_without_a_source_still_counts_as_declared(self):
        """The two-channel split: this is the case ``enabled`` cannot see."""
        dist = governance.PolicyDistribution(refresh_interval_secs=900)
        assert dist.declared is True
        assert dist.enabled is False


class TestABootThroughTheLoaderThenTwoQuietPolls:
    def test_the_generation_is_unchanged(self, transport, install_ceiling, monkeypatch, tmp_path):
        """The existing control installs via ``compose_installed_ceiling`` itself. This
        one boots through ``load_security_policy`` -- the path a real gateway takes --
        so the comparison basis is the loader's own fold."""
        from kiro_crew.platform.context import governance_generation

        body = _body("running", tools={"mode": "deny", "deny": ["danger"]})
        source = transport(_static_fetcher(pd.FetchedPolicy(body=body, etag="v1")))
        home = _write_policy(tmp_path / "home.json", "bootstrap", distribution={"source": source})
        monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
        monkeypatch.setattr(governance, "_policy_signature_required", lambda: False)

        booted = governance.load_security_policy()
        assert booted is not None
        install_ceiling(booted)
        generation = governance_generation()

        # The endpoint now answers 304 forever.
        quiet = _static_fetcher(pd.FetchedPolicy(etag="v1", not_modified=True))
        monkeypatch.setattr(pd, "_FETCHERS", {**pd._FETCHERS, _TEST_SCHEME: quiet})
        first = pd.refresh_now()
        second = pd.refresh_now()

        assert first.status == pd.REFRESH_UNCHANGED, first
        assert second.status == pd.REFRESH_UNCHANGED, second
        assert governance_generation() == generation
