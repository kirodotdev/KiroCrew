"""LTS end to end: no local tier can widen the organisation's remote ceiling.

RFC ``docs/request-for-change/rfc-crew-lts-edition.md``, D2 and D3: in an LTS build
the organisation's policy, read from the Kiro control plane (CPS), is the ceiling.
Every local tier may only narrow it.

Each test starts from one fake CPS reply and drives the real path: the transport
``enterprise_policy_fetch.fetch`` (registered for the build's ``kirocps`` source),
the translator, the tier fold in ``governance.load_security_policy``, then the
installed context. The answers are read at the runtime chokepoints a feature asks:
``governance_permits``, ``approval_mode_permitted``, ``effective_sandbox_mode``,
``tailnet.policy_off`` and ``edition_locks.policy_permits``. The fake answers behind
``policy_distribution._open``, the one seam the network call goes through, so
nothing opens a socket. The sign-in is a real vault entry in a temporary data home.

The local tiers tried: the env policy (``KIROCREW_SECURITY_POLICY``), the home policy
file, a governance profile, and user config (``agent.sandbox``). The pieces are
tested alone in ``test_governance_tier_ladder.py``, ``test_governance_lts_baseline.py``,
``test_enterprise_policy_fetch.py`` and ``test_enterprise_policy_translate_terms.py``;
this module pins that they hold together from a remote reply.
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from governance_distribution_helpers import _hermetic_governance_globals  # noqa: F401
from governance_distribution_helpers import install_ceiling_fixture  # noqa: F401
from governance_distribution_helpers import profiles_dir_fixture  # noqa: F401
from governance_distribution_helpers import _write_policy

from kiro_crew import sandbox_tiers
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.dashboard import tailnet
from kiro_crew.edition_locks import policy_permits
from kiro_crew.platform import enterprise_policy_fetch as epf
from kiro_crew.platform import governance
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.safety_override_permits import approval_mode_permitted

_ARN = "arn:aws:codewhisperer:us-east-1:123456789012:profile/ABCDEFGHIJKL"

_ALLOWED_HOSTS = ["api.corp.example", "docs.corp.example"]

#: The organisation's rules, as the control plane sends them for the CREW surface.
_REMOTE_POLICY = f"""\
version: "0.1"
rules:
  - {{capability: cron, effect: deny}}
  - {{capability: messaging, effect: deny}}
  - {{capability: browser_use, effect: deny}}
  - {{capability: tailnet_origin, effect: deny}}
  - {{capability: theme_persona, effect: allow}}
  - {{capability: network_egress, effect: allow, match: {json.dumps(_ALLOWED_HOSTS)}}}
  - {{capability: network_egress, effect: deny, exclude: {json.dumps(_ALLOWED_HOSTS)}}}
  - {{capability: permission_mode, effect: allow, match: ["normal", "trust_reads"]}}
  - {{capability: permission_mode, effect: deny, exclude: ["normal", "trust_reads"]}}
"""

#: The reply's settings: the organisation's sandbox floor is ``cc``.
_REMOTE_CONFIGURATION = [
    {
        "key": "crew_sandbox_min_level",
        "value": json.dumps("cc"),
        "source": "profile#" + _ARN,
        "composeBehavior": "OVERRIDE",
    }
]

#: What a local tier writes to try to turn every remote limit back off.
_WIDENING = {
    "capabilities": {
        "cron": {"enabled": True},
        "messaging": {"enabled": True},
        "browse": {"enabled": True},
        "tailnet_origin": {"enabled": True},
    },
    "network": {"egress": {"mode": "deny", "deny": []}},
    "approval_modes": {"mode": "allow", "allow": ["normal", "trust_reads", "trust", "yolo"]},
}

#: What a local tier writes to narrow the remote ceiling.
_NARROWING = {
    "capabilities": {"theme_persona": {"enabled": False}},
    "network": {"egress": {"mode": "deny", "deny": ["docs.corp.example"]}},
}

_HOST = gp.HOST_SESSION_KEY


def _envelope() -> bytes:
    digest = epf.body_digest(_REMOTE_POLICY, tuple(_REMOTE_CONFIGURATION))
    return json.dumps(
        {
            "enterpriseManagementStatus": "ENABLED",
            "manifest": {"hash": "sha256:" + digest, "versionId": "v1:" + digest},
            "body": {"policy": _REMOTE_POLICY, "configuration": _REMOTE_CONFIGURATION},
        }
    ).encode("utf-8")


class _Resp:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(name="cps")
def cps_fixture(monkeypatch):
    """The control plane: answers every request with the one remote reply."""
    requests: list = []

    def answer(req, timeout):
        requests.append(req)
        return _Resp(_envelope())

    monkeypatch.setattr(pd, "_open", answer)
    return requests


@pytest.fixture(name="signed_in")
def signed_in_fixture(tmp_path, monkeypatch, _floor_monkeypatch):
    """One enterprise sign-in in a temporary vault, pinned as a fresh process pins it."""
    store = TokenStore(tmp_path / "data")
    monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", lambda: store)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    # ``pinned_scope`` writes the pin straight into ``os.environ``. Set it first so
    # the delete records an undo and teardown removes the pin this boot writes.
    monkeypatch.setenv(epf.PROFILE_PIN_ENV, "")
    monkeypatch.delenv(epf.PROFILE_PIN_ENV)
    monkeypatch.setattr(epf, "_pinned", "")
    # A running gateway stops itself on an account change; never in a test.
    _floor_monkeypatch.setattr(epf.signal, "raise_signal", lambda sig: None)
    store.save(
        KasToken(
            access_token="fake-access-token-value",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            provider="Enterprise",
            identity="identity_center",
            profile_arn=_ARN,
            auth_method="identity_center",
            token_endpoint="https://idp.example.invalid/token",
        )
    )


@pytest.fixture(name="lts")
def lts_fixture(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


@pytest.fixture(name="boot")
def boot_fixture(install_ceiling, lts, signed_in, cps, profiles_dir):
    """Boot as ``bootstrap_context`` does: load, check the profiles, install.

    ``install_ceiling`` is requested before ``lts`` so its base context is composed
    as oss, before the edition flips.
    """

    def boot() -> governance.GovernanceCeiling:
        ceiling = governance.load_security_policy()
        assert ceiling is not None and ceiling.tier == governance.TIER_CENTRAL
        gp.reset_store()
        gp.assert_profiles_within_ceiling(ceiling)
        install_ceiling(ceiling)
        # Every local tier written was read: a policy file folded beneath the central
        # one, and a profile parsed rather than replaced by the deny-all fallback, so
        # no case below passes because its tier was skipped or refused.
        local_file = os.environ.get("KIROCREW_SECURITY_POLICY") or (
            governance._policy_home_path().exists()
        )
        assert ceiling.lower_tier_folded is bool(local_file)
        assert gp.fallback_profile_names() == frozenset()
        assert gp.unknown_profile_scopes() == {}
        bound = gp.resolve_active_scope(_HOST)
        assert (bound is not None) is any(profiles_dir.iterdir())
        return ceiling

    return boot


def _local_tier(tier: str, controls: dict, tmp_path, monkeypatch, profiles_dir) -> None:
    """Write *controls* into one local tier Crew reads."""
    if tier == "env":
        path = _write_policy(tmp_path / "env-policy.json", "local-env", **controls)
        monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(path))
    elif tier == "home":
        path = _write_policy(tmp_path / "home-policy.json", "local-home", **controls)
        monkeypatch.setattr(governance, "_policy_home_path", lambda: path)
    else:
        profile = {"name": "host", "bind": {"type": "surface", "id": "host"}, **controls}
        (profiles_dir / "host.json").write_text(json.dumps(profile), encoding="utf-8")


def _permits(scope: str, item: str = "") -> bool:
    return bool(gp.governance_permits(scope, item, session_key=_HOST).permitted)


_TIERS = ["env", "home", "profile"]


class TestTheRemoteReplyIsTheCeiling:
    def test_with_no_local_tier_the_remote_answers(self, boot, cps):
        boot()
        assert len(cps) == 1
        assert not _permits("capabilities.cron")
        assert not _permits("capabilities.messaging")
        assert not _permits("capabilities.browse")
        assert _permits("network.egress", "api.corp.example")
        assert not _permits("network.egress", "evil.example")
        assert approval_mode_permitted("trust_reads")
        assert not approval_mode_permitted("trust")
        assert sandbox_tiers.effective_sandbox_mode("standard") == "cc"
        assert tailnet.policy_off()


class TestALocalTierCannotWiden:
    @pytest.mark.parametrize("tier", _TIERS)
    def test_a_capability_the_remote_turns_off_stays_off(
        self, boot, tier, tmp_path, monkeypatch, profiles_dir
    ):
        _local_tier(tier, _WIDENING, tmp_path, monkeypatch, profiles_dir)
        boot()
        for scope in ("cron", "messaging", "browse", "tailnet_origin"):
            assert not _permits(f"capabilities.{scope}"), (tier, scope)

    @pytest.mark.parametrize("tier", _TIERS)
    def test_the_egress_allow_list_cannot_grow(
        self, boot, tier, tmp_path, monkeypatch, profiles_dir
    ):
        _local_tier(tier, _WIDENING, tmp_path, monkeypatch, profiles_dir)
        boot()
        assert not _permits("network.egress", "evil.example"), tier
        assert _permits("network.egress", "api.corp.example"), tier

    @pytest.mark.parametrize("tier", _TIERS)
    def test_trust_stays_denied(self, boot, tier, tmp_path, monkeypatch, profiles_dir):
        _local_tier(tier, _WIDENING, tmp_path, monkeypatch, profiles_dir)
        boot()
        assert not approval_mode_permitted("trust"), tier
        assert not approval_mode_permitted("yolo"), tier
        assert approval_mode_permitted("trust_reads"), tier

    @pytest.mark.parametrize("tier", _TIERS)
    def test_browser_and_tailnet_stay_off_at_their_process_switches(
        self, boot, tier, tmp_path, monkeypatch, profiles_dir
    ):
        _local_tier(tier, _WIDENING, tmp_path, monkeypatch, profiles_dir)
        boot()
        assert not policy_permits("capabilities.browse"), tier
        assert tailnet.policy_off(), tier

    @pytest.mark.parametrize("tier", ["env", "home"])
    def test_a_lower_local_sandbox_floor_does_not_lower_the_remote_one(
        self, boot, tier, tmp_path, monkeypatch, profiles_dir
    ):
        _local_tier(tier, {"sandbox": {"min_level": "off"}}, tmp_path, monkeypatch, profiles_dir)
        boot()
        assert gp.governance_floor_ordinal("sandbox.min_level", session_key=_HOST) == "cc"
        assert sandbox_tiers.effective_sandbox_mode("off") == "cc"

    def test_a_profile_below_the_remote_sandbox_floor_refuses_boot(self, boot, profiles_dir):
        (profiles_dir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "sandbox": {"min_level": "off"},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(PlatformCompositionError):
            boot()

    def test_user_config_cannot_lower_the_sandbox(self, boot):
        boot()
        config = SimpleNamespace(agent=SimpleNamespace(sandbox="off"))
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=config):
            configured = sandbox_tiers.configured_sandbox_mode()
        assert configured == "off"
        assert sandbox_tiers.effective_sandbox_mode(configured) == "cc"


class TestALocalTierCanNarrow:
    @pytest.mark.parametrize("tier", _TIERS)
    def test_narrowing_holds(self, boot, tier, tmp_path, monkeypatch, profiles_dir):
        _local_tier(tier, _NARROWING, tmp_path, monkeypatch, profiles_dir)
        boot()
        assert not _permits("capabilities.theme_persona"), tier
        assert not _permits("network.egress", "docs.corp.example"), tier
        assert _permits("network.egress", "api.corp.example"), tier

    @pytest.mark.parametrize("tier", _TIERS)
    def test_a_stricter_local_sandbox_floor_wins(
        self, boot, tier, tmp_path, monkeypatch, profiles_dir
    ):
        _local_tier(tier, {"sandbox": {"min_level": "strict"}}, tmp_path, monkeypatch, profiles_dir)
        boot()
        assert gp.governance_floor_ordinal("sandbox.min_level", session_key=_HOST) == "strict"
        if tier != "profile":
            # A profile is per surface; the session-less spawn floor reads the ceiling.
            assert sandbox_tiers.effective_sandbox_mode("standard") == "strict"


class TestOssIgnoresTheRemotePath:
    def test_the_fetch_is_refused_without_calling_the_service(self, signed_in, cps):
        with pytest.raises(PlatformCompositionError, match="only in an LTS build"):
            epf.fetch(pd.FetchRequest(url=pd.ENTERPRISE_POLICY_URL))
        assert cps == []

    def test_boot_reads_no_remote_policy(self, signed_in, cps):
        assert governance.load_security_policy() is None
        assert cps == []
