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
import os

import pytest

from kiro_crew.secrets_mediation.policy import (
    POLICY_FILENAME,
    POLICY_STAGING_DIRNAME,
    load_authorization,
)
from kiro_crew.secrets_mediation.provenance import SIGNATURE_FIELD, verify_authorizations


def _run_authorize(monkeypatch, tmp_path, **ns):
    from kiro_crew import cli_commands

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    # Gateway startup creates the certified host-only signing root. A test that
    # exercises the missing-key abort leaves it absent.
    from kiro_crew.secrets_mediation import provenance

    monkeypatch.setattr(provenance, "_token_root", lambda: b"T" * 32)
    if ns.get("with_host_key", True):
        provenance.initialize_host_key(tmp_path)
    args = argparse.Namespace(
        secret_name=ns["secret_name"],
        origin=ns.get("origin", ""),
        header=ns.get("header"),
        remove=ns.get("remove", False),
        reset=ns.get("reset", False),
    )
    cli_commands._handle_secrets_authorize(args)


def test_authorize_stages_policy_inside_wholly_masked_directory(tmp_path, monkeypatch):
    from kiro_crew import cli_commands, sandbox

    observed = []
    real_atomic_write = cli_commands.atomic_write

    def _record(path, *args, **kwargs):
        observed.append(path)
        return real_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(cli_commands, "atomic_write", _record)
    _run_authorize(monkeypatch, tmp_path, secret_name="K", origin="https://api.example.com")

    staging = os.path.realpath(tmp_path / POLICY_STAGING_DIRNAME)
    assert observed
    assert all(os.path.dirname(os.path.realpath(path)) == staging for path in observed)
    assert POLICY_STAGING_DIRNAME in sandbox._CREW_HIDDEN_LEAVES
    assert POLICY_STAGING_DIRNAME in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert list((tmp_path / POLICY_STAGING_DIRNAME).iterdir()) == []
    assert (tmp_path / POLICY_FILENAME).is_file()


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
        _run_authorize(
            monkeypatch,
            tmp_path,
            secret_name="X",
            origin="https://x.example.com",
            with_host_key=False,
        )
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


def test_authorize_refuses_an_oversized_policy_without_unbounded_load(tmp_path, monkeypatch):
    """A planted oversized (or sparse) pre-upgrade policy file must be refused on
    a bounded read BEFORE the JSON decode, so the CLI cannot be driven to OOM by
    an agent-authored file. The read is capped at _MAX_POLICY_BYTES+1 and refused
    on overflow; the file is left untouched (no clobber)."""
    from kiro_crew.secrets_mediation.policy import _MAX_POLICY_BYTES

    policy = tmp_path / POLICY_FILENAME
    # Syntactically valid JSON padded well past the cap, so the ONLY thing that
    # can stop it is the size bound (not a parse error). The value string alone
    # exceeds the ceiling.
    oversized = json.dumps({"authorizations": {}, "pad": "A" * (_MAX_POLICY_BYTES + 1024)})
    assert len(oversized.encode("utf-8")) > _MAX_POLICY_BYTES
    policy.write_text(oversized, encoding="utf-8")
    before = policy.read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        _run_authorize(monkeypatch, tmp_path, secret_name="X", origin="https://api.example.com")
    # The refusal names the size limit, not a parse failure.
    assert "size" in str(exc_info.value).lower()
    # File untouched — refused, not clobbered.
    assert policy.read_text(encoding="utf-8") == before


def test_authorize_refuses_when_the_new_entry_would_exceed_the_write_bound(
    tmp_path, monkeypatch, capsys
):
    """A new authorization that would push the SERIALIZED policy past the reader's
    _MAX_POLICY_BYTES must be refused BEFORE publishing — otherwise the file would
    be written oversize and the reader (which caps its read at that bound) could
    never load it again, losing every authorization. The write bound must match
    the read bound exactly. The existing validly-signed policy stays intact.

    This is distinct from the read-side test above (an agent-planted oversize file
    refused on read): here the oversize is the owner's OWN would-be write.
    """
    from kiro_crew import cli_commands
    from kiro_crew.secrets_mediation import policy as policy_mod
    from kiro_crew.secrets_mediation.provenance import sign_authorizations

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    from kiro_crew.secrets_mediation import provenance

    monkeypatch.setattr(provenance, "_token_root", lambda: b"T" * 32)
    provenance.initialize_host_key(tmp_path)

    # A VALIDLY owner-signed existing policy with one bearer entry. It must verify
    # so the authorize path carries it forward (an unsigned map would be discarded,
    # not grown).
    authorizations = {
        "FIRST": {"origin": "https://first.example.com", "placement": {"type": "bearer"}}
    }
    signed = {
        "version": 1,
        "authorizations": authorizations,
        SIGNATURE_FIELD: sign_authorizations(authorizations, tmp_path),
    }
    serialized = json.dumps(signed, indent=2) + "\n"
    seed_size = len(serialized.encode("utf-8"))

    # Set the bound JUST above the seed — enough that the seed is readable, but
    # too little for a second entry (~90+ bytes). The fix reads the bound as the
    # module-level ``cli_commands._MAX_POLICY_BYTES`` (its new write check AND its
    # read-back cap), and ``load_authorization`` reads ``policy._MAX_POLICY_BYTES``;
    # patch BOTH so the write bound the fix enforces and the read bound the reader
    # enforces move together, exactly as in prod. 30 bytes of headroom is far less
    # than one more entry, so adding a second authorization overflows it.
    small = seed_size + 30
    monkeypatch.setattr(cli_commands, "_MAX_POLICY_BYTES", small)
    monkeypatch.setattr(policy_mod, "_MAX_POLICY_BYTES", small)

    assert seed_size <= small  # seed is readable under the bound
    policy = tmp_path / POLICY_FILENAME
    policy.write_text(serialized, encoding="utf-8")
    assert verify_authorizations(authorizations, signed[SIGNATURE_FIELD], tmp_path)
    before = policy.read_text(encoding="utf-8")

    # Adding one more authorization pushes the serialized policy PAST the small
    # bound, so it must be refused before any write.
    with pytest.raises(SystemExit):
        _run_authorize(
            monkeypatch,
            tmp_path,
            secret_name="SECOND_ONE_TOO_MANY",
            origin="https://second.example.com",
            with_host_key=False,  # key already minted above; do not re-mint
        )
    # Refused for the size limit, before any write (message printed to stderr).
    assert "size limit" in capsys.readouterr().err.lower()
    # The existing policy is left intact and still readable (not clobbered oversize).
    assert policy.read_text(encoding="utf-8") == before
    assert load_authorization(tmp_path, "FIRST") is not None


def test_authorize_aborts_without_writing_when_the_audit_cannot_be_recorded(tmp_path, monkeypatch):
    """A credential-egress permission change must never land unaudited: if the SEL
    audit raises, authorize aborts BEFORE publishing the policy. Fails without the
    audit-before-write + abort-on-failure gate."""
    from kiro_crew import cli_commands

    policy = tmp_path / POLICY_FILENAME
    assert not policy.exists()

    def _boom(*_a, **_k):
        raise OSError("SEL write failed")

    class _FailingSel:
        def log_api_access(self, **_k):
            raise OSError("SEL write failed")

    monkeypatch.setattr(cli_commands, "sel", lambda: _FailingSel())
    with pytest.raises(SystemExit):
        _run_authorize(monkeypatch, tmp_path, secret_name="X", origin="https://api.example.com")
    # The policy was NOT written — the egress permission did not change unaudited.
    assert not policy.exists()


def test_authorize_refuses_a_symlinked_lock(tmp_path, monkeypatch):
    """The authorize lock must be an alias-free regular file: if the lock path is
    a symlink (a sandboxed agent could swap the inode between two concurrent
    authorizations), the run refuses rather than locking a decoy inode and
    dropping an authorization on the last write. The refusal must surface as a
    concise CLI error (SystemExit), never an uncaught RuntimeError/OSError
    traceback."""
    import os

    # Pre-plant the lock path as a symlink to a decoy file.
    lock_path = tmp_path / f".{POLICY_FILENAME}.lock"
    decoy = tmp_path / "decoy-lock"
    decoy.write_text("", encoding="utf-8")
    os.symlink(decoy, lock_path)
    with pytest.raises(SystemExit) as exc_info:
        _run_authorize(monkeypatch, tmp_path, secret_name="X", origin="https://api.example.com")
    assert exc_info.value.code == 1


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


def test_remove_succeeds_without_an_origin_argument(tmp_path, monkeypatch):
    """``kirocrew secrets authorize <name> --remove`` de-authorizes by name alone.

    ``origin`` is an optional positional, so the real argparse path leaves it
    ``None`` when omitted. The remove path never reads it; the authorization is
    dropped and the policy re-signed without the entry, with no ``SystemExit``.
    """
    from kiro_crew import cli_commands

    # Seed a validly-signed authorization, then remove it with origin omitted.
    _run_authorize(monkeypatch, tmp_path, secret_name="GONE", origin="https://api.example.com")
    load_authorization(tmp_path, "GONE")  # present before removal

    # origin=None mirrors the argparse default for an omitted optional positional.
    args = argparse.Namespace(
        secret_name="GONE", origin=None, header=None, remove=True, reset=False
    )
    cli_commands._handle_secrets_authorize(args)  # must NOT raise SystemExit

    with pytest.raises(Exception) as exc_info:
        load_authorization(tmp_path, "GONE")
    # The entry is gone -> not-configured guidance (empty map), never a crash.
    assert "authoriz" in str(exc_info.value).lower()


def test_authorize_without_origin_and_without_remove_exits_2(tmp_path, monkeypatch):
    """Adding an authorization still REQUIRES an origin: an omitted origin
    (``None``) without ``--remove`` is rejected with exit code 2 before any
    policy read/write, mirroring argparse's missing-required-argument exit."""
    from kiro_crew import cli_commands

    args = argparse.Namespace(secret_name="K", origin=None, header=None, remove=False, reset=False)
    with pytest.raises(SystemExit) as exc_info:
        cli_commands._handle_secrets_authorize(args)
    assert exc_info.value.code == 2
    # Nothing was published: no policy file exists.
    assert not (tmp_path / POLICY_FILENAME).exists()
