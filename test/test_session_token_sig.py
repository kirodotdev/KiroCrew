"""Tests for :mod:`kiro_crew.session_token_sig` — the signed token -> key mapping.

The mapping file lives in ``config_dir()``, which is same-uid agent-writable, so
the strict identity path must not trust it bare. These tests lock in the contract:
publication writes ONE MAC-bearing file, verification accepts only a pair that
matches the token PRESENTED (not the filename), and every tamper/degradation path
fails closed to ``""``.

The domain-separation test is the one that is easy to under-value: both this
protocol and ``session_pid_sig`` sign a ``"<identifier>:<session_key>"`` message
under a subkey of the same trust root, so without distinct labels a pid sidecar
for pid ``N`` would verify as a token sidecar for a token spelled ``N``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import session_pid_sig, session_token_sig

SESSION_KEY = "dashboard:chat-7-123456"
OTHER_KEY = "dashboard:chat-9-999999"
TOKEN = "a" * 64
OTHER_TOKEN = "b" * 64
LOGGER_NAME = "kiro_crew.session_token_sig"


def _path_for(cfg, token: str):
    return cfg / f"session_token_{hashlib.sha256(token.encode()).hexdigest()}.sig"


@pytest.fixture
def cfg(tmp_path):
    """Isolated mapping dir with a valid SEL trust-root key.

    Four patches, and each one is needed because the module deliberately SHARES
    two helpers with :mod:`kiro_crew.session_pid_sig` rather than copying them:

    * ``session_token_sig.config_dir`` — where this protocol's files go;
    * ``session_pid_sig.sel_hmac_key_path`` — read by the SHARED
      ``_load_hmac_key``, so patching only this module's copy would leave the
      loader resolving the real trust root;
    * ``session_token_sig.sel_hmac_key_path`` — read by this module's own
      trust-root-absent warning;
    * ``session_pid_sig._sel_hmac_key_bytes`` -> ``None`` so the tests exercise
      the FILE path in isolation; the in-memory recovery fallback depends on a
      live ``SecurityEventLog`` singleton that other tests in the same process
      may or may not have initialized.
    """
    key_path = tmp_path / "sel_hmac.key"
    key_path.write_bytes(b"\x01" * 32)
    with (
        patch.object(session_token_sig, "config_dir", return_value=tmp_path),
        patch.object(session_pid_sig, "sel_hmac_key_path", return_value=key_path),
        patch.object(session_token_sig, "sel_hmac_key_path", return_value=key_path),
        patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=None),
    ):
        session_pid_sig._reported.clear()
        yield tmp_path
        session_pid_sig._reported.clear()


class TestRoundTrip:
    def test_retract_removes_only_the_named_mapping_and_empty_is_a_noop(self, cfg, caplog):
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        session_token_sig.publish_session_token(OTHER_TOKEN, OTHER_KEY)
        assert _path_for(cfg, TOKEN).exists()
        session_token_sig.retract_session_token(TOKEN)
        assert not _path_for(cfg, TOKEN).exists()
        assert session_token_sig.verify_session_token(TOKEN) == ""
        assert session_token_sig.verify_session_token(OTHER_TOKEN) == OTHER_KEY

        caplog.clear()
        session_token_sig.retract_session_token(TOKEN)
        session_token_sig.retract_session_token("")
        assert list(cfg.glob("session_token_*.sig")) == [_path_for(cfg, OTHER_TOKEN)]
        assert not caplog.records

    def test_publish_then_verify_returns_the_key(self, cfg):
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        assert session_token_sig.verify_session_token(TOKEN) == SESSION_KEY

    def test_file_is_named_by_the_token_digest_not_the_token(self, cfg):
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        names = [p.name for p in cfg.glob("session_token_*.sig")]
        assert names == [_path_for(cfg, TOKEN).name]
        # The bearer name must not be recoverable from a directory listing.
        assert TOKEN not in names[0]

    def test_record_is_one_file_holding_mac_then_body(self, cfg):
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        raw = _path_for(cfg, TOKEN).read_text(encoding="utf-8")
        mac, _, body = raw.partition("\n")
        assert body == SESSION_KEY
        assert len(mac) == 64 and all(c in "0123456789abcdef" for c in mac)
        # ONE file: a torn read across a rekey has to be unrepresentable.
        assert list(cfg.glob("session_token_*")) == [_path_for(cfg, TOKEN)]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_owner_only_mode(self, cfg):
        """Owner-only on POSIX.

        Skipped on Windows rather than asserted loosely: NTFS carries an ACL, not
        permission bits, and ``os.chmod`` there moves only the read-only flag — so
        the value reads '666' whatever mode was requested, and an assertion that
        accepted that would assert nothing on either platform.
        """
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        assert oct(_path_for(cfg, TOKEN).stat().st_mode)[-3:] == "600"

    def test_empty_token_or_key_publishes_nothing(self, cfg):
        session_token_sig.publish_session_token("", SESSION_KEY)
        session_token_sig.publish_session_token(TOKEN, "")
        assert list(cfg.glob("session_token_*.sig")) == []
        assert session_token_sig.verify_session_token("") == ""


class TestFailsClosed:
    def test_wrong_token_returns_empty(self, cfg):
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        assert session_token_sig.verify_session_token(OTHER_TOKEN) == ""

    def test_no_mapping_returns_empty(self, cfg):
        assert session_token_sig.verify_session_token(TOKEN) == ""

    def test_tampered_body_returns_empty(self, cfg):
        """An agent edits the session key in place: the MAC does not cover it."""
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        path = _path_for(cfg, TOKEN)
        mac = path.read_text(encoding="utf-8").partition("\n")[0]
        path.write_text(f"{mac}\n{OTHER_KEY}", encoding="utf-8")
        assert session_token_sig.verify_session_token(TOKEN) == ""

    def test_tampered_mac_returns_empty(self, cfg):
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        path = _path_for(cfg, TOKEN)
        body = path.read_text(encoding="utf-8").partition("\n")[2]
        path.write_text(f"{'0' * 64}\n{body}", encoding="utf-8")
        assert session_token_sig.verify_session_token(TOKEN) == ""

    def test_cross_token_replay_returns_empty(self, cfg):
        """REPLAY: copy a valid mapping to the name derived from a token you hold.

        The filename is only an index; the TOKEN is bound into the MAC, so the
        stolen record cannot answer for the attacker's own token.
        """
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        stolen = _path_for(cfg, TOKEN).read_text(encoding="utf-8")
        _path_for(cfg, OTHER_TOKEN).write_text(stolen, encoding="utf-8")
        assert session_token_sig.verify_session_token(OTHER_TOKEN) == ""
        # ...and the original still verifies, so the test cannot pass vacuously.
        assert session_token_sig.verify_session_token(TOKEN) == SESSION_KEY

    def test_malformed_record_returns_empty(self, cfg):
        for content in ("", "\n", "just-a-mac-no-body", f"{'0' * 64}\n"):
            _path_for(cfg, TOKEN).write_text(content, encoding="utf-8")
            assert session_token_sig.verify_session_token(TOKEN) == ""

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX-only open flag")
    def test_symlink_at_the_path_returns_empty(self, cfg):
        """SYMLINK ATTACK: the reader must not follow a planted link.

        The target is given VALID contents, so a reader that followed the link
        would return the key and pass — the assertion only means something
        because the same bytes verify when they are a regular file.
        """
        target = cfg / "planted.txt"
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        real = _path_for(cfg, TOKEN)
        target.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
        assert session_token_sig.verify_session_token(TOKEN) == SESSION_KEY
        real.unlink()
        real.symlink_to(target)
        assert session_token_sig.verify_session_token(TOKEN) == ""

    def test_oversize_file_returns_empty(self, cfg):
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        path = _path_for(cfg, TOKEN)
        good = path.read_text(encoding="utf-8")
        path.write_text(good + "#" * 5000, encoding="utf-8")
        assert session_token_sig.verify_session_token(TOKEN) == ""

    def test_missing_sel_key_refuses_and_warns(self, cfg, caplog):
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        (cfg / "sel_hmac.key").unlink()
        with caplog.at_level("WARNING", logger=LOGGER_NAME):
            assert session_token_sig.verify_session_token(TOKEN) == ""
        assert [r for r in caplog.records if r.name == LOGGER_NAME]

    def test_short_sel_key_refuses(self, cfg):
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        (cfg / "sel_hmac.key").write_bytes(b"\x01" * 8)
        assert session_token_sig.verify_session_token(TOKEN) == ""

    def test_publish_without_sel_key_writes_nothing_and_drops_stale(self, cfg):
        """No trust root: publish nothing rather than something unforgeable-by-nobody.

        Unlike the pid sidecar — which keeps writing a bare ``.txt`` because a
        LENIENT reader contract depends on it — this protocol's only reader
        requires the MAC, so an unsigned file would be unreadable to its consumer
        and forgeable in place.
        """
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        (cfg / "sel_hmac.key").unlink()
        session_token_sig.publish_session_token(TOKEN, OTHER_KEY)
        assert not _path_for(cfg, TOKEN).exists()


class TestDomainSeparation:
    def test_pid_sidecar_mac_never_verifies_as_a_token_sidecar(self, cfg):
        """A pid sidecar's MAC, re-presented as a token record, must be refused.

        Both protocols sign ``"<identifier>:<session_key>"`` under a subkey of the
        SAME root, so the ONLY thing standing between them is the domain label.
        This drives that: a pid mapping is published for pid ``P``, its MAC is
        lifted into a token record for a token spelled ``P``, and verification
        must refuse.
        """
        pid_as_token = "4242"
        with (
            patch.object(session_pid_sig, "config_dir", return_value=cfg),
            patch.object(
                session_pid_sig.platform_compat, "get_process_start_id", return_value=None
            ),
        ):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        pid_mac = (cfg / "session_pid_4242.sig").read_text(encoding="utf-8").strip()
        _path_for(cfg, pid_as_token).write_text(f"{pid_mac}\n{SESSION_KEY}", encoding="utf-8")
        assert session_token_sig.verify_session_token(pid_as_token) == ""

    def test_the_two_subkeys_differ(self, cfg):
        root = b"\x01" * 32
        assert session_token_sig._derive_subkey(root) != session_pid_sig._derive_subkey(root)

    def test_subkey_is_the_documented_derivation(self, cfg):
        """Pin the derivation itself, so a label change is a deliberate rotation.

        Without this a refactor could swap the domain label for the pid module's
        and every other test here would still pass — they only ever compare this
        module against itself.
        """
        root = b"\x01" * 32
        assert (
            session_token_sig._derive_subkey(root)
            == hmac.new(root, b"kirocrew.session_token.sig.v1", hashlib.sha256).digest()
        )


class TestRekey:
    def test_republication_always_leaves_a_consistent_pair(self, cfg):
        """Publish twice for ONE token: the reader never sees a mixed record.

        This is what one file buys. With the MAC and the body in separate files a
        reader could observe the new MAC against the old body mid-rekey and refuse
        a live session; here every observable state is a whole record.
        """
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        first = _path_for(cfg, TOKEN).read_text(encoding="utf-8")
        assert session_token_sig.verify_session_token(TOKEN) == SESSION_KEY
        session_token_sig.publish_session_token(TOKEN, OTHER_KEY)
        second = _path_for(cfg, TOKEN).read_text(encoding="utf-8")
        assert first != second
        assert session_token_sig.verify_session_token(TOKEN) == OTHER_KEY
        # Both records are individually valid, which is the property a torn read
        # would break: re-instate the first and it still verifies.
        _path_for(cfg, TOKEN).write_text(first, encoding="utf-8")
        assert session_token_sig.verify_session_token(TOKEN) == SESSION_KEY

    def test_two_sessions_hold_independent_mappings(self, cfg):
        """The spawn_run topology: two sessions, one runtime, one pid.

        Every pid-keyed channel answers with the runtime's owner for both. The
        token mapping is what tells them apart, and nothing about publishing one
        may disturb the other.
        """
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        session_token_sig.publish_session_token(OTHER_TOKEN, OTHER_KEY)
        assert session_token_sig.verify_session_token(TOKEN) == SESSION_KEY
        assert session_token_sig.verify_session_token(OTHER_TOKEN) == OTHER_KEY
        session_token_sig.publish_session_token(TOKEN, "dashboard:reclaimed")
        assert session_token_sig.verify_session_token(OTHER_TOKEN) == OTHER_KEY


def _refuse_only(target: Path, error: OSError):
    """A patch side effect that fails for *target* alone and passes everything else.

    Blanket-patching ``Path.unlink`` would also break the trust-root read inside
    :func:`publish_session_token`, and a publication that stopped there would take
    the missing-key branch instead of the write branch -- the test would pass
    without ever reaching the code it is about. ``autospec=True`` keeps ``self``,
    which is what makes the narrowing possible.
    """

    def _side_effect(self, *args, **kwargs):
        if self == target:
            raise error
        return _side_effect.original(self, *args, **kwargs)

    return _side_effect


class TestFailedPublicationInvalidates:
    """A publication that does not succeed must not leave the last one standing.

    The distinction that makes this security-class rather than a lost update:
    :func:`publish_session_token` runs on every ``rekey()``, so the record a failed
    write could not replace names the session the process served BEFORE the claim.
    Left in place it is not stale data a reader can shrug off -- it is a valid MAC
    over a WRONG session, and the strict resolver reads the mapping above the env
    var precisely because the env var is the one that goes stale. So the safe state
    after a failure is absent, never previous.
    """

    def test_failed_rekey_write_does_not_leave_the_previous_session_standing(self, cfg):
        """The reported case: ENOSPC mid-rekey must not preserve the old identity."""
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        assert session_token_sig.verify_session_token(TOKEN) == SESSION_KEY

        with patch.object(
            session_token_sig, "atomic_write", side_effect=OSError(28, "No space left")
        ):
            session_token_sig.publish_session_token(TOKEN, OTHER_KEY)

        # Not OTHER_KEY -- the write failed, so that mapping was never published.
        # The point is that it is not SESSION_KEY either: a rekeyed caller must not
        # authenticate as the session this process served a moment ago.
        assert session_token_sig.verify_session_token(TOKEN) == ""
        assert not _path_for(cfg, TOKEN).exists()

    def test_a_sibling_session_mapping_survives_the_failure(self, cfg):
        """Invalidation is scoped to the token whose publication failed."""
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        session_token_sig.publish_session_token(OTHER_TOKEN, OTHER_KEY)

        with patch.object(
            session_token_sig, "atomic_write", side_effect=OSError(28, "No space left")
        ):
            session_token_sig.publish_session_token(TOKEN, "dashboard:reclaimed")

        assert session_token_sig.verify_session_token(TOKEN) == ""
        assert session_token_sig.verify_session_token(OTHER_TOKEN) == OTHER_KEY

    def test_a_refused_unlink_warns_and_still_returns(self, cfg, caplog):
        """The honest residual: unfixable on disk, so it is made loud instead.

        A refused unlink leaves a verifiable record for the previous session.
        Nothing in this module can undo that safely -- any in-place write to a path
        an agent may control is its own attack surface -- so what is owed is that
        it stops being silent (the caller's own line is debug-level) and that
        publication still never raises into a turn.
        """
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        path = _path_for(cfg, TOKEN)
        refuse_unlink = _refuse_only(path, OSError(13, "Permission denied"))
        refuse_unlink.original = Path.unlink

        with (
            patch.object(
                session_token_sig,
                "atomic_write",
                side_effect=OSError(28, "No space left"),
            ),
            patch.object(Path, "unlink", autospec=True, side_effect=refuse_unlink),
            caplog.at_level("WARNING", logger=LOGGER_NAME),
        ):
            session_token_sig.publish_session_token(TOKEN, OTHER_KEY)

        assert "could not invalidate the identity mapping" in caplog.text
        # The token is a secret and must not reach the log; the digest that names
        # the file may, and is what makes the record findable on disk.
        assert TOKEN not in caplog.text
        assert path.name in caplog.text
        # The record itself is untouched: no fallback write happened.
        assert path.exists()

    def test_a_planted_symlink_is_removed_not_followed(self, cfg, tmp_path):
        """Unlink acts on the link, never its target -- the reason it is the ONLY step.

        The composition this guards: an agent plants a symlink at the mapping path
        so a failed publication's cleanup reaches it. Removing the link is the
        right outcome; touching the target from the unsandboxed gateway would not
        be, and no code path here can.
        """
        target = tmp_path / "governance-ceiling.json"
        target.write_text('{"ceiling": "strict"}', encoding="utf-8")
        path = _path_for(cfg, TOKEN)
        path.symlink_to(target)

        with patch.object(
            session_token_sig, "atomic_write", side_effect=OSError(28, "No space left")
        ):
            session_token_sig.publish_session_token(TOKEN, OTHER_KEY)

        assert not path.exists() and not path.is_symlink()
        assert target.read_text(encoding="utf-8") == '{"ceiling": "strict"}'

    def test_missing_trust_root_invalidates_by_the_same_path(self, cfg):
        """The other failure branch, so the two cannot drift apart.

        A publisher that lost the trust root cannot sign, and a verifier that still
        HAS one would keep accepting the old record -- the trust-root split the
        reader's own warning describes -- so this branch owes the same invalidation
        as the write failure.
        """
        session_token_sig.publish_session_token(TOKEN, SESSION_KEY)
        assert _path_for(cfg, TOKEN).exists()

        with patch.object(session_token_sig, "_load_hmac_key", return_value=None):
            session_token_sig.publish_session_token(TOKEN, OTHER_KEY)

        assert not _path_for(cfg, TOKEN).exists()
