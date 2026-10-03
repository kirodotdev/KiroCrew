"""Branch coverage for mediated-request policy provenance.

The policy MAC derives from a gateway-created root in a sandbox-hidden
directory. The root carries a certificate under the independently hidden
dashboard token key, so an agent cannot make a planted key authoritative.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from kiro_crew.secrets_mediation import provenance
from kiro_crew.secrets_mediation.provenance import (
    initialize_host_key,
    sign_authorizations,
    verify_authorizations,
)

_AUTHZ = {"WEATHER_API_KEY": {"origin": "https://api.example", "placement": {"type": "bearer"}}}


def _install_host_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, token_root: bytes = b"T" * 32
) -> bytes:
    monkeypatch.setattr(provenance, "_token_root", lambda: token_root)
    return initialize_host_key(tmp_path)


def test_sign_then_verify_round_trips_from_the_host_root(tmp_path, monkeypatch):
    _install_host_root(tmp_path, monkeypatch)
    sig = sign_authorizations(_AUTHZ, tmp_path)
    assert verify_authorizations(_AUTHZ, sig, tmp_path) is True


def test_derived_key_is_stable_and_not_the_raw_host_root(tmp_path, monkeypatch):
    root = _install_host_root(tmp_path, monkeypatch)
    k1 = provenance._member_key(tmp_path)
    k2 = provenance._member_key(tmp_path)
    assert k1 == k2 and k1 is not None
    assert k1 != root


def test_verify_fails_closed_on_missing_or_typewrong_signature(tmp_path, monkeypatch):
    _install_host_root(tmp_path, monkeypatch)
    assert verify_authorizations(_AUTHZ, "", tmp_path) is False
    assert verify_authorizations(_AUTHZ, None, tmp_path) is False
    assert verify_authorizations(_AUTHZ, 12345, tmp_path) is False


def test_verify_fails_closed_when_the_host_root_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "_token_root", lambda: b"T" * 32)
    assert verify_authorizations(_AUTHZ, "0" * 64, tmp_path) is False
    assert not (tmp_path / provenance.HOST_KEY_DIRNAME).exists()


def test_verify_fails_closed_on_a_malformed_host_root(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "_token_root", lambda: b"T" * 32)
    key_dir = tmp_path / provenance.HOST_KEY_DIRNAME
    key_dir.mkdir()
    (key_dir / "key.json").write_text("{}", encoding="utf-8")
    assert verify_authorizations(_AUTHZ, "0" * 64, tmp_path) is False


def test_tampering_with_any_entry_invalidates_the_signature(tmp_path, monkeypatch):
    _install_host_root(tmp_path, monkeypatch)
    sig = sign_authorizations(_AUTHZ, tmp_path)
    tampered = {
        "WEATHER_API_KEY": {"origin": "https://evil.example", "placement": {"type": "bearer"}}
    }
    assert verify_authorizations(tampered, sig, tmp_path) is False


def test_sign_raises_and_does_not_mint_when_the_host_key_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "_token_root", lambda: b"T" * 32)
    with pytest.raises(RuntimeError, match="start the gateway"):
        sign_authorizations(_AUTHZ, tmp_path)
    assert not (tmp_path / provenance.HOST_KEY_DIRNAME).exists()


def test_gateway_refuses_an_uncertified_preseeded_key(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "_token_root", lambda: b"T" * 32)
    key_dir = tmp_path / provenance.HOST_KEY_DIRNAME
    key_dir.mkdir()
    planted = json.dumps({"key": (b"A" * 32).hex(), "cert": "0" * 64}) + "\n"
    key_path = key_dir / "key.json"
    key_path.write_text(planted, encoding="utf-8")

    with pytest.raises(RuntimeError, match="not gateway-certified"):
        initialize_host_key(tmp_path)

    assert key_path.read_text(encoding="utf-8") == planted


def test_signature_derived_from_the_sel_key_is_rejected(tmp_path, monkeypatch):
    _install_host_root(tmp_path, monkeypatch)
    sel_root = b"S" * 32
    legacy_subkey = hmac.new(
        sel_root, b"kirocrew.secret_request_policy.sig.v1", hashlib.sha256
    ).digest()
    legacy_signature = hmac.new(
        legacy_subkey, provenance._canonical_payload(_AUTHZ), hashlib.sha256
    ).hexdigest()

    assert verify_authorizations(_AUTHZ, legacy_signature, tmp_path) is False


def test_host_root_is_hidden_and_precreated():
    from kiro_crew import sandbox
    from kiro_crew.security import paths as security_paths

    leaf = provenance.HOST_KEY_DIRNAME
    assert leaf in security_paths._CREW_SECRET_LEAVES
    assert leaf in sandbox._CREW_HIDDEN_LEAVES
    assert leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert leaf not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES


def test_gateway_task_mints_the_host_root_off_loop_after_bind():
    import inspect

    from kiro_crew.dashboard.token_auth import mint_mediation_signing_root, warm_auth_singletons

    # The signing root is minted by a dedicated gateway-owned coroutine, which is
    # the single authorized minting site and runs the mint off the event loop.
    mint_source = inspect.getsource(mint_mediation_signing_root)
    assert "await asyncio.to_thread(initialize_host_key)" in mint_source

    # It must NOT be awaited inside warm_auth_singletons: that helper runs on the
    # boot path BEFORE the listener binds, so minting there (which touches the
    # data home) would let a stalled filesystem block the bind
    # (no-new-work-on-gateway-boot-path).
    warm_source = inspect.getsource(warm_auth_singletons)
    assert "initialize_host_key" not in warm_source


def test_warm_auth_singletons_does_not_touch_the_host_root():
    import inspect

    from kiro_crew.dashboard import server as server_mod
    from kiro_crew.dashboard.token_auth import warm_auth_singletons

    # Both gateway entrypoints kick the mint as a post-bind background task, so
    # the listener never waits on key creation.
    assert "initialize_host_key" not in inspect.getsource(warm_auth_singletons)
    server_source = inspect.getsource(server_mod)
    assert server_source.count("_kick_mediation_signing_root_mint(state)") == 2


def test_mediation_is_fail_closed_until_the_mint_task_runs(tmp_path, monkeypatch) -> None:
    """Deferring the mint past the bind must not open a window: with the root not
    yet minted, every mediated-secret signature verification refuses, and once the
    gateway's mint task runs the same payload verifies. This is what keeps
    security intact while the bind is never blocked by key creation.
    """
    import asyncio

    import kiro_crew.dashboard.token_auth as _ta
    import kiro_crew.secrets_mediation.provenance as _prov

    # initialize_host_key resolves its data home through the module-level
    # resolve_config_dir binding, so point that at the test home.
    monkeypatch.setattr(_prov, "resolve_config_dir", lambda: tmp_path)

    payload = {"TOKEN": {"origin": "https://api.example.com"}}

    # BEFORE the mint: no root on this data home -> _member_key is None -> refuse.
    assert verify_authorizations(payload, "0" * 64, tmp_path) is False

    # The gateway's deferred task mints the root (off-loop), the only authorized
    # minting site.
    asyncio.run(_ta.mint_mediation_signing_root())

    # AFTER the mint: a signature produced under the now-present root verifies,
    # and a forged one still fails.
    good = sign_authorizations(payload, tmp_path)
    assert verify_authorizations(payload, good, tmp_path) is True
    assert verify_authorizations(payload, "0" * 64, tmp_path) is False
