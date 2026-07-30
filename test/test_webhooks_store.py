"""Tests for the inbound-webhook token store, run ring, and freshness helper."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import time
from pathlib import Path

import pytest

from kiro_crew import webhooks


@pytest.fixture()
def store(tmp_path) -> webhooks.WebhookTokenStore:
    return webhooks.WebhookTokenStore(tmp_path)


@pytest.fixture()
def runs(tmp_path) -> webhooks.WebhookRunStore:
    return webhooks.WebhookRunStore(tmp_path)


class TestTokenPersistence:
    def test_raw_secret_never_written_to_disk(self, store):
        raw, _secret, entry = store.create("Review Bot")
        on_disk = store.path.read_text(encoding="utf-8")
        assert raw not in on_disk
        assert raw[len(webhooks.TOKEN_PREFIX):] not in on_disk
        assert webhooks.hash_token(raw) in on_disk
        # The stored record carries the hash, never a recoverable secret.
        stored = json.loads(on_disk)["tokens"][0]
        assert stored["token_hash"] == webhooks.hash_token(raw)
        assert "token" not in stored
        assert entry["display_prefix"] == raw[: len(webhooks.TOKEN_PREFIX) + 4]
        assert entry["last4"] == raw[-4:]

    def test_public_entry_from_create_has_no_hash(self, store):
        _, _secret, entry = store.create("Review Bot")
        assert "token_hash" not in entry
        assert entry["legacy"] is False

    def test_raw_secret_shape(self, store):
        raw, _secret, _ = store.create("Review Bot")
        assert raw.startswith(webhooks.TOKEN_PREFIX)
        body = raw[len(webhooks.TOKEN_PREFIX):]
        assert len(body) == webhooks.TOKEN_ENTROPY_CHARS

    def test_store_file_is_owner_only(self, store):
        store.create("Review Bot")
        if not webhooks.platform_compat.IS_POSIX:
            # Windows has no POSIX mode bits — os.stat reports 0o666 whatever we
            # do — so the equivalent guarantee there is the owner-only DACL that
            # write_json_atomic applies via restrict_to_owner. Asserting 0o600
            # on Windows would only be asserting a no-op.
            pytest.skip("POSIX mode bits; Windows uses the owner-only DACL instead")
        mode = stat.S_IMODE(store.path.stat().st_mode)
        assert mode == 0o600

    def test_one_time_reveal(self, store):
        raw, _secret, entry = store.create("Review Bot")
        # Nothing on any read path can reproduce the secret afterwards.
        assert all(raw not in json.dumps(e) for e in store.list_entries())
        assert all(raw not in json.dumps(e) for e in store.public_entries())
        assert store.verify(raw) == entry["id"]

    def test_corrupt_file_fails_closed_instead_of_reading_as_empty(self, store):
        """An unparseable store must raise, not report itself empty.

        Reading it as empty let the next write replace it, which destroyed every
        issued credential and its signing secret. Refusing keeps the bytes for
        an operator to recover.
        """
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{not json", encoding="utf-8")
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            store.list_entries()
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            store.create("Fresh")
        assert store.path.read_text(encoding="utf-8") == "{not json"

    def test_parseable_but_malformed_store_also_fails_closed(self, store, tmp_path):
        """Valid JSON of the wrong SHAPE must refuse just like unparseable bytes.

        The parse guard only covers bytes that will not decode. A file that
        decodes cleanly but holds a mapping where the list belongs, or a row
        with no hash, used to be filtered to nothing — and every mutating call
        writes the loaded list back, so the filtered rows were deleted on the
        next create. The kill switch shares this file, so the disabled state
        could go with them.
        """
        store.path.parent.mkdir(parents=True, exist_ok=True)
        for payload in ('{"tokens": {"id": "a"}}', '{"tokens": [{"label": "no hash"}]}'):
            store.path.write_text(payload, encoding="utf-8")
            with pytest.raises(webhooks.WebhookStoreUnreadable):
                store.list_entries()
            with pytest.raises(webhooks.WebhookStoreUnreadable):
                store.create("Fresh")
            # The bytes an operator needs are still on disk, unmodified.
            assert store.path.read_text(encoding="utf-8") == payload

    def test_absent_store_is_empty_not_malformed(self, store):
        """No file, and a file holding only the switch, both read as empty.

        Refusing here would break every fresh install: there is nothing on disk
        to lose, so absent must stay distinguishable from malformed.
        """
        assert store.list_entries() == []
        store.set_switch(False)
        assert store.list_entries() == []
        assert store.create("First") is not None


class TestTokenVerification:
    def test_unknown_token_rejected(self, store):
        store.create("Review Bot")
        assert store.verify("kc_whk_nope") is None
        assert store.verify("") is None

    def test_per_token_revoke_leaves_others_working(self, store):
        raw_a, _sa, entry_a = store.create("Bot A")
        raw_b, _sb, entry_b = store.create("Bot B")
        assert store.delete(entry_a["id"]) is True
        assert store.verify(raw_a) is None
        assert store.verify(raw_b) == entry_b["id"]
        assert store.delete(entry_a["id"]) is False  # already gone

    def test_legacy_scalar_still_authenticates(self, store):
        store.create("Review Bot")
        assert store.verify("legacy-secret", legacy_token="legacy-secret") == (
            webhooks.LEGACY_TOKEN_ID
        )
        assert store.verify("legacy-secret") is None
        assert store.verify("other", legacy_token="legacy-secret") is None

    def test_legacy_surfaces_as_synthetic_entry_without_head_of_secret(self, store):
        entries = store.public_entries(legacy_token="legacy-secret-value")
        legacy = [e for e in entries if e["legacy"]][0]
        assert legacy["id"] == webhooks.LEGACY_TOKEN_ID
        assert legacy["label"] == webhooks.LEGACY_TOKEN_LABEL
        assert legacy["last4"] == "alue"
        assert "legacy-secret" not in legacy["display_prefix"]

    def test_last_used_at_stamped_on_match(self, store):
        raw, _secret, entry = store.create("Review Bot")
        assert store.public_entries()[0]["last_used_at"] is None
        before = time.time()
        assert store.verify(raw) == entry["id"]
        stamped = store.public_entries()[0]["last_used_at"]
        assert stamped is not None and stamped >= before
        # A second, non-matching verification must not move the stamp.
        store.verify("kc_whk_wrong")
        assert store.public_entries()[0]["last_used_at"] == stamped

    def test_stamp_used_ignores_unknown_id(self, store):
        raw, _secret, _ = store.create("Review Bot")
        store.stamp_used("wht_missing")
        assert store.public_entries()[0]["last_used_at"] is None
        assert store.verify(raw) is not None


class TestTokenCap:
    def test_twenty_token_cap(self, store):
        for i in range(webhooks.MAX_TOKENS):
            store.create(f"Bot {i}")
        assert store.count() == webhooks.MAX_TOKENS
        with pytest.raises(webhooks.WebhookError, match="token limit reached"):
            store.create("One too many")
        assert store.count() == webhooks.MAX_TOKENS
        # Freeing a slot re-opens minting.
        store.delete(store.list_entries()[0]["id"])
        raw, _secret, _ = store.create("Replacement")
        assert store.verify(raw) is not None

    def test_label_validation(self, store):
        with pytest.raises(webhooks.WebhookError, match="required"):
            store.create("   ")
        with pytest.raises(webhooks.WebhookError, match="exceeds"):
            store.create("x" * (webhooks.LABEL_MAX_LEN + 1))
        assert store.count() == 0


class TestFreshness:
    """Tier boundaries must match what _load_hook_context actually injects."""

    def test_boundaries_at_one_hour_and_one_day(self):
        now = 1_000_000.0
        assert webhooks.context_freshness(now, now) == webhooks.FRESHNESS_FRESH
        assert webhooks.context_freshness(now - 3600, now) == webhooks.FRESHNESS_FRESH
        assert webhooks.context_freshness(now - 3601, now) == webhooks.FRESHNESS_STALE
        assert webhooks.context_freshness(now - 86400, now) == webhooks.FRESHNESS_STALE
        assert webhooks.context_freshness(now - 86401, now) == webhooks.FRESHNESS_EXPIRED

    def test_unknown_age_is_expired(self):
        assert webhooks.context_freshness(0, 1_000_000.0) == webhooks.FRESHNESS_EXPIRED
        assert webhooks.context_freshness(None, 1_000_000.0) == webhooks.FRESHNESS_EXPIRED

    def test_future_stamp_treated_as_fresh(self):
        now = 1_000_000.0
        assert webhooks.context_freshness(now + 500, now) == webhooks.FRESHNESS_FRESH

    def test_resolve_context_matches_tier(self):
        now = 1_000_000.0
        fresh = {"context_summary": "ctx", "registered_at": now - 60}
        stale = {"context_summary": "ctx", "registered_at": now - 7200}
        expired = {"context_summary": "ctx", "registered_at": now - 90000}

        assert webhooks.resolve_context(fresh, now) == (webhooks.FRESHNESS_FRESH, "ctx")
        tier, text = webhooks.resolve_context(stale, now)
        assert tier == webhooks.FRESHNESS_STALE
        assert text.startswith("[Context from 2h ago")
        assert text.endswith("\nctx")
        assert webhooks.resolve_context(expired, now) == (webhooks.FRESHNESS_EXPIRED, "")

    def test_resolve_context_tolerates_junk(self):
        now = 1_000_000.0
        assert webhooks.resolve_context(None, now)[1] == ""
        assert webhooks.resolve_context("string", now)[1] == ""
        assert webhooks.resolve_context({}, now)[1] == ""
        assert webhooks.resolve_context({"summary": ""}, now)[1] == ""
        assert webhooks.resolve_context(
            {"summary": "legacy key", "registered_at": now}, now
        ) == (webhooks.FRESHNESS_FRESH, "legacy key")
        assert webhooks.resolve_context(
            {"context_summary": "c", "registered_at": "bogus"}, now
        ) == (webhooks.FRESHNESS_EXPIRED, "")

    def test_load_hook_context_uses_shared_helper(self, tmp_path, monkeypatch):
        """The injection path and the badge must never disagree."""
        from kiro_crew.dashboard.handlers import hooks as hooks_handlers

        path = Path(tmp_path) / "hooks.json"
        now = time.time()
        path.write_text(
            json.dumps(
                {
                    "fresh-hook": {"context_summary": "F", "registered_at": now - 10},
                    "stale-hook": {"context_summary": "S", "registered_at": now - 7200},
                    "old-hook": {"context_summary": "O", "registered_at": now - 90000},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(hooks_handlers, "_HOOK_STORE_PATH", path)

        assert hooks_handlers._load_hook_context("fresh-hook") == "F"
        assert hooks_handlers._load_hook_context("stale-hook").endswith("\nS")
        assert "may be outdated" in hooks_handlers._load_hook_context("stale-hook")
        assert hooks_handlers._load_hook_context("old-hook") == ""
        assert hooks_handlers._load_hook_context("absent") == ""

    def test_load_hook_context_missing_file(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import hooks as hooks_handlers

        monkeypatch.setattr(hooks_handlers, "_HOOK_STORE_PATH", Path(tmp_path) / "nope.json")
        assert hooks_handlers._load_hook_context("x") == ""


class TestRunRing:
    def test_bounded_at_fifty_newest_first(self, runs):
        for i in range(webhooks.MAX_RUNS + 25):
            runs.record(outcome=webhooks.OUTCOME_COMPLETED, hook_id=f"h{i}")
        listed = runs.list_runs()
        assert len(listed) == webhooks.MAX_RUNS
        assert listed[0]["hook_id"] == f"h{webhooks.MAX_RUNS + 24}"
        assert listed[-1]["hook_id"] == f"h{25}"
        if webhooks.platform_compat.IS_POSIX:
            assert stat.S_IMODE(runs.path.stat().st_mode) == 0o600

    def test_record_shape(self, runs):
        rec = runs.record(
            outcome=webhooks.OUTCOME_COMPLETED,
            hook_id="review:pr-123",
            session_key="hook:review:pr-123",
            name="Review Bot",
            started_at=1753830000.0,
            duration_ms=41200,
            result_chars=3172,
            token_id="wht_7f3a91",
            delivered=True,
            detail="Delivered to notifications + Slack DM",
        )
        assert set(rec) == {
            "id", "hook_id", "session_key", "name", "outcome", "started_at",
            "duration_ms", "result_chars", "token_id", "delivered", "detail",
        }
        assert rec["id"].startswith("run_")
        assert runs.list_runs()[0] == rec

    def test_unknown_outcome_rejected(self, runs):
        with pytest.raises(webhooks.WebhookError, match="unknown outcome"):
            runs.record(outcome="exploded")
        assert runs.list_runs() == []

    def test_a_persisted_outcome_this_build_does_not_know_is_skipped(self, runs):
        """A row whose outcome is unknown is hidden from readers but KEPT on disk.

        Two requirements pull in opposite directions and both matter. The
        dashboard indexes its label/badge table BY outcome, so an unrecognised
        value arrives as a missing entry and takes the whole page down with its
        error boundary — hence the display filter. But ``record`` rewrites the
        history it loads, so filtering on the LOAD path made every recorded run
        delete rows it merely failed to recognise: a row written by a build with
        more outcomes than this one (a downgrade, or a rolling deploy) was
        destroyed by the next webhook call. The filter therefore belongs on
        ``list_runs``, not ``_load``.
        """
        runs.record(outcome=webhooks.OUTCOME_COMPLETED)
        raw = json.loads(runs.path.read_text(encoding="utf-8"))
        rows = raw["runs"] if isinstance(raw, dict) else raw
        rows.append({**rows[0], "id": "run_future", "outcome": "invented_later"})
        rows.append({**rows[0], "id": "run_missing"} | {"outcome": None})
        runs.path.write_text(json.dumps({"runs": rows}), encoding="utf-8")

        # The reader that feeds the client never sees an outcome it cannot render.
        kept = runs.list_runs()
        assert [r["outcome"] for r in kept] == [webhooks.OUTCOME_COMPLETED]
        assert all(r["outcome"] in webhooks.VALID_OUTCOMES for r in kept)

        # ...but recording another run must not destroy the rows it hid.
        runs.record(outcome=webhooks.OUTCOME_ERROR)
        on_disk = json.loads(runs.path.read_text(encoding="utf-8"))["runs"]
        assert "run_future" in {r.get("id") for r in on_disk}
        assert {r.get("id") for r in on_disk} >= {"run_future", "run_missing"}

    def test_corrupt_file_fails_closed_instead_of_reading_as_empty(self, runs):
        """Run history is preserved rather than replaced when unparseable.

        ``list_runs`` refuses so a reader cannot mistake corruption for an empty
        history. ``record`` deliberately does NOT propagate: it is called from
        the webhook request path, where failing the turn would be worse than
        losing one diagnostic row. Either way the bytes stay on disk.
        """
        runs.path.parent.mkdir(parents=True, exist_ok=True)
        runs.path.write_text("[[[", encoding="utf-8")
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            runs.list_runs()
        runs.record(outcome=webhooks.OUTCOME_ERROR)  # must not raise
        assert runs.path.read_text(encoding="utf-8") == "[[["


class TestCrossOsPermissions:
    """Store writes must not depend on POSIX-only calls.

    ``os.fchmod`` does not exist on Windows, so calling it directly raised
    ``AttributeError`` and broke every webhook store write there — minting,
    revoking, stamping and the switch. A bare 0600 is also a no-op on Windows,
    so the file holding signing secrets needs the owner-only DACL that
    ``restrict_to_owner`` applies, not just the mode bits.
    """

    def test_a_short_write_cannot_truncate_the_store(self, tmp_path, monkeypatch):
        """A partial write must not atomically replace a valid store.

        A single ``write(2)`` may transfer fewer bytes than asked and report the
        count with no error; ignoring it published truncated JSON over a good
        token store. Every ``os.write`` is capped at 8 bytes here, far below the
        payload, so a writer that leans on one unchecked raw call leaves the file
        unparseable. ``io.FileIO.write`` cannot be stubbed (immutable C type), so
        this pins the raw-call form specifically — that is the form that
        regressed, and the assertions below hold for any full-write writer.
        """
        real_os_write = os.write

        def _short_os_write(fd, data):
            return real_os_write(fd, bytes(data)[:8])

        monkeypatch.setattr(os, "write", _short_os_write)

        store = webhooks.WebhookTokenStore(tmp_path)
        raw, _secret, entry = store.create("short-write")

        text = store.path.read_text(encoding="utf-8")
        on_disk = json.loads(text)  # unparseable if a short write was ignored
        assert [e["id"] for e in on_disk["tokens"]] == [entry["id"]]
        assert text == json.dumps(on_disk, indent=2), "payload written in full"
        assert store.verify(raw) == entry["id"]
        assert not list(store.path.parent.glob("*.tmp"))

    def test_a_failed_flush_leaves_the_previous_store_intact(self, tmp_path, monkeypatch):
        """A write that fails part-way must not clobber what was there.

        Guards the fd-ownership handoff in the writer (the descriptor is adopted
        by a file object): a double close or a leaked temp file would show up
        here. Holds for the previous writer too, so it locks the property rather
        than proving the short-write fix.
        """
        store = webhooks.WebhookTokenStore(tmp_path)
        first_raw, _s, first = store.create("keeper")
        before = store.path.read_text(encoding="utf-8")

        def _boom(*_a, **_k):
            raise OSError("no space left on device")

        monkeypatch.setattr(os, "fsync", _boom)
        with pytest.raises(OSError):
            store.create("doomed")

        assert store.path.read_text(encoding="utf-8") == before
        assert store.verify(first_raw) == first["id"]
        assert not list(store.path.parent.glob("*.tmp"))

    def test_the_temp_file_is_locked_down_before_any_payload_is_written(
        self, tmp_path, monkeypatch
    ):
        """Secrets must never sit in a file that has not been locked down yet.

        On Windows the POSIX mode bits are a no-op, so the owner-only DACL from
        ``restrict_to_owner`` is the only protection; applying it after the write
        left the signing secret in a file carrying the parent directory's
        inherited ACL. The property is asserted by measuring the file's SIZE at
        the moment the lockdown is applied — zero means no payload byte existed
        yet. That is observable on every OS and does not depend on which write
        API the writer uses, which is what earlier attempts at this test got
        wrong (they watched ``os.write``, no longer on this path, and the POSIX
        mode, which ``mkstemp`` already sets to 0600).
        """
        sizes: list[int] = []
        real_restrict = webhooks.platform_compat.restrict_to_owner

        def _measuring_restrict(target):
            sizes.append(Path(target).stat().st_size)
            return real_restrict(target)

        monkeypatch.setattr(
            webhooks.platform_compat, "restrict_to_owner", _measuring_restrict
        )

        payload = {"signing_secret": "s3cr3t" * 200}
        webhooks.write_json_atomic(tmp_path / "store.json", payload)

        assert sizes, "premise: the lockdown ran at all"
        assert sizes[0] == 0, (
            "the file already held payload bytes when it was locked down: "
            f"{sizes[0]} bytes"
        )

    def test_an_unreadable_store_is_not_silently_overwritten(self, tmp_path):
        """A corrupt store must be preserved, not replaced with a fresh one.

        Returning an empty default for an unparseable file meant the next write
        serialised that default over the top, destroying every issued credential
        and its signing secret — and, because the kill switch lives in the same
        file, silently re-enabling webhooks.
        """
        store = webhooks.WebhookTokenStore(tmp_path)
        good_raw, _s, good = store.create("keeper")
        assert store.is_switch_on() is True
        store.set_switch(False)

        corrupt = store.path.read_text(encoding="utf-8")[:40]
        store.path.write_text(corrupt, encoding="utf-8")

        # Every path that would rewrite the file must refuse instead.
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            store.create("would-clobber")
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            store.set_switch(True)
        with pytest.raises(webhooks.WebhookStoreUnreadable):
            store.is_switch_on()

        # The bytes are still there for an operator to recover.
        assert store.path.read_text(encoding="utf-8") == corrupt
        assert good["id"] and good_raw

    def test_an_unreadable_history_does_not_abort_the_run_it_records(self, tmp_path):
        """Recording a run must never fail the webhook turn it describes.

        The store read refuses rather than reporting an empty file, and
        ``record`` is called on the rejection paths AND after a completed turn.
        Letting the refusal escape turned rejections into 500s and discarded
        finished turn output, so it is caught and logged like a failed write.
        """
        runs = webhooks.WebhookRunStore(tmp_path)
        runs.record(outcome=webhooks.OUTCOME_COMPLETED, name="first")
        runs.path.write_text("{truncated", encoding="utf-8")

        # Must return a record rather than raising.
        rec = runs.record(outcome=webhooks.OUTCOME_COMPLETED, name="after corruption")
        assert rec["outcome"] == webhooks.OUTCOME_COMPLETED
        # And the unreadable bytes are still on disk, not replaced.
        assert runs.path.read_text(encoding="utf-8") == "{truncated"

    def test_the_credential_directory_and_its_temp_files_are_all_gated(self):
        """Agent file tools must be refused the store AND its temp siblings.

        The store is published with ``mkstemp`` + ``os.replace``. Gating only the
        final filename left the not-yet-renamed ``*.tmp`` inode writable by a
        same-UID agent — 0600 does not stop the same user — so an agent could
        write its own bearer hash there and the rename would publish it as the
        live credential store, minting access to ``/api/hooks/agent``. The gate
        therefore has to cover the whole directory, which is what this pins.
        """
        from kiro_crew import security

        home = pathlib.Path.home()
        store_dir = home / ".kiro/crew" / webhooks.SECRETS_DIRNAME

        targets = [
            store_dir,                                     # the directory itself
            store_dir / webhooks.TOKENS_FILENAME,          # the live store
            store_dir / f"{webhooks.TOKENS_FILENAME}.lock",  # its lock file
            store_dir / "tmpa1b2c3.tmp",                   # a mkstemp sibling
            store_dir / "anything-an-agent-picks.json",
        ]
        for target in targets:
            assert security.is_sensitive_path(str(target)), (
                f"agent file tools must refuse {target}"
            )

    def test_the_store_path_lives_inside_the_gated_directory(self, tmp_path):
        """The writer must actually use the gated location.

        Without this the gate above could pass while the store still wrote
        somewhere ungated, which is the exact shape of the original defect.
        """
        store = webhooks.WebhookTokenStore(tmp_path)
        assert store.path.parent.name == webhooks.SECRETS_DIRNAME
        assert store.path.parent.parent == tmp_path

        # And the temp file a write produces is a sibling inside that directory.
        store.create("gated")
        assert store.path.is_file()
        assert not list(tmp_path.glob("*.tmp")), "temp file escaped to the parent"

    def test_write_routes_permissions_through_windows_safe_helpers(
        self, store, monkeypatch
    ):
        """No POSIX-only call may sit on the store write path.

        Asserted by observing that the write goes through
        ``platform_compat.fchmod_safe`` — which no-ops off POSIX — rather than
        by flipping ``IS_POSIX`` globally (that would also switch the advisory
        lock to its ``msvcrt`` branch, testing the harness) or by forbidding
        ``os.fchmod`` (the helper legitimately calls it on POSIX, and both
        modules share one ``os``, so the guard could not tell them apart).
        """
        calls: list[str] = []
        real_fchmod = webhooks.platform_compat.fchmod_safe

        def _spy_fchmod(fd: int, mode: int) -> None:
            calls.append("fchmod_safe")
            assert mode == 0o600
            real_fchmod(fd, mode)

        monkeypatch.setattr(webhooks.platform_compat, "fchmod_safe", _spy_fchmod)

        raw, _secret, entry = store.create("windows caller")
        assert store.verify(raw) == entry["id"]
        # The other writes on the same path: stamp, switch, revoke.
        store.stamp_used(entry["id"])
        assert store.set_switch(False) is False
        assert store.delete(entry["id"]) is True
        assert calls.count("fchmod_safe") >= 4

    def test_permissions_go_through_platform_compat(self, store, monkeypatch):
        """The owner-only lockdown is applied, and to the temp file pre-rename."""
        seen: list[tuple[str, object]] = []
        real_restrict = webhooks.platform_compat.restrict_to_owner

        def _spy_fchmod(fd: int, mode: int) -> None:
            seen.append(("fchmod_safe", mode))

        def _spy_restrict(path) -> None:
            # Still a temp file at this point: locking down only after the
            # rename would leave the secrets briefly readable at the real path.
            seen.append(("restrict_to_owner", str(path).endswith(".tmp")))
            real_restrict(path)

        monkeypatch.setattr(webhooks.platform_compat, "fchmod_safe", _spy_fchmod)
        monkeypatch.setattr(webhooks.platform_compat, "restrict_to_owner", _spy_restrict)
        store.create("audited")

        assert ("fchmod_safe", 0o600) in seen
        assert ("restrict_to_owner", True) in seen

    def test_failed_lockdown_leaves_no_file_behind(self, store, monkeypatch):
        """A store holding secrets must not land if it cannot be locked down."""
        def _boom(path) -> None:
            raise OSError("icacls unavailable")

        monkeypatch.setattr(webhooks.platform_compat, "restrict_to_owner", _boom)
        with pytest.raises(OSError):
            store.create("unlockable")
        assert not store.path.exists()
        assert not list(store.path.parent.glob("*.tmp"))


class TestCredentialStoreIsOffTheAgentFileFloor:
    """The token store must be unreachable through agent file tools.

    ``/api/hooks/agent`` is on the dashboard-auth bypass list because it
    authenticates itself against ``webhook_tokens.json``. That makes the file an
    auth boundary: an agent able to WRITE it could append a bearer hash of its
    own choosing and then drive arbitrary agent turns through the external route,
    and one able to READ it could sign requests as an existing integration with
    the stored HMAC secret. So it belongs on the same floor as the other
    data-home secrets, not merely at mode 0600 (which does not isolate another
    process running as the same user).
    """

    def test_token_store_is_a_sensitive_path(self):
        from kiro_crew.hooks import is_sensitive_path

        assert is_sensitive_path(str(webhooks.WebhookTokenStore().path))

    def test_it_is_registered_as_a_crew_secret_directory(self):
        """The whole credential directory is on the agent file floor.

        Registered as a DIRECTORY rather than a filename so the store's mkstemp
        temp files and lock file are covered too: gating just the filename left
        the pre-rename inode writable by a same-UID agent, and os.replace would
        have published that content as the live store.
        """
        from kiro_crew import security

        assert webhooks.SECRETS_DIRNAME in security._CREW_SECRET_LEAVES

    def test_the_store_itself_still_works(self, tmp_path):
        """The gate must not break the legitimate reader/writer."""
        store = webhooks.WebhookTokenStore(tmp_path)
        raw, _secret, entry = store.create("CI runner")
        assert store.verify(raw) == entry["id"]


class TestKillSwitchPreservesListFormStore:
    """A bare top-level list is a legal store shape, and toggling must keep it.

    ``_load`` reads a top-level list as the token rows, so a store written that
    way is valid and readable. ``set_switch`` writes the whole file back, so if
    it treats a non-dict as "no store" it replaces real credentials with an empty
    token list — silent, irreversible loss of every token and signing secret,
    which is precisely what WebhookStoreUnreadable exists to prevent elsewhere.
    """

    def _list_form_payload(self, store) -> list[dict[str, object]]:
        """A list-form store built from a genuinely issued row."""
        raw, _secret, entry = store.create("CI runner")
        rows = store.list_entries()
        assert rows, "expected the created row to be readable"
        store.path.write_text(json.dumps(rows), encoding="utf-8")
        assert isinstance(json.loads(store.path.read_text(encoding="utf-8")), list)
        return rows

    def test_disabling_keeps_the_tokens(self, store):
        rows = self._list_form_payload(store)

        store.set_switch(False)

        assert store.is_switch_on() is False
        surviving = store.list_entries()
        assert [row["token_hash"] for row in surviving] == [row["token_hash"] for row in rows]

    def test_enabling_keeps_the_tokens(self, store):
        rows = self._list_form_payload(store)

        store.set_switch(True)

        assert store.is_switch_on() is True
        assert [row["token_hash"] for row in store.list_entries()] == [
            row["token_hash"] for row in rows
        ]

    def test_the_secret_still_verifies_after_a_toggle(self, store):
        """The signing secret must survive, not just the row count."""
        raw, _secret, entry = store.create("Deploy pipeline")
        rows = store.list_entries()
        store.path.write_text(json.dumps(rows), encoding="utf-8")

        store.set_switch(False)

        assert store.verify(raw) == entry["id"]

    def test_a_scalar_store_is_still_treated_as_absent(self, store):
        """Only dicts and lists are real shapes; a scalar carries nothing to keep."""
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("42", encoding="utf-8")

        store.set_switch(False)

        data = json.loads(store.path.read_text(encoding="utf-8"))
        assert data == {"enabled": False, "tokens": []}
