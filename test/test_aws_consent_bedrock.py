"""Bedrock Knowledge Base consent: the targeted GET, the one-target confirm
conflict, and the Bedrock-only compare-and-delete.

Split from ``test_aws_consent.py`` (line cap); the shared fixtures and request
helpers stay there and are imported here.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from test_aws_consent import _consent_request, _grant, _kb_source_row, _wire_kb_sources

from kiro_crew import aws_consent


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolated data home so the keystone file never touches the real one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config.loader import config_dir

    config_dir().mkdir(parents=True, exist_ok=True)
    aws_consent._probe_cache.clear()
    yield tmp_path
    aws_consent._probe_cache.clear()


class TestBedrockTargetedGet:
    """GET with an explicit bedrock-kb target must not judge an unrelated grant."""

    @pytest.mark.asyncio
    async def test_probing_a_second_target_does_not_revoke_the_first_grant(self):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )
        # Probe a DIFFERENT target that resolves to a different account.
        other = aws_consent.Identity(ok=True, account="999988887777", arn="arn:x")
        req = _consent_request(
            query={"service": "bedrock-kb", "profile": "team-b", "region": "eu-west-1"}
        )
        with patch.object(aws_consent, "probe_identity", AsyncMock(return_value=other)):
            resp = await handler.api_aws_consent_get(req)
        assert resp.status == 200
        # The stored grant survived: target B's account says nothing about A.
        kept = aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB)
        assert kept is not None and kept.account == "111122223333"

    @pytest.mark.asyncio
    async def test_get_spares_a_same_target_grant_reconfirmed_mid_probe(self):
        """Race: a SAME-target re-confirmation lands while the
        GET's probe runs. The target match alone would judge it; the
        pre-probe grant_id pin must spare it."""
        from kiro_crew.dashboard.handlers import aws_consent as handler

        _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )
        moved = aws_consent.Identity(ok=True, account="999988887777", arn="arn:x")

        async def probe_and_reconfirm(profile, region, use_cache=True):
            aws_consent.record_grant(
                aws_consent.SERVICE_BEDROCK_KB,
                profile="team-a",
                region="us-east-1",
                account="111122223333",
                arn="arn:aws:iam::111122223333:user/x",
                granted_at="2026-09-07T01:00:00+00:00",
            )
            return moved

        req = _consent_request(
            query={"service": "bedrock-kb", "profile": "team-a", "region": "us-east-1"}
        )
        with patch.object(aws_consent, "probe_identity", probe_and_reconfirm):
            resp = await handler.api_aws_consent_get(req)
        assert resp.status == 200
        survivor = aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB)
        assert survivor is not None
        assert survivor.granted_at == "2026-09-07T01:00:00+00:00"

    @pytest.mark.asyncio
    async def test_probing_the_grants_own_target_still_reconciles_drift(self):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )
        moved = aws_consent.Identity(ok=True, account="999988887777", arn="arn:x")
        req = _consent_request(
            query={"service": "bedrock-kb", "profile": "team-a", "region": "us-east-1"}
        )
        with patch.object(aws_consent, "probe_identity", AsyncMock(return_value=moved)):
            resp = await handler.api_aws_consent_get(req)
        assert resp.status == 200
        # Same target, different account underneath -> the repoint IS drift.
        assert aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB) is None


class TestBedrockConfirmConflict:
    """POST confirm for a target that differs from a registered source's is
    refused: recording it would overwrite the single per-service grant and
    silently disable that source."""

    @pytest.mark.asyncio
    async def test_confirming_a_different_target_is_refused_and_grant_survives(self):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )
        req = _consent_request(
            body={
                "service": "bedrock-kb",
                "targetProfile": "team-b",
                "targetRegion": "eu-west-1",
                "expectedProfile": "team-b",
                "expectedRegion": "eu-west-1",
                "expectedAccount": "999988887777",
            }
        )
        _wire_kb_sources(req, [_kb_source_row("team-a", "us-east-1")])
        # The check now lives INSIDE the locked write (after the probe), so
        # the probe runs — but the write must be refused and grant A intact.
        other = aws_consent.Identity(
            ok=True, account="999988887777", arn="arn:aws:iam::999988887777:user/x"
        )
        with patch.object(aws_consent, "probe_identity", AsyncMock(return_value=other)):
            resp = await handler.api_aws_consent_post(req)
        assert resp.status == 409
        import json as _json

        payload = _json.loads(resp.text)
        assert payload["code"] == "bedrock_kb_target_conflict"
        kept = aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB)
        assert kept is not None and kept.account == "111122223333"

    @pytest.mark.asyncio
    async def test_the_refusal_redacts_the_registered_sources_name(self):
        """The 409 names the source that holds the other target. Its name is
        an agent-writable row the knowledge handler's bound never saw, so a
        planted credential or exfil URL reaches the operator as the redaction
        tag, not the value; the target itself stays readable. Found in review."""
        from kiro_crew.dashboard.handlers import aws_consent as handler

        _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )
        req = _consent_request(
            body={
                "service": "bedrock-kb",
                "targetProfile": "team-b",
                "targetRegion": "eu-west-1",
                "expectedProfile": "team-b",
                "expectedRegion": "eu-west-1",
                "expectedAccount": "999988887777",
            }
        )
        token = "aB3" * 70
        planted = (
            "Team KB AKIAIOSFODNN7EXAMPLE https://collect.attacker.example/?token="
            + token
            + "&host=corp-laptop"
        )
        _wire_kb_sources(req, [_kb_source_row("team-a", "us-east-1", name=planted)])
        other = aws_consent.Identity(
            ok=True, account="999988887777", arn="arn:aws:iam::999988887777:user/x"
        )
        with patch.object(aws_consent, "probe_identity", AsyncMock(return_value=other)):
            resp = await handler.api_aws_consent_post(req)
        assert resp.status == 409
        import json as _json

        error = _json.loads(resp.text)["error"]
        assert "AKIAIOSFODNN7EXAMPLE" not in error
        assert token not in error and "corp-laptop" not in error
        assert "[REDACTED: credential]" in error
        assert "[REDACTED: suspicious URL to collect.attacker.example]" in error
        assert "(team-a, us-east-1)" in error

    @pytest.mark.asyncio
    async def test_lookup_failure_fails_closed(self):
        """A broken sources table refuses the confirmation (503) instead of
        reading as no-conflict."""
        from kiro_crew.dashboard.handlers import aws_consent as handler

        req = _consent_request(
            body={
                "service": "bedrock-kb",
                "targetProfile": "team-b",
                "targetRegion": "eu-west-1",
                "expectedProfile": "team-b",
                "expectedRegion": "eu-west-1",
                "expectedAccount": "999988887777",
            }
        )
        req.app["state"].knowledge_store.db.execute.side_effect = RuntimeError("db locked")
        other = aws_consent.Identity(
            ok=True, account="999988887777", arn="arn:aws:iam::999988887777:user/x"
        )
        with patch.object(aws_consent, "probe_identity", AsyncMock(return_value=other)):
            resp = await handler.api_aws_consent_post(req)
        assert resp.status == 503
        import json as _json

        assert _json.loads(resp.text)["code"] == "bedrock_kb_conflict_check_failed"
        assert aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB) is None

    def test_both_gates_share_the_target_change_lock(self):
        """The race fix is one lock: the consent write and the add insert
        must serialize on the SAME object."""
        import inspect

        from kiro_crew.dashboard.handlers import aws_consent as consent_handler
        from kiro_crew.dashboard.handlers import knowledge as knowledge_handler
        from kiro_crew.knowledge.connectors import bedrock_kb

        assert bedrock_kb.target_change_lock is not None
        # add_source hands its bedrock_kb insert to _insert_bedrock_source,
        # which holds the lock across the recheck and the insert.
        assert "_insert_bedrock_source" in inspect.getsource(knowledge_handler.add_source)
        for mod, fn in (
            (consent_handler, "api_aws_consent_post"),
            (knowledge_handler, "_insert_bedrock_source"),
        ):
            src = inspect.getsource(getattr(mod, fn))
            assert "target_change_lock" in src, f"{fn} does not take the shared lock"
        # And the locked insert must re-check the LIVE grant for its exact
        # target: a target-B confirmation landing after validation must not
        # leave source A registered but grantless.
        src = inspect.getsource(knowledge_handler._insert_bedrock_source)
        assert "is_granted" in src, "add_source's locked insert skips the live-grant check"
        assert "bedrock_kb_grant_changed" in src

    @pytest.mark.asyncio
    async def test_same_target_reconfirmation_passes_the_guard(self):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )
        req = _consent_request(
            body={
                "service": "bedrock-kb",
                "targetProfile": "team-a",
                "targetRegion": "us-east-1",
                "expectedProfile": "team-a",
                "expectedRegion": "us-east-1",
                "expectedAccount": "111122223333",
            }
        )
        _wire_kb_sources(req, [_kb_source_row("team-a", "us-east-1")])
        same = aws_consent.Identity(
            ok=True, account="111122223333", arn="arn:aws:iam::111122223333:user/x"
        )
        with patch.object(aws_consent, "probe_identity", AsyncMock(return_value=same)):
            resp = await handler.api_aws_consent_post(req)
        assert resp.status == 200
        kept = aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB)
        assert kept is not None and kept.account == "111122223333"

    @pytest.mark.asyncio
    async def test_first_grant_with_no_registered_sources_flows(self):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        req = _consent_request(
            body={
                "service": "bedrock-kb",
                "targetProfile": "team-a",
                "targetRegion": "us-east-1",
                "expectedProfile": "team-a",
                "expectedRegion": "us-east-1",
                "expectedAccount": "111122223333",
            }
        )
        _wire_kb_sources(req, [])
        ident = aws_consent.Identity(
            ok=True, account="111122223333", arn="arn:aws:iam::111122223333:user/x"
        )
        with patch.object(aws_consent, "probe_identity", AsyncMock(return_value=ident)):
            resp = await handler.api_aws_consent_post(req)
        assert resp.status == 200
        assert aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB) is not None

    def test_reconcile_with_probed_target_ignores_foreign_grant_atomically(self):
        """The TOCTOU shape: a grant for target B lands between a caller's
        pre-read and reconcile's own read. With the probed target passed in,
        reconcile's OWN read sees B != probed A and refuses to judge it."""
        _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-b",
            region="eu-west-1",
            account="999988887777",
        )
        identity_a = aws_consent.Identity(ok=True, account="111122223333", arn="arn:x")
        revoked = aws_consent.reconcile_drift(
            aws_consent.SERVICE_BEDROCK_KB,
            identity_a,
            probed_profile="team-a",
            probed_region="us-east-1",
        )
        assert not revoked
        kept = aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB)
        assert kept is not None and kept.account == "999988887777"

    def test_revoke_on_drift_spares_a_grant_replaced_after_the_read(self, monkeypatch):
        """The revoke-side race: reconcile reads grant A and decides to
        revoke, but grant B lands on the same store key before the delete.
        The compare-and-delete must spare B (stale evidence about A)."""
        _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )
        real_revoke_if_matches = aws_consent.revoke_if_matches

        def replace_then_revoke(service, **kwargs):
            # Simulate the concurrent POST landing between reconcile's read
            # and the delete: grant B replaces A on the same key.
            _grant(service, profile="team-b", region="eu-west-1", account="555566667777")
            return real_revoke_if_matches(service, **kwargs)

        monkeypatch.setattr(aws_consent, "revoke_if_matches", replace_then_revoke)
        moved = aws_consent.Identity(ok=True, account="999988887777", arn="arn:x")
        revoked = aws_consent.reconcile_drift(
            aws_consent.SERVICE_BEDROCK_KB,
            moved,
            probed_profile="team-a",
            probed_region="us-east-1",
        )
        assert not revoked
        survivor = aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB)
        assert survivor is not None and survivor.account == "555566667777"

    def test_revoke_on_drift_spares_a_same_target_reconfirmation(self, monkeypatch):
        """Even a replacement for the SAME target and account is a different
        grant (fresh granted_at) — the narrowed race — and must be spared."""
        _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )
        real = aws_consent.revoke_if_matches

        def reconfirm_then_revoke(service, **kwargs):
            aws_consent.record_grant(
                service,
                profile="team-a",
                region="us-east-1",
                account="111122223333",
                arn="arn:aws:iam::111122223333:user/x",
                granted_at="2026-09-06T19:30:00+00:00",  # fresh re-confirmation
            )
            return real(service, **kwargs)

        monkeypatch.setattr(aws_consent, "revoke_if_matches", reconfirm_then_revoke)
        moved = aws_consent.Identity(ok=True, account="999988887777", arn="arn:x")
        revoked = aws_consent.reconcile_drift(
            aws_consent.SERVICE_BEDROCK_KB,
            moved,
            probed_profile="team-a",
            probed_region="us-east-1",
        )
        assert not revoked
        survivor = aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB)
        assert survivor is not None and survivor.granted_at == "2026-09-06T19:30:00+00:00"

    def test_revoke_spares_a_byte_identical_same_second_reconfirmation(self):
        """granted_at is second-granular, so a re-confirmation within one
        second is byte-identical to its predecessor. grant_id (unique per
        RECORDING) still tells them apart: the old grant's identity never
        matches the replacement, so stale drift evidence cannot delete it."""
        first = _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )
        replacement = _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )  # identical content (same fixed granted_at in _grant), new grant_id
        assert first.grant_id and replacement.grant_id
        assert first.grant_id != replacement.grant_id
        revoked = aws_consent.revoke_if_matches(
            aws_consent.SERVICE_BEDROCK_KB,
            profile=first.profile,
            region=first.region,
            account=first.account,
            arn=first.arn,
            granted_at=first.granted_at,
            grant_id=first.grant_id,
        )
        assert not revoked
        assert aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB) is not None

    @pytest.mark.asyncio
    async def test_authorize_mismatch_spares_a_replacement_grant(self, monkeypatch):
        """authorize()'s account-mismatch revoke must also be compare-and-
        delete: a grant recorded while its STS probe ran is not the grant the
        mismatch evidence is about."""
        _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )
        moved = aws_consent.Identity(ok=True, account="999988887777", arn="arn:x")

        async def probe_and_reconfirm(profile, region, use_cache=True):
            # The replacement lands during the probe suspension point.
            aws_consent.record_grant(
                aws_consent.SERVICE_BEDROCK_KB,
                profile="team-b",
                region="eu-west-1",
                account="555566667777",
                arn="arn:aws:iam::555566667777:user/y",
                granted_at="2026-09-06T20:00:00+00:00",
            )
            return moved

        monkeypatch.setattr(aws_consent, "probe_identity", probe_and_reconfirm)
        ok, reason = await aws_consent.authorize(
            aws_consent.SERVICE_BEDROCK_KB, profile="team-a", region="us-east-1"
        )
        assert not ok  # the stale grant's authorization still refuses
        survivor = aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB)
        assert survivor is not None and survivor.account == "555566667777"


class TestCompareAndDeleteIsBedrockOnly:
    """bedrock-kb is the one service whose grant target comes from the request,
    so it is the one whose drift revoke is compare-and-delete on a captured
    grant. Every other service keeps the key-wise ``revoke`` and the grant
    record shape it had before bedrock-kb existed."""

    def test_only_the_bedrock_grant_carries_a_grant_id(self, home):
        polly = _grant(profile="voice", region="us-east-1", account="111122223333")
        bedrock = _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )
        assert polly.grant_id == ""
        assert bedrock.grant_id
        stored = json.loads(aws_consent.aws_consent_path().read_text(encoding="utf-8"))
        assert "grant_id" not in stored[aws_consent.SERVICE_POLLY]
        assert stored[aws_consent.SERVICE_BEDROCK_KB]["grant_id"] == bedrock.grant_id

    def test_polly_drift_revokes_key_wise(self, home, monkeypatch):
        _grant(profile="voice", region="us-east-1", account="111122223333")
        compared: list[str] = []
        monkeypatch.setattr(
            aws_consent,
            "revoke_if_matches",
            lambda service, **kwargs: compared.append(service) or False,
        )
        moved = aws_consent.Identity(ok=True, account="999988887777")
        assert aws_consent.reconcile_drift(aws_consent.SERVICE_POLLY, moved) is True
        assert compared == []
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None

    def test_drift_revokes_the_key_it_read_not_the_rows_own_service(self, home):
        """The store key decides which grant a drift revoke deletes. A Polly
        row whose own ``service`` field names Transcribe (a hand edit) must
        lose the Polly grant on drift and leave Transcribe's alone, for both
        the reconcile path and the authorize path."""
        _grant(profile="voice", region="us-east-1", account="111122223333")
        _grant(aws_consent.SERVICE_TRANSCRIBE, profile="voice", account="111122223333")
        path = aws_consent.aws_consent_path()
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored[aws_consent.SERVICE_POLLY]["service"] = aws_consent.SERVICE_TRANSCRIBE
        path.write_text(json.dumps(stored), encoding="utf-8")
        moved = aws_consent.Identity(ok=True, account="999988887777")

        assert aws_consent.reconcile_drift(aws_consent.SERVICE_POLLY, moved) is True
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None
        assert aws_consent.read_grant(aws_consent.SERVICE_TRANSCRIBE) is not None

        path.write_text(json.dumps(stored), encoding="utf-8")
        with patch.object(aws_consent, "probe_identity", AsyncMock(return_value=moved)):
            granted, _reason = asyncio.run(
                aws_consent.authorize(
                    aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
                )
            )
        assert granted is False
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None
        assert aws_consent.read_grant(aws_consent.SERVICE_TRANSCRIBE) is not None

    def test_polly_authorize_mismatch_revokes_key_wise(self, home, monkeypatch):
        _grant(profile="voice", region="us-east-1", account="111122223333")
        compared: list[str] = []
        monkeypatch.setattr(
            aws_consent,
            "revoke_if_matches",
            lambda service, **kwargs: compared.append(service) or False,
        )
        moved = aws_consent.Identity(ok=True, account="999988887777")
        with patch.object(aws_consent, "probe_identity", AsyncMock(return_value=moved)):
            granted, _reason = asyncio.run(
                aws_consent.authorize(
                    aws_consent.SERVICE_POLLY, profile="voice", region="us-east-1"
                )
            )
        assert granted is False
        assert compared == []
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None

    @pytest.mark.asyncio
    async def test_the_get_pins_the_drift_check_for_bedrock_only(self, home):
        from kiro_crew.dashboard.handlers import aws_consent as handler

        _grant(profile="voice", region="us-east-1", account="111122223333")
        _grant(
            aws_consent.SERVICE_BEDROCK_KB,
            profile="team-a",
            region="us-east-1",
            account="111122223333",
        )
        same = aws_consent.Identity(ok=True, account="111122223333", arn="arn:x")
        calls: list[tuple] = []

        def recording_reconcile(service, identity, **kwargs):
            calls.append((service, kwargs))
            return False

        with (
            patch.object(handler.aws_consent, "probe_identity", AsyncMock(return_value=same)),
            patch.object(handler.aws_consent, "reconcile_drift", recording_reconcile),
            patch.object(
                handler, "_effective_target", AsyncMock(return_value=("voice", "us-east-1"))
            ),
        ):
            await handler.api_aws_consent_get(_consent_request(query={"service": "polly"}))
        with (
            patch.object(handler.aws_consent, "probe_identity", AsyncMock(return_value=same)),
            patch.object(handler.aws_consent, "reconcile_drift", recording_reconcile),
        ):
            await handler.api_aws_consent_get(
                _consent_request(
                    query={"service": "bedrock-kb", "profile": "team-a", "region": "us-east-1"}
                )
            )
        bedrock_id = aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB).grant_id
        assert calls == [
            (aws_consent.SERVICE_POLLY, {}),
            (
                aws_consent.SERVICE_BEDROCK_KB,
                {
                    "probed_profile": "team-a",
                    "probed_region": "us-east-1",
                    "expected_grant_id": bedrock_id,
                },
            ),
        ]


# ── Step 1: the default provider is local ──
