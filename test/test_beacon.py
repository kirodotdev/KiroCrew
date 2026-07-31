"""Tests for the anonymous usage beacon (kiro_crew.beacon).

Drives real production code — no reimplementation of the payload shape or the
suppression rules in the test, so drift in either fails here.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
import urllib.error

import pytest

from kiro_crew import beacon, platform_compat

# Captured before any fixture can monkeypatch the module attribute, so the
# dedicated tests below can exercise the REAL implementation.
_REAL_IS_DEFAULT_HOME = beacon.is_default_home


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the data home at tmp_path and neutralize ambient env.

    The real CI environment sets CI=1, which would otherwise suppress every
    send and make the positive-path tests vacuous.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(beacon, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(beacon, "is_default_home", lambda: True)
    monkeypatch.setattr(beacon, "is_ci", lambda: False)
    monkeypatch.delenv(beacon.DISABLE_ENV, raising=False)
    monkeypatch.delenv(beacon.DIST_ENV, raising=False)
    return tmp_path


class TestInstallId:
    def test_generated_once_and_stable(self, _isolated_home):
        first = beacon.install_id()
        assert len(first) == 32
        assert beacon.install_id() == first, "id must be stable across calls"

    def test_persisted_to_data_home(self, _isolated_home):
        ident = beacon.install_id()
        assert (_isolated_home / beacon.INSTALL_ID_FILE).read_text().strip() == ident

    def test_create_false_does_not_materialize(self, _isolated_home):
        assert beacon.install_id(create=False) == ""
        assert not (_isolated_home / beacon.INSTALL_ID_FILE).exists()

    def test_corrupt_id_is_regenerated(self, _isolated_home):
        (_isolated_home / beacon.INSTALL_ID_FILE).write_text("not-a-valid-id")
        fresh = beacon.install_id()
        assert len(fresh) == 32 and fresh != "not-a-valid-id"

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX, reason="/dev/zero and os.mkfifo are POSIX-only"
    )
    def test_special_file_symlink_is_not_read(self, _isolated_home):
        """A symlink to /dev/zero must not turn the read into an infinite one.

        Regression test: ``read_text`` follows symlinks, so this allocated
        unboundedly until OOM — inside the gateway's beacon thread. Bounded with a
        real timeout because the failure mode is "never returns", which a plain
        assertion cannot catch.
        """
        import os
        import threading

        os.symlink("/dev/zero", _isolated_home / beacon.INSTALL_ID_FILE)
        result: dict = {}

        def probe():
            result["id"] = beacon.install_id(create=False)

        t = threading.Thread(target=probe, daemon=True)
        t.start()
        t.join(10)
        assert not t.is_alive(), "read did not terminate — unbounded /dev/zero read"
        assert result["id"] == "", "a device node must be treated as absent"

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="os.mkfifo is POSIX-only")
    def test_fifo_does_not_block(self, _isolated_home):
        """A FIFO at the state path must be rejected without opening it."""
        import os
        import threading

        os.mkfifo(_isolated_home / beacon.STAMP_FILE)
        done = threading.Event()

        def probe():
            beacon.already_sent_today()
            done.set()

        threading.Thread(target=probe, daemon=True).start()
        assert done.wait(10), "opening a FIFO blocked forever"

    def test_oversized_state_file_is_bounded(self, _isolated_home):
        """A huge regular file must be read only up to the cap."""
        (_isolated_home / beacon.INSTALL_ID_FILE).write_text("a" * 2_000_000)
        # Far longer than a valid id, so it is corrupt -> regenerated, not returned.
        assert beacon.install_id(create=False) == ""

    def test_non_utf8_id_does_not_crash(self, _isolated_home):
        """A non-UTF-8 id file must be treated as corrupt, not raise.

        Regression test: a strict decode raises UnicodeDecodeError — a
        ValueError, NOT an OSError — so it escaped the handler and killed
        `kirocrew telemetry status` outright.
        """
        (_isolated_home / beacon.INSTALL_ID_FILE).write_bytes(b"\xff\xfe bad \x80")
        # status path: must report nothing rather than raise
        assert beacon.install_id(create=False) == ""
        # send path: must regenerate a valid id
        fresh = beacon.install_id()
        assert len(fresh) == 32
        info = beacon.status("https://e.invalid", enabled=True, app_version="1.2.3")
        assert beacon.DISABLE_ENV in beacon.format_status(info)

    def test_id_is_not_derived_from_identity(self, _isolated_home, monkeypatch):
        """The id must not be a function of hostname/username.

        Guards the deliberate choice NOT to reuse handlers_system's owner hash,
        which is HMAC(salt, hostname + ":" + username).
        """
        import getpass
        import platform as _platform

        monkeypatch.setattr(_platform, "node", lambda: "host-alpha")
        monkeypatch.setattr(getpass, "getuser", lambda: "alice")
        a = beacon.install_id()
        (_isolated_home / beacon.INSTALL_ID_FILE).unlink()
        monkeypatch.setattr(_platform, "node", lambda: "host-beta")
        monkeypatch.setattr(getpass, "getuser", lambda: "bob")
        b = beacon.install_id()
        assert a != b, "a fresh id must be random, not identity-derived"


class TestPayloadAllowlist:
    EXPECTED_KEYS = {"id", "v", "os", "arch", "py", "dist", "first_seen"}

    def test_exactly_seven_keys(self, _isolated_home):
        assert set(beacon.payload("1.2.3")) == self.EXPECTED_KEYS

    def test_no_value_leaks_identity_or_paths(self, _isolated_home, monkeypatch):
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", "/Users/secret/my-private-repo")
        blob = json.dumps(beacon.payload("1.2.3"))
        for forbidden in ("secret", "my-private-repo", "/Users", "\\Users"):
            assert forbidden not in blob

    def test_python_minor_has_no_patch_component(self, _isolated_home):
        assert beacon.python_minor().count(".") == 1

    def test_distribution_clamped_to_known_set(self, _isolated_home, monkeypatch):
        monkeypatch.setenv(beacon.DIST_ENV, "definitely-not-a-channel")
        assert beacon.distribution() == beacon.DEFAULT_DISTRIBUTION
        monkeypatch.setenv(beacon.DIST_ENV, "DMG")
        assert beacon.distribution() == "dmg", "case-insensitive, still clamped"

    def test_first_seen_flips_after_a_send(self, _isolated_home, monkeypatch):
        assert beacon.payload("1.2.3")["first_seen"] == "1"
        monkeypatch.setattr(beacon.urllib.request, "urlopen", _fake_urlopen())
        beacon.send("https://example.invalid", "1.2.3", enabled=True)
        assert beacon.payload("1.2.3")["first_seen"] == "0"


class TestSuppression:
    def test_env_opt_out_wins_over_enabled(self, _isolated_home, monkeypatch):
        monkeypatch.setenv(beacon.DISABLE_ENV, "1")
        ok, reason = beacon.should_send(enabled=True)
        assert not ok and beacon.DISABLE_ENV in reason

    def test_config_toggle_off(self, _isolated_home):
        ok, reason = beacon.should_send(enabled=False)
        assert not ok and "disabled" in reason

    def test_ci_suppressed(self, _isolated_home, monkeypatch):
        monkeypatch.setattr(beacon, "is_ci", lambda: True)
        ok, reason = beacon.should_send(enabled=True)
        assert not ok and "CI" in reason

    def test_non_default_home_suppressed(self, _isolated_home, monkeypatch):
        monkeypatch.setattr(beacon, "is_default_home", lambda: False)
        ok, reason = beacon.should_send(enabled=True)
        assert not ok and "KIROCREW_HOME" in reason


class TestDefaultHomeDetection:
    """Exercises the REAL is_default_home (the suppression fixture stubs it)."""

    @pytest.fixture(autouse=True)
    def _unstub(self, monkeypatch):
        monkeypatch.setattr(beacon, "is_default_home", _REAL_IS_DEFAULT_HOME)

    def test_dev_home_is_not_default(self, monkeypatch, tmp_path):
        """is_default_home must NOT compare against config_dir().

        config_dir() honors KIROCREW_HOME, so comparing the two would always
        match and the dev-home/pod suppression would never fire. This test
        failed against exactly that bug during development.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "dev-home"))
        assert beacon.is_default_home() is False

    def test_unset_home_is_default(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        assert beacon.is_default_home() is True

    def test_real_home_spelled_explicitly_is_default(self, monkeypatch):
        from pathlib import Path

        from kiro_crew.config.paths import CONFIG_DIR_LEAF, KIRO_BASE_DIR_NAME

        real = Path.home() / KIRO_BASE_DIR_NAME / CONFIG_DIR_LEAF
        monkeypatch.setenv("KIROCREW_HOME", str(real))
        assert beacon.is_default_home() is True


class TestThrottle:
    def test_second_send_same_day_suppressed(self, _isolated_home, monkeypatch):
        calls = []
        monkeypatch.setattr(
            beacon.urllib.request, "urlopen", _fake_urlopen(calls)
        )
        assert beacon.send("https://example.invalid", "1.2.3", enabled=True) is True
        assert beacon.send("https://example.invalid", "1.2.3", enabled=True) is False
        assert len(calls) == 1, "at most one request per day"

    def test_stamp_does_not_follow_a_symlink(self, _isolated_home, monkeypatch):
        """A symlink planted at the stamp path must not have its target clobbered.

        Regression test: `path.write_text` FOLLOWS a symlink, so a link at
        beacon_last_sent would have its TARGET truncated and overwritten with
        today's date on the first successful beacon. atomic_write renames over the
        path, replacing the link itself.
        """
        victim = _isolated_home / "important.txt"
        victim.write_text("USER DATA")
        (_isolated_home / beacon.STAMP_FILE).symlink_to("important.txt")

        monkeypatch.setattr(beacon.urllib.request, "urlopen", _fake_urlopen())
        assert beacon.send("https://e.invalid", "1.2.3", enabled=True) is True

        assert victim.read_text() == "USER DATA", "symlink target was clobbered"
        assert not (_isolated_home / beacon.STAMP_FILE).is_symlink()
        assert beacon.already_sent_today() is True

    def test_failed_send_is_not_stamped(self, _isolated_home, monkeypatch):
        def boom(*_a, **_k):
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(beacon.urllib.request, "urlopen", boom)
        assert beacon.send("https://example.invalid", "1.2.3", enabled=True) is False
        assert not beacon.already_sent_today(), "a failure must retry later"


class TestUrlAndTransport:
    def test_id_in_path_fields_in_query(self, _isolated_home):
        url = beacon.beacon_url("https://e.invalid", beacon.payload("1.2.3"))
        head, _, query = url.partition("?")
        assert head.startswith(f"https://e.invalid/b/{beacon.BEACON_SCHEMA}/")
        assert "id=" not in query, "id belongs in the path (clean dedup key)"
        for key in ("v=", "os=", "arch=", "py=", "dist=", "first_seen="):
            assert key in query

    def test_non_https_endpoint_rejected(self, _isolated_home):
        with pytest.raises(ValueError, match="https"):
            beacon.beacon_url("http://e.invalid", beacon.payload("1.2.3"))

    def test_malformed_id_rejected(self, _isolated_home):
        with pytest.raises(ValueError, match="malformed"):
            beacon.beacon_url("https://e.invalid", {"id": "short", "v": "1"})

    def test_empty_endpoint_never_sends(self, _isolated_home, monkeypatch):
        calls = []
        monkeypatch.setattr(beacon.urllib.request, "urlopen", _fake_urlopen(calls))
        assert beacon.send("", "1.2.3", enabled=True) is False
        assert calls == []

    def test_send_never_raises_on_any_error(self, _isolated_home, monkeypatch):
        for exc in (
            urllib.error.URLError("x"),
            OSError("y"),
            TimeoutError("z"),
            # NOT an OSError/ValueError subclass — needs naming explicitly.
            http.client.InvalidURL("bad host"),
            http.client.HTTPException("protocol error"),
        ):
            def boom(*_a, _e=exc, **_k):
                raise _e

            monkeypatch.setattr(beacon.urllib.request, "urlopen", boom)
            assert beacon.send("https://e.invalid", "1.2.3", enabled=True) is False

    def test_unwritable_data_home_is_silent(self, _isolated_home, monkeypatch):
        """An unwritable data home must not propagate out of send()/status().

        Regression test: should_send() and payload() probe the filesystem, and
        they used to run OUTSIDE send()'s try, so a PermissionError from
        config_dir() escaped into the gateway's daemon thread (traceback on every
        boot) and made `kirocrew telemetry status` crash — while the module
        documents an in-memory fallback for exactly this case.
        """
        def denied(*_a, **_k):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(beacon, "config_dir", denied)
        # already_sent_today() swallows OSError on its own, so drive the probe
        # that does NOT: the stamp/id lookups reached via payload() + status().
        monkeypatch.setattr(beacon, "already_sent_today", denied)

        assert beacon.send("https://e.invalid", "1.2.3", enabled=True) is False
        info = beacon.status("https://e.invalid", enabled=True, app_version="1.2.3")
        assert info["would_send"] is False
        assert "could not read the data home" in str(info["reason"])
        # Still renderable — a diagnostic must work when things are broken.
        assert beacon.DISABLE_ENV in beacon.format_status(info)

    def test_no_passwd_entry_is_silent(self, _isolated_home, monkeypatch):
        """Path.home() raises RuntimeError (not OSError) when the UID has no
        passwd entry — normal in a container. It must not escape either."""
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(
            beacon, "is_default_home", lambda: _REAL_IS_DEFAULT_HOME()
        )

        def no_home():
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(beacon.Path, "home", staticmethod(no_home))
        monkeypatch.setattr(beacon, "config_dir", no_home)
        assert beacon.send("https://e.invalid", "1.2.3", enabled=True) is False
        assert beacon.is_first_send() is True  # unreadable state → treat as first

    def test_malformed_https_endpoint_is_silent(self, _isolated_home):
        """A host with a space passes the https:// check but breaks urlopen.

        Regression test: http.client.InvalidURL is not an OSError or ValueError,
        so it used to escape send() into the gateway's detached daemon thread,
        where threading.excepthook printed a traceback on every boot — violating
        this function's documented silent-on-failure contract. Drives the REAL
        urlopen (no stub), because the bug was in the except tuple itself.
        """
        assert beacon.send("https://exa mple.invalid", "1.2.3", enabled=True) is False


class TestFailOpen:
    """Telemetry must NEVER block, delay, or break a user action.

    This is the load-bearing property of the whole feature: a beacon that can
    fail a turn, delay a boot, or surface an error is worse than no beacon. Each
    test here drives a real failure mode through the real ``send()``.
    """

    @pytest.mark.parametrize(
        "exc",
        [
            urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
            urllib.error.URLError(ConnectionRefusedError(61, "refused")),
            urllib.error.URLError(ssl.SSLError("handshake failed")),
            urllib.error.HTTPError("u", 500, "server error", {}, None),
            urllib.error.HTTPError("u", 403, "forbidden", {}, None),
            TimeoutError("timed out"),
            http.client.BadStatusLine("\x16\x03\x01"),  # captive portal / TLS bytes
            http.client.InvalidURL("space in host"),
            OSError(101, "Network unreachable"),
            OSError(28, "No space left on device"),
        ],
        ids=[
            "dns", "refused", "tls", "http500", "http403",
            "timeout", "captive-portal", "bad-url", "unreachable", "disk-full",
        ],
    )
    def test_every_transport_failure_returns_false_silently(
        self, _isolated_home, monkeypatch, exc
    ):
        def boom(*_a, **_k):
            raise exc

        monkeypatch.setattr(beacon.urllib.request, "urlopen", boom)
        assert beacon.send("https://e.invalid", "1.2.3", enabled=True) is False

    def test_a_hanging_beacon_does_not_delay_the_caller(self, _isolated_home, monkeypatch):
        """The gateway starts the beacon on a thread and never joins it.

        Pins the boot-path contract: even a beacon that hangs far past its own
        timeout costs the caller only the thread spawn.
        """
        def hang(*_a, **_k):
            time.sleep(30)

        monkeypatch.setattr(beacon.urllib.request, "urlopen", hang)
        start = time.monotonic()
        thread = threading.Thread(
            target=beacon.send,
            args=("https://e.invalid", "1.2.3"),
            kwargs={"enabled": True},
            daemon=True,
        )
        thread.start()
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"spawning the beacon cost {elapsed:.2f}s"
        assert thread.daemon, "must not pin interpreter exit"

    def test_gateway_wiring_is_detached_and_daemon(self):
        """The gateway must never await the beacon.

        Guards against a refactor to ``await asyncio.to_thread(beacon.send, ...)``,
        which would silently reintroduce up to HTTP_TIMEOUT_SECS of boot delay.
        """
        import inspect

        from kiro_crew.slack import gateway

        src = inspect.getsource(gateway.run_gateway)
        assert "beacon.send" in src
        assert "daemon=True" in src
        assert "await asyncio.to_thread(\n                beacon.send" not in src
        assert "await beacon.send" not in src


class TestStatusOutput:
    def test_status_does_not_create_id(self, _isolated_home):
        info = beacon.status("https://e.invalid", enabled=True, app_version="1.2.3")
        assert info["install_id"] == "(not yet generated)"
        assert not (_isolated_home / beacon.INSTALL_ID_FILE).exists()

    def test_empty_endpoint_reports_would_not_send(self, _isolated_home):
        """status() must agree with send(), which returns early on no endpoint.

        Reachable whenever __post_init__ clears a non-https endpoint — precisely
        when an operator runs `telemetry status` to find out why nothing is sent.
        """
        info = beacon.status("", enabled=True, app_version="1.2.3")
        assert info["would_send"] is False
        assert "endpoint" in str(info["reason"])

    def test_formatted_status_discloses_optout_and_exclusions(self, _isolated_home):
        text = beacon.format_status(
            beacon.status("https://e.invalid", enabled=True, app_version="1.2.3")
        )
        assert beacon.DISABLE_ENV in text
        for claim in ("prompts", "credentials", "hostname", "IP address"):
            assert claim in text


class TestTelemetryCliWrite:
    """`telemetry disable/enable` rewrites the user's WHOLE config.json."""

    def _args(self, action):
        import argparse

        return argparse.Namespace(telemetry_action=action)

    def test_toggle_preserves_unrelated_config(self, _isolated_home, monkeypatch):
        from kiro_crew.cli_commands import _telemetry

        cfg = _isolated_home / "config.json"
        cfg.write_text(json.dumps({"slack": {"command": "kirocrew"}, "timezone": "UTC"}))
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)
        _telemetry(self._args("disable"))
        data = json.loads(cfg.read_text())
        assert data["telemetry"]["beacon_enabled"] is False
        # The user's own values must survive. (load() also performs a migration
        # write-back that fills in defaults for other keys — pre-existing
        # behavior, so assert the values we set, not the exact section shape.)
        assert data["slack"]["command"] == "kirocrew", "must not drop other settings"
        assert data["timezone"] == "UTC"

    @pytest.mark.parametrize(
        "raw", ['[{"important": "data"}]', '"a string"', "42"], ids=["array", "str", "num"]
    )
    def test_non_object_config_is_never_overwritten(
        self, _isolated_home, monkeypatch, raw
    ):
        """A config.json that is valid JSON but not an object must not be replaced.

        Regression test: the toggle used to coerce non-dict data to ``{}``, then
        write — silently destroying the file's contents AND printing success. A
        privacy toggle must never be a data-loss path.
        """
        from kiro_crew.cli_commands import _telemetry

        cfg = _isolated_home / "config.json"
        cfg.write_text(raw)
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)

        with pytest.raises(SystemExit) as exc:
            _telemetry(self._args("disable"))
        assert exc.value.code == 1
        assert cfg.read_text() == raw, "the original file must be untouched"

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="POSIX permission bits. Windows has no mode bits — it enforces "
        "owner-only with a DACL, covered by test_lockdown_is_enforced_cross_platform",
    )
    def test_preserves_existing_config_permissions(self, _isolated_home, monkeypatch):
        """A telemetry toggle must not widen who can read config.json.

        Regression test: atomic_write creates a NEW file and renames it over the
        old one, so without an explicit mode an operator's tightened 0600 became
        the umask default (0644 on a typical host) — and config.json can hold
        inline credentials, so a privacy toggle would have leaked them to every
        other local user.
        """
        import os
        import stat as _stat

        from kiro_crew.cli_commands import _telemetry

        cfg = _isolated_home / "config.json"
        cfg.write_text(json.dumps({"slack": {"bot_token": "xoxb-secret"}}))
        os.chmod(cfg, 0o600)
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)

        _telemetry(self._args("disable"))

        mode = _stat.S_IMODE(cfg.stat().st_mode)
        assert mode == 0o600, f"mode widened to {oct(mode)}"
        assert not mode & 0o077, "group/other must not gain access"

    def test_lockdown_is_enforced_cross_platform(self, _isolated_home, monkeypatch):
        """atomic_write's `mode` is POSIX-only, so the lockdown must be explicit.

        Regression test: `mode=` routes through fchmod_safe, a documented NO-OP on
        Windows, so the replacement file would inherit the directory ACL — and a
        permissive data home would expose a config.json holding inline
        credentials. restrict_to_owner must be called for the secret case.
        """
        import os

        from kiro_crew import cli_commands

        cfg = _isolated_home / "config.json"
        cfg.write_text(json.dumps({"slack": {"bot_token": "xoxb-secret"}}))
        os.chmod(cfg, 0o600)
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)

        calls: list[str] = []
        real = cli_commands.platform_compat.restrict_to_owner
        monkeypatch.setattr(
            cli_commands.platform_compat,
            "restrict_to_owner",
            lambda p: (calls.append(str(p)), real(p))[1],
        )
        _telemetry = cli_commands._telemetry
        _telemetry(self._args("disable"))

        assert str(cfg) in calls, "owner-only lockdown must be applied explicitly"

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="POSIX permission bits; see test_lockdown_is_enforced_cross_platform",
    )
    def test_new_config_is_created_owner_only(self, _isolated_home, monkeypatch):
        """A config.json this command creates must start owner-only."""
        import stat as _stat

        from kiro_crew.cli_commands import _telemetry

        cfg = _isolated_home / "config.json"
        assert not cfg.exists()
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)

        _telemetry(self._args("disable"))

        assert not _stat.S_IMODE(cfg.stat().st_mode) & 0o077

    def test_uses_atomic_write_not_write_text(self, _isolated_home, monkeypatch):
        """The toggle must route through atomic_write, never path.write_text.

        Regression test: it used to call ``path.write_text``, which truncates in
        place — a disk-full or interrupted write mid-rewrite of the user's WHOLE
        config.json would leave a partial file and every later load would
        silently discard their configuration. ``atomic_write`` writes a temp file
        and renames, so a failure leaves the original untouched.

        Asserted at the call site rather than by simulating a failed write,
        because ``KiroCrewConfig.load()`` performs its own migration write-back
        that rewrites config.json independently of this code path.
        """
        from kiro_crew import cli_commands

        cfg = _isolated_home / "config.json"
        cfg.write_text(json.dumps({"timezone": "UTC"}))
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg)

        calls: list[dict] = []

        def spy(path, content, **kwargs):
            calls.append({"path": str(path), **kwargs})
            from kiro_crew.atomic_write import atomic_write as real

            real(path, content, **kwargs)

        monkeypatch.setattr(cli_commands, "atomic_write", spy)
        cli_commands._telemetry(self._args("disable"))

        assert calls, "toggle must write through atomic_write"
        assert calls[0]["path"] == str(cfg)
        assert calls[0].get("fsync") is True, "rename must be durable"
        assert json.loads(cfg.read_text())["telemetry"]["beacon_enabled"] is False


class TestSnapshotAndPortabilityRegistration:
    """The install id must never ride an export/snapshot to another machine.

    Enforced by NON-SELECTION, not by a basename filter. An earlier revision put
    the beacon filenames in ``EXPORT_EXCLUDE`` / ``NEVER_SNAPSHOT_FILES``, but
    both sets are matched by BASENAME over the workspace/, plan_memory/ and
    skills/ trees — so they would have silently dropped any USER file sharing the
    name, while protecting nothing (the root paths are never selected anyway).
    """

    def test_beacon_names_are_not_basename_filtered(self):
        from kiro_crew.portability import EXPORT_EXCLUDE
        from kiro_crew.snapshot import NEVER_SNAPSHOT_FILES

        for name in (beacon.INSTALL_ID_FILE, beacon.STAMP_FILE):
            assert name not in EXPORT_EXCLUDE, (
                f"{name} in EXPORT_EXCLUDE would drop a user's workspace file "
                "with the same basename"
            )
            assert name not in NEVER_SNAPSHOT_FILES, (
                f"{name} in NEVER_SNAPSHOT_FILES would drop a user's workspace "
                "file with the same basename"
            )

    def test_root_export_allowlist_excludes_beacon_state(self):
        """Root-level export copies a fixed allowlist; beacon files aren't on it."""
        import inspect

        from kiro_crew import portability

        src = inspect.getsource(portability.create_export_zip)
        assert beacon.INSTALL_ID_FILE not in src
        assert beacon.STAMP_FILE not in src

    def test_snapshot_components_never_name_beacon_state(self):
        from kiro_crew.snapshot import CORE_FILES

        listed = {f for files in CORE_FILES.values() for f in files}
        assert beacon.INSTALL_ID_FILE not in listed
        assert beacon.STAMP_FILE not in listed

    def test_workspace_file_with_beacon_name_survives_export(self):
        """A user file merely SHARING the name must not be filtered out."""
        from pathlib import PurePosixPath

        from kiro_crew.portability import _is_excluded

        for rel in (
            f"workspace/proj/{beacon.INSTALL_ID_FILE}",
            f"plan_memory/{beacon.STAMP_FILE}",
        ):
            assert not _is_excluded(PurePosixPath(rel)), rel


class TestConfigDefaults:
    def test_beacon_on_by_default_with_https_endpoint(self):
        from kiro_crew.config.loader import TelemetryConfig

        cfg = TelemetryConfig()
        assert cfg.beacon_enabled is True
        assert cfg.beacon_endpoint.startswith("https://")

    def test_non_https_endpoint_is_cleared(self):
        from kiro_crew.config.loader import TelemetryConfig

        assert TelemetryConfig(beacon_endpoint="http://insecure.invalid").beacon_endpoint == ""

    def test_unusable_https_endpoints_are_cleared(self):
        """A startswith('https://') test is not enough.

        A host containing whitespace passes that check and also passes
        beacon_url's scheme check, then fails only inside urlopen — deep in the
        beacon thread. Reject it at config load instead.
        """
        from kiro_crew.config.loader import TelemetryConfig

        for bad in (
            "https://exa mple.invalid",   # whitespace in host
            "https://",                    # no netloc
            "https:///path-only",          # empty netloc
        ):
            assert TelemetryConfig(beacon_endpoint=bad).beacon_endpoint == "", bad

    def test_local_metrics_switch_stays_off(self):
        """The beacon must not ride the local-only telemetry.enabled switch."""
        from kiro_crew.config.loader import TelemetryConfig

        assert TelemetryConfig().enabled is False


def _fake_urlopen(calls: list | None = None):
    """Return a urlopen stand-in recording called URLs, usable as a CM."""

    class _Resp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _open(req, *_a, **_k):
        if calls is not None:
            calls.append(getattr(req, "full_url", req))
        return _Resp()

    return _open
