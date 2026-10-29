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
import stat

import pytest

from conftest import requires_symlinks
from kiro_crew import cli_commands_secrets
from kiro_crew.secrets_mediation.policy import (
    POLICY_LOCK_FILENAME,
    load_authorization,
    policy_path,
)
from kiro_crew.secrets_mediation.provenance import SIGNATURE_FIELD, verify_secret_authorizations

#: Captured before the autouse fixture pins it, for the tests of the predicate itself.
_REAL_PREDICATE = cli_commands_secrets._spawn_precreates_policy_directory


def _run_authorize(monkeypatch, tmp_path, **ns):
    from kiro_crew import cli_commands

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    # The policy key derives from the token-signing secret. A test that exercises
    # the unreadable-secret abort makes that read fail.
    from kiro_crew.secrets_mediation import provenance

    if ns.get("real_token_root", False):
        pass  # the caller arranged the shared loader itself
    elif ns.get("with_host_key", True):
        monkeypatch.setattr(provenance, "_token_root", lambda: b"T" * 32)
    else:

        def _unreadable() -> bytes:
            raise OSError("token_signing.key is unreadable")

        monkeypatch.setattr(provenance, "_token_root", _unreadable)
    args = argparse.Namespace(
        name=ns["secret_name"],
        origin=ns.get("origin", ""),
        header=ns.get("header"),
        remove=ns.get("remove", False),
        reset=ns.get("reset", False),
    )
    cli_commands._handle_secrets_authorize(args)


@pytest.fixture(autouse=True)
def _gateway_owned_policy_directory(tmp_path, _floor_monkeypatch):
    """The gateway precreates the hidden directory before any spawn; so do we.

    Pinned to the namespace backend (the CLI never creates the directory there), so
    no test here probes the host's real sandbox backend. Tests of the other
    backends override the pin.
    """
    from kiro_crew import cli_commands_secrets

    _floor_monkeypatch.setattr(
        cli_commands_secrets, "_spawn_precreates_policy_directory", lambda: True
    )
    policy_path(tmp_path).parent.mkdir(mode=0o700, exist_ok=True)


def test_authorize_publishes_only_inside_the_hidden_policy_directory(tmp_path, monkeypatch):
    """Every name the publication creates, temps included, sits inside the wholly
    masked directory, so a running sandbox's mask is never on a renamed dentry.

    A rename over a masked FILE name lazily detaches that mount in every running
    namespace -- the agent would then see the real policy and could replay a
    revoked one. Mutation guard: publishing to a data-home-root name (or staging a
    temp there) leaves a new name beside ``secret-request-policy`` and fails here.
    """
    from kiro_crew import sandbox
    from kiro_crew.secrets_mediation.provenance import REQUEST_POLICY_DIRNAME

    root_before = set(os.listdir(tmp_path))
    _run_authorize(monkeypatch, tmp_path, secret_name="K", origin="https://api.example.com")
    _run_authorize(monkeypatch, tmp_path, secret_name="L", origin="https://other.example.com")
    _run_authorize(monkeypatch, tmp_path, secret_name="K", remove=True)
    assert set(os.listdir(tmp_path)) == root_before
    assert policy_path(tmp_path).parent.name == REQUEST_POLICY_DIRNAME
    assert REQUEST_POLICY_DIRNAME in sandbox._CREW_HIDDEN_LEAVES
    assert REQUEST_POLICY_DIRNAME in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    doc = json.loads(policy_path(tmp_path).read_text(encoding="utf-8"))
    assert set(doc["authorizations"]) == {"L"}
    assert verify_secret_authorizations(doc["authorizations"], doc[SIGNATURE_FIELD])


def test_authorize_refuses_without_the_gateway_owned_directory(tmp_path, monkeypatch):
    """The CLI never creates the hidden directory: one it created while a sandbox
    was running would be visible, unmasked, inside that sandbox."""
    from kiro_crew import cli_commands

    policy_path(tmp_path).parent.rmdir()
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    args = argparse.Namespace(
        secret_name="K", origin="https://api.example.com", header=None, remove=False, reset=False
    )
    with pytest.raises(SystemExit) as exc_info:
        cli_commands._handle_secrets_authorize(args)
    assert exc_info.value.code == 1
    assert not policy_path(tmp_path).parent.exists()


def test_authorize_creates_the_directory_where_no_namespace_mask_needs_it(tmp_path, monkeypatch):
    """macOS Seatbelt denies by path (a name that does not exist yet is still
    denied) and a sandbox-off host masks nothing, so neither backend ever
    precreates the directory. There the CLI creates it, owner-only, and publishes.
    Mutation guard: without the create, authorize exits 1 on every run here."""
    from kiro_crew import cli_commands_secrets

    policy_path(tmp_path).parent.rmdir()
    monkeypatch.setattr(cli_commands_secrets, "_spawn_precreates_policy_directory", lambda: False)
    _run_authorize(monkeypatch, tmp_path, secret_name="K", origin="https://api.example.com")
    directory = policy_path(tmp_path).parent
    assert directory.is_dir() and not directory.is_symlink()
    if os.name == "posix":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert load_authorization(tmp_path, "K").origin == "https://api.example.com"


@requires_symlinks
def test_authorize_never_creates_through_a_planted_link(tmp_path, monkeypatch):
    """A link squatting the name is not followed and not replaced: the CLI refuses."""
    from kiro_crew import cli_commands_secrets

    directory = policy_path(tmp_path).parent
    directory.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    directory.symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setattr(cli_commands_secrets, "_spawn_precreates_policy_directory", lambda: False)
    with pytest.raises(SystemExit) as exc_info:
        _run_authorize(monkeypatch, tmp_path, secret_name="K", origin="https://api.example.com")
    assert exc_info.value.code == 1
    assert directory.is_symlink() and list(elsewhere.iterdir()) == []


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("namespace", True), ("sandbox-exec", False), ("none", False)],
)
def test_the_precreate_predicate_follows_the_spawn_backend(monkeypatch, backend, expected):
    from kiro_crew import cli_commands_secrets, sandbox

    # Drop the autouse pin: this exercises the real predicate.
    monkeypatch.setattr(cli_commands_secrets, "_spawn_precreates_policy_directory", _REAL_PREDICATE)
    seen: list[str] = []
    # Through the ``kiro_crew.sandbox`` facade, which reaches every owner holding each name.
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "standard")
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda m: m)

    def _detect(config_mode="auto"):
        seen.append(config_mode)
        return backend

    monkeypatch.setattr(sandbox, "detect_backend", _detect)
    assert cli_commands_secrets._spawn_precreates_policy_directory() is expected
    assert seen == ["standard"]


def test_the_precreate_predicate_fails_closed(monkeypatch):
    """An unanswerable backend question keeps the refuse-unless-precreated path."""
    from kiro_crew import cli_commands_secrets, sandbox

    monkeypatch.setattr(cli_commands_secrets, "_spawn_precreates_policy_directory", _REAL_PREDICATE)

    def _boom(config_mode="auto"):
        raise OSError("probe failed")

    monkeypatch.setattr(sandbox, "detect_backend", _boom)
    assert cli_commands_secrets._spawn_precreates_policy_directory() is True


def test_authorize_reset_recovers_a_torn_policy(tmp_path, monkeypatch):
    """A crash mid-rewrite can leave unparseable bytes; ``--reset`` is the way back."""
    policy = policy_path(tmp_path)
    policy.write_text('{"version": 1, "authoriz', encoding="utf-8")
    with pytest.raises(SystemExit):
        _run_authorize(monkeypatch, tmp_path, secret_name="K", origin="https://api.example.com")
    _run_authorize(
        monkeypatch, tmp_path, secret_name="K", origin="https://api.example.com", reset=True
    )
    assert load_authorization(tmp_path, "K").origin == "https://api.example.com"


def test_authorize_discards_a_key_present_but_invalidly_signed_policy(tmp_path, monkeypatch):
    # A first legitimate authorize mints the signing key + a validly signed file.
    _run_authorize(monkeypatch, tmp_path, secret_name="MY_KEY", origin="https://api.example.com")
    # Agent then TAMPERS the file: injects an attacker origin, leaving the (now
    # stale) owner signature in place. The key IS present, so verification can
    # tell this is not owner-signed → the tampered entry must be discarded, not
    # carried forward and re-signed.
    doc = json.loads((policy_path(tmp_path)).read_text())
    doc["authorizations"]["STOLEN"] = {
        "origin": "https://evil.example",
        "placement": {"type": "bearer"},
    }
    (policy_path(tmp_path)).write_text(json.dumps(doc), encoding="utf-8")
    # Without --reset, authorize REFUSES to overwrite the unverifiable file rather
    # than silently erasing it (data-loss protection).
    before = (policy_path(tmp_path)).read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        _run_authorize(
            monkeypatch, tmp_path, secret_name="OTHER", origin="https://other.example.com"
        )
    assert (policy_path(tmp_path)).read_text(encoding="utf-8") == before
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
    out = json.loads((policy_path(tmp_path)).read_text())
    assert "STOLEN" not in out["authorizations"]
    assert verify_secret_authorizations(out["authorizations"], out[SIGNATURE_FIELD])


def test_authorize_refuses_to_erase_when_the_signing_secret_is_unreadable(tmp_path, monkeypatch):
    """An unreadable token-signing secret makes verification IMPOSSIBLE. Silently
    discarding the existing authorizations would erase legitimate owner data just
    because the secret cannot be read — so authorize aborts without writing."""
    # A nonempty existing policy, and a token-signing secret that cannot be read.
    planted = {"REAL": {"origin": "https://api.example.com", "placement": {"type": "bearer"}}}
    (policy_path(tmp_path)).write_text(
        json.dumps({"version": 1, "authorizations": planted}), encoding="utf-8"
    )
    before = (policy_path(tmp_path)).read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        _run_authorize(
            monkeypatch,
            tmp_path,
            secret_name="X",
            origin="https://x.example.com",
            with_host_key=False,
        )
    # Untouched — no clobber, no erase.
    assert (policy_path(tmp_path)).read_text(encoding="utf-8") == before


def test_first_authorize_publishes_nothing_on_an_ephemeral_signing_key(tmp_path, monkeypatch):
    """With ``token_signing.key`` unreadable the shared loader hands back a
    process-local random key. Signing the FIRST policy with it would report
    success while the gateway (a different process) refuses the signature, and
    fixing the key file later would not repair it. So authorize must refuse and
    publish nothing. Mutation guard: provenance reading ``_get_secret`` publishes."""
    from kiro_crew.dashboard import token_secret

    def _ephemeral(outcome=None):
        if outcome is not None:
            outcome.append("ephemeral")
        return b"E" * 32

    monkeypatch.setattr(token_secret, "_SECRET", None)
    monkeypatch.setattr(token_secret, "_EPHEMERAL_SECRET", None)
    monkeypatch.setattr(token_secret, "_load_or_create_secret", _ephemeral)
    with pytest.raises(SystemExit) as exc:
        _run_authorize(
            monkeypatch,
            tmp_path,
            secret_name="X",
            origin="https://x.example.com",
            real_token_root=True,
        )
    assert exc.value.code != 0
    assert not policy_path(tmp_path).exists()


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
    policy = policy_path(tmp_path)
    policy.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    before = policy.read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        _run_authorize(monkeypatch, tmp_path, secret_name="X", origin="https://api.example.com")
    # File is untouched (no clobber) and the run did not proceed.
    assert policy.read_text(encoding="utf-8") == before


def test_authorize_aborts_without_writing_on_malformed_json(tmp_path, monkeypatch):
    """A present-but-unparseable policy is not silently replaced (which could
    drop owner authorizations we merely failed to read)."""
    policy = policy_path(tmp_path)
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

    policy = policy_path(tmp_path)
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
    from kiro_crew.secrets_mediation import policy as policy_mod
    from kiro_crew.secrets_mediation.provenance import sign_secret_authorizations

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    from kiro_crew.secrets_mediation import provenance

    monkeypatch.setattr(provenance, "_token_root", lambda: b"T" * 32)

    # A VALIDLY owner-signed existing policy with one bearer entry. It must verify
    # so the authorize path carries it forward (an unsigned map would be discarded,
    # not grown).
    authorizations = {
        "FIRST": {"origin": "https://first.example.com", "placement": {"type": "bearer"}}
    }
    signed = {
        "version": 1,
        "authorizations": authorizations,
        SIGNATURE_FIELD: sign_secret_authorizations(authorizations),
    }
    serialized = json.dumps(signed, indent=2) + "\n"
    seed_size = len(serialized.encode("utf-8"))

    # Set the bound JUST above the seed — enough that the seed is readable, but
    # too little for a second entry (~90+ bytes). The CLI's write check and read-back
    # cap and ``load_authorization`` all read the one ``policy._MAX_POLICY_BYTES``
    # (the CLI imports it when it runs), so patching it moves the write bound and the
    # read bound together, exactly as in prod. 30 bytes of headroom is far less than
    # one more entry, so adding a second authorization overflows it.
    small = seed_size + 30
    monkeypatch.setattr(policy_mod, "_MAX_POLICY_BYTES", small)

    assert seed_size <= small  # seed is readable under the bound
    policy = policy_path(tmp_path)
    policy.write_text(serialized, encoding="utf-8")
    assert verify_secret_authorizations(authorizations, signed[SIGNATURE_FIELD])
    before = policy.read_text(encoding="utf-8")

    # Adding one more authorization pushes the serialized policy PAST the small
    # bound, so it must be refused before any write.
    with pytest.raises(SystemExit):
        _run_authorize(
            monkeypatch,
            tmp_path,
            secret_name="SECOND_ONE_TOO_MANY",
            origin="https://second.example.com",
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

    policy = policy_path(tmp_path)
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


@requires_symlinks
def test_authorize_refuses_a_symlinked_lock(tmp_path, monkeypatch):
    """The authorize lock must be an alias-free regular file: if the lock path is
    a symlink (a sandboxed agent could swap the inode between two concurrent
    authorizations), the run refuses rather than locking a decoy inode and
    dropping an authorization on the last write. The refusal must surface as a
    concise CLI error (SystemExit), never an uncaught RuntimeError/OSError
    traceback."""
    import os

    # Pre-plant the lock path as a symlink to a decoy file.
    lock_path = policy_path(tmp_path).parent / POLICY_LOCK_FILENAME
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
    assert ev["resources"] == "secret=MY_KEY action=authorize origin=https://api.example.com"

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
    args = argparse.Namespace(name="GONE", origin=None, header=None, remove=True, reset=False)
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
    assert not (policy_path(tmp_path)).exists()
