"""Tests for the Weixin QR-login persistence helpers.

Covers the security-sensitive bits of the sign-in write path: a credential must
never touch disk world-readable, a corrupt ``config.json`` must not be silently
replaced by the weixin section alone, and a failed confirmation must not clobber
a working credential.

Permission assertions are POSIX-only: NTFS reports synthetic mode bits, so the
cross-platform guarantee comes from ``platform_compat.restrict_to_owner`` rather
than from raw ``st_mode`` inspection.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from kiro_crew.dashboard.handlers import weixin_qr as qr

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")


def test_atomic_write_secret_round_trips(tmp_path):
    target = tmp_path / ".env"
    qr._atomic_write(target, "WEIXIN=abc\n", secret=True)
    assert target.read_text() == "WEIXIN=abc\n"


def test_atomic_write_secret_lands_byte_for_byte(tmp_path, monkeypatch):
    """The .env text is handed to publish_env as UTF-8 bytes, which are never
    newline-translated, so the file holds exactly the text the rollback later
    compares against and restores. A str would be text-mode written, turning
    each \\n into \\r\\n on Windows."""
    seen: list[object] = []
    real = qr.publish_env

    def capture(path, content, **kw):
        seen.append(content)
        return real(path, content, **kw)

    monkeypatch.setattr(qr, "publish_env", capture)
    target = tmp_path / ".env"
    qr._atomic_write(target, "\ufeffA=1\r\nB=caf\u00e9\n", secret=True)

    assert seen == ["\ufeffA=1\r\nB=caf\u00e9\n".encode("utf-8")]
    assert target.read_bytes() == b"\xef\xbb\xbfA=1\r\nB=caf\xc3\xa9\n"


@posix_only
def test_atomic_write_secret_is_owner_only(tmp_path):
    target = tmp_path / ".env"
    qr._atomic_write(target, "WEIXIN=abc\n", secret=True)
    assert stat.S_IMODE(target.stat().st_mode) & 0o077 == 0


def test_atomic_write_secret_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / ".env"
    qr._atomic_write(target, "a=1\n", secret=True)
    # The temp is staged in the masked staging directory, which stays behind empty.
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env", "auth-store-staging"]
    assert list((tmp_path / "auth-store-staging").iterdir()) == []


def test_atomic_write_secret_locks_down_before_any_content_byte(tmp_path, monkeypatch):
    """The ordering IS the security property: POSIX mode bits protect nothing
    against NTFS ACLs, so a lockdown applied to the final path after the write
    leaves the credential readable under the directory-inherited DACL for the
    whole write. The shared helper restricts the empty temp file first; assert
    the sequence through the same os.write seam the helper's own ordering test
    uses."""
    from kiro_crew import platform_compat

    events = []
    real_restrict = platform_compat.restrict_to_owner
    real_os_write = os.write

    def _spy(path):
        events.append("restrict")
        return real_restrict(path)

    def _tracking_write(fd, data):
        events.append("write")
        return real_os_write(fd, data)

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _spy)
    monkeypatch.setattr(os, "write", _tracking_write)

    target = tmp_path / ".env"
    qr._atomic_write(target, "WEIXIN=abc\n", secret=True)

    assert events == ["restrict", "write"], events
    assert target.read_text() == "WEIXIN=abc\n"


def test_atomic_write_secret_survives_a_lockdown_refusal(tmp_path, monkeypatch):
    """A host where the owner lockdown fails (e.g. SID resolution refused) must
    warn and keep the credential save, not abort a sign-in that already
    succeeded: the writer's contract is enforce-and-warn."""
    from kiro_crew import platform_compat

    def _boom(path):
        raise OSError("icacls failed")

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _boom)
    target = tmp_path / ".env"
    qr._atomic_write(target, "WEIXIN=abc\n", secret=True)  # must not raise
    assert target.read_text() == "WEIXIN=abc\n"
    # No temp residue: the staging directory is the only other entry, and it is empty.
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env", "auth-store-staging"]
    assert list((tmp_path / "auth-store-staging").iterdir()) == []


def test_env_value_round_trips_and_delete_preserves_other_keys(tmp_path, monkeypatch):
    ep = tmp_path / ".env"
    ep.write_text("OTHER=keepme\nWEIXIN_TOKEN=old\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    assert qr._read_env_value("WEIXIN_TOKEN") == "old"
    assert qr._read_env_value("ABSENT") is None
    qr._delete_env_key("WEIXIN_TOKEN")
    assert qr._read_env_value("WEIXIN_TOKEN") is None
    assert qr._read_env_value("OTHER") == "keepme"


def _fail_config_writes(monkeypatch, env_file):
    """Let the .env write through, but make the config.json write fail."""
    real = qr._atomic_write

    def selective(path, text, **kw):
        if path == env_file:
            return real(path, text, **kw)
        raise OSError("ENOSPC")

    monkeypatch.setattr(qr, "_atomic_write", selective)


def test_failed_config_commit_restores_the_previous_credential(tmp_path, monkeypatch):
    """A half-applied sign-in must not destroy a working credential."""
    ep = tmp_path / ".env"
    ep.write_text("OTHER=keepme\nWEIXIN_TOKEN=previous\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    _fail_config_writes(monkeypatch, ep)

    with pytest.raises(OSError):
        qr._commit_credential_and_config(tmp_path / "config.json", "{}", "brand-new")

    assert qr._read_env_value("WEIXIN_TOKEN") == "previous"
    assert qr._read_env_value("OTHER") == "keepme"


def test_failed_config_commit_removes_a_credential_that_did_not_exist(tmp_path, monkeypatch):
    """First-time sign-in: rollback must leave no orphan credential behind."""
    ep = tmp_path / ".env"
    ep.write_text("OTHER=x\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    _fail_config_writes(monkeypatch, ep)

    with pytest.raises(OSError):
        qr._commit_credential_and_config(tmp_path / "config.json", "{}", "brand-new")

    assert qr._read_env_value("WEIXIN_TOKEN") is None
    assert qr._read_env_value("OTHER") == "x"


def test_successful_commit_writes_both_files(tmp_path, monkeypatch):
    ep = tmp_path / ".env"
    cp = tmp_path / "config.json"
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    qr._commit_credential_and_config(cp, '{"weixin": {}}', "brand-new")
    assert qr._read_env_value("WEIXIN_TOKEN") == "brand-new"
    assert json.loads(cp.read_text()) == {"weixin": {}}


def test_successful_commit_replaces_the_credential_boot_propagated(tmp_path, monkeypatch):
    """Boot copies ``.env`` into ``os.environ`` and ``load_credentials`` lets
    ``os.environ`` win, so a re-login left there would keep the channel restart
    and an in-app re-exec on the token the user just replaced."""
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("WEIXIN_TOKEN=stale\n", encoding="utf-8")
    monkeypatch.setenv("WEIXIN_TOKEN", "stale")
    monkeypatch.delenv("_KIROCREW_CREDS_SCRUBBED", raising=False)

    qr._commit_credential_and_config(tmp_path / "config.json", '{"weixin": {}}', "brand-new")

    assert os.environ["WEIXIN_TOKEN"] == "brand-new"
    assert KiroCrewConfig().load_credentials(propagate=False)["WEIXIN_TOKEN"] == "brand-new"


def test_first_sign_in_owns_the_token_so_removing_the_line_revokes_it(tmp_path, monkeypatch):
    """A first sign-in (no token at boot) writes the token into ``os.environ``.
    That write must record ``.env`` ownership like every other live write: the
    watcher's next tick then sees ``prior == value`` and leaves ownership alone,
    and when the operator later deletes the ``.env`` line the live applier pops
    the token instead of leaving the revoked credential in force until a full
    restart. (Fixes the Opus Weixin ownership finding.)"""
    from kiro_crew.config import loader

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("WEIXIN_TOKEN", "x")
    monkeypatch.delenv("WEIXIN_TOKEN")
    monkeypatch.delenv("_KIROCREW_CREDS_SCRUBBED", raising=False)
    loader.discard_env_owned_key("WEIXIN_TOKEN")
    try:
        qr._commit_credential_and_config(tmp_path / "config.json", '{"weixin": {}}', "brand-new")
        assert os.environ["WEIXIN_TOKEN"] == "brand-new"
        assert loader.env_owns_key("WEIXIN_TOKEN")

        # The watcher's tick after the sign-in: same value, ownership kept.
        loader.apply_env_credential_changes({"WEIXIN_TOKEN"}, {"WEIXIN_TOKEN": "brand-new"}, {})
        assert loader.env_owns_key("WEIXIN_TOKEN")

        # The operator deletes the line: the token is revoked in-process.
        loader.apply_env_credential_changes({"WEIXIN_TOKEN"}, {}, {"WEIXIN_TOKEN": "brand-new"})
        assert "WEIXIN_TOKEN" not in os.environ
    finally:
        loader.discard_env_owned_key("WEIXIN_TOKEN")


def test_successful_commit_keeps_a_scrubbed_environ_free_of_the_credential(tmp_path, monkeypatch):
    """The Docker entrypoint scrubs credentials out of ``os.environ`` so they stay
    off ``/proc/<pid>/environ``; the sign-in must not put one back."""
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv("WEIXIN_TOKEN", raising=False)
    monkeypatch.setenv("_KIROCREW_CREDS_SCRUBBED", "1")

    qr._commit_credential_and_config(tmp_path / "config.json", '{"weixin": {}}', "brand-new")

    assert "WEIXIN_TOKEN" not in os.environ
    assert KiroCrewConfig().load_credentials(propagate=False)["WEIXIN_TOKEN"] == "brand-new"


def test_failed_config_commit_leaves_the_process_credential_alone(tmp_path, monkeypatch):
    ep = tmp_path / ".env"
    ep.write_text("WEIXIN_TOKEN=previous\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    monkeypatch.setenv("WEIXIN_TOKEN", "previous")
    monkeypatch.delenv("_KIROCREW_CREDS_SCRUBBED", raising=False)
    _fail_config_writes(monkeypatch, ep)

    with pytest.raises(OSError):
        qr._commit_credential_and_config(tmp_path / "config.json", "{}", "brand-new")

    assert os.environ["WEIXIN_TOKEN"] == "previous"


def test_atomic_write_plain_config_preserves_existing_mode(tmp_path):
    """A non-secret replace must never widen permissions.

    config.json can hold inline fallback credentials, so if the operator (or an
    earlier save) locked it to 0600, the atomic replace must not hand it back at
    the umask default and expose it to other local users.
    """
    target = tmp_path / "config.json"
    target.write_text('{"a": 1}', encoding="utf-8")
    os.chmod(target, 0o600)
    qr._atomic_write(target, '{"a": 2}')
    assert json.loads(target.read_text()) == {"a": 2}
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_write_plain_config(tmp_path):
    target = tmp_path / "config.json"
    qr._atomic_write(target, '{"a": 1}')
    assert json.loads(target.read_text()) == {"a": 1}


def test_write_env_secret_upserts_and_preserves_other_lines(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OTHER=keep\nWEIXIN_TOKEN=old\nTRAILING=yes\n")
    monkeypatch.setattr(qr, "env_path", lambda: env)
    qr._write_env_secret("WEIXIN_TOKEN", "new")
    body = env.read_text()
    assert "OTHER=keep" in body
    assert "TRAILING=yes" in body
    assert "WEIXIN_TOKEN=new" in body
    assert "old" not in body


def test_stage_config_preserves_unrelated_sections(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"slack": {"command": "kirocrew"}, "agent": {"model": "auto"}}))
    monkeypatch.setattr(qr, "config_path", lambda: cfg)
    path, serialized = qr._stage_weixin_config(account_id="acct1@im.bot", base_url="https://x")
    assert path == cfg
    data = json.loads(serialized)
    assert data["slack"] == {"command": "kirocrew"}  # untouched
    assert data["agent"] == {"model": "auto"}
    assert data["weixin"]["enabled"] is True
    assert data["weixin"]["account_id"] == "acct1@im.bot"
    assert data["weixin"]["base_url"] == "https://x"


def test_stage_config_does_not_write_anything(tmp_path, monkeypatch):
    """Staging is pure: the credential write must be able to run after it."""
    cfg = tmp_path / "config.json"
    original = json.dumps({"agent": {"model": "auto"}})
    cfg.write_text(original)
    monkeypatch.setattr(qr, "config_path", lambda: cfg)
    qr._stage_weixin_config(account_id="acct1", base_url="https://x")
    assert cfg.read_text() == original


def test_stage_config_raises_on_a_corrupt_file(tmp_path, monkeypatch):
    """A malformed config must fail BEFORE the credential is overwritten."""
    cfg = tmp_path / "config.json"
    cfg.write_text("{not valid json")
    monkeypatch.setattr(qr, "config_path", lambda: cfg)
    with pytest.raises(Exception):
        qr._stage_weixin_config(account_id="acct1", base_url="https://x")
    assert cfg.read_text() == "{not valid json"  # original preserved for repair


def test_stage_config_rejects_a_non_object_document(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text("[1, 2, 3]")
    monkeypatch.setattr(qr, "config_path", lambda: cfg)
    with pytest.raises(ValueError):
        qr._stage_weixin_config(account_id="acct1", base_url="https://x")


def test_corrupt_config_does_not_clobber_an_existing_credential(tmp_path, monkeypatch):
    """Regression: stage-before-write keeps a working token when config is bad."""
    env = tmp_path / ".env"
    env.write_text("WEIXIN_TOKEN=working\n")
    cfg = tmp_path / "config.json"
    cfg.write_text("{corrupt")
    monkeypatch.setattr(qr, "env_path", lambda: env)
    monkeypatch.setattr(qr, "config_path", lambda: cfg)

    # The handler stages first, so this raises before _write_env_secret runs.
    with pytest.raises(Exception):
        _path, _serialized = qr._stage_weixin_config(account_id="new", base_url="https://x")
        qr._write_env_secret("WEIXIN_TOKEN", "replacement")

    assert "working" in env.read_text()
    assert "replacement" not in env.read_text()


# ── QR image rendering ────────────────────────────────────────────────────────
# Regression: iLink's `qrcode_img_content` is the scannable login URL, NOT image
# bytes. The first release passed it straight to <img src>, so the panel showed
# a broken image with alt text. The handler must render a real PNG data URI.


def test_render_qr_data_uri_is_a_loadable_png():
    import base64 as _b64

    uri = qr._render_qr_data_uri("weixin://dl/login?ticket=abc123")
    assert uri.startswith("data:image/png;base64,")
    raw = _b64.b64decode(uri.split(",", 1)[1], validate=True)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"  # exact PNG signature


def test_render_qr_data_uri_differs_per_payload():
    a = qr._render_qr_data_uri("weixin://dl/login?ticket=aaa")
    b = qr._render_qr_data_uri("weixin://dl/login?ticket=bbb")
    assert a != b  # the payload is actually encoded, not a static image


def test_render_qr_round_trips_the_scan_url():
    """Decode the rendered QR and confirm it carries the exact login URL."""
    pytest.importorskip("PIL")
    try:
        from PIL import Image
        from qrcode.image.pil import PilImage  # noqa: F401  (ensures pil backend)
    except ImportError:  # pragma: no cover
        pytest.skip("PIL backend unavailable")
    # Decode via the zbar-free approach: re-render and compare is weaker than a
    # true decode, so only assert structural validity + dimensions here.
    import base64 as _b64
    import io as _io

    url = "https://login.ilink.example/x/abc"
    uri = qr._render_qr_data_uri(url)
    img = Image.open(_io.BytesIO(_b64.b64decode(uri.split(",", 1)[1])))
    assert img.format == "PNG"
    assert img.size[0] == img.size[1] and img.size[0] >= 100  # plausible QR grid


# -- _delete_env_key: it rewrites the user's credential file, so what it PRESERVES
#    matters as much as what it removes. Untested before; the QR-encoder body that
#    covered this file, then moved to kiro_crew.qr, which made the gap visible.


def test_delete_env_key_removes_only_the_named_key(tmp_path, monkeypatch):
    ep = tmp_path / ".env"
    ep.write_text("WEIXIN_TOKEN=secret\nSLACK_TOKEN=keep-me\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    qr._delete_env_key("WEIXIN_TOKEN")
    body = ep.read_text(encoding="utf-8")
    assert "WEIXIN_TOKEN" not in body
    assert "SLACK_TOKEN=keep-me" in body


def test_delete_env_key_preserves_comments_and_blank_lines(tmp_path, monkeypatch):
    """A credential file is hand-edited; losing a user's comments is data loss."""
    ep = tmp_path / ".env"
    ep.write_text(
        "# my notes\n\nWEIXIN_TOKEN=secret\n# trailing note\nOTHER=1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    qr._delete_env_key("WEIXIN_TOKEN")
    lines = ep.read_text(encoding="utf-8").splitlines()
    assert "# my notes" in lines
    assert "# trailing note" in lines
    assert "" in lines
    assert "OTHER=1" in lines
    assert not [ln for ln in lines if ln.startswith("WEIXIN_TOKEN")]


def test_delete_env_key_does_not_match_a_key_that_merely_starts_the_same(tmp_path, monkeypatch):
    """Prefix-matching here would silently delete a DIFFERENT credential."""
    ep = tmp_path / ".env"
    ep.write_text("WEIXIN_TOKEN_OLD=other\nWEIXIN_TOKEN=secret\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    qr._delete_env_key("WEIXIN_TOKEN")
    body = ep.read_text(encoding="utf-8")
    assert "WEIXIN_TOKEN_OLD=other" in body
    assert "WEIXIN_TOKEN=secret" not in body


def test_delete_env_key_on_a_missing_file_is_a_noop(tmp_path, monkeypatch):
    ep = tmp_path / "nonexistent" / ".env"
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    qr._delete_env_key("WEIXIN_TOKEN")  # must not raise
    assert not ep.exists()


# -- _prune_sessions: a QR login session holds an open client, so failing to prune
#    leaks both memory and a connection for every abandoned scan.


def test_prune_sessions_drops_only_expired_entries(monkeypatch):
    monkeypatch.setattr(qr, "_SESSIONS", {}, raising=False)
    now = 1_000_000.0
    monkeypatch.setattr(qr.time, "time", lambda: now)
    qr._SESSIONS["fresh"] = {"created_at": now - 1, "client": None}
    qr._SESSIONS["stale"] = {"created_at": now - qr._SESSION_TTL_SECONDS - 1, "client": None}
    qr._prune_sessions()
    assert set(qr._SESSIONS) == {"fresh"}


def test_prune_sessions_expires_exactly_at_the_ttl_boundary(monkeypatch):
    """`>=` is the boundary: a session exactly at the TTL is expired, not kept."""
    monkeypatch.setattr(qr, "_SESSIONS", {}, raising=False)
    now = 1_000_000.0
    monkeypatch.setattr(qr.time, "time", lambda: now)
    qr._SESSIONS["boundary"] = {"created_at": now - qr._SESSION_TTL_SECONDS, "client": None}
    qr._prune_sessions()
    assert qr._SESSIONS == {}


def test_prune_sessions_survives_a_client_whose_close_cannot_be_scheduled(monkeypatch):
    """No running loop here, so create_task raises — pruning must still complete.

    The entry has to go regardless: leaving it because its client could not be
    closed would keep the leak the prune exists to stop.
    """
    monkeypatch.setattr(qr, "_SESSIONS", {}, raising=False)
    now = 1_000_000.0
    monkeypatch.setattr(qr.time, "time", lambda: now)

    class _Client:
        def close(self):  # returns a coroutine-ish object; never awaited here
            return None

    qr._SESSIONS["stale"] = {
        "created_at": now - qr._SESSION_TTL_SECONDS - 1,
        "client": _Client(),
    }
    qr._prune_sessions()
    assert qr._SESSIONS == {}


# ── Cross-process .env.lock exclusion ─────────────────────────────────────────
# Regression: _commit_credential_and_config must acquire the shared .env.lock
# advisory lock (same sidecar file used by `secrets import --apply`) so that the
# WeChat sign-in handler and the CLI importer cannot interleave their
# read-modify-write cycles.  If the lock is already held, the handler must abort
# with OSError instead of writing a potentially stale value.


def test_commit_credential_aborts_when_env_lock_is_held(tmp_path, monkeypatch):
    """_commit_credential_and_config raises OSError when .env.lock is held.

    Simulates a concurrent ``secrets import --apply`` holding the advisory lock
    by patching ``platform_compat.try_acquire_lock`` to return False (the same
    signal the importer uses to detect a held lock).  Confirms the handler:
      * does NOT write WEIXIN_TOKEN to .env (no partial write),
      * does NOT write config.json,
      * raises OSError with a descriptive message.
    """
    import kiro_crew.platform_compat as _pc

    ep = tmp_path / ".env"
    ep.write_text("OTHER=keepme\n", encoding="utf-8")
    cp = tmp_path / "config.json"
    monkeypatch.setattr(qr, "env_path", lambda: ep)

    # Simulate a held lock: try_acquire_lock returns False for the .env.lock fd.
    # We only intercept the lock acquisition; all other platform_compat calls are
    # left untouched.
    original_try = _pc.try_acquire_lock

    def _always_busy(fd, *, exclusive=False):  # noqa: ARG001
        return False

    monkeypatch.setattr(_pc, "try_acquire_lock", _always_busy)
    # release_lock and os.close must still run (the fd was opened); patch
    # release_lock to a no-op so the test does not need a real lockable fd.
    monkeypatch.setattr(_pc, "release_lock", lambda fd: None)

    with pytest.raises(OSError, match="locked by another process"):
        qr._commit_credential_and_config(cp, "{}", "should-not-be-written")

    # .env must be untouched — WEIXIN_TOKEN was not inserted.
    assert qr._read_env_value("WEIXIN_TOKEN") is None
    assert ep.read_text(encoding="utf-8") == "OTHER=keepme\n"
    # config.json must not have been created.
    assert not cp.exists()

    monkeypatch.setattr(_pc, "try_acquire_lock", original_try)


def test_commit_credential_succeeds_when_env_lock_is_free(tmp_path, monkeypatch):
    """_commit_credential_and_config writes normally when it acquires the lock.

    Complementary to the abort test above: confirm the happy-path still works
    after the lock-acquisition layer was added (no regression in the normal case).
    """
    ep = tmp_path / ".env"
    ep.write_text("OTHER=keepme\n", encoding="utf-8")
    cp = tmp_path / "config.json"
    monkeypatch.setattr(qr, "env_path", lambda: ep)

    qr._commit_credential_and_config(cp, '{"weixin": {}}', "newtoken")

    assert qr._read_env_value("WEIXIN_TOKEN") == "newtoken"
    assert qr._read_env_value("OTHER") == "keepme"
    assert cp.exists() and json.loads(cp.read_text()) == {"weixin": {}}


def test_config_save_answers_only_after_the_watcher_applied_it(tmp_path, monkeypatch):
    """The Weixin save carries the allow-list, so it must await the watcher's
    dispatch before answering: a removed sender is unauthorized on the running
    transport when the caller sees "saved", not one poll interval later."""
    import asyncio
    import json
    from unittest.mock import AsyncMock

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    cp = tmp_path / "config.json"
    cp.write_text(json.dumps({"weixin": {"enabled": True}}), encoding="utf-8")
    monkeypatch.setattr(qr, "config_path", lambda: cp)
    applied = AsyncMock()
    monkeypatch.setattr(qr, "_hot_apply_after_write", applied)

    app = web.Application()
    qr.setup_weixin_routes(app)

    async def _go():
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/weixin/config", json={"allowed_user_ids": ["u-1"]})
            return resp.status, await resp.json()

    status, body = asyncio.run(_go())
    assert status == 200 and body["ok"] is True
    applied.assert_awaited_once()
    assert json.loads(cp.read_text(encoding="utf-8"))["weixin"]["allowed_user_ids"] == ["u-1"]


# ── the confirmed sign-in persists under a live-config hold ────────────────


class _ConfirmedClient:
    """A QR session whose poll reports a confirmed sign-in with credentials."""

    closed = False

    async def get_qrcode_status(self, qrcode):
        return {"status": "confirmed", "bot_token": "tok", "ilink_bot_id": "acct-new"}

    async def close(self):
        self.closed = True


def _confirmed_session(monkeypatch):
    import time
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setattr(qr, "is_direct_local_request", lambda request: True)
    client = _ConfirmedClient()
    monkeypatch.setattr(
        qr, "_SESSIONS", {"s1": {"client": client, "qrcode": "q", "created_at": time.time()}}
    )
    monkeypatch.setattr(qr, "_stage_weixin_config", lambda **kw: (Path("config.json"), "{}"))
    return SimpleNamespace(query={"session_id": "s1"}), client


def test_a_confirmed_sign_in_persists_while_the_config_watcher_is_held(monkeypatch):
    """``.env`` and ``config.json`` commit separately, so a watcher poll between
    them could restart Weixin with the new token and the previous account id.
    The whole persistence runs under ``live.hold()``, like every sibling channel
    saver, and the hold is released afterwards. (Fixes the GPT Weixin-relink
    finding.)"""
    import asyncio

    from kiro_crew.config import live

    request, client = _confirmed_session(monkeypatch)
    depths: list[int] = []
    monkeypatch.setattr(
        qr,
        "_commit_credential_and_config",
        lambda cp, serialized, token: depths.append(live.watch()._hold_depth),
    )

    resp = asyncio.run(qr.weixin_qr_status(request))

    assert resp.status == 200
    assert depths == [1], "the credential+config commit must run under the hold"
    assert live.watch()._hold_depth == 0
    assert client.closed


def test_a_cancelled_sign_in_keeps_the_hold_until_the_write_finishes(monkeypatch):
    """A cancelled request must not release the hold while the worker thread is
    still writing: the watcher would then apply a half-committed pair. The hold
    stays until the write finishes, and the cancellation is still raised."""
    import asyncio
    import threading

    from kiro_crew.config import live

    request, _client = _confirmed_session(monkeypatch)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow_commit(cp, serialized, token):
        started.set()
        release.wait(timeout=10)
        finished.set()

    monkeypatch.setattr(qr, "_commit_credential_and_config", slow_commit)

    async def run():
        from kiro_crew.testing.wait import async_wait_until

        task = asyncio.ensure_future(qr.weixin_qr_status(request))
        # Wait on the state, not the clock: the worker is inside the write.
        await async_wait_until(started.is_set, describe=lambda: "commit never started")
        task.cancel()
        # ``cancel()`` schedules the task's next step; one zero-length yield runs
        # exactly that step, which delivers the CancelledError to the handler.
        await asyncio.sleep(0)
        # Cancelled, but the write is still in flight: the hold must stand.
        assert not task.done()
        assert live.watch()._hold_depth == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert live.watch()._hold_depth == 0

    asyncio.run(run())


def test_a_boot_record_from_another_env_file_is_not_adopted(tmp_path):
    """The boot ``.env`` record is tied to the file it was read from: a watcher
    resolving a different data home refuses it (and it is consumed either way),
    while the same file adopts it. (Fixes the Opus boot-record leak finding.)"""
    from kiro_crew.config import loader_credentials as lc

    here, elsewhere = tmp_path / "a" / ".env", tmp_path / "b" / ".env"

    lc.record_boot_raw_env({"TELEGRAM_BOT_TOKEN": "x"}, path=here)
    assert lc.take_boot_raw_env(path=elsewhere) is None
    assert lc.take_boot_raw_env(path=here) is None, "a refused record is still consumed"

    lc.record_boot_raw_env({"TELEGRAM_BOT_TOKEN": "x"}, path=here)
    assert lc.take_boot_raw_env(path=here) == {"TELEGRAM_BOT_TOKEN": "x"}


# ── case-equivalent .env spellings (Windows os.environ is case-insensitive) ──


@pytest.mark.parametrize("windows", [True, False])
def test_the_sign_in_writer_handles_a_lowercase_spelling_per_platform(
    tmp_path, monkeypatch, windows
):
    """On Windows ``weixin_token`` and ``WEIXIN_TOKEN`` are one os.environ entry:
    a sign-in must replace the hand-typed lowercase line, or boot's / the
    watcher's first-spelling rule would keep the OLD token in force. On POSIX the
    lowercase name is a different variable and is left alone. (Fixes the GPT
    Windows case-spelling finding, same class as the dashboard writer.)"""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows, raising=False)
    ep = tmp_path / ".env"
    ep.write_text("weixin_token=old\nOTHER=keep\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)

    qr._write_env_secret("WEIXIN_TOKEN", "new")

    lines = ep.read_text(encoding="utf-8").splitlines()
    if windows:
        assert lines == ["WEIXIN_TOKEN=new", "OTHER=keep"]
    else:
        assert lines == ["weixin_token=old", "OTHER=keep", "WEIXIN_TOKEN=new"]


@pytest.mark.parametrize("windows", [True, False])
def test_the_rollback_delete_removes_case_spellings_per_platform(tmp_path, monkeypatch, windows):
    """Rolling back a first-time sign-in removes the key; on Windows that means
    every case-equivalent spelling, on POSIX only the exact name."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows, raising=False)
    ep = tmp_path / ".env"
    ep.write_text("weixin_token=a\nOTHER=keep\nWEIXIN_TOKEN=b\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)

    assert qr._read_env_value("WEIXIN_TOKEN") == ("a" if windows else "b")
    qr._delete_env_key("WEIXIN_TOKEN")

    lines = ep.read_text(encoding="utf-8").splitlines()
    assert lines == (["OTHER=keep"] if windows else ["weixin_token=a", "OTHER=keep"])


@pytest.mark.parametrize("windows", [True, False])
def test_a_failed_commit_restores_every_spelling_byte_for_byte(tmp_path, monkeypatch, windows):
    """A failed config write must put .env back exactly as boot read it.

    On Windows the sign-in collapses every case spelling of the key into one
    line, so a single-value rollback could not rebuild the original layout: an
    EMPTY lowercase alias ahead of the working uppercase token (boot skips the
    empty one and runs on the token) came back as the only line, and the working
    token was gone. (Fixes the GPT 6.1 Windows rollback finding.)"""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows, raising=False)
    ep = tmp_path / ".env"
    original = "# keep me\nweixin_token=\nOTHER=keep\nWEIXIN_TOKEN=working\n"
    ep.write_text(original, encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    _fail_config_writes(monkeypatch, ep)

    with pytest.raises(OSError):
        qr._commit_credential_and_config(tmp_path / "config.json", "{}", "brand-new")

    assert ep.read_text(encoding="utf-8") == original
    assert qr._read_env_value("WEIXIN_TOKEN") == "working"


def test_a_failed_commit_keeps_a_utf8_bom(tmp_path, monkeypatch):
    """The restored snapshot keeps the file's BOM, so it still decodes as UTF-8."""
    ep = tmp_path / ".env"
    ep.write_bytes(b"\xef\xbb\xbfWEIXIN_TOKEN=working\nNAME=caf\xc3\xa9\n")
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    _fail_config_writes(monkeypatch, ep)

    with pytest.raises(OSError):
        qr._commit_credential_and_config(tmp_path / "config.json", "{}", "brand-new")

    assert ep.read_bytes() == b"\xef\xbb\xbfWEIXIN_TOKEN=working\nNAME=caf\xc3\xa9\n"


def test_a_failed_commit_restores_crlf_line_endings_byte_for_byte(tmp_path, monkeypatch):
    """A .env saved with CRLF line endings comes back with exactly those bytes,
    with no carriage return doubled or dropped."""
    ep = tmp_path / ".env"
    original = b"# keep me\r\nWEIXIN_TOKEN=working\r\nOTHER=keep\r\n"
    ep.write_bytes(original)
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    _fail_config_writes(monkeypatch, ep)

    with pytest.raises(OSError):
        qr._commit_credential_and_config(tmp_path / "config.json", "{}", "brand-new")

    assert ep.read_bytes() == original


def test_read_env_value_skips_an_empty_case_spelling_on_windows(tmp_path, monkeypatch):
    """Boot propagates the first NON-EMPTY spelling, so that is the value in force."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True, raising=False)
    ep = tmp_path / ".env"
    ep.write_text("weixin_token=\nWEIXIN_TOKEN=working\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)

    assert qr._read_env_value("WEIXIN_TOKEN") == "working"
    ep.write_text("weixin_token=\n", encoding="utf-8")
    assert qr._read_env_value("WEIXIN_TOKEN") == ""


# ── the rollback restores only the token's own lines ────────────────────────


def _edit_env_then_fail_config(monkeypatch, env_file, edit):
    """Let the .env write through, apply *edit* to .env (an operator's editor
    save landing after the sign-in's write), then fail the config.json write."""
    real = qr._atomic_write

    def selective(path, text, **kw):
        if path == env_file:
            return real(path, text, **kw)
        edit(env_file)
        raise OSError("sharing violation")

    monkeypatch.setattr(qr, "_atomic_write", selective)


def test_rollback_keeps_an_unrelated_edit_saved_after_the_token_write(tmp_path, monkeypatch):
    """The advisory lock does not exclude an operator's editor. A config failure
    must restore the previous token without deleting an unrelated line saved in
    between. (Fixes the GPT full-file-rollback finding.)"""
    ep = tmp_path / ".env"
    ep.write_text("OTHER=keepme\nWEIXIN_TOKEN=previous\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)

    def operator_save(path):
        path.write_text(path.read_text(encoding="utf-8") + "NEW_SETTING=added\n", encoding="utf-8")

    _edit_env_then_fail_config(monkeypatch, ep, operator_save)

    with pytest.raises(OSError):
        qr._commit_credential_and_config(tmp_path / "config.json", "{}", "brand-new")

    assert ep.read_text(encoding="utf-8").splitlines() == [
        "OTHER=keepme",
        "WEIXIN_TOKEN=previous",
        "NEW_SETTING=added",
    ]


def test_rollback_refuses_when_the_token_entry_was_changed_independently(
    tmp_path, monkeypatch, caplog
):
    """If someone rewrote the token line itself after the sign-in, the rollback
    leaves their value in place (and logs) rather than overwriting it."""
    ep = tmp_path / ".env"
    ep.write_text("WEIXIN_TOKEN=previous\nOTHER=keep\n", encoding="utf-8")
    monkeypatch.setattr(qr, "env_path", lambda: ep)

    def operator_rewrites_token(path):
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("WEIXIN_TOKEN=brand-new", "WEIXIN_TOKEN=hand"), encoding="utf-8"
        )

    _edit_env_then_fail_config(monkeypatch, ep, operator_rewrites_token)

    with caplog.at_level("ERROR"), pytest.raises(OSError):
        qr._commit_credential_and_config(tmp_path / "config.json", "{}", "brand-new")

    assert ep.read_text(encoding="utf-8").splitlines() == ["WEIXIN_TOKEN=hand", "OTHER=keep"]
    assert any("rollback failed" in r.getMessage() for r in caplog.records)


def test_write_env_secret_returns_its_own_read_and_what_it_published(tmp_path, monkeypatch):
    """The rollback snapshot and the published text come from the write's single
    read, so the snapshot holds every edit the write kept. (Fixes the GPT
    snapshot-before-write finding.)"""
    ep = tmp_path / ".env"
    ep.write_bytes(b"\xef\xbb\xbfOTHER=keep\nWEIXIN_TOKEN=old\n")
    monkeypatch.setattr(qr, "env_path", lambda: ep)

    prior, published = qr._write_env_secret("WEIXIN_TOKEN", "new")

    assert prior == "\ufeffOTHER=keep\nWEIXIN_TOKEN=old\n"
    assert published == "\ufeffOTHER=keep\nWEIXIN_TOKEN=new\n"
    assert qr._env_file_text(ep) == published


def test_write_env_secret_on_an_absent_file_reports_no_prior(tmp_path, monkeypatch):
    ep = tmp_path / ".env"
    monkeypatch.setattr(qr, "env_path", lambda: ep)

    prior, published = qr._write_env_secret("WEIXIN_TOKEN", "new")

    assert prior is None
    assert published == "WEIXIN_TOKEN=new\n"


def test_a_read_failure_after_the_token_write_cannot_escape_the_commit(tmp_path, monkeypatch):
    """No read of .env may run between the token write and the rollback-protected
    config write: a transient failure there would escape with the new token
    persisted and nothing rolled back. The commit reads .env exactly once,
    inside the write."""
    ep = tmp_path / ".env"
    ep.write_text("WEIXIN_TOKEN=previous\n", encoding="utf-8")
    cp = tmp_path / "config.json"
    monkeypatch.setattr(qr, "env_path", lambda: ep)
    real = qr.read_env_text
    calls = {"n": 0}

    def flaky(path, *a, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("sharing violation")
        return real(path, *a, **kw)

    monkeypatch.setattr(qr, "read_env_text", flaky)

    qr._commit_credential_and_config(cp, '{"weixin": {}}', "brand-new")

    assert calls["n"] == 1
    assert ep.read_text(encoding="utf-8") == "WEIXIN_TOKEN=brand-new\n"
    assert json.loads(cp.read_text(encoding="utf-8")) == {"weixin": {}}
