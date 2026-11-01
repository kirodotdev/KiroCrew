"""``.env`` credential hot-reload in the live config watcher.

A hand edit to ``$KIROCREW_HOME/.env`` takes effect on a running gateway without
a restart: the live watcher reconciles ``.env`` in the same cycle it reloads
``config.json``. These tests pin the three seams that make that work:

* the loader helpers -- ``env_fingerprint`` (the credential twin of the document
  fingerprint, kept separate so a ``.env`` edit never busts the parsed-config
  cache), ``read_env_credentials_raw``, ``changed_env_keys`` and
  ``apply_env_credential_changes`` (set/pop into ``os.environ`` with the Docker
  scrub and PATH carve-outs);
* ``ConfigWatch`` tracking the ``.env`` fingerprint alongside the document one,
  so a ``.env``-only edit wakes a cycle, applies the credential diff to
  ``os.environ`` and dispatches a synthetic ``credentials.<KEY>`` change -- while
  the first read establishes a baseline and dispatches nothing;
* a combined ``config.json`` + ``.env`` tick folding both diffs into one
  dispatch.

The watcher is driven with ``refresh_now()`` against temp files, the shape a
subscriber really receives; the poll task is never started.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import forget_env_at_teardown
from kiro_crew.config import live
from kiro_crew.config.live import ConfigWatch
from kiro_crew.config.loader import KiroCrewConfig


class _FoldedEnviron(dict):
    """A stand-in for Windows' case-insensitive ``os.environ``.

    Every key is folded to upper case, so ``HTTPS_PROXY`` and ``https_proxy``
    read and write ONE entry, as they do on Windows. Used only for the duration
    of a test, via ``monkeypatch.setattr(os, "environ", ...)``.
    """

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        super().__init__({k.upper(): v for k, v in (initial or {}).items()})

    def __getitem__(self, key: str) -> str:
        return super().__getitem__(key.upper())

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key.upper(), value)

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key.upper())

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and super().__contains__(key.upper())

    def get(self, key: str, default=None):  # type: ignore[override]
        return super().get(key.upper(), default)

    def pop(self, key: str, *default):  # type: ignore[override]
        return super().pop(key.upper(), *default)


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_env_ownership_record():
    """Reset the process-wide ``.env`` ownership record and boot map around EVERY
    test here, not only the ones that take ``home``.

    The ownership set and the boot ``.env`` map are module globals that a helper
    test mutates directly (``note_env_owned_key``, ``apply_env_credential_changes``,
    ``load_credentials``). ``monkeypatch.setenv`` restores ``os.environ`` at
    teardown but NOT these records, so without this a helper test leaves a key
    marked owned and a later test popping that key by ownership fails -- an
    order-dependent flake. Autouse so coverage does not depend on requesting a
    fixture.
    """
    from kiro_crew.config.loader_credentials import reset_boot_raw_env, reset_env_owned_keys

    reset_boot_raw_env()
    reset_env_owned_keys()
    yield
    reset_boot_raw_env()
    reset_env_owned_keys()


@pytest.fixture(name="home")
def home_fixture(tmp_path: Path):
    """Point ``config.json`` AND ``.env`` at a fresh temp data home.

    ``env_path`` is a name the ``config.loader`` facade mirror shares across its
    family, so it is patched THROUGH the facade (``kiro_crew.config.loader``),
    never on the ``loader_credentials`` owner directly: the mirror routes a
    facade patch to every holder, and patching one owner would let the facade's
    undo restore the others to that owner's value (``test_facade_mirror_guards``
    enforces this). The config paths are patched the same way, as
    ``config_live_helpers`` does.
    """
    from kiro_crew.config.loader_credentials import reset_boot_raw_env, reset_env_owned_keys

    live.reset_for_tests()
    # The boot ``.env`` record and the ``.env`` ownership set are once-per-process
    # boot state; clear both so a prior test's boot does not leak its baseline or
    # its ownership record into another test's watcher.
    reset_boot_raw_env()
    reset_env_owned_keys()
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    env = tmp_path / ".env"
    with (
        patch("kiro_crew.config.loader.config_path", return_value=cfg),
        patch("kiro_crew.config.loader.config_local_path", return_value=tmp_path / "local.json"),
        patch("kiro_crew.config.loader.env_path", return_value=env),
    ):
        yield tmp_path
    live.reset_for_tests()
    reset_boot_raw_env()
    reset_env_owned_keys()


_LAST_ENV_MTIME_NS: dict[Path, int] = {}


def _write_env(path: Path, body: str) -> None:
    """Write ``.env`` with a strictly-increasing mtime so a same-size edit is seen.

    A same-size edit on a coarse-timestamp filesystem lands on the same mtime,
    making it invisible to the fingerprint -- so the mtime is forced forward,
    the same trick ``_hot_reload_helpers.write_config`` uses for ``config.json``.
    """
    env = path / ".env"
    env.write_text(body, encoding="utf-8")
    st = env.stat()
    mtime = max(st.st_mtime_ns, _LAST_ENV_MTIME_NS.get(env, 0)) + 1_000_000_000
    os.utime(env, ns=(st.st_atime_ns, mtime))
    _LAST_ENV_MTIME_NS[env] = mtime


def _reexec_with_stubbed_execv(monkeypatch) -> list[dict[str, str]]:
    """Run the real ``reexec_launcher`` seam with ``os.execv`` stubbed.

    Returns the environment each exec would have handed its successor. Like a
    real ``os.execv``, the seam's writes stay in ``os.environ`` (this process
    plays the successor next), so every name it can write is restored or
    removed at teardown.
    """
    from kiro_crew import platform_compat
    from kiro_crew import platform_compat_runtime as runtime
    from kiro_crew.config import loader

    for name in ("PYTHONUTF8", "PYTHONIOENCODING"):
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            forget_env_at_teardown(monkeypatch, name)
    forget_env_at_teardown(monkeypatch, loader._ENV_OWNED_KEYS_REEXEC_MARKER)
    # Through the facade: IS_WINDOWS is a mirrored name, so patching one owner
    # would let the facade's undo restore every holder to the patched value.
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    execs: list[dict[str, str]] = []
    monkeypatch.setattr(runtime.os, "execv", lambda path, argv: execs.append(dict(os.environ)))
    runtime.reexec_launcher("/opt/kirocrew/bin/kirocrew", ["gateway"])
    return execs


# ── loader helpers ────────────────────────────────────────────────────────


class TestLoaderCredentialHelpers:
    def test_read_env_credentials_raw_parses_literal_values(self, home: Path):
        _write_env(home, "# comment\nTELEGRAM_BOT_TOKEN=abc123\nFOO=bar\n\n")
        from kiro_crew.config import loader

        assert loader.read_env_credentials_raw() == {
            "TELEGRAM_BOT_TOKEN": "abc123",
            "FOO": "bar",
        }

    def test_read_env_credentials_raw_is_empty_when_file_absent(self, home: Path):
        from kiro_crew.config import loader

        definitive: list[bool] = []
        assert loader.read_env_credentials_raw(definitive=definitive) == {}
        assert definitive == [True], "an absent .env is definitive-and-empty, like boot"

    @pytest.mark.skipif(not os.name == "posix", reason="POSIX mode bits only")
    def test_read_env_credentials_raw_enforces_owner_only_permissions(self, home: Path):
        """The watcher's off-loop reader must not be a weaker path to the file
        than boot's: it narrows a group/other-readable .env to 0600 before read."""
        from kiro_crew.config import loader

        env = home / ".env"
        _write_env(home, "TELEGRAM_BOT_TOKEN=x\n")
        env.chmod(0o644)
        loader.read_env_credentials_raw()
        assert (env.stat().st_mode & 0o077) == 0, "group/other read bits cleared"

    def test_read_env_credentials_raw_wide_encoding_is_not_definitive(self, home: Path):
        """A UTF-16 save returns {} but marks the read NON-definitive, so a caller
        diffing credentials does not read it as every key cleared."""
        from kiro_crew.config import loader

        (home / ".env").write_bytes(b"\xff\xfeT\x00O\x00K\x00E\x00N\x00=\x00x\x00")
        definitive: list[bool] = []
        assert loader.read_env_credentials_raw(definitive=definitive) == {}
        assert definitive == [False]

    def test_read_env_credentials_raw_bad_byte_is_not_definitive(self, home: Path, monkeypatch):
        """A byte that is not valid UTF-8 (no BOM involved, e.g. a Latin-1 byte
        under a UTF-8 locale) must be caught the same way as the wide-encoding
        case: non-definitive, empty map -- not an uncaught UnicodeDecodeError
        that would propagate out of the live watcher's poll cycle and wedge
        config hot-reload on every later tick. (Fixes the Opus finding.) The
        warn-once behaviour on this path is already covered by the wide-encoding
        test above; ``_warned_undecodable_env`` is a module-level dedup set not
        reset between tests, so asserting on the log here would be order-
        dependent."""
        from types import SimpleNamespace

        from kiro_crew.config import loader

        # Pin the decoder's locale seam to UTF-8 so \xe9 is an invalid byte on
        # every host: under a CP1252 (Windows ANSI) locale it would decode cleanly
        # and the read would be definitive, making this assertion host-dependent.
        monkeypatch.setattr(
            loader, "_locale", SimpleNamespace(getpreferredencoding=lambda _=False: "utf-8")
        )
        (home / ".env").write_bytes(b"TELEGRAM_BOT_TOKEN=\xe9caf\xe9\n")
        definitive: list[bool] = []
        assert loader.read_env_credentials_raw(definitive=definitive) == {}
        assert definitive == [False]

    def test_apply_rotation_does_not_warn(self, monkeypatch, caplog):
        """A routine rotation does NOT warn: boot propagated the old .env value
        into os.environ, so the current value equals old[k] and the overwrite is
        not a foreign-value collision. (Fixes the warning-on-every-rotation
        First-Principles finding.)"""
        import logging

        from kiro_crew.config import loader

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "old-value")  # what boot propagated from .env
        with caplog.at_level(logging.WARNING):
            loader.apply_env_credential_changes(
                {"TELEGRAM_BOT_TOKEN"},
                {"TELEGRAM_BOT_TOKEN": "new-value"},
                {"TELEGRAM_BOT_TOKEN": "old-value"},  # old .env baseline
            )
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "new-value"
        assert not any("overwrites" in r.message for r in caplog.records)

    def test_apply_warns_only_on_a_foreign_value_collision(self, monkeypatch, caplog):
        """Warns only when the process value matches NEITHER the new nor the old
        .env value -- a genuinely inherited/foreign value being overwritten."""
        import logging

        from kiro_crew.config import loader

        # The environment holds a value .env never set (inherited shell/systemd).
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-shell")
        with caplog.at_level(logging.WARNING):
            loader.apply_env_credential_changes(
                {"TELEGRAM_BOT_TOKEN"},
                {"TELEGRAM_BOT_TOKEN": "from-dotenv"},
                {"TELEGRAM_BOT_TOKEN": ""},  # .env baseline holds no value for it
            )
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-dotenv"
        assert any(
            "TELEGRAM_BOT_TOKEN" in r.message and "inherited" in r.message for r in caplog.records
        )
        # No credential VALUE is logged.
        assert "from-shell" not in caplog.text and "from-dotenv" not in caplog.text

    def test_apply_removal_pops_only_what_env_set(self, monkeypatch):
        """A key removed from .env is popped ONLY when .env is what PROPAGATED
        it (the recorded ownership), never inferred from value equality; an
        inherited value .env never set is left in place, so a removal cannot
        strand a channel until restart. Ownership, not value-matching, so an
        inherited value that happens to EQUAL the .env line is still left alone.
        (Fixes the Design pop-on-unset finding and the GPT matching-value
        ownership finding.)"""
        from kiro_crew.config import loader

        # Case 1: .env owns the key (its propagation wrote it) -> popped.
        monkeypatch.setenv("WEBEX_BOT_TOKEN", "from-dotenv")
        loader.note_env_owned_key("WEBEX_BOT_TOKEN")
        loader.apply_env_credential_changes(
            {"WEBEX_BOT_TOKEN"}, {}, {"WEBEX_BOT_TOKEN": "from-dotenv"}
        )
        assert "WEBEX_BOT_TOKEN" not in os.environ

        # Case 2: the value is inherited (.env never owned it) -> left alone,
        # even though it differs from what .env had.
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "from-systemd")
        loader.apply_env_credential_changes(
            {"DISCORD_BOT_TOKEN"}, {}, {"DISCORD_BOT_TOKEN": "from-dotenv"}
        )
        assert os.environ["DISCORD_BOT_TOKEN"] == "from-systemd"

        # Case 3 (the GPT finding): an inherited value that EQUALS the .env line.
        # Value-equality would wrongly pop it; ownership keeps it because boot's
        # setdefault never wrote it, so .env does not own it.
        monkeypatch.setenv("WEBEX_BOT_TOKEN", "same-value")
        # NOT noted as owned: simulates a service env that supplied it, with .env
        # carrying the identical value (boot's setdefault was a no-op).
        loader.apply_env_credential_changes(
            {"WEBEX_BOT_TOKEN"}, {}, {"WEBEX_BOT_TOKEN": "same-value"}
        )
        assert (
            os.environ["WEBEX_BOT_TOKEN"] == "same-value"
        ), "an inherited value .env merely matched must survive a .env removal"

    def test_a_live_add_that_matches_an_inherited_value_does_not_take_ownership(self, monkeypatch):
        """The service supplies a token; the operator later ADDS the same value to
        .env while the gateway runs, then deletes that line. The add changes
        nothing in os.environ, so it must not record .env as the owner -- or the
        removal would pop the inherited token and restart the channel with no
        credential. (Fixes the Opus matching-value live-add finding.)"""
        from kiro_crew.config import loader

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-service")
        loader.discard_env_owned_key("TELEGRAM_BOT_TOKEN")

        # Live add of the identical value: applied, but not owned.
        loader.apply_env_credential_changes(
            {"TELEGRAM_BOT_TOKEN"}, {"TELEGRAM_BOT_TOKEN": "from-service"}, {}
        )
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-service"
        assert not loader.env_owns_key("TELEGRAM_BOT_TOKEN")

        # The line is deleted again: the inherited token stays.
        loader.apply_env_credential_changes(
            {"TELEGRAM_BOT_TOKEN"}, {}, {"TELEGRAM_BOT_TOKEN": "from-service"}
        )
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-service"

    def test_a_live_add_that_changes_the_value_takes_ownership(self, monkeypatch):
        """Control for the case above: an add that actually writes a new value is
        a genuine .env write, so .env owns the key and a later removal pops it."""
        from kiro_crew.config import loader

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
        loader.discard_env_owned_key("TELEGRAM_BOT_TOKEN")

        loader.apply_env_credential_changes(
            {"TELEGRAM_BOT_TOKEN"}, {"TELEGRAM_BOT_TOKEN": "from-dotenv"}, {}
        )
        assert loader.env_owns_key("TELEGRAM_BOT_TOKEN")
        loader.apply_env_credential_changes(
            {"TELEGRAM_BOT_TOKEN"}, {}, {"TELEGRAM_BOT_TOKEN": "from-dotenv"}
        )
        assert "TELEGRAM_BOT_TOKEN" not in os.environ

    def test_apply_without_baseline_pops_unconditionally_and_is_silent(self, monkeypatch, caplog):
        """With no baseline (the dashboard path) a removed key is popped
        unconditionally and no overwrite warning fires."""
        import logging

        from kiro_crew.config import loader

        monkeypatch.setenv("WEBEX_BOT_TOKEN", "anything")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "old")
        with caplog.at_level(logging.WARNING):
            loader.apply_env_credential_changes(
                {"WEBEX_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"},
                {"TELEGRAM_BOT_TOKEN": "new"},  # WEBEX absent -> removed
            )
        assert "WEBEX_BOT_TOKEN" not in os.environ
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "new"
        assert not any(
            "overwrites" in r.message or "inherited" in r.message for r in caplog.records
        )

    def test_enforce_permissions_silent_on_missing_file(self, home: Path, caplog):
        """A home with no .env must not log a misleading 'cannot enforce
        permissions' warning on a forced read cycle. (Fixes the Opus finding.)"""
        import logging

        from kiro_crew.config import loader

        # No .env written in this home.
        definitive: list[bool] = []
        with caplog.at_level(logging.WARNING):
            assert loader.read_env_credentials_raw(definitive=definitive) == {}
        assert definitive == [True], "absent file is definitive-and-empty"
        assert not any("enforce permissions" in r.message for r in caplog.records)

    def test_env_fingerprint_moves_on_edit_and_is_a_sentinel_when_absent(self, home: Path):
        from kiro_crew.config import loader

        absent = loader.env_fingerprint()
        assert absent[1] is None, "a missing .env is a sentinel, not a stat tuple"
        _write_env(home, "FOO=1\n")
        present = loader.env_fingerprint()
        assert present != absent
        _write_env(home, "FOO=2\n")
        assert loader.env_fingerprint() != present

    def test_changed_env_keys_reports_add_remove_edit(self):
        from kiro_crew.config import loader

        old = {"A": "1", "B": "2", "C": "3"}
        new = {"A": "1", "B": "22", "D": "4"}
        assert loader.changed_env_keys(old, new) == frozenset({"B", "C", "D"})

    def test_a_non_propagating_read_does_not_reseed_a_removed_key(self, home: Path, monkeypatch):
        """A post-boot read with propagate=False must NOT write .env into
        os.environ: the channel-reconnect and readiness readers run on worker
        threads, and a setdefault there could restore a credential the live
        watcher just popped -- silently undoing an external .env removal.
        (Fixes the GPT concurrent-reader race.)"""
        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        _write_env(home, "TELEGRAM_BOT_TOKEN=from-dotenv\n")
        # The watcher has popped the key (an external .env removal); os.environ
        # does not hold it. A propagate=False read must leave it absent.
        assert "TELEGRAM_BOT_TOKEN" not in os.environ
        creds = KiroCrewConfig.load().load_credentials(propagate=False)
        assert (
            "TELEGRAM_BOT_TOKEN" not in os.environ
        ), "a non-propagating read must not setdefault the key back into os.environ"
        # It still RETURNS the resolved value for the caller that needs it.
        assert creds.get("TELEGRAM_BOT_TOKEN") == "from-dotenv"

    def test_boot_propagation_records_env_ownership_only_for_keys_it_wrote(
        self, home: Path, monkeypatch
    ):
        """load_credentials records a key as .env-owned ONLY when its setdefault
        actually wrote it (the name was absent). A key the process already holds
        from its service environment is NOT owned even when .env carries the same
        value, so a later .env removal leaves the inherited value in place rather
        than deleting it and disabling the channel. (Fixes the GPT
        matching-value ownership finding, end to end.)"""
        from kiro_crew.config import loader
        from kiro_crew.config.loader import KiroCrewConfig

        # DISCORD is supplied by the service env AND .env with the SAME value;
        # TELEGRAM comes only from .env.
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "from-service")
        _write_env(home, "TELEGRAM_BOT_TOKEN=from-dotenv\nDISCORD_BOT_TOKEN=from-service\n")

        KiroCrewConfig.load().load_credentials()
        # .env wrote TELEGRAM (absent before) -> owned; DISCORD was inherited
        # (setdefault no-op) -> NOT owned, despite equal values.
        assert loader.env_owns_key("TELEGRAM_BOT_TOKEN")
        assert not loader.env_owns_key("DISCORD_BOT_TOKEN")

        # Remove both from .env: TELEGRAM (owned) is popped; DISCORD (inherited)
        # survives, so the service-provided credential stays available.
        loader.apply_env_credential_changes(
            {"TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN"},
            {},
            {"TELEGRAM_BOT_TOKEN": "from-dotenv", "DISCORD_BOT_TOKEN": "from-service"},
        )
        assert "TELEGRAM_BOT_TOKEN" not in os.environ
        assert (
            os.environ["DISCORD_BOT_TOKEN"] == "from-service"
        ), "an inherited credential must survive a .env removal"

    def test_env_ownership_is_carried_across_a_reexec_marker(self, home: Path, monkeypatch):
        """An in-app restart (os.execv) keeps os.environ but resets Python state,
        so the ownership record must ride a marker the seam writes and the
        successor's load_credentials reads back -- otherwise every .env key reads
        as inherited on the successor and a later removal never pops it.
        (Fixes the Opus re-exec finding.)"""
        from kiro_crew.config import loader
        from kiro_crew.config.loader import KiroCrewConfig

        # TWO owned keys, so the serialised marker joins them -- a NUL separator
        # would make os.environ reject the whole value and silently lose the
        # record (the GPT/Opus multi-key finding); a comma separator is legal.
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        marker = loader._ENV_OWNED_KEYS_REEXEC_MARKER
        _write_env(home, "TELEGRAM_BOT_TOKEN=tok\nDISCORD_BOT_TOKEN=dtok\n")
        # First boot: .env writes both absent keys -> both owned.
        KiroCrewConfig.load().load_credentials()
        assert loader.env_owns_key("TELEGRAM_BOT_TOKEN")
        assert loader.env_owns_key("DISCORD_BOT_TOKEN")
        # Kept for the exec successor only: no child spawned now inherits it.
        assert marker not in os.environ

        # The real exec seam hands the marker to the successor (the exec is
        # stubbed, so the successor's environment is this one, as with execv).
        execs = _reexec_with_stubbed_execv(monkeypatch)
        assert len(execs) == 1
        assert execs[0][marker].split(",") == ["DISCORD_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"]
        assert "\x00" not in execs[0][marker]

        # Simulate the successor: Python state reset (empty record), os.environ
        # kept (both tokens already present + the marker).
        loader.reset_env_owned_keys()
        assert not loader.env_owns_key("TELEGRAM_BOT_TOKEN")
        # The successor's first load_credentials adopts the marker and deletes it.
        KiroCrewConfig.load().load_credentials()
        assert loader.env_owns_key("TELEGRAM_BOT_TOKEN"), "ownership survived the re-exec"
        assert loader.env_owns_key("DISCORD_BOT_TOKEN"), "both keys survived, not just one"
        assert loader._ENV_OWNED_KEYS_REEXEC_MARKER not in os.environ, "marker consumed"

        # A later .env removal now correctly pops both owned keys.
        loader.apply_env_credential_changes(
            {"TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN"},
            {},
            {"TELEGRAM_BOT_TOKEN": "tok", "DISCORD_BOT_TOKEN": "dtok"},
        )
        assert "TELEGRAM_BOT_TOKEN" not in os.environ
        assert "DISCORD_BOT_TOKEN" not in os.environ

    def test_a_reexec_with_nothing_owned_still_carries_an_empty_marker(
        self, home: Path, monkeypatch
    ):
        """A restart before ``.env`` owns any key must still hand the successor a
        marker (empty), so it adopts "nothing owned" instead of re-deriving
        ownership from an environment where every key looks inherited. And a key
        the live remover drops is not carried: the marker follows the record."""
        from kiro_crew.config import loader
        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-the-service-unit")
        marker = loader._ENV_OWNED_KEYS_REEXEC_MARKER
        _write_env(home, "TELEGRAM_BOT_TOKEN=from-the-service-unit\n")
        KiroCrewConfig.load().load_credentials()  # boot seed: inherited, not owned

        execs = _reexec_with_stubbed_execv(monkeypatch)
        assert execs[0][marker] == ""

        loader.note_env_owned_key("DISCORD_BOT_TOKEN")
        loader.discard_env_owned_key("DISCORD_BOT_TOKEN")
        execs = _reexec_with_stubbed_execv(monkeypatch)
        assert execs[0][marker] == ""

    def test_only_the_boot_seed_propagates_credentials_into_os_environ(self):
        """A credential read runs POST-BOOT on a gateway worker thread unless it
        is the one boot seed, so it must call ``load_credentials(propagate=False)``:
        a propagating read could ``setdefault`` a credential the live watcher just
        popped back into ``os.environ``, silently undoing an external ``.env``
        removal. This contract scans the WHOLE source tree so a new reader in any
        module cannot reintroduce the race, with a small allowlist for the reads
        that legitimately propagate. (Fixes the GPT concurrent-reader class of
        findings, which surfaced one missed reader at a time.)"""
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        # The reads that MUST still propagate, each with why:
        #  - process_entry: the gateway's one boot seed, before the watcher arms.
        #  - orchestrator_init: the boot path hands it the already-loaded map;
        #    the fallback only runs standalone (no running gateway, no watcher).
        #  - cli_setup / doctor: separate short-lived CLI processes, not the
        #    running gateway, so they race no watcher and seed their own children.
        allow = {
            "slack/gateway_runtime/process_entry.py",
            "slack/gateway_runtime/orchestrator_init.py",
            "cli_setup.py",
            "doctor_checks/services.py",
        }

        def _propagating_calls(tree: ast.AST) -> list[int]:
            # A real Call whose function is an attribute access `.load_credentials`
            # and whose keywords do not include propagate=False. AST, not text, so
            # a docstring or comment mentioning load_credentials never matches.
            # Also a call that is HANDED `.load_credentials` as its first argument
            # (`asyncio.to_thread(cfg.load_credentials, ...)`): the keyword then
            # rides on that outer call, and a bare reference propagates.
            out: list[int] = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                direct = (
                    isinstance(node.func, ast.Attribute) and node.func.attr == "load_credentials"
                )
                handed = (
                    bool(node.args)
                    and isinstance(node.args[0], ast.Attribute)
                    and node.args[0].attr == "load_credentials"
                )
                if direct or handed:
                    passes_false = any(
                        kw.arg == "propagate"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is False
                        for kw in node.keywords
                    )
                    if not passes_false:
                        out.append(node.lineno)
            return out

        offenders: list[str] = []
        for path in sorted(src.rglob("*.py")):
            # as_posix so the forward-slash allowlist matches on the Windows
            # backend shard too (str(WindowsPath) uses backslashes).
            rel = path.relative_to(src).as_posix()
            if rel in allow:
                continue
            for lineno in _propagating_calls(ast.parse(path.read_text(encoding="utf-8"))):
                offenders.append(f"{rel}:{lineno}")
        assert offenders == [], (
            "a post-boot credential read must pass propagate=False to load_credentials "
            "(a propagating read can resurrect a removed credential); add a genuine "
            f"boot/CLI read to the allowlist with its reason: {offenders}"
        )

    def test_is_legal_env_name_rejects_empty_and_equals_and_nul(self):
        from kiro_crew.config import loader

        assert loader.is_legal_env_name("TELEGRAM_BOT_TOKEN")
        assert not loader.is_legal_env_name("")  # an `=value` line parses to this
        assert not loader.is_legal_env_name("A=B")
        assert not loader.is_legal_env_name("A\x00B")

    def test_apply_rejects_the_whole_batch_before_mutating_on_an_illegal_key(self, monkeypatch):
        """A malformed `.env` line (empty name) in the diff makes the applier
        raise BEFORE writing anything: a legal key sharing the batch must NOT be
        left applied, or a later file restore would leave the stray value behind.
        (Fixes the GPT partial-mutation finding.)"""
        from kiro_crew.config import loader

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "before")
        with pytest.raises(ValueError):
            loader.apply_env_credential_changes(
                {"TELEGRAM_BOT_TOKEN", ""},
                {"TELEGRAM_BOT_TOKEN": "rotated", "": "garbage"},
                {"TELEGRAM_BOT_TOKEN": "before"},
            )
        assert (
            os.environ["TELEGRAM_BOT_TOKEN"] == "before"
        ), "no key may be written when the batch is rejected"

    def test_a_windows_case_only_rename_does_not_delete_the_renamed_entry(self, monkeypatch):
        """On Windows os.environ is case-insensitive, so a case-only .env rename
        (``https_proxy`` -> ``HTTPS_PROXY``) is one key added and one removed that
        share the SAME entry. The removal must NOT pop the entry the addition
        just wrote. (Fixes the GPT Windows case-rename finding.)"""
        from kiro_crew import platform_compat
        from kiro_crew.config import loader

        # _env_name_key reads platform_compat.IS_WINDOWS at call time.
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True, raising=False)
        # The code under test writes HTTPS_PROXY via os.environ[...] (not through
        # monkeypatch), so record the pre-test state for teardown or it leaks
        # into the xdist worker for every later test.
        forget_env_at_teardown(monkeypatch, "https_proxy", "HTTPS_PROXY")

        # Boot owned the lowercase key; the live edit renames it to uppercase.
        loader.note_env_owned_key("https_proxy")
        loader.apply_env_credential_changes(
            {"HTTPS_PROXY", "https_proxy"},
            {"HTTPS_PROXY": "http://proxy:8080"},  # lowercase dropped from new
            {"https_proxy": "http://proxy:8080"},
        )
        # The renamed entry survives; the removed-case key did not delete it.
        assert os.environ.get("HTTPS_PROXY") == "http://proxy:8080"
        # Ownership follows the canonical entry (case-folded on Windows), so a
        # later removal of either case still recognises it.
        assert loader.env_owns_key("https_proxy")
        assert loader.env_owns_key("HTTPS_PROXY")

    def test_windows_both_spellings_edited_resolves_to_boot_precedence(self, monkeypatch):
        """On Windows both spellings of one key are one os.environ entry. When a
        single save edits BOTH to different values, the entry must take the value
        boot's setdefault would -- the first spelling in .env order -- whatever
        order the changed set happens to iterate in. (Fixes the GPT
        order-dependent case-alias rotation finding.)"""
        from kiro_crew import platform_compat
        from kiro_crew.config import loader

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True, raising=False)
        # Model Windows' case-insensitive os.environ for the applier's reads and
        # writes: on a POSIX runner the two spellings would otherwise be two
        # separate variables and the order dependence could not show.
        folded = _FoldedEnviron({"TELEGRAM_BOT_TOKEN": "a"})
        monkeypatch.setattr(os, "environ", folded)

        new = {"TELEGRAM_BOT_TOKEN": "valid-rotation", "telegram_bot_token": "revoked"}
        old = {"TELEGRAM_BOT_TOKEN": "a", "telegram_bot_token": "b"}
        for order in (
            ["TELEGRAM_BOT_TOKEN", "telegram_bot_token"],
            ["telegram_bot_token", "TELEGRAM_BOT_TOKEN"],
        ):
            folded["TELEGRAM_BOT_TOKEN"] = "a"
            loader.note_env_owned_key("TELEGRAM_BOT_TOKEN")
            loader.apply_env_credential_changes(order, new, old)
            assert folded["telegram_bot_token"] == "valid-rotation", order
            assert loader.env_owns_key("TELEGRAM_BOT_TOKEN")

    def test_windows_reordering_two_spellings_is_a_change_and_applies_the_new_winner(
        self, monkeypatch
    ):
        """A save that only REORDERS two case spellings holding different values
        changes no key's value, but on Windows it changes which spelling boot (and
        the live applier) installs. The diff must report it, and applying it must
        install the new first spelling's value. On POSIX the two names are two
        variables, so a reorder changes nothing. (Fixes the GPT order-only-edit
        finding.)"""
        from kiro_crew import platform_compat
        from kiro_crew.config import loader

        before = {"TELEGRAM_BOT_TOKEN": "first", "telegram_bot_token": "second"}
        after = {"telegram_bot_token": "second", "TELEGRAM_BOT_TOKEN": "first"}

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False, raising=False)
        assert loader.changed_env_keys(before, after) == frozenset()

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True, raising=False)
        changed = loader.changed_env_keys(before, after)
        assert changed == frozenset({"TELEGRAM_BOT_TOKEN", "telegram_bot_token"})

        folded = _FoldedEnviron({"TELEGRAM_BOT_TOKEN": "first"})
        monkeypatch.setattr(os, "environ", folded)
        loader.note_env_owned_key("TELEGRAM_BOT_TOKEN")
        loader.apply_env_credential_changes(changed, after, before)
        assert folded["TELEGRAM_BOT_TOKEN"] == "second"

    def test_windows_reordering_spellings_with_equal_values_is_not_a_change(self, monkeypatch):
        """Control: when both spellings hold the same value the winner's value is
        unchanged, so a reorder is still no diff and restarts nothing."""
        from kiro_crew import platform_compat
        from kiro_crew.config import loader

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True, raising=False)
        before = {"TELEGRAM_BOT_TOKEN": "same", "telegram_bot_token": "same"}
        after = {"telegram_bot_token": "same", "TELEGRAM_BOT_TOKEN": "same"}
        assert loader.changed_env_keys(before, after) == frozenset()

    def test_windows_reconcile_to_a_different_surviving_value_takes_ownership(self, monkeypatch):
        """A service supplies the token (unowned); .env holds two spellings with
        different values. Removing the service-matching spelling installs the
        surviving spelling's value -- a genuine .env write -- so .env must own the
        entry now. Otherwise removing the surviving line later would find an
        unowned key and leave the deleted credential active. (Fixes the GPT
        surviving-alias ownership finding.)"""
        from kiro_crew import platform_compat
        from kiro_crew.config import loader

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True, raising=False)
        folded = _FoldedEnviron({"TELEGRAM_BOT_TOKEN": "from-service"})
        monkeypatch.setattr(os, "environ", folded)
        loader.discard_env_owned_key("TELEGRAM_BOT_TOKEN")

        loader.apply_env_credential_changes(
            {"TELEGRAM_BOT_TOKEN"},
            {"telegram_bot_token": "from-dotenv"},
            {"TELEGRAM_BOT_TOKEN": "from-service", "telegram_bot_token": "from-dotenv"},
        )
        assert folded["TELEGRAM_BOT_TOKEN"] == "from-dotenv"
        assert loader.env_owns_key("telegram_bot_token")
        assert loader.env_owns_key("TELEGRAM_BOT_TOKEN")

    def test_windows_reconcile_that_changes_nothing_leaves_ownership_alone(self, monkeypatch):
        """Control: when the surviving spelling already holds the live value, the
        reconcile writes nothing, so an unowned inherited entry stays unowned."""
        from kiro_crew import platform_compat
        from kiro_crew.config import loader

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True, raising=False)
        folded = _FoldedEnviron({"TELEGRAM_BOT_TOKEN": "same"})
        monkeypatch.setattr(os, "environ", folded)
        loader.discard_env_owned_key("TELEGRAM_BOT_TOKEN")

        loader.apply_env_credential_changes(
            {"TELEGRAM_BOT_TOKEN"},
            {"telegram_bot_token": "same"},
            {"TELEGRAM_BOT_TOKEN": "same", "telegram_bot_token": "same"},
        )
        assert folded["TELEGRAM_BOT_TOKEN"] == "same"
        assert not loader.env_owns_key("TELEGRAM_BOT_TOKEN")

    def test_windows_removing_one_of_two_case_duplicates_keeps_the_entry(self, monkeypatch):
        """On Windows os.environ is case-insensitive, so ``HTTPS_PROXY`` and
        ``https_proxy`` are ONE entry. When ``.env`` holds BOTH and the operator
        removes only the lowercase line, the uppercase line still configures the
        entry -- but it is UNCHANGED, so it is absent from ``changed``. The
        removal must still not pop the shared entry, or a channel restart would
        build its client without the proxy it still needs. (Fixes the GPT
        duplicate-case-alias finding.)"""
        from kiro_crew import platform_compat
        from kiro_crew.config import loader

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True, raising=False)
        forget_env_at_teardown(monkeypatch, "https_proxy", "HTTPS_PROXY")

        # Boot owned both spellings; os.environ folds them to one entry.
        loader.note_env_owned_key("https_proxy")
        loader.note_env_owned_key("HTTPS_PROXY")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
        # Only the lowercase spelling left .env; the uppercase one is unchanged
        # (same value in old and new), so it is NOT in the changed set.
        loader.apply_env_credential_changes(
            {"https_proxy"},
            {"HTTPS_PROXY": "http://proxy:8080"},
            {"https_proxy": "http://proxy:8080", "HTTPS_PROXY": "http://proxy:8080"},
        )
        # The surviving uppercase line still configures the entry; the removed
        # lowercase alias did not delete it.
        assert os.environ.get("HTTPS_PROXY") == "http://proxy:8080"
        # Ownership of the shared entry is left intact for the surviving spelling.
        assert loader.env_owns_key("HTTPS_PROXY")

    def test_apply_rejects_a_nul_bearing_value_before_mutating(self, monkeypatch):
        """A NUL in a VALUE (not just the key) is also something os.environ
        rejects, so the batch must be refused before any key is written -- a
        legal key sharing the batch must not be left applied. (Fixes the GPT
        NUL-value partial-mutation finding.)"""
        from kiro_crew.config import loader

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "before")
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError):
            loader.apply_env_credential_changes(
                {"TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN"},
                {"TELEGRAM_BOT_TOKEN": "rotated", "DISCORD_BOT_TOKEN": "has\x00nul"},
                {"TELEGRAM_BOT_TOKEN": "before"},
            )
        assert (
            os.environ["TELEGRAM_BOT_TOKEN"] == "before"
        ), "no key may be written when a NUL-bearing value makes the batch illegal"

    def test_apply_sets_and_pops_overriding_an_inherited_value(self, monkeypatch):
        """A live edit MEANS the edit: unlike boot's setdefault, it overrides an
        inherited value and pops a removed key."""
        from kiro_crew.config import loader

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-shell")
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.setenv("WEBEX_BOT_TOKEN", "to-be-cleared")
        applied = loader.apply_env_credential_changes(
            {"TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "WEBEX_BOT_TOKEN"},
            {"TELEGRAM_BOT_TOKEN": "from-dotenv", "DISCORD_BOT_TOKEN": "new"},
        )
        assert applied == frozenset({"TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "WEBEX_BOT_TOKEN"})
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-dotenv"
        assert os.environ["DISCORD_BOT_TOKEN"] == "new"
        assert "WEBEX_BOT_TOKEN" not in os.environ

    def test_apply_skips_credential_keys_when_docker_scrubbed(self, monkeypatch):
        """With ``_KIROCREW_CREDS_SCRUBBED`` set, re-injecting a credential key would
        re-expose it in /proc/<pid>/environ; it is not applied. A non-credential
        operator setting still applies."""
        from kiro_crew.config import loader

        monkeypatch.setenv("_KIROCREW_CREDS_SCRUBBED", "1")
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        # The code under test writes MY_PROXY via os.environ[...] (not through
        # monkeypatch), so record the pre-test state for teardown or it leaks
        # into the xdist worker for every later test.
        forget_env_at_teardown(monkeypatch, "MY_PROXY")
        loader.apply_env_credential_changes(
            {"TELEGRAM_BOT_TOKEN", "MY_PROXY"},
            {"TELEGRAM_BOT_TOKEN": "secret", "MY_PROXY": "http://p"},
        )
        assert "TELEGRAM_BOT_TOKEN" not in os.environ
        assert os.environ["MY_PROXY"] == "http://p"


# ── ConfigWatch .env tracking ─────────────────────────────────────────────


class TestWatchEnvTracking:
    def _primed_watch(self, home: Path) -> ConfigWatch:
        """A watcher primed with the current config AND ``.env`` fingerprints, so
        a later ``.env`` edit is the ONLY thing a non-forced ``_cycle`` sees move
        -- the env-only branch (``not config_changed``)."""
        w = ConfigWatch(poll_interval_secs=0.05)
        w.prime(KiroCrewConfig.load(), w._current_fingerprint())
        w._creds = w._read_env_credentials()[0]
        w._env_fingerprint = w._current_env_fingerprint()
        return w

    @pytest.mark.asyncio
    async def test_env_only_edit_dispatches_credentials_paths_and_applies_environ(
        self, home: Path, monkeypatch
    ):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        _write_env(home, "TELEGRAM_BOT_TOKEN=first\n")
        w = self._primed_watch(home)

        seen: list[frozenset[str]] = []
        w.subscribe("credentials", callback=lambda c: seen.append(c.changed), name="creds")

        _write_env(home, "TELEGRAM_BOT_TOKEN=second\n")
        change = await w._cycle()  # not forced: only .env moved
        assert change is not None
        assert change.changed == frozenset({"credentials.TELEGRAM_BOT_TOKEN"})
        # config.json did not change, so old and new are the same live config.
        assert change.old is change.new is w.snapshot()
        assert seen == [frozenset({"credentials.TELEGRAM_BOT_TOKEN"})]
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "second"

    @pytest.mark.asyncio
    async def test_env_path_edit_expands_additions_off_the_event_loop(
        self, home: Path, monkeypatch
    ):
        """A ``.env`` PATH line is expanded/validated in a worker thread, not on
        the loop. Expanding a ``~otheruser`` entry calls ``pwd.getpwnam`` / the
        SAM database synchronously, which an LDAP/SSSD/AD lookup can stall; doing
        it on the loop would freeze chat processing and the liveness heartbeat.
        (Fixes the GPT finding at loader_credentials.py extend_process_path.)
        """
        import threading

        from kiro_crew import env as env_mod

        extra = str(home / "extra-bin")
        monkeypatch.setenv("PATH", str(home / "sys-bin"))
        _write_env(home, "TELEGRAM_BOT_TOKEN=first\n")
        w = self._primed_watch(home)

        loop_thread = threading.get_ident()
        prepare_threads: list[int] = []
        real_prepare = env_mod.prepare_env_path_additions

        def _spy(value: str):
            prepare_threads.append(threading.get_ident())
            return real_prepare(value)

        monkeypatch.setattr(env_mod, "prepare_env_path_additions", _spy)

        _write_env(home, f"TELEGRAM_BOT_TOKEN=first\nPATH={extra}\n")
        change = await w._cycle()  # not forced: only .env moved

        assert change is not None
        assert "credentials.PATH" in change.changed
        # The additions landed on PATH (expansion really ran) ...
        assert extra in os.environ["PATH"].split(os.pathsep)
        # ... and the expand/validate step ran OFF the event-loop thread, so a
        # blocking ~otheruser lookup could never stall the loop.
        assert prepare_threads, "prepare_env_path_additions was never called"
        assert loop_thread not in prepare_threads

    @pytest.mark.asyncio
    async def test_a_properly_primed_first_read_dispatches_nothing(self, home: Path):
        """When _prime_creds_baseline already read the SAME map definitively
        (the normal boot path), the first _cycle() sees no change and dispatches
        nothing: the map really was already applied at boot."""
        _write_env(home, "TELEGRAM_BOT_TOKEN=only\n")
        w = ConfigWatch(poll_interval_secs=0.05)
        w.prime(KiroCrewConfig.load(), w._current_fingerprint())
        await w._prime_creds_baseline()
        assert w._creds == {"TELEGRAM_BOT_TOKEN": "only"}
        seen: list[frozenset[str]] = []
        w.subscribe("credentials", callback=lambda c: seen.append(c.changed), name="creds")
        change = await w._cycle()
        assert change is None
        assert seen == []

    @pytest.mark.asyncio
    async def test_an_unprimed_first_read_diffs_against_empty_and_applies(
        self, home: Path, monkeypatch
    ):
        """_creds is None only when no prior read ever became a baseline (boot's
        own credential load failed the same decode the same way, so nothing
        from .env ever reached os.environ -- see _prime_creds_baseline). The
        first definitive read must then diff against an EMPTY baseline and
        apply/dispatch the key, not silently adopt it as already in force.
        (Fixes the GPT finding: a repaired .env's credential was dropped on the
        floor because the first successful read after a decode failure only
        baselined instead of applying.)"""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        _write_env(home, "TELEGRAM_BOT_TOKEN=only\n")
        w = ConfigWatch(poll_interval_secs=0.05)
        w.prime(KiroCrewConfig.load(), w._current_fingerprint())
        # _creds left None: simulates boot's own read never having been definitive.
        seen: list[frozenset[str]] = []
        w.subscribe("credentials", callback=lambda c: seen.append(c.changed), name="creds")
        change = await w._cycle()
        assert change is not None
        assert change.changed == frozenset({"credentials.TELEGRAM_BOT_TOKEN"})
        assert seen == [frozenset({"credentials.TELEGRAM_BOT_TOKEN"})]
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "only"
        assert w._creds == {"TELEGRAM_BOT_TOKEN": "only"}

    @pytest.mark.asyncio
    async def test_recovering_from_a_wide_encoded_env_applies_the_repaired_value(
        self, home: Path, monkeypatch
    ):
        """A UTF-16 .env at boot leaves _creds None (nothing applied). The
        operator repairs it to UTF-8; the first definitive read after the repair
        must apply the credential, not just adopt it as baseline."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        (home / ".env").write_bytes(b"\xff\xfeT\x00")
        w = ConfigWatch(poll_interval_secs=0.05)
        w.prime(KiroCrewConfig.load(), w._current_fingerprint())
        await w._prime_creds_baseline()
        assert w._creds is None, "a wide-encoded boot read leaves no baseline"
        seen: list[frozenset[str]] = []
        w.subscribe("credentials", callback=lambda c: seen.append(c.changed), name="creds")

        _write_env(home, "TELEGRAM_BOT_TOKEN=repaired\n")
        change = await w._cycle()
        assert change is not None
        assert change.changed == frozenset({"credentials.TELEGRAM_BOT_TOKEN"})
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "repaired"
        assert w._creds == {"TELEGRAM_BOT_TOKEN": "repaired"}

    @pytest.mark.asyncio
    async def test_a_transient_read_failure_is_retried_next_tick_not_dropped(
        self, home: Path, monkeypatch
    ):
        """A one-off OSError (e.g. a Windows sharing violation) must not advance
        the .env fingerprint: if it did, the next tick would see no fingerprint
        change, skip the read entirely, and the rotation that raced the failure
        would never be retried. (Fixes the GPT finding.)"""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "before")
        _write_env(home, "TELEGRAM_BOT_TOKEN=before\n")
        w = self._primed_watch(home)
        seen: list[frozenset[str]] = []
        w.subscribe("credentials", callback=lambda c: seen.append(c.changed), name="creds")
        fp_before = w._env_fingerprint

        _write_env(home, "TELEGRAM_BOT_TOKEN=rotated\n")
        # A one-off transient read failure: read_env_credentials_raw's own
        # OSError handling makes this non-definitive without raising.
        with patch("kiro_crew.config.loader.read_env_text", side_effect=OSError("transient")):
            change = await w._cycle()
        assert change is None
        assert seen == []
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "before", "not yet applied"
        assert w._env_fingerprint == fp_before, "fingerprint not advanced on a failed read"

        # The sharing violation clears; the SAME file state (same fingerprint)
        # must still be retried because env_changed stays true against the
        # stale fingerprint.
        change = await w._cycle()
        assert change is not None
        assert change.changed == frozenset({"credentials.TELEGRAM_BOT_TOKEN"})
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "rotated"

    @pytest.mark.asyncio
    async def test_a_failed_apply_leaves_the_baseline_so_a_repair_reapplies_the_rotation(
        self, home: Path, monkeypatch
    ):
        """If applying the diff raises (the apply is best-effort and swallows),
        the baseline and the .env fingerprint must NOT advance and nothing may
        dispatch -- otherwise a rotation that never reached os.environ is
        recorded as in force, and a later repair that reverts the offending edit
        diffs only that edit and never re-applies the rotation, stranding the
        channel after the fix. (Fixes the GPT finding at live.py:_apply_credential_diff.)"""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "before")
        _write_env(home, "TELEGRAM_BOT_TOKEN=before\n")
        w = self._primed_watch(home)
        seen: list[frozenset[str]] = []
        w.subscribe("credentials", callback=lambda c: seen.append(c.changed), name="creds")
        assert w._creds is not None
        baseline_before = dict(w._creds)
        fp_before = w._env_fingerprint

        # The operator rotates the token in the same save as a malformed line
        # (an empty-name `=value`): the applier validates the whole diff first
        # and raises ValueError before writing anything, so the rotation is NOT
        # partially applied to os.environ.
        _write_env(home, "TELEGRAM_BOT_TOKEN=rotated\n=garbage\n")
        change = await w._cycle()
        assert change is None, "a failed apply dispatches no restart"
        assert seen == []
        assert w._creds == baseline_before, "baseline not advanced on a failed apply"
        assert w._env_fingerprint == fp_before, "fingerprint not advanced on a failed apply"
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "before", (
            "the rotated token must NOT reach os.environ when the batch is rejected; "
            "a partial mutation would survive a later file restore"
        )

        # The operator repairs the file: removes the malformed line, keeps the
        # rotated token. The diff must still see before->rotated and apply it.
        _write_env(home, "TELEGRAM_BOT_TOKEN=rotated\n")
        change = await w._cycle()
        assert change is not None
        assert change.changed == frozenset({"credentials.TELEGRAM_BOT_TOKEN"})
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "rotated"
        assert w._creds == {"TELEGRAM_BOT_TOKEN": "rotated"}

    @pytest.mark.asyncio
    async def test_a_persistently_failing_apply_warns_once_not_every_tick(
        self, home: Path, monkeypatch, caplog
    ):
        """A failed apply keeps the .env fingerprint unadvanced so the next tick
        retries -- which means the SAME bad file re-fails every ~2s. The WARNING
        (with its traceback) must fire ONCE per distinct failing file state and
        the retries log at DEBUG, not spew a traceback every tick. (Fixes the
        Opus log-spam finding.)"""
        import logging

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "before")
        _write_env(home, "TELEGRAM_BOT_TOKEN=before\n")
        w = self._primed_watch(home)

        def _warn_count() -> int:
            return sum(
                1
                for r in caplog.records
                if r.levelno == logging.WARNING
                and "applying .env credential changes failed" in r.getMessage()
            )

        # A malformed save that fails the apply on every tick. The fingerprint
        # does not advance, so re-running _cycle keeps retrying the same file.
        _write_env(home, "TELEGRAM_BOT_TOKEN=rotated\n=garbage\n")
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.config.live"):
            for _ in range(5):
                assert await w._cycle() is None
        assert _warn_count() == 1, "the apply-failure WARNING fires once, not every tick"

        # A DIFFERENT malformed save is a new failing fingerprint -> warns again.
        _write_env(home, "TELEGRAM_BOT_TOKEN=rotated2\n=garbage\n")
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.config.live"):
            assert await w._cycle() is None
        assert _warn_count() == 2, "a new failing file state warns afresh"

    @pytest.mark.asyncio
    async def test_a_pure_env_edit_does_not_rebuild_the_config_snapshot(
        self, home: Path, monkeypatch
    ):
        """Only ``.env`` moved: the document snapshot object is untouched (the
        parsed-config cache stays valid), and only the credential diff dispatches."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        _write_env(home, "TELEGRAM_BOT_TOKEN=a\n")
        w = self._primed_watch(home)
        before = w.snapshot()
        _write_env(home, "TELEGRAM_BOT_TOKEN=b\n")
        await w._cycle()
        assert w.snapshot() is before, "the config snapshot object is not rebuilt"

    @pytest.mark.asyncio
    async def test_a_config_and_env_edit_in_one_tick_fold_into_one_dispatch(
        self, home: Path, monkeypatch
    ):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        (home / "config.json").write_text('{"agent": {"model": "m1"}}', encoding="utf-8")
        _write_env(home, "TELEGRAM_BOT_TOKEN=a\n")
        w = ConfigWatch(poll_interval_secs=0.05)
        w.prime(KiroCrewConfig.load())
        w._creds = w._read_env_credentials()[0]
        w._env_fingerprint = w._current_env_fingerprint()

        seen: list[frozenset[str]] = []
        w.subscribe(callback=lambda c: seen.append(c.changed), name="all")

        (home / "config.json").write_text('{"agent": {"model": "m2"}}', encoding="utf-8")
        st = (home / "config.json").stat()
        os.utime(home / "config.json", ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        _write_env(home, "TELEGRAM_BOT_TOKEN=b\n")

        change = await w.refresh_now()
        assert change is not None
        assert "agent.model" in change.changed
        assert "credentials.TELEGRAM_BOT_TOKEN" in change.changed
        assert change.new.agent.model == "m2"
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "b"
        assert seen == [change.changed]

    @pytest.mark.asyncio
    async def test_unchanged_env_and_config_returns_none(self, home: Path):
        _write_env(home, "FOO=1\n")
        w = ConfigWatch(poll_interval_secs=0.05)
        w.prime(KiroCrewConfig.load())
        w._creds = w._read_env_credentials()[0]
        w._env_fingerprint = w._current_env_fingerprint()
        assert await w.refresh_now() is None

    @pytest.mark.asyncio
    async def test_a_hold_begun_during_the_env_read_defers_the_credential_change(
        self, home: Path, monkeypatch
    ):
        """A dashboard save writes config.json then .env under live.hold(). If the
        hold begins during the watcher's off-loop .env read, applying now would
        pair the new credential with the pre-save authorization roster. The cycle
        must defer -- apply and dispatch nothing, not even os.environ, not advance
        the .env fingerprint -- and force another cycle that runs after commit."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        _write_env(home, "TELEGRAM_BOT_TOKEN=a\n")
        w = self._primed_watch(home)
        seen: list[frozenset[str]] = []
        w.subscribe("credentials", callback=lambda c: seen.append(c.changed), name="creds")
        fp_before = w._env_fingerprint
        _write_env(home, "TELEGRAM_BOT_TOKEN=b\n")

        held = w.hold()
        real_read = w._read_env_credentials

        def read_then_hold():
            result = real_read()
            held.__enter__()  # a transaction begins while this off-loop read is in flight
            return result

        monkeypatch.setattr(w, "_read_env_credentials", read_then_hold)
        change = await w._cycle()
        assert change is None, "a transaction mid-read defers the credential change"
        assert seen == []
        assert "TELEGRAM_BOT_TOKEN" not in os.environ, "os.environ is not mutated under the hold"
        assert w._env_fingerprint == fp_before, "the .env fingerprint is not advanced"
        assert w._force is True, "a fresh cycle is forced for after the hold releases"
        held.__exit__(None, None, None)

    @pytest.mark.asyncio
    async def test_a_wide_encoded_save_does_not_unset_loaded_keys(self, home: Path, monkeypatch):
        """A UTF-16 .env save is a NON-definitive read: its empty map must not be
        diffed as every key cleared (which would close channels that depend on
        them). The baseline is kept and nothing is dispatched."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "live")
        _write_env(home, "TELEGRAM_BOT_TOKEN=live\n")
        w = self._primed_watch(home)
        seen: list[frozenset[str]] = []
        w.subscribe("credentials", callback=lambda c: seen.append(c.changed), name="creds")
        # Replace the file with a UTF-16 (wide-encoded) save.
        (home / ".env").write_bytes(b"\xff\xfeT\x00")
        st = (home / ".env").stat()
        os.utime(home / ".env", ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        change = await w._cycle()
        assert change is None, "a non-definitive read dispatches nothing"
        assert seen == []
        assert w._creds == {"TELEGRAM_BOT_TOKEN": "live"}, "baseline kept, not cleared"
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "live", "the loaded key is not unset"


class TestBootWindowCredentialBaseline:
    """The watcher's start() boot-window seam.

    ``start(initial=...)`` must seed its credential baseline with the raw
    ``.env`` map boot's own ``load_credentials()`` actually read -- NOT a fresh
    disk read at start() time. The gateway loads credentials well before the
    post-bind watcher start; a ``.env`` rotation in that window would otherwise
    be read off disk at start() and adopted as the baseline, so the first cycle
    re-reads the same rotated value, sees no diff, and never pushes it into
    ``os.environ`` -- leaving the channel on the revoked credential until the
    next gateway restart. (Fixes GPT finding F1.)
    """

    async def _start_then_quiesce(self, w: ConfigWatch, initial):
        """start() the watcher with its poll loop stubbed to a no-op, so the
        seeding start() does runs but the background loop never races the
        _cycle() a test drives by hand."""

        async def _noop() -> None:
            return None

        with patch.object(w, "_run", _noop):
            await w.start(initial=initial)
        await w.stop()

    @pytest.mark.asyncio
    async def test_a_boot_window_rotation_is_applied_on_the_first_cycle(
        self, home: Path, monkeypatch
    ):
        from kiro_crew.config.loader_credentials import record_boot_raw_env

        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        # Boot read .env as "boot-value" and recorded it (os.environ carries it).
        _write_env(home, "TELEGRAM_BOT_TOKEN=boot-value\n")
        initial = KiroCrewConfig.load()
        record_boot_raw_env({"TELEGRAM_BOT_TOKEN": "boot-value"})
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "boot-value")

        # The operator rotates .env in the window before the watcher starts.
        _write_env(home, "TELEGRAM_BOT_TOKEN=rotated-in-window\n")

        w = ConfigWatch(poll_interval_secs=0.05)
        await self._start_then_quiesce(w, initial)
        # Seeded from the boot map, NOT the rotated disk value read at start().
        assert w._creds == {"TELEGRAM_BOT_TOKEN": "boot-value"}

        seen: list[frozenset[str]] = []
        w.subscribe("credentials", callback=lambda c: seen.append(c.changed), name="creds")
        change = await w._cycle()
        assert change is not None
        assert change.changed == frozenset({"credentials.TELEGRAM_BOT_TOKEN"})
        assert seen == [frozenset({"credentials.TELEGRAM_BOT_TOKEN"})]
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "rotated-in-window"

    @pytest.mark.asyncio
    async def test_no_window_edit_dispatches_nothing_no_channel_bounce(
        self, home: Path, monkeypatch
    ):
        """When .env did not change in the boot window, the baseline equals the
        current disk map, so the first cycle dispatches nothing -- channels do
        not bounce on every boot."""
        from kiro_crew.config.loader_credentials import record_boot_raw_env

        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        _write_env(home, "TELEGRAM_BOT_TOKEN=steady\n")
        initial = KiroCrewConfig.load()
        record_boot_raw_env({"TELEGRAM_BOT_TOKEN": "steady"})
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "steady")

        w = ConfigWatch(poll_interval_secs=0.05)
        await self._start_then_quiesce(w, initial)
        assert w._creds == {"TELEGRAM_BOT_TOKEN": "steady"}

        seen: list[frozenset[str]] = []
        w.subscribe("credentials", callback=lambda c: seen.append(c.changed), name="creds")
        change = await w._cycle()
        assert change is None
        assert seen == []

    @pytest.mark.asyncio
    async def test_falls_back_to_off_loop_read_when_no_boot_map_recorded(
        self, home: Path, monkeypatch
    ):
        """With no recorded boot map (a test or path that never ran
        load_credentials), start() falls back to the off-loop disk read so the
        baseline is still established."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        _write_env(home, "TELEGRAM_BOT_TOKEN=disk\n")
        initial = KiroCrewConfig.load()
        # reset_for_tests() in the home fixture already cleared any boot map.

        w = ConfigWatch(poll_interval_secs=0.05)
        await self._start_then_quiesce(w, initial)
        assert w._creds == {"TELEGRAM_BOT_TOKEN": "disk"}
