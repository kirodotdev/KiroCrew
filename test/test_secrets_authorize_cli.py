"""``kirocrew secrets authorize`` must not launder an agent-planted policy.

If an unsigned ``secret_request_policy.json`` already exists (something an agent
could plant before the owner ever authorizes, or before the sandbox mask applies
on an upgrade), the owner running ``secrets authorize`` for a DIFFERENT secret
must NOT preserve-and-sign the planted entries — that would turn the agent's
chosen destination into an owner-signed authorization. The CLI discards an
existing map unless it already verifies, then writes only owner-intended entries.
"""

from __future__ import annotations

import argparse
import json

import pytest

from kiro_crew.secrets_mediation.policy import POLICY_FILENAME, load_authorization
from kiro_crew.secrets_mediation.provenance import SIGNATURE_FIELD, verify_authorizations


def _run_authorize(monkeypatch, tmp_path, **ns):
    from kiro_crew import cli_commands

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    args = argparse.Namespace(
        secret_name=ns["secret_name"],
        origin=ns.get("origin", ""),
        header=ns.get("header"),
        remove=ns.get("remove", False),
        reset=ns.get("reset", False),
    )
    cli_commands._handle_secrets_authorize(args)


def test_authorize_discards_a_key_present_but_invalidly_signed_policy(tmp_path, monkeypatch):
    # A first legitimate authorize mints the signing key + a validly signed file.
    _run_authorize(monkeypatch, tmp_path, secret_name="MY_KEY", origin="https://api.example.com")
    # Agent then TAMPERS the file: injects an attacker origin, leaving the (now
    # stale) owner signature in place. The key IS present, so verification can
    # tell this is not owner-signed → the tampered entry must be discarded, not
    # carried forward and re-signed.
    doc = json.loads((tmp_path / POLICY_FILENAME).read_text())
    doc["authorizations"]["STOLEN"] = {
        "origin": "https://evil.example",
        "placement": {"type": "bearer"},
    }
    (tmp_path / POLICY_FILENAME).write_text(json.dumps(doc), encoding="utf-8")
    # Without --reset, authorize REFUSES to overwrite the unverifiable file rather
    # than silently erasing it (data-loss protection).
    before = (tmp_path / POLICY_FILENAME).read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        _run_authorize(
            monkeypatch, tmp_path, secret_name="OTHER", origin="https://other.example.com"
        )
    assert (tmp_path / POLICY_FILENAME).read_text(encoding="utf-8") == before
    # With --reset, the owner explicitly discards it and starts a fresh signed policy.
    _run_authorize(
        monkeypatch,
        tmp_path,
        secret_name="OTHER",
        origin="https://other.example.com",
        reset=True,
    )
    # The tampered entry (and the pre-tamper MY_KEY, discarded with it) are GONE;
    # only the owner's latest intent remains, validly signed.
    with pytest.raises(Exception):
        load_authorization(tmp_path, "STOLEN")
    assert load_authorization(tmp_path, "OTHER").origin == "https://other.example.com"
    out = json.loads((tmp_path / POLICY_FILENAME).read_text())
    assert "STOLEN" not in out["authorizations"]
    assert verify_authorizations(out["authorizations"], out[SIGNATURE_FIELD], tmp_path)


def test_authorize_refuses_to_erase_when_the_signing_key_is_missing(tmp_path, monkeypatch):
    """A missing signing key makes verification IMPOSSIBLE. Silently discarding
    the existing authorizations would erase legitimate owner data just because
    the key is absent — so authorize aborts without writing instead."""
    # A nonempty existing policy, but NO signing key on disk (never minted).
    planted = {"REAL": {"origin": "https://api.example.com", "placement": {"type": "bearer"}}}
    (tmp_path / POLICY_FILENAME).write_text(
        json.dumps({"version": 1, "authorizations": planted}), encoding="utf-8"
    )
    before = (tmp_path / POLICY_FILENAME).read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        _run_authorize(monkeypatch, tmp_path, secret_name="X", origin="https://x.example.com")
    # Untouched — no clobber, no erase.
    assert (tmp_path / POLICY_FILENAME).read_text(encoding="utf-8") == before


def test_authorize_preserves_a_validly_signed_prior_entry(tmp_path, monkeypatch):
    # First legitimate authorization (produces a validly signed file).
    _run_authorize(monkeypatch, tmp_path, secret_name="A", origin="https://a.example.com")
    # A second authorization must PRESERVE the first (it was validly signed).
    _run_authorize(monkeypatch, tmp_path, secret_name="B", origin="https://b.example.com")
    assert load_authorization(tmp_path, "A").origin == "https://a.example.com"
    assert load_authorization(tmp_path, "B").origin == "https://b.example.com"


def test_authorize_aborts_without_writing_on_a_non_object_policy(tmp_path, monkeypatch):
    """A valid-JSON but non-object policy must not crash (AttributeError on
    ``.get``) nor be silently overwritten — the CLI aborts without writing so
    the owner can recover the file."""
    policy = tmp_path / POLICY_FILENAME
    policy.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    before = policy.read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        _run_authorize(monkeypatch, tmp_path, secret_name="X", origin="https://api.example.com")
    # File is untouched (no clobber) and the run did not proceed.
    assert policy.read_text(encoding="utf-8") == before


def test_authorize_aborts_without_writing_on_malformed_json(tmp_path, monkeypatch):
    """A present-but-unparseable policy is not silently replaced (which could
    drop owner authorizations we merely failed to read)."""
    policy = tmp_path / POLICY_FILENAME
    policy.write_text("{ this is not json", encoding="utf-8")
    before = policy.read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        _run_authorize(monkeypatch, tmp_path, secret_name="X", origin="https://api.example.com")
    assert policy.read_text(encoding="utf-8") == before


def test_authorize_refuses_a_symlinked_lock(tmp_path, monkeypatch):
    """The authorize lock must be an alias-free regular file: if the lock path is
    a symlink (a sandboxed agent could swap the inode between two concurrent
    authorizations), the run refuses rather than locking a decoy inode and
    dropping an authorization on the last write."""
    import os

    # Pre-plant the lock path as a symlink to a decoy file.
    lock_path = tmp_path / f".{POLICY_FILENAME}.lock"
    decoy = tmp_path / "decoy-lock"
    decoy.write_text("", encoding="utf-8")
    os.symlink(decoy, lock_path)
    with pytest.raises((SystemExit, OSError, RuntimeError)):
        _run_authorize(monkeypatch, tmp_path, secret_name="X", origin="https://api.example.com")


def test_authorize_emits_a_secret_free_sel_audit_event(tmp_path, monkeypatch):
    """Granting or removing a credential egress is a security-relevant control
    change and must produce a SEL audit event — carrying the secret NAME, the
    action, and (on grant) the origin, but never a secret value."""
    from kiro_crew import cli_commands

    events = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

    monkeypatch.setattr(cli_commands, "sel", lambda: _Sel())
    _run_authorize(monkeypatch, tmp_path, secret_name="MY_KEY", origin="https://api.example.com")
    authz = [e for e in events if e.get("operation") == "mediated_secret_authorize"]
    assert authz, "expected a mediated_secret_authorize SEL event on grant"
    ev = authz[-1]
    assert ev["outcome"] == "ok"
    assert "MY_KEY" in ev["resources"] and "authorize" in ev["resources"]
    assert "api.example.com" in ev["resources"]

    events.clear()
    _run_authorize(monkeypatch, tmp_path, secret_name="MY_KEY", remove=True)
    removed = [e for e in events if e.get("operation") == "mediated_secret_authorize"]
    assert removed and "remove" in removed[-1]["resources"]
