"""Tests for :mod:`kiro_crew.session_pid_sig` — signed session_pid publication.

The ``session_pid_<pid>.txt`` file is same-uid agent-writable, so the strict
identity path must not trust it bare. These tests lock in the sidecar
contract: publish writes ``.txt`` + HMAC ``.sig`` (keyed by the SEL trust
root); verify accepts only a matching pair and fails closed on every
tamper/degradation path.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from kiro_crew import session_pid_sig

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
    assertions still require an exact count, they just no longer count other
    people's records as ours.
    """
    return [
        r for r in caplog.records if r.levelname == level and r.name == LOGGER_NAME
    ]


@pytest.fixture
def cfg(tmp_path):
    """Isolated config dir with a valid SEL trust-root key. Patches both the
    mapping-file dir (config_dir) and the canonical trust-root path accessor
    (sel_hmac_key_path — single source of truth owned by sel.py).

    ``_sel_hmac_key_bytes`` is stubbed to ``None`` so these tests exercise the
    FILE path in isolation: the in-memory recovery fallback depends on a live
    ``SecurityEventLog`` singleton, which other tests in the same process may
    or may not have initialized. Its own behavior is covered by
    ``TestTrustRootRecovery``.
    """
    (tmp_path / "sel_hmac.key").write_bytes(b"\x01" * 32)
    with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
         patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=None), \
         patch.object(
             session_pid_sig,
             "sel_hmac_key_path",
             return_value=tmp_path / "sel_hmac.key",
         ):
        session_pid_sig._reported.clear()
        yield tmp_path
        session_pid_sig._reported.clear()


class TestPublish:
    def test_writes_txt_and_sig(self, cfg):
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert (cfg / "session_pid_4242.txt").read_text(encoding="utf-8") == SESSION_KEY
        sig = (cfg / "session_pid_4242.sig").read_text(encoding="utf-8")
        assert len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)

    def test_publish_without_key_writes_unsigned_and_drops_stale_sig(self, cfg):
        """SEL key missing: txt still published (lenient readers keep
        working) but any stale sidecar is removed so a rekeyed mapping can
        never verify against an old signature."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)  # signed
        (cfg / "sel_hmac.key").unlink()
        session_pid_sig.publish_session_pid(4242, "dashboard:rekeyed")
        assert (
            cfg / "session_pid_4242.txt"
        ).read_text(encoding="utf-8") == "dashboard:rekeyed"
        assert not (cfg / "session_pid_4242.sig").exists()

    def test_rekey_overwrites_both_files(self, cfg):
        session_pid_sig.publish_session_pid(4242, "dashboard:old")
        old_sig = (cfg / "session_pid_4242.sig").read_text(encoding="utf-8")
        session_pid_sig.publish_session_pid(4242, "dashboard:new")
        assert session_pid_sig.verify_session_pid(4242) == "dashboard:new"
        assert (cfg / "session_pid_4242.sig").read_text(encoding="utf-8") != old_sig

    def test_preplanted_symlink_not_followed(self, cfg):
        """SYMLINK ATTACK: an agent plants symlinks at the predictable
        mapping paths pointing at another writable file. Publication must
        replace the symlink (os.replace semantics), never follow it and
        truncate the target."""
        victim = cfg / "victim.dat"
        victim.write_text("precious", encoding="utf-8")
        for name in ("session_pid_4242.txt", "session_pid_4242.sig"):
            (cfg / name).symlink_to(victim)
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        # Victim untouched; both paths are now regular files, not symlinks.
        assert victim.read_text(encoding="utf-8") == "precious"
        assert not (cfg / "session_pid_4242.txt").is_symlink()
        assert not (cfg / "session_pid_4242.sig").is_symlink()
        assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY


class TestVerify:
    def test_round_trip(self, cfg):
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY
        # str pid (as read from KIROCREW_HOST_PID) verifies identically.
        assert session_pid_sig.verify_session_pid("4242") == SESSION_KEY

    def test_missing_files_refused(self, cfg):
        assert session_pid_sig.verify_session_pid(9999) == ""

    def test_unsigned_txt_refused(self, cfg):
        """FORGERY: bare .txt written without the SEL key."""
        (cfg / "session_pid_4242.txt").write_text(
            "dashboard:victim", encoding="utf-8"
        )
        assert session_pid_sig.verify_session_pid(4242) == ""

    def test_tampered_txt_refused(self, cfg):
        """FORGERY: legitimate pair, then the .txt is redirected at another
        slot — the old signature no longer matches."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (cfg / "session_pid_4242.txt").write_text(
            "dashboard:victim", encoding="utf-8"
        )
        assert session_pid_sig.verify_session_pid(4242) == ""

    def test_replayed_pair_under_other_pid_refused(self, cfg):
        """REPLAY: parent's .txt/.sig copied under a different pid — the pid
        is bound into the MAC."""
        session_pid_sig.publish_session_pid(1000, "dashboard:parent")
        for ext in ("txt", "sig"):
            (cfg / f"session_pid_2000.{ext}").write_text(
                (cfg / f"session_pid_1000.{ext}").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        assert session_pid_sig.verify_session_pid(2000) == ""

    def test_short_key_refused(self, cfg):
        """A truncated/corrupted trust-root key must not verify anything."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (cfg / "sel_hmac.key").write_bytes(b"\x01" * 8)
        assert session_pid_sig.verify_session_pid(4242) == ""

    def test_missing_key_refused(self, cfg, caplog):
        """Missing trust root refuses AND emits the trust-root diagnostic —
        distinguishable from the forgery (MAC-mismatch) warning so a
        publisher/verifier trust-root split doesn't silently reproduce the
        original sandboxed-session bug while looking like forgery refusal."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (cfg / "sel_hmac.key").unlink()
        with caplog.at_level("WARNING", logger=session_pid_sig.logger.name):
            assert session_pid_sig.verify_session_pid(4242) == ""
        assert any(
            "trust-root key absent/short" in r.getMessage() for r in caplog.records
        )

    def test_symlinked_mapping_files_refused_on_read(self, cfg):
        """READ-SIDE SYMLINK ATTACK: after a legitimate publish, an agent
        swaps a mapping file for a symlink to a sensitive target. The
        trusted verifier must refuse (O_NOFOLLOW) — never follow the link
        and read the target."""
        secret = cfg / "secret.dat"
        secret.write_text("sensitive-content", encoding="utf-8")
        # Symlinked .txt refused.
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (cfg / "session_pid_4242.txt").unlink()
        (cfg / "session_pid_4242.txt").symlink_to(secret)
        assert session_pid_sig.verify_session_pid(4242) == ""
        # Symlinked .sig refused (fresh legitimate pair first).
        session_pid_sig.publish_session_pid(5555, SESSION_KEY)
        (cfg / "session_pid_5555.sig").unlink()
        (cfg / "session_pid_5555.sig").symlink_to(secret)
        assert session_pid_sig.verify_session_pid(5555) == ""

    def test_oversized_mapping_file_refused(self, cfg):
        """RESOURCE ATTACK: an agent swaps a mapping file for a huge one.
        Verification must reject it from fstat size, never buffer it."""
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        (cfg / "session_pid_4242.txt").write_text(
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
        monkeypatch.setattr(
            "os.lstat", lambda p, *a, **k: real_lstat(decoy)
        )
        assert session_pid_sig.read_session_pid_txt(4242) == ""

    def test_symlink_present_at_lstat_refused(self, cfg, monkeypatch, tmp_path):
        """The pre-check itself still refuses a symlink already in place."""
        monkeypatch.delattr("os.O_NOFOLLOW")
        secret = tmp_path / "victim-secret"
        secret.write_text("hunter2", encoding="utf-8")
        (cfg / "session_pid_4242.txt").symlink_to(secret)
        assert session_pid_sig.read_session_pid_txt(4242) == ""


class TestDomainSeparation:
    """The sidecar and the SEL audit chain share one on-disk trust-root key
    (``sel_hmac.key``) but MUST NOT share a signing key: the sidecar signs
    with a subkey *derived* from the root via a domain-separation label, so a
    MAC from one protocol can never be presented as a valid MAC for the
    other."""

    def test_sig_is_not_signed_with_raw_root_key(self, cfg):
        import hashlib
        import hmac

        root = b"\x01" * 32
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        stored = (cfg / "session_pid_4242.sig").read_text(encoding="utf-8")

        # A MAC computed with the RAW root key (the SEL scheme) must differ
        # from the stored sidecar MAC — proving the root key is not used
        # directly to sign the sidecar.
        raw_mac = hmac.new(
            root, f"4242:{SESSION_KEY}".encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert stored != raw_mac

        # The stored MAC matches the DERIVED-subkey scheme.
        subkey = hmac.new(
            root, session_pid_sig._SUBKEY_DOMAIN, hashlib.sha256
        ).digest()
        derived_mac = hmac.new(
            subkey, f"4242:{SESSION_KEY}".encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert stored == derived_mac


class TestTrustRootRecovery:
    """The resolved trust-root path is frozen at ``SecurityEventLog`` init and
    never re-resolved, while SEL keeps signing from key bytes it cached at that
    moment. So a key file that later moves (a concurrent legacy -> ``trust/``
    migration), is deleted, loses read permission, or is truncated silently
    takes this protocol down for the life of the process — with a healthy audit
    chain giving no hint. Recovery reads the same bytes SEL validated at init.
    """

    def test_missing_file_recovers_from_live_sel_key(self, cfg):
        (cfg / "sel_hmac.key").unlink()
        with patch.object(
            session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32
        ):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            assert session_pid_sig.verify_session_pid(4242) == SESSION_KEY

    def test_recovery_still_announces_the_broken_file(self, cfg, caplog):
        """Recovering from memory must NOT go quiet: signing works HERE, but the
        file is what every other process resolves, so a verifier that never held
        these bytes still fails closed. Silence would move the original silent
        failure one layer over instead of removing it."""
        (cfg / "sel_hmac.key").unlink()
        with patch.object(
            session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32
        ), caplog.at_level("ERROR", logger=LOGGER_NAME):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        errors = records_from_this_module(caplog)
        assert len(errors) == 1
        message = errors[0].getMessage()
        assert str(cfg / "sel_hmac.key") in message
        assert "every other process" in message
        assert "sub-agent dispatch" in message and "memory writes" in message

    def test_broken_file_report_is_throttled_per_path(self, cfg, caplog):
        (cfg / "sel_hmac.key").unlink()
        with patch.object(
            session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32
        ), caplog.at_level("DEBUG", logger=LOGGER_NAME):
            session_pid_sig.publish_session_pid(1, SESSION_KEY)
            session_pid_sig.publish_session_pid(2, SESSION_KEY)
            session_pid_sig.publish_session_pid(3, SESSION_KEY)
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
        """SEL validates the length only at init, this protocol on every call —
        so a post-init truncation is exactly the asymmetry to recover from."""
        (cfg / "sel_hmac.key").write_bytes(b"\x01" * 8)
        with patch.object(
            session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32
        ):
            assert session_pid_sig._load_hmac_key() == b"\x01" * 32

    def test_readable_file_wins_over_live_sel_key(self, cfg):
        """The file is the anchor every OTHER process resolves independently, so
        a readable file must never be overridden by this process's memory —
        otherwise a publisher signs with bytes its verifier does not have."""
        with patch.object(
            session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x02" * 32
        ):
            assert session_pid_sig._load_hmac_key() == b"\x01" * 32

    def test_no_file_and_no_live_key_still_fails_closed(self, cfg):
        (cfg / "sel_hmac.key").unlink()
        session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert not (cfg / "session_pid_4242.sig").exists()
        assert session_pid_sig.verify_session_pid(4242) == ""


class TestSigningUnavailableReport:
    """Publication happens on every session claim, so the operator-facing
    message must not be emitted per publish, and must name what stops working
    rather than only the mechanism."""

    def test_the_two_reports_do_not_suppress_each_other(self, cfg, caplog):
        """The broken-file notice and the cannot-sign notice tell an operator
        different things (signing survives here vs signing is gone), so they are
        throttled independently. Sharing one key would let whichever fired first
        silence the other for the rest of the process."""
        (cfg / "sel_hmac.key").unlink()
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            with patch.object(
                session_pid_sig, "_sel_hmac_key_bytes", return_value=b"\x01" * 32
            ):
                session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            # Same path, but the in-memory fallback is gone now.
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        messages = [r.getMessage() for r in records_from_this_module(caplog)]
        assert len(messages) == 2, messages
        assert "every other process" in messages[0]
        assert "cannot sign session identities" in messages[1]

    def test_reported_once_per_process_then_debug(self, cfg, caplog):
        (cfg / "sel_hmac.key").unlink()
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

    def test_message_names_the_consequence_and_the_path(self, cfg, caplog):
        (cfg / "sel_hmac.key").unlink()
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        message = records_from_this_module(caplog)[0].getMessage()
        assert str(cfg / "sel_hmac.key") in message
        assert "sub-agent dispatch" in message
        assert "memory writes" in message

    def test_relocated_path_is_reported_again(self, cfg, caplog):
        """Suppression is keyed on the resolved path, so a genuine relocation
        is not swallowed by the first failure's entry."""
        (cfg / "sel_hmac.key").unlink()
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            with patch.object(
                session_pid_sig,
                "sel_hmac_key_path",
                return_value=cfg / "trust" / "sel_hmac.key",
            ):
                session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert len(records_from_this_module(caplog)) == 2

    def test_recovery_rearms_the_report_for_the_same_path(self, cfg, caplog):
        """Break -> restore -> break again on ONE path must produce a second
        ERROR: on a long-lived gateway that is never restarted, the log is the
        only signal the operator gets."""
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            (cfg / "sel_hmac.key").unlink()
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            (cfg / "sel_hmac.key").write_bytes(b"\x01" * 32)
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
            (cfg / "sel_hmac.key").unlink()
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)
        assert len(records_from_this_module(caplog)) == 2

    def test_a_stray_error_from_another_logger_is_not_counted_as_ours(
        self, cfg, caplog
    ):
        """Guards `records_from_this_module` against being narrowed back to a
        level-only filter.

        Every count in this class is exact, and `caplog.records` collects every
        record that propagates — not only this module's. A leaked asyncio task
        being destroyed inside the window (observed in CI) must therefore not be
        counted as one of our reports, or these assertions fail for a reason
        that has nothing to do with the code under test.
        """
        (cfg / "sel_hmac.key").unlink()
        with caplog.at_level("ERROR", logger=LOGGER_NAME):
            logging.getLogger("asyncio").error(
                "Task was destroyed but it is pending!\n"
                "task: <Task pending coro=<SessionManager._cleanup_loop()>>"
            )
            session_pid_sig.publish_session_pid(4242, SESSION_KEY)

        ours = records_from_this_module(caplog)
        assert len(ours) == 1
        assert str(cfg / "sel_hmac.key") in ours[0].getMessage()
        # The stray record really was captured — this test would be vacuous if
        # caplog had filtered it out for us.
        assert any(
            r.name == "asyncio" and r.levelname == "ERROR" for r in caplog.records
        )


class TestSigningHealth:
    """The diagnostic surface (`kirocrew doctor`) asks proactively; publication
    only reports once a session is claimed."""

    def test_reports_healthy_with_the_resolved_path(self, cfg):
        ok, path = session_pid_sig.signing_health()
        assert ok is True
        assert path == cfg / "sel_hmac.key"

    def test_reports_unhealthy_when_the_trust_root_is_gone(self, cfg):
        (cfg / "sel_hmac.key").unlink()
        ok, path = session_pid_sig.signing_health()
        assert ok is False
        assert path == cfg / "sel_hmac.key"

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

        assert "signing_health" not in inspect.getsource(
            token_auth.warm_auth_singletons
        )
