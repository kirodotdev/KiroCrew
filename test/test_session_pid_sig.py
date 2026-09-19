"""Tests for :mod:`kiro_crew.session_pid_sig` — signed session_pid publication.

Publication writes the mapping TWICE, and the two copies have different jobs.
The copy in the data-home root is same-uid agent-writable and attribution only:
the lenient resolvers read it, and a wrong answer there mislabels an audit line.
The copy under ``session-identity/`` is the one the strict resolvers authorize
on, in a directory ``sandbox.py`` masks from every sandboxed process, signed by
an identity root kept in there with it.

These tests lock in that split: publish writes both copies plus the HMAC sidecar
beside the authoritative one; verify reads the fenced copy alone and fails closed
on every tamper/degradation path; and the operator-facing reports name the root
that actually failed, because the audit root and the identity root now break
independently.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat, session_pid_sig

SESSION_KEY = "dashboard:chat-7-123456"
LOGGER_NAME = "kiro_crew.session_pid_sig"


def records_from_this_module(caplog, level="ERROR"):
    """Captured records this module emitted — scoped by LOGGER as well as level.

    ``caplog.at_level(..., logger=LOGGER_NAME)`` scopes the *level* it captures
    at; it does not scope *which* loggers land in ``caplog.records``, which
    still collects everything that propagates to the root handler. So counting
    by level alone makes every assertion below depend on whether an unrelated
    test happened to emit an ERROR inside the same window.

    That is not hypothetical: CI saw nine ``asyncio`` "Task was destroyed but it
    is pending!" records — leaked ``SessionManager._cleanup_loop()`` tasks from
    other tests sharing the xdist worker, reported whenever those task objects
    were collected — turn ``assert len(errors) == 1`` into ``assert 10 == 1``.
    Nothing about this module had changed.

    Scoping by logger name is strictly narrower than scoping by level: these
    assertions still require an exact count, they just do not count other
    people's records as ours.
    """
    return [r for r in caplog.records if r.levelname == level and r.name == LOGGER_NAME]


def break_identity_root(cfg):
    """Leave the identity root present but too short to sign with.

    TRUNCATION, not deletion, because deletion is not a reachable
    cannot-sign state: publication mints the root with an exclusive create, so an
    absent file is created rather than refused. A file that exists and is short
    survives that create (``FileExistsError`` → re-read → still short), which is
    what the degrade paths below have to exercise.
    """
    ident = session_pid_sig.identity_dir(cfg)
    ident.mkdir(mode=0o700, parents=True, exist_ok=True)
    (ident / session_pid_sig._IDENTITY_KEY_FILE).write_bytes(b"\x01" * 8)


def identity_key_bytes(cfg):
    """The identity root's bytes as publication minted them."""
    return (session_pid_sig.identity_dir(cfg) / session_pid_sig._IDENTITY_KEY_FILE).read_bytes()


@pytest.fixture
def cfg(tmp_path):
    """Isolated data home. ``ident`` is the fenced subdirectory inside it.

    A valid SEL trust-root key is written, and ``sel_hmac_key_path`` patched,
    because the SIBLING protocol in ``session_token_sig`` signs with that
    root and this module owns its loader. Identity signing does not touch it:
    publication mints ``session-identity/identity_hmac.key`` on first claim.

    ``_sel_hmac_key_bytes`` is stubbed to ``None`` so the SEL-root tests exercise
    the FILE path in isolation: the in-memory recovery fallback depends on a live
    ``SecurityEventLog`` singleton, which other tests in the same process may or
    may not have initialized. Its own behavior is covered by
    ``TestTrustRootRecovery``.

    ``platform_compat.get_process_start_id`` is pinned to ``None`` (no start
    token) so the fixture is deterministic: the fake pids used here (4242,
    1000, ...) can be LIVE processes on the test host, and a live pid would
    otherwise make ``publish_session_pid`` capture a real start token and
    change the exact ``.txt`` bytes these tests assert on. Recycle-guard
    tests (``TestPidRecycleGuard``) re-patch it per test with controlled
    values.
    """
    (tmp_path / "sel_hmac.key").write_bytes(b"\x01" * 32)
    with (
        patch.object(session_pid_sig, "config_dir", return_value=tmp_path),
        patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=None),
        patch.object(platform_compat, "get_process_start_id", return_value=None),
        patch.object(
            session_pid_sig,
            "sel_hmac_key_path",
            return_value=tmp_path / "sel_hmac.key",
        ),
    ):
        session_pid_sig._reported.clear()
        yield tmp_path
        session_pid_sig._reported.clear()


@pytest.fixture
def ident(cfg):
    """The fenced directory holding the authoritative bindings and their root."""
    return session_pid_sig.identity_dir(cfg)


class TestPublish:
    def test_writes_both_copies_and_the_sig(self, cfg, ident):
        """The authoritative copy carries the sidecar; the attribution copy does not.

        A sidecar beside the attribution copy would be the wrong shape twice over:
        nothing reads it, and its presence would suggest that the agent-writable
        copy is something a caller may authorize on.
        """
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert (ident / "session_pid_4242.txt").read_text(encoding="utf-8") == SESSION_KEY
        sig = (ident / "session_pid_4242.sig").read_text(encoding="utf-8")
        assert len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)
        # The lenient readers' copy, in the root, where it has always been.
        assert (cfg / "session_pid_4242.txt").read_text(encoding="utf-8") == SESSION_KEY
        assert not (cfg / "session_pid_4242.sig").exists()

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="POSIX permission bits; Windows reports 0o777 for a directory",
    )
    def test_the_fenced_directory_is_owner_only(self, cfg, ident):
        """0700. The mask is the control, but a permission bit costs nothing and
        covers the host where the namespace mask could not be applied at all.

        POSIX only, and nothing is lost by that: both halves of this pairing are
        POSIX mechanisms. Windows carries no namespace mask and no mode bits, so
        the fence there rests on the agent file-tool registry alone, which
        ``test_sandbox_governance_mask`` covers on every platform.
        """
        import stat

        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert stat.S_IMODE(ident.stat().st_mode) == 0o700
        key = ident / session_pid_sig._IDENTITY_KEY_FILE
        assert stat.S_IMODE(key.stat().st_mode) == 0o600

    def test_a_failed_mint_leaves_no_key_behind(self, cfg, ident, monkeypatch):
        """A partial write is unlinked, so the next attempt can still mint.

        The create is exclusive, so a short or failed write that left the file in
        place would be seen by every later attempt as "already there", re-read as
        too short, and refused. Strict identity would then fail closed for the life
        of the host with no path back except deleting the file by hand, which is a
        worse outcome than the write error that started it.
        """
        import os as _os

        fail = {"now": True}

        def _maybe_fail_write(fd, data):
            if fail["now"]:
                _os.write(fd, data[:4])
                raise OSError("disk full")
            _os.write(fd, data)

        monkeypatch.setattr(session_pid_sig, "_write_all", _maybe_fail_write)
        assert session_pid_sig._load_identity_key(cfg, create=True) is None
        assert not (ident / session_pid_sig._IDENTITY_KEY_FILE).exists()

        fail["now"] = False
        key = session_pid_sig._load_identity_key(cfg, create=True)
        assert key is not None and len(key) >= session_pid_sig._IDENTITY_KEY_BYTES

    def test_identity_root_is_minted_on_first_claim_and_then_reused(self, cfg, ident):
        """Minted by the publisher, never by a verifier: a first-touch create on
        the verify side would mint a key the publisher never signed with, turning
        a trust-root problem into a silent accept."""
        assert not (ident / session_pid_sig._IDENTITY_KEY_FILE).exists()
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        first = identity_key_bytes(cfg)
        assert len(first) == session_pid_sig._IDENTITY_KEY_BYTES
        session_pid_sig.publish_session_pid(4243, SESSION_KEY)
        assert identity_key_bytes(cfg) == first

    def test_identity_root_is_not_the_audit_root(self, cfg):
        """Separate roots, so a key the sandbox can read cannot sign an identity."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert identity_key_bytes(cfg) != (cfg / "sel_hmac.key").read_bytes()

    def test_publish_without_key_writes_unsigned_and_drops_stale_sig(self, cfg, ident):
        """Identity root unsignable: both txt copies still published (lenient
        readers keep working) but any stale sidecar is removed so a rekeyed
        mapping can never verify against an old signature."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)  # signed
        break_identity_root(cfg)
        session_pid_sig.publish_session_pid(4242, "dashboard:rekeyed")
        assert (ident / "session_pid_4242.txt").read_text(encoding="utf-8") == "dashboard:rekeyed"
        assert not (ident / "session_pid_4242.sig").exists()
        assert (cfg / "session_pid_4242.txt").read_text(encoding="utf-8") == "dashboard:rekeyed"

    def test_rekey_overwrites_both_files(self, cfg, ident):
        session_pid_sig.publish_session_pid(4242, "dashboard:old")
        old_sig = (ident / "session_pid_4242.sig").read_text(encoding="utf-8")
        session_pid_sig.publish_session_pid(4242, "dashboard:new")
        assert session_pid_sig.verify_session_pid(4242) == "dashboard:new"
        assert (ident / "session_pid_4242.sig").read_text(encoding="utf-8") != old_sig

    def test_preplanted_symlink_not_followed(self, cfg, ident):
        """SYMLINK ATTACK: symlinks planted at the predictable mapping paths
        pointing at another writable file. Publication must replace the symlink
        (os.replace semantics), never follow it and truncate the target.

        Still asserted inside the fenced directory even though the mask is what
        keeps an agent out of it: the hardening is what holds on a host where the
        namespace mask could not be applied, and removing it would make the fence
        the only control.
        """
        ident.mkdir(mode=0o700, parents=True, exist_ok=True)
        victim = cfg / "victim.dat"
        victim.write_text("precious", encoding="utf-8")
        for name in ("session_pid_4242.txt", "session_pid_4242.sig"):
            (ident / name).symlink_to(victim)
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        # Victim untouched; both paths are now regular files, not symlinks.
        assert victim.read_text(encoding="utf-8") == "precious"
        assert not (ident / "session_pid_4242.txt").is_symlink()
        assert not (ident / "session_pid_4242.sig").is_symlink()
        assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY


class TestVerify:
    def test_round_trip(self, cfg):
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY
        # str pid (as read from KIROCREW_HOST_PID) verifies identically.
        assert session_pid_sig.verify_session_pid("4242") == SESSION_KEY

    def test_missing_files_refused(self, cfg):
        assert session_pid_sig.verify_session_pid(9999) == ""

    def test_unsigned_txt_refused(self, cfg, ident):
        """FORGERY: bare .txt written without the identity root."""
        ident.mkdir(mode=0o700, parents=True, exist_ok=True)
        (ident / "session_pid_4242.txt").write_text("dashboard:victim", encoding="utf-8")
        assert session_pid_sig.verify_session_pid(4242) == ""

    def test_a_binding_at_the_attribution_path_authorizes_nothing(self, cfg):
        """THE PROPERTY THIS FIX ADDS. The copy an agent can write is not read by
        the resolver that authorizes, so writing one there buys no identity — and
        this holds for a WELL-FORMED binding, not only a malformed one, because a
        malformed one would be refused by parsing and prove nothing."""
        (cfg / "session_pid_4242.txt").write_text(SESSION_KEY, encoding="utf-8")
        assert session_pid_sig.verify_session_pid(4242) == ""
        # The lenient reader does see it: that is the copy's whole purpose.
        assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY

    def test_a_binding_signed_with_the_audit_key_is_refused(self, cfg, ident):
        """The audit root stays readable in the sandbox, so the refusal must NOT
        depend on an attacker being unable to sign. Here it signs a correct
        binding with the key it can read, at the path that authorizes, and is
        still refused — because that key does not sign identities."""
        ident.mkdir(mode=0o700, parents=True, exist_ok=True)
        stolen = (cfg / "sel_hmac.key").read_bytes()
        (ident / "session_pid_4242.txt").write_text(SESSION_KEY, encoding="utf-8")
        (ident / "session_pid_4242.sig").write_text(
            session_pid_sig._compute_sig(stolen, 4242, SESSION_KEY), encoding="utf-8"
        )
        assert session_pid_sig.verify_session_pid(4242) == ""

    def test_tampered_txt_refused(self, cfg, ident):
        """FORGERY: legitimate pair, then the .txt is redirected at another
        slot — the old signature does not match."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (ident / "session_pid_4242.txt").write_text("dashboard:victim", encoding="utf-8")
        assert session_pid_sig.verify_session_pid(4242) == ""

    def test_replayed_pair_under_other_pid_refused(self, cfg, ident):
        """REPLAY: parent's .txt/.sig copied under a different pid — the pid
        is bound into the MAC."""
        session_pid_sig.publish_session_pid(1000, "dashboard:parent")
        for ext in ("txt", "sig"):
            (ident / f"session_pid_2000.{ext}").write_text(
                (ident / f"session_pid_1000.{ext}").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        assert session_pid_sig.verify_session_pid(2000) == ""

    def test_short_key_refused(self, cfg):
        """A truncated/corrupted identity root must not verify anything."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        break_identity_root(cfg)
        assert session_pid_sig.verify_session_pid(4242) == ""

    def test_missing_key_refused(self, cfg, caplog):
        """An unsignable identity root refuses AND emits the trust-root
        diagnostic — distinguishable from the forgery (MAC-mismatch) warning so a
        publisher/verifier root split doesn't silently reproduce the original
        sandboxed-session bug while looking like forgery refusal."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        break_identity_root(cfg)
        with caplog.at_level("WARNING", logger=session_pid_sig.logger.name):
            assert session_pid_sig.verify_session_pid(4242) == ""
        assert any("identity root absent/short" in r.getMessage() for r in caplog.records)

    def test_the_refusal_names_the_identity_root_not_the_audit_root(self, cfg, caplog):
        """An operator sent to the audit key would find it intact and be stuck.
        The two roots fail independently now, so the message names the one that
        did."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        break_identity_root(cfg)
        with caplog.at_level("WARNING", logger=session_pid_sig.logger.name):
            session_pid_sig.verify_session_pid(4242)
        messages = [r.getMessage() for r in caplog.records]
        assert any(session_pid_sig._IDENTITY_KEY_FILE in m for m in messages), messages
        assert not any("sel_hmac.key" in m for m in messages), messages

    def test_symlinked_mapping_files_refused_on_read(self, cfg, ident):
        """READ-SIDE SYMLINK ATTACK: a mapping file swapped for a symlink to a
        sensitive target. The verifier must refuse (O_NOFOLLOW) — never follow
        the link and read the target. Asserted inside the fenced directory so the
        hardening still holds on a host where the namespace mask could not be
        applied, rather than leaving the fence as the only control."""
        secret = cfg / "secret.dat"
        secret.write_text("sensitive-content", encoding="utf-8")
        # Symlinked .txt refused.
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (ident / "session_pid_4242.txt").unlink()
        (ident / "session_pid_4242.txt").symlink_to(secret)
        assert session_pid_sig.verify_session_pid(4242) == ""
        # Symlinked .sig refused (fresh legitimate pair first).
        session_pid_sig.publish_session_pid(5555, SESSION_KEY)
        (ident / "session_pid_5555.sig").unlink()
        (ident / "session_pid_5555.sig").symlink_to(secret)
        assert session_pid_sig.verify_session_pid(5555) == ""

    def test_oversized_mapping_file_refused(self, cfg, ident):
        """RESOURCE ATTACK: a mapping file swapped for a huge one.
        Verification must reject it from fstat size, never buffer it."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (ident / "session_pid_4242.txt").write_text(
            "x" * (session_pid_sig._MAX_MAPPING_FILE_BYTES + 1), encoding="utf-8"
        )
        assert session_pid_sig.verify_session_pid(4242) == ""


class TestLenientReader:
    """``read_session_pid_txt`` is the lenient (unsigned) read for callers
    that tolerate misattribution — but it MUST share the strict verifier's
    hardened read discipline: a planted symlink or non-regular file at the
    predictable agent-writable path is refused, never followed."""

    def test_reads_plain_txt_without_sig(self, cfg):
        (cfg / "session_pid_4242.txt").write_text(SESSION_KEY, encoding="utf-8")
        assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY
        # Explicit cfg passthrough (lenient resolver passes its own dir).
        assert session_pid_sig.read_session_pid_txt("4242", cfg) == SESSION_KEY

    def test_missing_file_returns_empty(self, cfg):
        assert session_pid_sig.read_session_pid_txt(9999) == ""

    def test_symlinked_txt_refused(self, cfg, tmp_path):
        """SYMLINK ATTACK on the lenient path: without the hardened reader a
        plain read_text() in the trusted MCP process would follow this link
        (the read-side twin of the strict-path defense)."""
        secret = tmp_path / "victim-secret"
        secret.write_text("hunter2", encoding="utf-8")
        (cfg / "session_pid_4242.txt").symlink_to(secret)
        assert session_pid_sig.read_session_pid_txt(4242) == ""

    def test_oversized_txt_refused(self, cfg):
        (cfg / "session_pid_4242.txt").write_text(
            "x" * (session_pid_sig._MAX_MAPPING_FILE_BYTES + 1), encoding="utf-8"
        )
        assert session_pid_sig.read_session_pid_txt(4242) == ""


class TestPidRecycleGuard:
    """The mapping and its MAC binding only the pid NUMBER let a recycled pid
    keep verifying and answer with the previous owner's session key until the
    next restart's orphan sweep. Publication also records the process START
    TOKEN (``platform_compat.get_process_start_id``
    — the same incarnation identity ``session_pid.py``'s
    ``<gw>:<pid>:<start_token>`` sweep records use), the signature covers it,
    and BOTH readers refuse on a proven mismatch.

    The asymmetry under test: a MISMATCH is positive evidence of a recycled
    pid → refuse; an ABSENT recorded token (legacy file) or an UNREADABLE
    live token (Windows, exited process) is merely unknown → resolve as
    before the guard existed.
    """

    @staticmethod
    def _live_token(value):
        return patch.object(platform_compat, "get_process_start_id", return_value=value)

    def test_recycled_pid_refused_by_strict_resolver(self, cfg):
        """HEADLINE (red against pre-fix main): publish under one process
        incarnation, present another incarnation of the same pid number —
        the strict resolver must refuse, not answer with the old key."""
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        with self._live_token("222"):
            assert session_pid_sig.verify_session_pid(4242) == ""

    def test_recycled_pid_refused_by_lenient_reader(self, cfg):
        """A proven mismatch refuses on the LENIENT path too: callers like
        peer_resolve fall back from the strict resolver to this reader, so a
        refusal surfaced only from the strict path would be silently
        recovered by the fallback and the stale attribution kept."""
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        with self._live_token("222"):
            assert session_pid_sig.read_session_pid_txt(4242) == ""

    def test_same_incarnation_still_resolves(self, cfg):
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY
            assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY

    def test_legacy_tokenless_file_still_resolves(self, cfg, ident):
        """BACKWARD COMPATIBILITY: a signed mapping written before the
        format change (no token line, MAC over ``"<pid>:<session_key>"``)
        must not read as tampered or as a mismatch, even when the live
        token IS readable (absent recorded token = unknown, not mismatch)."""
        session_pid_sig.publish_session_pid(4242, "dashboard:seed")  # mints the root
        key = identity_key_bytes(cfg)
        (ident / "session_pid_4242.txt").write_text(SESSION_KEY, encoding="utf-8")
        (ident / "session_pid_4242.sig").write_text(
            session_pid_sig._compute_sig(key, 4242, SESSION_KEY), encoding="utf-8"
        )
        (cfg / "session_pid_4242.txt").write_text(SESSION_KEY, encoding="utf-8")
        with self._live_token("222"):
            assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY
            assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY

    def test_unreadable_live_token_resolves_as_today(self, cfg):
        """UNKNOWN ≠ MISMATCH: a recorded token whose live counterpart
        cannot be read (Windows, process exited, permission) keeps today's
        behaviour on both paths."""
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        with self._live_token(None):
            assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY
            assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY

    def test_publish_records_the_token_as_a_second_line(self, cfg, ident):
        """The on-disk form: ``<session_key>\\n<start_token>``. A second
        LINE, not a colon field like session_pid.py's integer records,
        because the session key itself contains colons. Both copies carry it:
        the recycle guard runs on the lenient path too."""
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert (ident / "session_pid_4242.txt").read_text(encoding="utf-8") == f"{SESSION_KEY}\n111"
        assert (cfg / "session_pid_4242.txt").read_text(encoding="utf-8") == f"{SESSION_KEY}\n111"

    def test_signature_covers_the_token(self, cfg, ident):
        """Flipping ONLY the token line invalidates the MAC — even when the
        rewritten token matches the live process, so the refusal proven
        here is the signature's, not the recycle check's."""
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (ident / "session_pid_4242.txt").write_text(f"{SESSION_KEY}\n222", encoding="utf-8")
        with self._live_token("222"):
            assert session_pid_sig.verify_session_pid(4242) == ""

    def test_unsigned_publish_with_token_still_degrades(self, cfg, ident):
        """The documented unsigned-publish degrade path survives the token:
        identity root unsignable → token-bearing ``.txt`` still published (the
        lenient reader keeps working, recycle guard included), stale sidecar
        removed, strict resolvers fail closed."""
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)  # signed
        break_identity_root(cfg)
        with self._live_token("111"):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            assert not (ident / "session_pid_4242.sig").exists()
            assert session_pid_sig.verify_session_pid(4242) == ""
            assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY
        with self._live_token("222"):
            assert session_pid_sig.read_session_pid_txt(4242) == ""

    def test_malformed_multiline_body_refused(self, cfg, ident):
        """Three-plus lines were never written by publish_session_pid —
        refuse on both paths rather than guess a parse, even under a valid
        MAC over the raw body."""
        session_pid_sig.publish_session_pid(4242, "dashboard:seed")  # mints the root
        key = identity_key_bytes(cfg)
        body = f"{SESSION_KEY}\n111\nextra"
        (ident / "session_pid_4242.txt").write_text(body, encoding="utf-8")
        (ident / "session_pid_4242.sig").write_text(
            session_pid_sig._compute_sig(key, 4242, body), encoding="utf-8"
        )
        (cfg / "session_pid_4242.txt").write_text(body, encoding="utf-8")
        with self._live_token("111"):
            assert session_pid_sig.verify_session_pid(4242) == ""
            assert session_pid_sig.read_session_pid_txt(4242) == ""


class TestNoNofollowPlatform:
    """Platforms without ``O_NOFOLLOW`` (Windows) use an ``lstat`` pre-check
    plus a post-open ``(st_dev, st_ino)`` identity check. The identity check
    closes the lstat->open TOCTOU window: a path swapped to a symlink in
    that window opens the symlink's TARGET, whose identity can never match
    the vetted regular file. Simulated on POSIX by removing ``O_NOFOLLOW``."""

    def test_regular_file_still_reads(self, cfg, monkeypatch):
        monkeypatch.delattr("os.O_NOFOLLOW")
        (cfg / "session_pid_4242.txt").write_text(SESSION_KEY, encoding="utf-8")
        assert session_pid_sig.read_session_pid_txt(4242) == SESSION_KEY

    def test_lstat_open_swap_refused(self, cfg, monkeypatch, tmp_path):
        """TOCTOU RACE: the file vetted by lstat is not the file the open
        lands on (as when an agent swaps in a symlink between the two
        calls). Simulated by pointing lstat at a decoy file so the opened
        handle's identity mismatches the vetted one."""
        import os as _os

        monkeypatch.delattr("os.O_NOFOLLOW")
        target = cfg / "session_pid_4242.txt"
        target.write_text(SESSION_KEY, encoding="utf-8")
        decoy = tmp_path / "vetted-then-swapped"
        decoy.write_text("x", encoding="utf-8")
        real_lstat = _os.lstat
        monkeypatch.setattr("os.lstat", lambda p, *a, **k: real_lstat(decoy))
        assert session_pid_sig.read_session_pid_txt(4242) == ""

    def test_symlink_present_at_lstat_refused(self, cfg, monkeypatch, tmp_path):
        """The pre-check itself still refuses a symlink already in place."""
        monkeypatch.delattr("os.O_NOFOLLOW")
        secret = tmp_path / "victim-secret"
        secret.write_text("hunter2", encoding="utf-8")
        (cfg / "session_pid_4242.txt").symlink_to(secret)
        assert session_pid_sig.read_session_pid_txt(4242) == ""


class TestDomainSeparation:
    """The sidecar signs with a subkey *derived* from its root via a
    domain-separation label, so a MAC from one protocol can never be presented
    as a valid MAC for the other.

    Two independent controls now, not one: identity signs with its OWN root
    (``session-identity/identity_hmac.key``), and even within a root the derived
    subkey keeps the schemes apart. The derivation is still asserted because the
    sibling token protocol continues to share the audit root, so the label is
    what separates those two.
    """

    def test_sig_is_not_signed_with_the_raw_identity_root(self, cfg, ident):
        import hashlib
        import hmac

        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        root = identity_key_bytes(cfg)
        stored = (ident / "session_pid_4242.sig").read_text(encoding="utf-8")

        # A MAC computed with the RAW root key (the SEL scheme) must differ
        # from the stored sidecar MAC — proving the root key is not used
        # directly to sign the sidecar.
        raw_mac = hmac.new(root, f"4242:{SESSION_KEY}".encode("utf-8"), hashlib.sha256).hexdigest()
        assert stored != raw_mac

        # The stored MAC matches the DERIVED-subkey scheme.
        subkey = hmac.new(root, session_pid_sig._SUBKEY_DOMAIN, hashlib.sha256).digest()
        derived_mac = hmac.new(
            subkey, f"4242:{SESSION_KEY}".encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert stored == derived_mac

    def test_the_audit_root_cannot_sign_an_identity(self, cfg, ident):
        """The root split, stated as a MAC comparison: the key the sandbox can
        read produces a different signature from the one the verifier accepts."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        stored = (ident / "session_pid_4242.sig").read_text(encoding="utf-8")
        audit = (cfg / "sel_hmac.key").read_bytes()
        assert stored != session_pid_sig._compute_sig(audit, 4242, SESSION_KEY)


class TestTrustRootRecovery:
    """The AUDIT root's recovery path, which the sibling token protocol in
    ``session_token_sig`` signs with: it imports ``_load_hmac_key`` from here
    rather than copying it, so this module owns the loader's behaviour.

    SEL signs from key bytes it cached at init, while this loader re-reads the
    file on every call. The shared accessor re-resolves a key that MOVED (a
    concurrent legacy -> ``trust/`` migration), so what reaches recovery is the
    residue no path can resolve: a key deleted, unreadable, truncated, or
    replaced by bytes that are not the anchor. Those would otherwise take the
    dependent protocol down for the life of the process — with a healthy audit
    chain giving no hint. Recovery reads the same bytes SEL validated at init.

    Driven through ``_load_hmac_key`` directly rather than through
    ``publish_session_pid``: identity publication signs with the identity root
    and does not reach this loader at all, so routing these assertions through
    it would exercise a path that cannot fail for this reason.
    """

    def test_missing_file_recovers_from_live_sel_key(self, cfg):
        (cfg / "sel_hmac.key").unlink()
        with patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32):
            assert session_pid_sig._load_hmac_key() == b"\x01" * 32

    def test_recovery_still_announces_the_broken_file(self, cfg, caplog):
        """Recovering from memory must NOT go quiet: signing works HERE, but the
        file is what every other process resolves, so a verifier that never held
        these bytes still fails closed. Silence would move the original silent
        failure one layer over instead of removing it."""
        (cfg / "sel_hmac.key").unlink()
        with (
            patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32),
            caplog.at_level("ERROR", logger=LOGGER_NAME),
        ):
            session_pid_sig._load_hmac_key()
        errors = records_from_this_module(caplog)
        assert len(errors) == 1
        message = errors[0].getMessage()
        assert str(cfg / "sel_hmac.key") in message
        assert "every other process" in message
        assert "sub-agent dispatch" in message and "memory writes" in message

    def test_broken_file_report_is_throttled_per_path(self, cfg, caplog):
        (cfg / "sel_hmac.key").unlink()
        with (
            patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32),
            caplog.at_level("DEBUG", logger=LOGGER_NAME),
        ):
            session_pid_sig._load_hmac_key()
            session_pid_sig._load_hmac_key()
            session_pid_sig._load_hmac_key()
        assert len(records_from_this_module(caplog)) == 1
        assert (
            len(
                [
                    r
                    for r in records_from_this_module(caplog, "DEBUG")
                    if "signing from memory" in r.getMessage()
                ]
            )
            == 2
        )

    def test_truncated_file_recovers_from_live_sel_key(self, cfg):
        """SEL validates the length only at init, this loader on every call —
        so a post-init truncation is exactly the asymmetry to recover from."""
        (cfg / "sel_hmac.key").write_bytes(b"\x01" * 8)
        with patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32):
            assert session_pid_sig._load_hmac_key() == b"\x01" * 32

    def test_readable_file_wins_over_live_sel_key(self, cfg):
        """The file is the anchor every OTHER process resolves independently, so
        a readable file must never be overridden by this process's memory —
        otherwise a publisher signs with bytes its verifier does not have."""
        with patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x02" * 32):
            assert session_pid_sig._load_hmac_key() == b"\x01" * 32

    def test_no_file_and_no_live_key_still_fails_closed(self, cfg):
        (cfg / "sel_hmac.key").unlink()
        assert session_pid_sig._load_hmac_key() is None

    def test_a_broken_audit_root_does_not_break_identity(self, cfg):
        """The roots are independent in BOTH directions. An audit root that
        cannot be read at all leaves identity signing and verification intact,
        which is the property that let the audit writer stay in the sandbox."""
        (cfg / "sel_hmac.key").unlink()
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY


class TestTrustRootRelocationIsFollowed:
    """Trust-root relocation is followed, from the dependent protocol's side.

    Deliberately does NOT use the ``cfg`` fixture: that fixture patches
    ``sel_hmac_key_path`` to a fixed path, which is exactly the seam under test.
    A real singleton is required because re-resolution is verified against the
    key bytes it validated at init.
    """

    def test_a_relocated_key_is_read_from_the_file_not_memory(self, tmp_path):
        """The class is closed rather than worked around: the accessor follows
        the moved file, so a verifier in ANOTHER process resolves the same bytes.
        The memory fallback is stubbed to different bytes, so a result equal to
        the real key can only have come from the file."""
        from kiro_crew.sel import SecurityEventLog

        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        try:
            log = SecurityEventLog(base_dir=tmp_path, sync=True)
            key = log._hmac_key
            # A failed migration left this process naming the legacy location;
            # the key actually lives in trust/ because a sibling completed it.
            log._hmac_key_file = tmp_path / "sel_hmac.key"
            assert not (tmp_path / "sel_hmac.key").exists()

            with (
                patch.object(session_pid_sig, "config_dir", return_value=tmp_path),
                patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\xfe" * 32),
            ):
                session_pid_sig._reported.clear()
                loaded = session_pid_sig._load_hmac_key()
                session_pid_sig._reported.clear()

            assert loaded == key
            assert loaded != b"\xfe" * 32
        finally:
            SecurityEventLog._instance = None
            SecurityEventLog._initialized = False


class TestSigningUnavailableReport:
    """Publication happens on every session claim, so the operator-facing
    message must not be emitted per publish, and must name what stops working
    rather than only the mechanism.

    Every case here breaks the IDENTITY root, because that is the root identity
    signs with. A report keyed or worded on the audit root would send an operator
    to a file that is intact.
    """

    def test_the_two_reports_do_not_suppress_each_other(self, cfg, caplog):
        """The broken-audit-file notice and the cannot-sign-identities notice tell
        an operator different things, about different roots, so they are throttled
        independently. Sharing one entry would let whichever fired first silence
        the other for the rest of the process."""
        (cfg / "sel_hmac.key").unlink()
        break_identity_root(cfg)
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            with patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32):
                session_pid_sig._load_hmac_key()
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        messages = [r.getMessage() for r in records_from_this_module(caplog)]
        assert len(messages) == 2, messages
        assert "every other process" in messages[0]
        assert "cannot sign session identities" in messages[1]

    def test_reported_once_per_process_then_debug(self, cfg, caplog):
        break_identity_root(cfg)
        with caplog.at_level("DEBUG", logger=LOGGER_NAME):
            session_pid_sig.publish_session_pid(1, SESSION_KEY)
            session_pid_sig.publish_session_pid(2, SESSION_KEY)
            session_pid_sig.publish_session_pid(3, SESSION_KEY)
        errors = records_from_this_module(caplog)
        assert len(errors) == 1
        debugs = [
            r
            for r in records_from_this_module(caplog, "DEBUG")
            if "still unavailable" in r.getMessage()
        ]
        assert len(debugs) == 2

    def test_message_names_the_consequence_and_the_path(self, cfg):
        """The path has to be the identity root. Naming the audit root would be a
        true sentence about the wrong file: it is readable, intact, and not what
        failed."""
        import logging as _logging

        break_identity_root(cfg)
        records: list[_logging.LogRecord] = []
        handler = _logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        logger = _logging.getLogger(LOGGER_NAME)
        logger.addHandler(handler)
        try:
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        finally:
            logger.removeHandler(handler)
        errors = [r for r in records if r.levelname == "ERROR"]
        assert len(errors) == 1, [r.getMessage() for r in records]
        message = errors[0].getMessage()
        assert str(session_pid_sig._identity_key_path(cfg)) in message
        assert str(cfg / "sel_hmac.key") not in message
        assert "sub-agent dispatch" in message
        assert "memory writes" in message

    def test_relocated_path_is_reported_again(self, cfg, caplog):
        """Suppression is keyed on the resolved path, so a genuine relocation
        is not swallowed by the first failure's entry."""
        break_identity_root(cfg)
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            with patch.object(
                session_pid_sig,
                "_IDENTITY_SUBDIR",
                "session-identity-relocated",
            ):
                break_identity_root(cfg)
                session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert len(records_from_this_module(caplog)) == 2

    def test_recovery_rearms_the_report_for_the_same_path(self, cfg, caplog):
        """Break -> restore -> break again on ONE path must produce a second
        ERROR: on a long-lived gateway that is never restarted, the log is the
        only signal the operator gets."""
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            break_identity_root(cfg)
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            session_pid_sig._identity_key_path(cfg).write_bytes(b"\x02" * 32)
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            break_identity_root(cfg)
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert len(records_from_this_module(caplog)) == 2

    def test_a_stray_error_from_another_logger_is_not_counted_as_ours(self, cfg, caplog):
        """Guards `records_from_this_module` against being narrowed back to a
        level-only filter.

        Every count in this class is exact, and `caplog.records` collects every
        record that propagates — not only this module's. A leaked asyncio task
        being destroyed inside the window (observed in CI) must therefore not be
        counted as one of our reports, or these assertions fail for a reason
        that has nothing to do with the code under test.
        """
        break_identity_root(cfg)
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            logging.getLogger("asyncio").error(
                "Task was destroyed but it is pending!\n"
                "task: <Task pending coro=<SessionManager._cleanup_loop()>>"
            )
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)

        ours = records_from_this_module(caplog)
        assert len(ours) == 1
        assert str(session_pid_sig._identity_key_path(cfg)) in ours[0].getMessage()
        # The stray record really was captured — this test would be vacuous if
        # caplog had filtered it out for us.
        assert any(r.name == "asyncio" and r.levelname == "ERROR" for r in caplog.records)


class TestSigningHealth:
    """The diagnostic surface (`kirocrew doctor`) asks proactively; publication
    only reports once a session is claimed.

    Health is reported for the IDENTITY root, which is what signs identities.
    Before the gateway's first session claim that root does not exist yet, so the
    honest answer then is unhealthy: nothing can be signed until it is minted.
    """

    def test_reports_healthy_once_the_root_is_minted(self, cfg):
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        ok, path = session_pid_sig.signing_health()
        assert ok is True
        assert path == session_pid_sig._identity_key_path(cfg)

    def test_reports_unhealthy_before_the_first_claim(self, cfg):
        """And does NOT mint the root to answer: a read-only diagnostic that
        creates a trust root reports on a state it just produced."""
        ok, path = session_pid_sig.signing_health()
        assert ok is False
        assert path == session_pid_sig._identity_key_path(cfg)
        assert not path.exists()

    def test_reports_unhealthy_when_the_trust_root_is_gone(self, cfg):
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        session_pid_sig._identity_key_path(cfg).unlink()
        ok, path = session_pid_sig.signing_health()
        assert ok is False
        assert path == session_pid_sig._identity_key_path(cfg)

    def test_never_constructs_the_sel_singleton(self, cfg):
        """Asking the question must not create the trust root it asks about,
        and must not put a mkdir + key write behind a read-only command."""
        with patch("kiro_crew.sel.SecurityEventLog") as sel_cls:
            session_pid_sig.signing_health()
        sel_cls.assert_not_called()

    def test_is_not_wired_into_the_gateway_boot_path(self):
        """`no-new-work-on-gateway-boot-path` forbids a new awaited step before
        the socket binds, and this check is a diagnostic, not a gate."""
        import inspect

        from kiro_crew.dashboard import token_auth

        assert "signing_health" not in inspect.getsource(token_auth.warm_auth_singletons)
