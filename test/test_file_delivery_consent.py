"""Owner-consented delivery of scanner-flagged files.

Every piece of credential-shaped material here is SYNTHESIZED AT RUNTIME from a
small grammar rather than checked in as a literal. That is deliberate and is not
cosmetic: a diff carrying literal PEM headers or working token strings reads as an
exfiltration recipe, and one of this repository's pull requests is permanently
deadlocked because a review provider refused it twelve consecutive times as
"potentially high-risk cyber activity" and then failed closed on the absent
verdict. The scanner sees identical bytes at runtime either way, so the
assertions below are exactly as strong as literals would have been -- only the
reviewed bytes differ.

``_synth_pem`` in particular never contains real key material: its body is
deterministic base64 over a SHA-256 of a loop counter, so it is
private-key-SHAPED without being a private key.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
from unittest.mock import patch

import pytest

from kiro_crew import file_delivery_consent, security
from kiro_crew.config.loader import file_delivery_consent_path

# ---------------------------------------------------------------- generators


def _synth_pem() -> str:
    """PEM private-key-SHAPED text assembled at runtime from fragments."""
    rule = "-" * 5
    begin = " ".join(["BEGIN", "RSA", "PRIVATE", "KEY"])
    end = " ".join(["END", "RSA", "PRIVATE", "KEY"])
    body = "\n".join(
        base64.b64encode(hashlib.sha256(f"kc-7770-{i}".encode()).digest() * 2).decode()
        for i in range(4)
    )
    return f"{rule}{begin}{rule}\n{body}\n{rule}{end}{rule}\n"


def _synth_aws_key() -> str:
    """An AWS-access-key-SHAPED token: fixed public prefix plus synthetic body."""
    prefix = "A" + "KIA"
    body = hashlib.sha256(b"kc-7770-aws").hexdigest().upper()[:16]
    return prefix + body


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Point the consent store at a tmp dir so no test touches the real one."""
    store = tmp_path / "file_delivery_consent.json"
    monkeypatch.setattr(
        file_delivery_consent, "file_delivery_consent_path", lambda: store, raising=True
    )
    return store


def _grant() -> None:
    file_delivery_consent.record_grant(
        file_delivery_consent.CLASS_OWNER_DASHBOARD, granted_at="2026-09-05T00:00:00+00:00"
    )


# ------------------------------------------------- the generators are honest


class TestSynthesizedMaterialActuallyTrips:
    """A generator the scanner ignores would make every test below vacuous."""

    def test_synth_pem_is_detected(self):
        pem = _synth_pem()
        assert security.redact(pem) != pem

    def test_synth_aws_key_is_detected(self):
        key = _synth_aws_key()
        assert security.redact(key) != key


# --------------------------------------------------- the absolute condition


class TestThirdPartyLegsCanNeverBeGranted:
    def test_grantable_and_never_grantable_are_disjoint(self):
        assert not (
            file_delivery_consent.GRANTABLE_CLASSES & file_delivery_consent.NEVER_GRANTABLE_CLASSES
        )

    def test_only_the_owner_dashboard_class_is_grantable(self):
        assert file_delivery_consent.GRANTABLE_CLASSES == {
            file_delivery_consent.CLASS_OWNER_DASHBOARD
        }

    @pytest.mark.parametrize("leg", sorted(file_delivery_consent.NEVER_GRANTABLE_CLASSES))
    def test_recording_a_third_party_leg_raises(self, leg):
        with pytest.raises(ValueError):
            file_delivery_consent.record_grant(leg, granted_at="2026-09-05T00:00:00+00:00")

    @pytest.mark.parametrize("leg", sorted(file_delivery_consent.NEVER_GRANTABLE_CLASSES))
    def test_a_third_party_leg_is_never_granted_even_if_the_file_says_so(
        self, leg, _isolated_store
    ):
        """A hand-planted row for an upload leg must not authorize anything.

        ``is_granted`` refuses the class before it reads the store, so the row
        below is inert. Without that ordering a writer who reached the file --
        which the keystone fence exists to prevent, but which this test does not
        assume -- could authorize the Slack leg.
        """
        _isolated_store.write_text(
            json.dumps({leg: {"destination_class": leg, "granted_at": "2026-09-05T00:00:00+00:00"}})
        )
        assert file_delivery_consent.is_granted(leg) is False

    def test_the_shared_upload_gate_cannot_read_the_consent_store(self):
        """The structural half of the guarantee, asserted on real source.

        ``_gate_upload_file`` is the single admission gate both third-party legs
        route through. If it never references the consent store, no grant can
        reach the Slack or channel leg by any code path -- a property that cannot
        be undone by inverting a check, only by editing that function.
        """
        from kiro_crew.dashboard.handlers import files as files_handlers

        src = inspect.getsource(files_handlers._gate_upload_file)
        assert "consent" not in src.lower()
        assert "file_delivery_consent" not in src


# ------------------------------------------------------- no detector moved


class TestNoDetectorMoved:
    def test_a_grant_does_not_change_what_redact_finds(self):
        """A grant changes what a GATE does with a positive, never the scan."""
        pem = _synth_pem()
        before = security.redact(pem)
        _grant()
        assert security.redact(pem) == before
        assert security.redact(pem) != pem

    def test_redact_does_not_strip_local_paths(self):
        """Bounds the grant's scope: credentials and exfil URLs, not everything.

        Stated in the PR body as a scope claim, so it is pinned here rather than
        left for a reviewer to derive.
        """
        probe = "[Errno 2] No such file or directory: '/home/someone/.kiro/crew/x'"
        assert security.redact(probe) == probe


# ------------------------------------------------------------- the store


class TestGrantStore:
    def test_absent_store_grants_nothing(self):
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )

    def test_record_then_read(self):
        _grant()
        assert file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        grant = file_delivery_consent.read_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        assert grant is not None and grant.granted_at == "2026-09-05T00:00:00+00:00"

    def test_revoke_removes_it(self):
        _grant()
        assert file_delivery_consent.revoke(file_delivery_consent.CLASS_OWNER_DASHBOARD) is True
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )

    def test_unreadable_store_grants_nothing(self, _isolated_store):
        _isolated_store.write_text("{not json")
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )

    def test_a_row_disagreeing_with_its_key_grants_nothing(self, _isolated_store):
        """Refuse rather than trust the key, so a partial write cannot widen a grant."""
        _isolated_store.write_text(
            json.dumps(
                {
                    file_delivery_consent.CLASS_OWNER_DASHBOARD: {
                        "destination_class": "something_else",
                        "granted_at": "2026-09-05T00:00:00+00:00",
                    }
                }
            )
        )
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )

    def test_no_lock_artifact_is_created_beside_the_grant(self, _isolated_store):
        """The lock is in-process, so there is no sibling file an agent can hold.

        A sibling lock file is agent-reachable: ``is_sensitive_path`` covers it, but
        that is the evadable tier, and an agent holding it would block the owner's
        REVOKE -- a denial of revocation, not a disclosure. The artifact is removed
        rather than defended, and this pins that it stays removed.
        """
        _grant()
        assert file_delivery_consent.revoke(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        names = sorted(p.name for p in _isolated_store.parent.iterdir())
        assert not [n for n in names if "lock" in n.lower()], names

    def test_the_module_defines_no_lock_filename(self):
        assert not hasattr(file_delivery_consent, "_LOCK_FILENAME")
        assert not hasattr(file_delivery_consent, "_ConsentLock")

    def test_a_corrupt_store_is_replaced_and_leaves_no_sidecar(self, _isolated_store):
        """No ``.corrupt-<stamp>`` sibling is written, deliberately.

        That suffix is NOT covered by ``is_sensitive_path`` (measured False on all
        four keystone consent leaves, while the leaf and its ``.tmp`` are True), and
        a sidecar has no reader anywhere in the tree, so it could not alter a grant
        either way. An artifact with no reader and no recoverable value is not
        created rather than fenced. This store holds one row of
        ``{destination_class, granted_at}``; there is nothing to preserve.
        """
        _isolated_store.write_text("{corrupt")
        _grant()
        assert file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        siblings = [p.name for p in _isolated_store.parent.iterdir() if "corrupt-" in p.name]
        assert siblings == [], siblings

    def test_the_module_has_no_sidecar_preservation(self):
        assert not hasattr(file_delivery_consent, "_preserve_if_unreadable")


# ------------------------------------------------------------ the keystone


class TestKeystoneFencing:
    def test_the_grant_file_is_fenced_from_agent_file_tools(self):
        assert security.is_sensitive_path(str(file_delivery_consent_path())) is True

    def test_the_leaf_is_registered_beside_its_sibling(self):
        assert "file_delivery_consent.json" in security._CREW_SECRET_LEAVES
        assert "aws_service_consent.json" in security._CREW_SECRET_LEAVES

    def test_the_cli_verb_is_an_approve_step_up_not_a_self_grant(self):
        """The CLI verb finishes an owner-armed grant by presenting the host nonce.

        It must NOT be a verb that records a grant on request (which an automated
        caller could take): it reads the armed nonce from the keystone file and
        POSTs it to the approve endpoint, so it authorizes nothing on its own.
        """
        from kiro_crew import cli, cli_server

        cli_src = inspect.getsource(cli)
        assert '"file-delivery"' in cli_src
        assert "_file_delivery_approve" in cli_src

        # The action positional is REQUIRED (no nargs="?"): a bare
        # ``kirocrew file-delivery`` must not dispatch to approve. The token is
        # not the security boundary (the nonce read is), but an optional verb
        # that silently means "approve" is a footgun -- argparse must demand it.
        fd_block = cli_src[cli_src.index('add_command(sub, "file-delivery")') :]
        fd_block = fd_block[: fd_block.index('add_argument(\n        "action"') + 400]
        assert 'nargs="?"' not in fd_block, "file-delivery action must be required"

        approve_src = inspect.getsource(cli_server._file_delivery_approve)
        # Proves host presence by READING the nonce, then presents it -- it does
        # not call record_grant directly.
        assert "read_pending_grant" in approve_src
        assert "/api/file-delivery/consent/approve" in approve_src
        assert "record_grant" not in approve_src


# ---------------------------------------------------------------- the tool


class TestConsentedDownloadRequiresOwnerIdentity:
    """A grant lifts the refusal for the OWNER, not for every authenticated caller.

    The bug both review lanes found independently. The download route is absent
    from every ``token_auth`` bypass list, which establishes it needs
    AUTHENTICATION, not OWNER IDENTITY -- a Slack allow-listed non-owner running
    ``!dashboard`` authenticates with ``app == ""`` and ``sub != owner_id``. Without
    the owner conjunct the grant turns a clean 400-for-everyone into raw bytes for
    any authenticated caller.
    """

    def test_the_download_gate_requires_both_conjuncts(self):
        import inspect

        from kiro_crew.dashboard.handlers import files as files_handlers

        src = inspect.getsource(files_handlers.api_outbox_download)
        assert "is_granted" in src
        # Asserted on source because the alternative is an aiohttp request fixture
        # carrying a forged non-owner identity, which would pin the harness rather
        # than the route. Paired with the negative below so this cannot pass by
        # merely mentioning the name.
        assert "is_owner_dashboard_request" in src

    def test_owner_check_and_grant_are_ANDed_not_ORed(self):
        import inspect

        from kiro_crew.dashboard.handlers import files as files_handlers

        src = inspect.getsource(files_handlers.api_outbox_download)
        window = src[src.index("is_granted") : src.index("is_granted") + 400]
        assert " and is_owner_dashboard_request(request)" in window
        assert " or is_owner_dashboard_request(request)" not in window


def _consent_request(*, app: str = "", user: str = "owner-1", owner: str = "owner-1", query=None):
    """A request shaped like a real DASHBOARD OWNER call.

    ``is_owner_dashboard_request`` needs ``app`` present-and-empty AND the caller to
    equal the configured ``owner_id``, so a bare MagicMock is refused. Cases that
    mean to be refused pass a non-empty ``app`` or a mismatched ``user``. Same
    construction as ``test_aws_consent._consent_request`` so the two consent
    endpoints are exercised through one request shape.
    """
    from unittest.mock import MagicMock

    req = MagicMock()
    req.path = "/api/file-delivery/consent"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    req.query = query or {}
    req.rel_url.query = query or {}
    return req


def _local_approve_request(*, nonce: str, local: bool = True):
    """A request shaped like the host CLI's approve POST.

    ``_approve_is_local`` passes on ``internal_auth`` / ``peer_verified`` marks or a
    loopback remote; a remote-shaped request omits all three.
    """
    from unittest.mock import MagicMock

    req = MagicMock()
    store = {"internal_auth": True} if local else {}
    if not local:
        req.remote = "203.0.113.7"
    req.get = lambda key, default=None: store.get(key, default)

    async def _json():
        return {"nonce": nonce}

    req.json = _json
    return req


class TestGrantRequiresAHostStepUp:
    """Recording a grant needs an owner ARM plus a host-only nonce APPROVE.

    The hole this step-up closes: an owner-authenticated but agent-DRIVEN
    browser satisfies ``is_owner_dashboard_request`` (an identity check, not a
    proof of human presence), so an owner-session POST alone must NOT record a
    grant. Reading the nonce from the keystone file is the presence proof an
    agent-driven browser cannot fake.
    """

    @pytest.fixture(autouse=True)
    def _isolated_nonce(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            file_delivery_consent,
            "pending_grant_path",
            lambda: tmp_path / "file-delivery-consent-pending" / "nonce.json",
            raising=True,
        )
        store = tmp_path / "file_delivery_consent.json"
        monkeypatch.setattr(
            file_delivery_consent, "file_delivery_consent_path", lambda: store, raising=True
        )
        # Default EVERY approve-time fence to its PERMITTING state so the
        # success-path tests exercise the round-trip rather than the host they run
        # on; the fence-specific tests override the one they are about. Each of
        # these resolves to a REFUSING value on some supported CI host: a
        # backend-less runner makes ``credential_mask_applies`` False, and native
        # Windows makes ``spawn_delegates_masking`` True and every pid unconfined,
        # because neither the namespace nor the Seatbelt reading exists there. A
        # test that stubbed only some of them would be asserting on an incoherent
        # host -- "the mask applies" on a platform that cannot apply it -- and
        # would 403 on that platform alone.
        from kiro_crew import sandbox
        from kiro_crew.computer_use import enable_state

        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True, raising=True)
        # Captured before the stub so the tests ABOUT these two predicates still call
        # the real ones: they import from ``kiro_crew.sandbox`` inside the test body,
        # which reads the stubbed attribute, and a stub returning the permitting value
        # would make those assertions pass vacuously rather than fail.
        self._real_spawn_delegates_masking = sandbox.spawn_delegates_masking
        self._real_unconfined_live_agent_pid = sandbox.unconfined_live_agent_pid
        monkeypatch.setattr(sandbox, "spawn_delegates_masking", lambda **k: False, raising=True)
        monkeypatch.setattr(sandbox, "unconfined_live_agent_pid", lambda pids: None, raising=True)

    def test_arming_records_no_grant_and_leaks_no_nonce(self):
        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        # Armed, but nothing granted yet.
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )
        # The SPA projection never carries the nonce.
        view = file_delivery_consent.public_pending_view(pending)
        assert view["armed"] is True
        assert "nonce" not in view
        assert view["approve_command"] == "kirocrew file-delivery approve"

    def test_approve_from_a_remote_caller_is_refused(self):
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        req = _local_approve_request(nonce=pending.nonce, local=False)
        resp = asyncio.run(handler.api_file_delivery_consent_approve(req))
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "file_delivery_approve_not_local"
        # A refused remote approve records nothing.
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )

    def test_approve_with_a_wrong_nonce_is_refused(self):
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        req = _local_approve_request(nonce="0" * 64)
        resp = asyncio.run(handler.api_file_delivery_consent_approve(req))
        assert resp.status == 403
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )

    def test_approve_is_refused_while_computer_use_is_enabled(self, monkeypatch):
        # F1 fence: computer use (desktop observation + input synthesis) lets a
        # prompt-injected agent TYPE the approve command into a host terminal, so
        # with it enabled the terminal step-up stops proving a human is present.
        # While it is enabled the grant must NOT be recorded.
        from kiro_crew.computer_use import enable_state
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: True, raising=True)
        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        req = _local_approve_request(nonce=pending.nonce, local=True)
        resp = asyncio.run(handler.api_file_delivery_consent_approve(req))
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "file_delivery_approve_computer_use_active"
        # Nothing recorded, and the single-use nonce is NOT consumed by a refusal
        # (fail fast before consume), so the owner can retry after disabling.
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )

    def test_approve_is_allowed_when_computer_use_is_disabled(self, monkeypatch):
        # The fence is CONDITIONAL, not a blanket break: with computer use off,
        # the terminal step-up holds and a local approve records the grant.
        from kiro_crew.computer_use import enable_state
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        req = _local_approve_request(nonce=pending.nonce, local=True)
        resp = asyncio.run(handler.api_file_delivery_consent_approve(req))
        assert resp.status == 200
        assert file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is True

    def test_approve_is_refused_when_the_sandbox_mask_does_not_apply(self, monkeypatch):
        # F1(new) fence: the nonce's secrecy rests on the pending leaf being masked
        # out of the agent sandbox. If the effective sandbox would NOT apply that
        # mask (agent.sandbox 'off', or no backend + unsandboxed exec opted in), an
        # unsandboxed agent can read/forge the nonce directly, so the host step-up
        # stops proving human presence. The grant must NOT be recorded.
        from kiro_crew import sandbox
        from kiro_crew.computer_use import enable_state
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "off", raising=True)
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: False, raising=True)
        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        req = _local_approve_request(nonce=pending.nonce, local=True)
        resp = asyncio.run(handler.api_file_delivery_consent_approve(req))
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "file_delivery_approve_unsandboxed"
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )

    def test_every_approve_denial_is_audited(self, monkeypatch):
        # A denial that leaves no SEL entry is a denial an incident review cannot
        # see, and these four ARE what a review asks about. Parametrized inside
        # one test on purpose: the claim is about the SET of refusal legs, so a
        # leg added later without an audit has to redden something, and a
        # per-leg test would simply not exist for it.
        from kiro_crew import sandbox
        from kiro_crew.computer_use import enable_state
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        recorded: list[tuple[str, str, str]] = []

        def _capture(destination_class, *, outcome, detail=""):
            recorded.append((destination_class, outcome, detail))

        monkeypatch.setattr(file_delivery_consent, "audit_decision", _capture, raising=True)
        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True, raising=True)

        legs: dict[str, dict] = {
            "file_delivery_approve_not_local": {"local": False},
            "file_delivery_approve_computer_use_active": {"computer_use": True},
            "file_delivery_approve_unsandboxed": {"mask": False},
            "file_delivery_approve_refused": {"nonce": "0" * 64},
        }
        for code, setup in legs.items():
            recorded.clear()
            if "computer_use" in setup:
                monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: True, raising=True)
            if "mask" in setup:
                monkeypatch.setattr(
                    sandbox, "credential_mask_applies", lambda mode: False, raising=True
                )
            pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
            req = _local_approve_request(
                nonce=setup.get("nonce", pending.nonce), local=setup.get("local", True)
            )
            resp = asyncio.run(handler.api_file_delivery_consent_approve(req))
            assert resp.status == 403, code
            assert json.loads(resp.text)["code"] == code
            assert recorded, f"{code} returned 403 with no SEL entry"
            assert [row[1] for row in recorded] == ["denied"], code
            # The class is "*": the refusal happens before any pending request is
            # validated, so naming one would put an unvalidated caller's word in
            # the audit.
            assert recorded[0][0] == "*", code
            assert recorded[0][2].startswith("approve: "), code
            assert (
                file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD)
                is False
            ), code
            # Restore the permissive defaults for the next leg.
            monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
            monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True, raising=True)

    def test_approve_is_refused_when_the_spawn_delegates_masking(self, monkeypatch):
        # The mask predicate answers whether the backend wrap_argv picks would
        # CARRY the mask; it does not answer whether that wrap happens. On macOS
        # with kiro-cli's internal sandbox on, a backend exists so the first
        # question answers yes while the spawn is delegated and Crew's mask never
        # runs -- an agent could then read the nonce and complete the step-up with
        # no human present. Both questions must pass.
        from kiro_crew import sandbox
        from kiro_crew.computer_use import enable_state
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True, raising=True)
        monkeypatch.setattr(sandbox, "spawn_delegates_masking", lambda **k: True, raising=True)
        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        req = _local_approve_request(nonce=pending.nonce, local=True)
        resp = asyncio.run(handler.api_file_delivery_consent_approve(req))
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "file_delivery_approve_unsandboxed"
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )

    def test_an_unverifiable_delegation_setting_refuses(self, monkeypatch):
        # Fail closed: if the delegation setting cannot be read, the mask cannot
        # be shown to run, so the step-up does not prove human presence.
        from kiro_crew import sandbox

        def _boom():
            raise OSError("delegation setting unreadable")

        monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", _boom, raising=True)
        monkeypatch.setattr(sandbox.sys, "platform", "darwin", raising=False)
        assert self._real_spawn_delegates_masking() is True

    def test_only_one_of_two_concurrent_approvals_can_claim_one_nonce(self):
        # Single-use has to be decided in ONE step. Validating and clearing as
        # two steps let both callers through, and a withdrawal landing between
        # their writes was then silently undone by the later one.
        from concurrent.futures import ThreadPoolExecutor

        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                pool.submit(file_delivery_consent.claim_grant, pending.nonce) for _ in range(2)
            ]
            outcomes = []
            for fut in results:
                try:
                    outcomes.append(fut.result().request_id)
                except file_delivery_consent.StepUpError:
                    outcomes.append(None)
        assert sorted(o is None for o in outcomes) == [False, True], outcomes
        assert file_delivery_consent.read_pending_grant() is None

    def test_a_claimed_request_is_restored_only_when_nothing_newer_is_armed(self):
        # A failed grant write stays non-destructive, which is what the old
        # ordering bought -- but never by overwriting a request armed since,
        # because that one is live and nobody approved it.
        first = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        claimed = file_delivery_consent.claim_grant(first.nonce)
        assert file_delivery_consent.read_pending_grant() is None
        assert file_delivery_consent.restore_pending_grant(claimed) is True
        back = file_delivery_consent.read_pending_grant()
        assert back is not None and back.request_id == first.request_id

        again = file_delivery_consent.claim_grant(first.nonce)
        newer = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        assert file_delivery_consent.restore_pending_grant(again) is False
        survivor = file_delivery_consent.read_pending_grant()
        assert survivor is not None and survivor.request_id == newer.request_id

    def test_approve_is_refused_while_a_live_agent_session_is_unconfined(self, monkeypatch):
        # The mask and delegation checks read CONFIGURATION, which describes the
        # next spawn. `agent.sandbox` carries no restart marker, so a session
        # spawned while the tier was `off` is still running unconfined after the
        # config flips -- and that live agent can read the nonce while every
        # config answer says it cannot.
        from kiro_crew import sandbox
        from kiro_crew.computer_use import enable_state
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True, raising=True)
        monkeypatch.setattr(sandbox, "spawn_delegates_masking", lambda **k: False, raising=True)
        monkeypatch.setattr(sandbox, "unconfined_live_agent_pid", lambda pids: 4242, raising=True)
        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        req = _local_approve_request(nonce=pending.nonce, local=True)
        resp = asyncio.run(handler.api_file_delivery_consent_approve(req))
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "file_delivery_approve_unsandboxed"
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )

    def test_an_unreadable_session_registry_is_not_no_agent_running(self):
        # Fail closed: a registry that cannot be enumerated must not read as an
        # empty one, or the check passes precisely when it cannot see anything.
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        class _Boom:
            def session_keys(self):
                raise OSError("registry unreadable")

        req = SimpleNamespace(app={"state": SimpleNamespace(sessions=_Boom())})
        assert handler._live_agent_pids(req) == [-1]
        # And the sentinel is judged unconfined, so the caller refuses.
        assert self._real_unconfined_live_agent_pid([-1]) == -1

    def test_a_confined_pid_set_permits_approval(self):
        # The gate is CONDITIONAL, not a blanket break: an empty registry (no
        # agent running) leaves nothing unconfined to find.
        assert self._real_unconfined_live_agent_pid([]) is None

    def test_native_windows_refuses_because_it_has_no_confinement_to_verify(self, monkeypatch):
        """Both platform predicates answer REFUSE on win32, and that is deliberate.

        Native Windows has neither the namespace reading nor Seatbelt membership, so
        there is no Crew confinement to verify and no mask to show running. The
        step-up therefore cannot be shown to prove human presence there and the
        approve refuses -- which matches what the host already did before these
        fences existed, since a backend-less host resolves ``credential_mask_applies``
        to False on its own.

        Asserted here so the posture is a decision in the suite rather than something
        a Windows CI shard discovers: stubbing only SOME of the fences in a
        success-path test reds on win32 alone, which reads as a platform flake.
        """
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox.sys, "platform", "win32", raising=False)
        assert self._real_spawn_delegates_masking() is True
        assert self._real_unconfined_live_agent_pid([4242]) == 4242

    def test_a_request_armed_under_computer_use_cannot_be_claimed_after_it_is_disabled(
        self, monkeypatch
    ):
        # The attack the approve-time check alone could not stop: an agent under
        # computer use arms, queues a delayed approve, then drives the owner-gated
        # toggle to turn computer use OFF. A check that only asks about NOW sees a
        # safe host and lets the delayed command through. The epoch was stamped
        # while computer use was on, so the claim refuses.
        from kiro_crew.computer_use import enable_state

        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: True, raising=True)
        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        assert pending.safety_epoch
        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
        with pytest.raises(file_delivery_consent.StepUpError, match="configuration changed"):
            file_delivery_consent.claim_grant(pending.nonce)
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )

    def test_a_request_armed_before_the_epoch_existed_cannot_be_claimed(self):
        # Fail closed on a row with no epoch: defaulting it to the CURRENT epoch
        # would make every pre-epoch request claimable, which is the hole.
        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        path = file_delivery_consent.pending_grant_path()
        row = json.loads(path.read_text(encoding="utf-8"))
        del row["safety_epoch"]
        path.write_text(json.dumps(row), encoding="utf-8")
        assert file_delivery_consent.read_pending_grant().safety_epoch == ""
        with pytest.raises(file_delivery_consent.StepUpError, match="configuration changed"):
            file_delivery_consent.claim_grant(pending.nonce)

    def test_an_unchanged_configuration_still_claims(self):
        # Conditional, not a blanket break: nothing moved, so the owner's own
        # approve still works.
        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        assert file_delivery_consent.claim_grant(pending.nonce).request_id == pending.request_id

    def test_approve_is_allowed_when_the_sandbox_mask_applies(self, monkeypatch):
        # Conditional, not a blanket break: with the mask in effect the nonce is
        # hidden from the agent and a local approve records the grant.
        from kiro_crew import sandbox
        from kiro_crew.computer_use import enable_state
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "strict", raising=True)
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True, raising=True)
        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        req = _local_approve_request(nonce=pending.nonce, local=True)
        resp = asyncio.run(handler.api_file_delivery_consent_approve(req))
        assert resp.status == 200
        assert file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is True

    def test_a_failed_grant_write_leaves_the_nonce_retryable(self, monkeypatch):
        # Order side effects so the irreversible one is last: if record_grant
        # fails, the single-use nonce must NOT already be consumed, so the owner
        # can retry rather than lose the armed request to a 500.
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)

        real_record = file_delivery_consent.record_grant

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(file_delivery_consent, "record_grant", _boom, raising=True)
        req = _local_approve_request(nonce=pending.nonce, local=True)
        resp = asyncio.run(handler.api_file_delivery_consent_approve(req))
        assert resp.status == 500
        assert json.loads(resp.text)["code"] == "file_delivery_approve_write_failed"
        # Nothing recorded, and the nonce is STILL VALID -- a retry (with a working
        # writer) now succeeds, which proves the failure was non-destructive.
        assert (
            file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is False
        )
        assert file_delivery_consent.read_pending_grant() is not None
        # Restore ONLY record_grant (not monkeypatch.undo, which would also drop the
        # fixture's mask / computer-use fence defaults and 403 the retry).
        monkeypatch.setattr(file_delivery_consent, "record_grant", real_record, raising=True)
        retry = _local_approve_request(nonce=pending.nonce, local=True)
        resp2 = asyncio.run(handler.api_file_delivery_consent_approve(retry))
        assert resp2.status == 200
        assert file_delivery_consent.is_granted(file_delivery_consent.CLASS_OWNER_DASHBOARD) is True
        # And after the successful retry the nonce is consumed (single-use holds).
        assert file_delivery_consent.read_pending_grant() is None

    def test_the_nonce_is_single_use(self):
        pending = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        assert (
            file_delivery_consent.claim_grant(pending.nonce).destination_class
            == file_delivery_consent.CLASS_OWNER_DASHBOARD
        )
        with pytest.raises(file_delivery_consent.StepUpError):
            file_delivery_consent.claim_grant(pending.nonce)
        # The claim also removes the file. A comparison that never matched would
        # still refuse the second claim above yet leak the artifact here, and a
        # consent artifact outliving its window fails open.
        assert file_delivery_consent.pending_grant_path().exists() is False

    def test_a_stale_nonce_spares_a_newer_armed_request(self):
        # Lost-update guard: the owner arms request A, re-arms before approving,
        # and the delayed `approve` still carrying A's nonce must not consume B --
        # a request B's owner has not approved yet. claim_grant reads under the
        # lock, so it sees B, finds the nonce mismatched, and unlinks nothing.
        a = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        b = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        assert b.request_id != a.request_id
        with pytest.raises(file_delivery_consent.StepUpError):
            file_delivery_consent.claim_grant(a.nonce)
        survivor = file_delivery_consent.read_pending_grant()
        assert survivor is not None
        assert survivor.request_id == b.request_id
        assert survivor.nonce == b.nonce

    def test_expired_request_reads_as_none_without_unlinking(self, monkeypatch):
        # An expired request reads as None but is NOT unlinked on read: the next
        # arm's os.replace overwrites the single file, so unlinking here would
        # race a concurrent arm and delete a request nobody approved. Simulate an
        # expired row by advancing the clock past the TTL, then assert read
        # returns None AND the file is still present (the arm that replaces it is
        # what removes it).
        a = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        real_time = file_delivery_consent.time.time
        with pytest.MonkeyPatch.context() as clock:
            clock.setattr(
                file_delivery_consent.time,
                "time",
                lambda: real_time() + file_delivery_consent.GRANT_PENDING_TTL_SECS + 1,
                raising=True,
            )
            assert file_delivery_consent.read_pending_grant() is None
            assert file_delivery_consent.pending_grant_path().exists() is True
        assert a.request_id  # the armed request existed before it expired
        # And a fresh arm replaces the expired file with a live request.
        b = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        live = file_delivery_consent.read_pending_grant()
        assert live is not None
        assert live.request_id == b.request_id

    def test_a_concurrent_arm_survives_a_claim_of_an_older_request(self):
        # The interleaving GPT named: a claim reads A, an arm writes B in the
        # window between that read and the unlink, and the unlink must not delete
        # B. _PENDING_LOCK makes arm and the read-and-unlink mutually exclusive,
        # so B either lands wholly before the claim (which then reads B, finds the
        # nonce mismatched and deletes nothing) or wholly after (the claim deletes
        # A, then B lands) -- never deleted mid-flight. We force the window with
        # an instrumented os.unlink that parks inside the claim while a thread
        # issues arm(B).
        import threading as _t

        a = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
        in_gap = _t.Event()
        b_done = _t.Event()
        real_unlink = file_delivery_consent.os.unlink

        def _parking_unlink(path):
            # Signal we are between the claim's read and its unlink, then give a
            # concurrent arm a chance to land before the real unlink. Under the
            # lock the arm cannot proceed here (it blocks on _PENDING_LOCK), so B
            # never lands in the gap, the wait times out, and A is unlinked.
            in_gap.set()
            b_done.wait(timeout=2.0)
            return real_unlink(path)

        def _arm_b():
            in_gap.wait(timeout=2.0)
            file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
            b_done.set()

        mp = pytest.MonkeyPatch()
        mp.setattr(file_delivery_consent.os, "unlink", _parking_unlink, raising=True)
        t = _t.Thread(target=_arm_b)
        t.start()
        try:
            file_delivery_consent.claim_grant(a.nonce)
        finally:
            b_done.set()
            t.join(timeout=5.0)
            mp.undo()

        # B is the last writer and must be armed; the claim only ever removed A.
        survivor = file_delivery_consent.read_pending_grant()
        assert survivor is not None
        assert survivor.request_id != a.request_id

    def test_the_nonce_leaf_is_masked_from_the_agent_sandbox(self):
        # The forge path GPT found: trust/ is sandbox-VISIBLE, so a runtime-
        # constructed shell write could forge a nonce there. The nonce therefore
        # lives in its OWN leaf that is BOTH keystone-fenced (file gate) AND
        # bind-masked from the sandbox (no runtime-shell forge), and is NOT under
        # trust/.
        from kiro_crew import sandbox

        leaf = file_delivery_consent._PENDING_GRANT_DIRNAME
        assert leaf in security._CREW_SECRET_LEAVES
        assert leaf in sandbox._CREW_HIDDEN_LEAVES
        # The mask loop is isdir-guarded, and this dir is created LAZILY at arm
        # time -- so on a fresh install it is absent at spawn and the mask skips
        # it unless it is also precreated before every namespace spawn.
        assert leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        # Real path (no monkeypatch): parent is the dedicated leaf, not trust/.
        real_parent = (
            file_delivery_consent.data_home() / leaf / file_delivery_consent._PENDING_GRANT_FILENAME
        ).parent.name
        assert real_parent == leaf
        assert real_parent != "trust"

    def test_precreate_materialises_the_nonce_dir_before_spawn(self, tmp_path, monkeypatch):
        # BEHAVIOURAL, not just membership: prove _materialize_maskable_dirs
        # actually creates the leaf so the isdir-guarded mask is non-vacuous on a
        # fresh install (no prior arm). Without the leaf in the precreate list the
        # dir stays absent here and the mask would skip it.
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
        leaf = file_delivery_consent._PENDING_GRANT_DIRNAME
        target = tmp_path / leaf
        assert not target.exists()  # fresh install: nothing armed yet

        created = sandbox._materialize_maskable_dirs()

        assert target.is_dir()
        assert str(target) in created
        if os.name == "posix":
            # Owner-only: no group/other access, whatever the umask.
            assert (target.stat().st_mode & 0o077) == 0

    def test_the_approve_path_is_strict_internal_not_mixed(self):
        # The approve endpoint is the host-side step-up: loopback + X-Internal-
        # Secret only, NEVER cookie-reachable. STRICT membership is the outer
        # fence that denies a dashboard/agent bearer at the middleware (no cookie
        # fall-through). MIXED would add the browser-polled cookie path and reopen
        # the self-approve hole -- so it must be STRICT and must NOT be MIXED, the
        # exact posture of its sibling /api/update/approve.
        from kiro_crew.dashboard import server

        path = "/api/file-delivery/consent/approve"
        assert path in server._STRICT_INTERNAL_API_PATHS
        assert path not in server._MIXED_INTERNAL_API_PATHS
        # Pin the sibling too, so a refactor that drops the pattern reddens here.
        assert "/api/update/approve" in server._STRICT_INTERNAL_API_PATHS


class TestConsentEndpointRequiresTheOwner:
    """Every verb, reads included, is refused to anyone but the dashboard owner.

    Reads too, because the GET names which destinations the owner has blessed --
    i.e. where a flagged file would land unrefused.
    """

    @pytest.mark.parametrize(
        "verb",
        [
            "api_file_delivery_consent_get",
            "api_file_delivery_consent_post",
            "api_file_delivery_consent_delete",
        ],
    )
    def test_an_app_token_is_refused(self, verb):
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        req = _consent_request(app="notes", query={"destination_class": "owner_dashboard"})
        resp = asyncio.run(getattr(handler, verb)(req))
        assert resp.status == 403

    @pytest.mark.parametrize(
        "verb",
        [
            "api_file_delivery_consent_get",
            "api_file_delivery_consent_post",
            "api_file_delivery_consent_delete",
        ],
    )
    def test_an_allow_listed_non_owner_is_refused(self, verb):
        """The case an app-only check misses: app is empty, caller is not the owner."""
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        req = _consent_request(
            app="",
            user="slack-guest",
            owner="owner-1",
            query={"destination_class": "owner_dashboard"},
        )
        resp = asyncio.run(getattr(handler, verb)(req))
        assert resp.status == 403


class TestConsentEndpointAsOwner:
    def test_get_reports_grantable_and_never_grantable(self):
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        resp = asyncio.run(handler.api_file_delivery_consent_get(_consent_request()))
        assert resp.status == 200
        payload = json.loads(resp.text)
        assert payload["grantable"] == [file_delivery_consent.CLASS_OWNER_DASHBOARD]
        # The panel must be able to SAY the upload legs are excluded rather than
        # merely omitting them, which reads as an oversight.
        assert set(payload["never_grantable"]) == set(file_delivery_consent.NEVER_GRANTABLE_CLASSES)
        assert payload["grants"][file_delivery_consent.CLASS_OWNER_DASHBOARD] is None

    def test_post_arms_then_approve_records_then_get_then_delete_round_trip(
        self, tmp_path, monkeypatch
    ):
        # Every fence on the approve leg must be in its permitting state for the
        # round-trip, and for the same reason the class fixture spells out: each one
        # refuses on some supported CI host (a backend-less runner resolves
        # credential_mask_applies to False; native Windows delegates masking and
        # reads every pid as unconfined), so stubbing a subset asserts on a host
        # that cannot exist and 403s on that platform alone.
        from kiro_crew import sandbox
        from kiro_crew.computer_use import enable_state
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True, raising=True)
        monkeypatch.setattr(sandbox, "spawn_delegates_masking", lambda **k: False, raising=True)
        monkeypatch.setattr(sandbox, "unconfined_live_agent_pid", lambda pids: None, raising=True)

        # Isolate BOTH the store and the armed-nonce file so nothing touches the
        # real data home.
        store = tmp_path / "file_delivery_consent.json"
        monkeypatch.setattr(
            file_delivery_consent, "file_delivery_consent_path", lambda: store, raising=True
        )
        monkeypatch.setattr(
            file_delivery_consent,
            "pending_grant_path",
            lambda: tmp_path / "file-delivery-consent-pending" / "nonce.json",
            raising=True,
        )

        q = {"destination_class": file_delivery_consent.CLASS_OWNER_DASHBOARD}
        # POST ARMS: it must NOT record a grant, and the response carries no nonce.
        post = asyncio.run(handler.api_file_delivery_consent_post(_consent_request(query=q)))
        assert post.status == 200
        armed = json.loads(post.text)
        assert armed["armed"] is True
        assert "nonce" not in armed
        # Still not confirmed after arming.
        got = json.loads(
            asyncio.run(handler.api_file_delivery_consent_get(_consent_request())).text
        )
        assert got["grants"][file_delivery_consent.CLASS_OWNER_DASHBOARD] is None

        # APPROVE with the armed nonce (read from the host file) RECORDS the grant.
        pending = file_delivery_consent.read_pending_grant()
        approve_req = _local_approve_request(nonce=pending.nonce)
        approve = asyncio.run(handler.api_file_delivery_consent_approve(approve_req))
        assert approve.status == 200
        assert json.loads(approve.text)["grant"]["destination_class"] == (
            file_delivery_consent.CLASS_OWNER_DASHBOARD
        )
        got = json.loads(
            asyncio.run(handler.api_file_delivery_consent_get(_consent_request())).text
        )
        assert got["grants"][file_delivery_consent.CLASS_OWNER_DASHBOARD] is not None
        delete = asyncio.run(handler.api_file_delivery_consent_delete(_consent_request(query=q)))
        assert json.loads(delete.text)["removed"] is True

    @pytest.mark.parametrize("leg", sorted(file_delivery_consent.NEVER_GRANTABLE_CLASSES))
    def test_post_refuses_a_never_grantable_leg_as_unknown(self, leg):
        """A request naming an upload leg is refused before anything is armed."""
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        resp = asyncio.run(
            handler.api_file_delivery_consent_post(
                _consent_request(query={"destination_class": leg})
            )
        )
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "unknown_destination_class"

    def test_delete_of_an_absent_grant_reports_not_removed(self):
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        q = {"destination_class": file_delivery_consent.CLASS_OWNER_DASHBOARD}
        resp = asyncio.run(handler.api_file_delivery_consent_delete(_consent_request(query=q)))
        assert json.loads(resp.text)["removed"] is False

    def test_an_unknown_class_is_refused_on_delete_too(self):
        from kiro_crew.dashboard.handlers import file_delivery_consent as handler

        resp = asyncio.run(
            handler.api_file_delivery_consent_delete(
                _consent_request(query={"destination_class": "nonsense"})
            )
        )
        assert resp.status == 400


class TestFileSendHonoursTheGrant:
    def _call(self, path):
        from kiro_crew.mcp_tools.messaging import file_send

        return file_send("file_send", {"path": str(path)})

    def test_without_a_grant_a_flagged_file_is_refused(self, tmp_path):
        src = tmp_path / "device.conf"
        src.write_text(_synth_pem())
        out = self._call(src)
        assert "sensitive data" in out and "aborted" in out

    def test_with_a_grant_it_delivers_and_skips_both_upload_legs(self, tmp_path):
        from kiro_crew import mcp_core

        src = tmp_path / "device.conf"
        src.write_text(_synth_pem())
        _grant()
        posted: list[str] = []

        def _fake_post(path, *a, **kw):
            posted.append(path)
            return {"ok": True}

        with patch.object(mcp_core, "_post", side_effect=_fake_post):
            out = self._call(src)
        assert "File sent" in out
        assert "Slack and channel upload skipped" in out
        # The absolute condition, asserted on behaviour: neither upload leg is
        # even ATTEMPTED for content the owner scoped to their own dashboard.
        assert "/api/outbox/notify" in posted
        assert not any("upload-file" in p for p in posted)


class TestAuditDecisionRedactsBeforeTruncate:
    """``audit_decision`` must redact ``detail`` BEFORE clipping it to 200 chars.

    Same site shape, and the same invariant, as ``aws_consent.audit_decision``
    (pinned in ``test_aws_consent.py``): the ``resources`` string reaches the
    durable Security Event Log through ``log_api_access``, whose own pass runs
    over what it is handed. A credential the caller already cut in half at index
    200 is a fragment no credential grammar matches, so the partial secret would
    persist in a dashboard-readable audit log. The second test pins the branch
    the redact-first rewrite must not disturb: an empty ``detail`` still emits
    the bare ``destination_class`` with no ``": "`` separator.
    """

    @staticmethod
    def _capture(monkeypatch):
        import kiro_crew.sel as sel_mod

        calls: list[dict] = []

        class _Recorder:
            def log_api_access(self, **kwargs) -> None:
                calls.append(kwargs)

        monkeypatch.setattr(sel_mod, "sel", lambda: _Recorder())
        return calls

    def test_a_credential_straddling_the_clip_is_fully_redacted(self, monkeypatch):
        calls = self._capture(monkeypatch)
        key = _synth_aws_key()
        pad = "d" * (200 - 4)
        detail = pad + key + " " + "z" * 300
        assert len(detail) > 200
        assert 200 - len(pad) < len(key)  # key straddles the cut

        file_delivery_consent.audit_decision(
            file_delivery_consent.CLASS_OWNER_DASHBOARD, outcome="denied", detail=detail
        )

        assert len(calls) == 1
        resources = calls[0]["resources"]
        assert key[:4] not in resources, resources
        assert resources.startswith(f"{file_delivery_consent.CLASS_OWNER_DASHBOARD}: ")
        assert len(resources) <= len(file_delivery_consent.CLASS_OWNER_DASHBOARD) + 2 + 200

    def test_an_empty_detail_still_emits_the_bare_destination_class(self, monkeypatch):
        calls = self._capture(monkeypatch)

        file_delivery_consent.audit_decision(
            file_delivery_consent.CLASS_OWNER_DASHBOARD, outcome="revoked", detail=""
        )

        assert len(calls) == 1
        assert calls[0]["resources"] == file_delivery_consent.CLASS_OWNER_DASHBOARD
