"""AWS Control's profile surface: discovering, registering and unregistering local profiles, and
the billable bootstrap that re-checks authorization inside its lock.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from aws_control_app_helpers import (  # noqa: F401 -- fixtures are used by name
    ACCOUNT,
    _fresh_snapshot,
    _payload,
    _registered,
    _request,
)

from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod


def _denial(audit: mock.Mock, error: str) -> str:
    """The operation a patched ``_audit`` recorded ``denied`` with ``error``.

    Asserted BEFORE any field is read: a regression that stops auditing leaves
    nothing to match, and returning early with an assertion reports the event
    that went missing instead of an IndexError on an empty list.
    """
    calls = [
        call
        for call in audit.call_args_list
        if call.args[2:3] == ("denied",) and call.kwargs.get("error") == error
    ]
    assert calls, f"no denial audited with error={error!r}"
    return calls[0].args[0]


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

    def test_available_reports_a_failed_scan_rather_than_an_empty_list(self):
        # A 200 carrying no profiles is the page's authoritative "none left to
        # add", so a scan that could not run must not borrow it -- on AWS CLI v1
        # that would report every configured profile as absent.
        handlers = _registered()
        p1, p2 = self._env()
        with (
            p1,
            p2,
            mock.patch.object(routes_mod.os, "name", "posix"),
            mock.patch.object(
                routes_mod.deploy_profiles, "discover_aws_profiles", return_value=None
            ),
            mock.patch.object(routes_mod.deploy_profiles, "load_registry") as registry,
        ):
            resp = asyncio.run(
                handlers[("GET", "/profiles/available")](  # type: ignore[operator]
                    _request("GET", "/profiles/available")
                )
            )
        assert resp.status == 503
        assert _payload(resp)["code"] == "profiles_unavailable"
        # Nothing was read either: the answer does not depend on the registry.
        registry.assert_not_called()

    def test_available_on_windows_keeps_the_platform_answer(self):
        # Windows cannot enumerate profiles at all, and the page has copy naming
        # WSL for exactly that. It stays a 200 so the operator reads the remedy
        # instead of a retryable failure they cannot clear by retrying.
        handlers = _registered()
        p1, p2 = self._env()
        with (
            p1,
            p2,
            mock.patch.object(routes_mod.os, "name", "nt"),
            mock.patch.object(
                routes_mod.deploy_profiles, "discover_aws_profiles", return_value=None
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
        assert resp.status == 200
        body = _payload(resp)
        assert body["supported"] is False
        assert body["profiles"] == []

    def test_register_refuses_the_batch_when_the_scan_could_not_run(self):
        # The refusal an operator cannot act on is "not profiles on this
        # machine", when the profiles are there and only the listing failed.
        handlers = _registered()
        p1, p2 = self._env()
        with (
            p1,
            p2,
            mock.patch.object(routes_mod.os, "name", "posix"),
            mock.patch.object(
                routes_mod.deploy_profiles, "discover_aws_profiles", return_value=None
            ),
            mock.patch.object(routes_mod.deploy_profiles, "locked_registry") as locked,
            mock.patch.object(routes_mod.accounts_mod, "invalidate_cache") as invalidated,
        ):
            resp = asyncio.run(
                handlers[("POST", "/profiles/register")](  # type: ignore[operator]
                    self._post({"names": ["real"]})
                )
            )
        assert resp.status == 503
        payload = _payload(resp)
        assert payload["code"] == "profiles_unavailable"
        assert "not profiles on this machine" not in payload["error"]
        locked.assert_not_called()
        invalidated.assert_not_called()

    def test_register_does_not_ask_windows_to_retry(self):
        # 503 means "try again", which clears a failed scan and never clears a
        # platform that cannot scan. Windows gets the answer that stays true.
        handlers = _registered()
        p1, p2 = self._env()
        with (
            p1,
            p2,
            mock.patch.object(routes_mod.os, "name", "nt"),
            mock.patch.object(
                routes_mod.deploy_profiles, "discover_aws_profiles", return_value=None
            ),
            mock.patch.object(routes_mod.deploy_profiles, "locked_registry") as locked,
        ):
            resp = asyncio.run(
                handlers[("POST", "/profiles/register")](  # type: ignore[operator]
                    self._post({"names": ["real"]})
                )
            )
        assert resp.status == 501
        assert _payload(resp)["code"] == "unsupported_platform"
        locked.assert_not_called()


class TestProfileUnregister:
    """Removing a key is registry-only: it must never reach ``~/.aws`` or AWS,
    and it must withdraw the consent grants that named the removed key."""

    def _env(self):
        return (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod, "is_owner_dashboard_request", return_value=True),
        )

    def _post(self, body):
        req = _request("POST", "/profiles/unregister")
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        return req

    def _run(self, reg, body, *, grants=None, revoke_raises=None):
        """Drive the handler over an in-memory registry.

        ``grants`` maps a profile name to the services whose grant names it, which
        is what the (mocked) ``revoke_for_profile`` sweep answers. Returns
        ``(response, revoke_mock, invalidated_mock, credential_writers)``;
        ``credential_writers`` are the two mocks that must stay uncalled for the
        route to be registry-only.
        """
        handlers = _registered()
        grants = grants or {}
        trace: list[str] = []

        @contextlib.contextmanager
        def _fake_locked():
            trace.append("registry")
            yield reg

        def _fake_revoke(profile: str) -> list[str]:
            trace.append(f"revoke:{profile}")
            if revoke_raises is not None:
                raise revoke_raises
            return sorted(grants.get(profile, []))

        p1, p2 = self._env()
        with (
            p1,
            p2,
            mock.patch.object(routes_mod.deploy_profiles, "load_registry", return_value=reg),
            mock.patch.object(routes_mod.deploy_profiles, "locked_registry", _fake_locked),
            mock.patch.object(routes_mod.deploy_profiles, "create_aws_profile") as configure,
            mock.patch.object(routes_mod.deploy_profiles, "discover_aws_profiles") as discover,
            mock.patch.object(
                routes_mod.aws_consent, "revoke_for_profile", side_effect=_fake_revoke
            ) as revoke,
            mock.patch.object(routes_mod.accounts_mod, "invalidate_cache") as invalidated,
        ):
            resp = asyncio.run(
                handlers[("POST", "/profiles/unregister")](self._post(body))  # type: ignore[operator]
            )
        revoke.trace = trace  # type: ignore[attr-defined]
        return resp, revoke, invalidated, (configure, discover)

    @pytest.mark.parametrize("body", [{}, {"names": []}, {"names": "alpha"}])
    def test_rejects_an_empty_or_non_list_body(self, body):
        reg = {"version": 2, "profiles": [{"name": "alpha"}], "default": "alpha"}
        resp, _revoke, invalidated, _ = self._run(reg, body)
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_names"
        assert [p["name"] for p in reg["profiles"]] == ["alpha"]
        invalidated.assert_not_called()

    def test_rejects_a_name_that_fails_the_shared_pattern(self):
        reg = {"version": 2, "profiles": [{"name": "alpha"}], "default": "alpha"}
        resp, _revoke, invalidated, _ = self._run(reg, {"names": ["bad name; rm -rf ~"]})
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_names"
        assert [p["name"] for p in reg["profiles"]] == ["alpha"]
        invalidated.assert_not_called()

    def test_all_unknown_names_answer_404_and_change_nothing(self):
        reg = {"version": 2, "profiles": [{"name": "alpha"}], "default": "alpha"}
        resp, revoke, invalidated, _ = self._run(reg, {"names": ["ghost"]})
        assert resp.status == 404
        assert _payload(resp)["code"] == "unknown_profile"
        assert [p["name"] for p in reg["profiles"]] == ["alpha"]
        revoke.assert_not_called()
        invalidated.assert_not_called()

    def test_removes_the_entry_repicks_the_default_and_skips_absent_names(self):
        reg = {
            "version": 2,
            "profiles": [{"name": "alpha"}, {"name": "beta"}, {"name": "gamma"}],
            "default": "alpha",
        }
        resp, _revoke, invalidated, _ = self._run(reg, {"names": ["alpha", "ghost"]})
        assert resp.status == 200
        assert _payload(resp) == {"removed": 1, "skipped": 1, "consentWithdrawn": []}
        assert [p["name"] for p in reg["profiles"]] == ["beta", "gamma"]
        # The default must keep naming a registered profile: the nightly
        # backup and the deploy engine both resolve through it.
        assert reg["default"] == "beta"
        invalidated.assert_called_once()

    def test_removing_the_last_profile_empties_the_default(self):
        reg = {"version": 2, "profiles": [{"name": "alpha"}], "default": "alpha"}
        resp, *_ = self._run(reg, {"names": ["alpha"]})
        assert resp.status == 200
        assert reg["profiles"] == []
        assert reg["default"] == ""

    def test_a_default_that_survives_is_left_alone(self):
        reg = {"version": 2, "profiles": [{"name": "alpha"}, {"name": "beta"}], "default": "beta"}
        self._run(reg, {"names": ["alpha"]})
        assert reg["default"] == "beta"

    def test_withdraws_the_removed_profiles_grants_before_touching_the_registry(self):
        reg = {"version": 2, "profiles": [{"name": "alpha"}, {"name": "beta"}], "default": "beta"}
        grants = {"alpha": ["s3"], "beta": ["ce"]}
        resp, revoke, _inv, _ = self._run(reg, {"names": ["alpha", "ghost"]}, grants=grants)
        assert _payload(resp)["consentWithdrawn"] == ["s3"]
        # Only the registered name is swept -- never a caller-supplied one -- and
        # the sweep runs BEFORE the registry write, so a request that dies between
        # the two leaves a registered-but-unconsented profile, not the reverse.
        revoke.assert_called_once_with("alpha")
        assert revoke.trace == ["revoke:alpha", "registry"]  # type: ignore[attr-defined]
        assert [p["name"] for p in reg["profiles"]] == ["beta"]

    def test_a_failed_withdrawal_leaves_the_registry_untouched(self):
        reg = {"version": 2, "profiles": [{"name": "alpha"}], "default": "alpha"}
        resp, revoke, invalidated, _ = self._run(
            reg, {"names": ["alpha"]}, revoke_raises=OSError("consent store unwritable")
        )
        assert resp.status == 500
        assert _payload(resp)["code"] == "consent_unwritable"
        assert revoke.trace == ["revoke:alpha"]  # type: ignore[attr-defined]
        assert [p["name"] for p in reg["profiles"]] == ["alpha"]
        invalidated.assert_not_called()

    def test_never_reaches_a_credential_writer_or_the_aws_cli(self):
        # Structural pin: the only ``aws configure`` writer and the profile
        # discovery subprocess are both off this path, so ``~/.aws`` is
        # untouched by construction rather than by a check.
        reg = {"version": 2, "profiles": [{"name": "alpha"}], "default": "alpha"}
        with mock.patch.object(routes_mod, "run_aws", create=True) as run_aws:
            resp, _revoke, _inv, (configure, discover) = self._run(reg, {"names": ["alpha"]})
        # The unregister still completed, through the registry alone.
        assert resp.status == 200
        assert reg["profiles"] == []
        configure.assert_not_called()
        discover.assert_not_called()
        run_aws.assert_not_called()

    def test_success_is_audited_as_a_profiles_unregister_mutation(self):
        handlers = _registered()
        reg = {"version": 2, "profiles": [{"name": "alpha"}], "default": "alpha"}

        @contextlib.contextmanager
        def _fake_locked():
            yield reg

        p1, p2 = self._env()
        with (
            p1,
            p2,
            mock.patch.object(routes_mod.deploy_profiles, "load_registry", return_value=reg),
            mock.patch.object(routes_mod.deploy_profiles, "locked_registry", _fake_locked),
            mock.patch.object(routes_mod.aws_consent, "revoke_for_profile", return_value=[]),
            mock.patch.object(routes_mod.accounts_mod, "invalidate_cache"),
            mock.patch.object(routes_mod, "_audit") as audit,
        ):
            resp = asyncio.run(
                handlers[("POST", "/profiles/unregister")](  # type: ignore[operator]
                    self._post({"names": ["alpha"]})
                )
            )
        assert resp.status == 200
        outcomes = [
            (call.args[0], call.args[2])
            for call in audit.call_args_list
            if call.args[0] == "profiles_unregister"
        ]
        assert outcomes == [("profiles_unregister", "success")]


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
            mock.patch.object(routes_mod, "_audit") as audit,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/bootstrap")](  # type: ignore[operator]
                    self._confirm_request()
                )
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "account_mismatch"
        create.assert_not_called()
        # The refusal is also a permission DECISION, so it reaches SEL from the
        # point of decision: `_mutating` records that a response left with a 4xx,
        # which does not say WHY the confirmed bucket was never created.
        assert _denial(audit, "account_mismatch") == "drive_bootstrap"

    def test_the_app_disabled_mid_create_refuses_and_creates_nothing(self):
        # `_guarded` checks the app BEFORE the lock, and the wait for the lock is
        # unbounded -- so a queued confirm can outlive the app being switched off
        # and still reach the BILLABLE `create_drive`. Guard 1 of the module
        # promises a disabled app answers 403 `app_disabled` to everyone; the
        # recheck inside the lock is what makes the billable path keep it.
        handlers = _registered()
        target = (ACCOUNT, "prof-a", "us-west-2")
        # Enabled at the door, switched off by the time the lock is held.
        enabled_results = [True, False]

        def _enabled(_name):
            return enabled_results.pop(0) if enabled_results else False

        with (
            mock.patch.object(routes_mod, "is_app_enabled", side_effect=_enabled),
            mock.patch.object(routes_mod, "is_owner_dashboard_request", return_value=True),
            mock.patch.object(routes_mod, "_account_target", AsyncMock(return_value=target)),
            mock.patch.object(routes_mod, "_consent", AsyncMock(return_value=None)),
            mock.patch.object(routes_mod, "_drive_bucket", AsyncMock(return_value="")),
            # A real bucket name, so a missing gate fails on the 403 contract
            # below rather than on an unserializable mock in the 200 body.
            mock.patch.object(
                routes_mod.storage_mod, "create_drive", return_value="kirocrew-drive-abc123def456"
            ) as create,
            mock.patch.object(routes_mod, "_audit") as audit,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/bootstrap")](  # type: ignore[operator]
                    self._confirm_request()
                )
            )
        assert resp.status == 403
        assert _payload(resp)["code"] == "app_disabled"
        # The decisive assertion: no bucket was made, so nothing was billed.
        create.assert_not_called()
        assert _denial(audit, "app_disabled") == "drive_bootstrap"

    def test_an_account_that_stops_resolving_mid_create_is_audited(self):
        # The other shape the in-lock re-probe returns: not a different triple
        # but a refusal response, because the profile does not resolve to the
        # requested account at all.
        handlers = _registered()
        # The code here is deliberately NOT `account_unavailable`: `_resolve_target`
        # answers with three different codes, so a test whose fixture happens to use
        # the one the audit hard-coded would pass against the bug. `account_mismatch`
        # is the case an incident review most needs to see correctly.
        unavailable = routes_mod._conflict("profile no longer resolves", "account_mismatch")
        targets = [(ACCOUNT, "prof-a", "us-west-2"), unavailable]

        async def _target(_request_obj):
            return targets.pop(0)

        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod, "is_owner_dashboard_request", return_value=True),
            mock.patch.object(routes_mod, "_account_target", side_effect=_target),
            mock.patch.object(routes_mod, "_consent", AsyncMock(return_value=None)),
            mock.patch.object(routes_mod, "_drive_bucket", AsyncMock(return_value="")),
            mock.patch.object(routes_mod.storage_mod, "create_drive") as create,
            mock.patch.object(routes_mod, "_audit") as audit,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/bootstrap")](  # type: ignore[operator]
                    self._confirm_request()
                )
            )
        assert resp is unavailable
        create.assert_not_called()
        assert _denial(audit, "account_mismatch") == "drive_bootstrap"

    def test_the_app_disabled_during_the_consent_read_still_creates_nothing(self):
        # A gate at the TOP of the lock is already stale by the time `create_drive`
        # runs: the identity re-probe and the consent read are both awaits after it.
        # Switching the app off DURING the last of those awaits is the case only a
        # gate placed immediately before the billable call can catch.
        handlers = _registered()
        enabled = {"value": True}

        async def _consent_and_disable(*_a, **_k):
            enabled["value"] = False  # owner switches the app off mid-read
            return None

        with (
            mock.patch.object(
                routes_mod, "is_app_enabled", side_effect=lambda *_a, **_k: enabled["value"]
            ),
            mock.patch.object(routes_mod, "is_owner_dashboard_request", return_value=True),
            mock.patch.object(
                routes_mod,
                "_account_target",
                AsyncMock(return_value=(ACCOUNT, "prof-a", "us-west-2")),
            ),
            mock.patch.object(routes_mod, "_consent", side_effect=_consent_and_disable),
            mock.patch.object(routes_mod, "_drive_bucket", AsyncMock(return_value="")),
            mock.patch.object(routes_mod.storage_mod, "create_drive") as create,
            mock.patch.object(routes_mod, "_audit") as audit,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/bootstrap")](  # type: ignore[operator]
                    self._confirm_request()
                )
            )
        assert resp.status == 403
        create.assert_not_called()
        assert _denial(audit, "app_disabled") == "drive_bootstrap"

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
