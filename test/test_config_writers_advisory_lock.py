"""Converted config writers share the advisory lock, so neither side is lost (#8032).

``update_config_locked`` holds an advisory lock on a ``<path>.lock`` sidecar for
the whole read-modify-write. A writer that instead reads with a bare
``read_config_for_update`` / ``json.loads`` and writes with
``write_config_atomically`` under the in-process asyncio ``_get_config_lock()``
is serialized against same-loop callers ONLY -- not against a holder of the
sidecar, not against a worker thread, and not against another process. Such a
writer and a locked read-modify-write can therefore interleave, and whichever
renames second publishes a document that never saw the other's change.

Each test here drives one converted writer against a locked writer in the
interleave that used to lose data, and asserts BOTH changes survive. They fail on
the pre-conversion shape and pass after it, which is the property that matters:
"holds a lock" is not observable, "did not lose the other writer's setting" is.
"""

from __future__ import annotations

import json
import threading
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.config import loader as cfg_loader

#: Bounded so a regression fails the test instead of hanging the suite.
_TIMEOUT = 30.0


@pytest.fixture()
def cfg_file(tmp_path: Path) -> Path:
    """A ``config.json`` carrying settings BOTH writers must preserve.

    ``model`` belongs to neither writer, so it is the canary for a whole-document
    clobber; ``session.timeout_secs`` proves a nested section survives.
    """
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "model": "sonnet",
                "session": {"timeout_secs": 7200},
                "agent": {"apps_trusted": ["zibble-app"], "model": "sonnet"},
            }
        ),
        encoding="utf-8",
    )
    return path


class TestAppsManagerTrustRevoke:
    """``apps.manager._drop_trust_grant`` -- the CLI-side uninstall writer.

    It runs synchronously in the ``kirocrew`` CLI process and in the dashboard's
    ``subprocess_executor()`` worker thread. Neither can take the loop's asyncio
    lock, so the sidecar is the only thing that can serialize it against a
    settings write.
    """

    def test_a_locked_writer_landing_mid_revoke_is_not_lost(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two writers, deterministically interleaved: neither may lose the other.

        Writer A is an ordinary locked config write (the shape ``kirocrew config
        set`` and the dashboard settings PATCH both use), suspended inside its
        mutate callback so it is holding the sidecar. Writer B is the trust
        revoke, started while A holds it.

        Before the conversion B took no advisory lock: it read straight away --
        observing the document as it was BEFORE A's write -- and renamed over the
        top, so exactly one of the two changes reached disk depending on who
        renamed last. After it, B waits for the sidecar and re-reads inside its
        own hold, so both land.
        """
        from kiro_crew.apps import manager as appmanager

        monkeypatch.setattr(appmanager, "config_path", lambda: cfg_file)
        monkeypatch.setattr(appmanager, "config_local_path", lambda: cfg_file.parent / "local.json")

        a_holding = threading.Event()
        a_may_finish = threading.Event()
        errors: list[BaseException] = []

        def _settings_write(data: dict) -> dict:
            a_holding.set()
            assert a_may_finish.wait(_TIMEOUT), "test bug: the settings write was never released"
            data.setdefault("session", {})["autocompact_pct"] = 42.0
            return data

        def _writer_a() -> None:
            try:
                cfg_loader.update_config_locked(cfg_file, mutate=_settings_write, stamp_meta=False)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        def _writer_b() -> None:
            try:
                appmanager._drop_trust_grant("zibble-app")
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        thread_a = threading.Thread(target=_writer_a, daemon=True)
        thread_b = threading.Thread(target=_writer_b, daemon=True)
        # try/finally: an assertion below would otherwise exit with a writer
        # still parked on the event, leaking it into later tests.
        try:
            thread_a.start()
            assert a_holding.wait(_TIMEOUT), "the settings write never reached its mutate"

            thread_b.start()
            # Give B a chance to reach (or block on) its write before A resumes.
            # A short join, not a bare sleep: it returns as soon as B is done in
            # the unlocked case, and simply times out while B is blocked.
            thread_b.join(timeout=1.0)

            a_may_finish.set()
            thread_a.join(timeout=_TIMEOUT)
            thread_b.join(timeout=_TIMEOUT)
            assert not thread_a.is_alive(), "the settings write did not finish"
            assert not thread_b.is_alive(), "the trust revoke did not finish"
        finally:
            a_may_finish.set()
            for thread in (thread_a, thread_b):
                if thread.is_alive():
                    thread.join(timeout=_TIMEOUT)

        assert not errors, f"writer raised: {errors!r}"

        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert on_disk["agent"]["apps_trusted"] == [], (
            "the trust revoke was lost -- the app is still trusted after being "
            "uninstalled, which is the 'uninstalled but still trusted' state the "
            "withdrawal exists to prevent"
        )
        assert on_disk["session"]["autocompact_pct"] == 42.0, (
            "the settings write that landed while the revoke was in flight was "
            "lost -- the revoke does not share the advisory lock"
        )
        # Neither writer owns these, so a whole-document clobber shows up here.
        assert on_disk["model"] == "sonnet"
        assert on_disk["session"]["timeout_secs"] == 7200

    def test_the_revoke_takes_the_sidecar(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Structural companion: the lock is taken on the sidecar, not the file.

        ``write_config_atomically`` replaces the inode, so a lock on the config
        file's own fd would not serialize against the rename. Asserting the
        sidecar exists pins that the revoke reached the primitive at all, which
        the interleave test above can only show indirectly.
        """
        from kiro_crew.apps import manager as appmanager

        monkeypatch.setattr(appmanager, "config_path", lambda: cfg_file)
        monkeypatch.setattr(appmanager, "config_local_path", lambda: cfg_file.parent / "local.json")

        appmanager._drop_trust_grant("zibble-app")

        assert (cfg_file.parent / "config.json.lock").exists(), (
            "the trust revoke wrote config.json without taking the <config>.lock "
            "sidecar, so it is still unserialized against every other writer"
        )
        assert json.loads(cfg_file.read_text(encoding="utf-8"))["agent"]["apps_trusted"] == []

    def test_an_unreadable_config_refuses_instead_of_resetting(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-closed is preserved: a corrupt config is refused, never replaced.

        The conversion must not quietly become ``on_corrupt="reset"``. A revoke
        that wrote a single-key document over a truncated config would destroy
        every setting the file holds -- the exact loss ``read_config_for_update``
        exists to prevent -- and the old code raised here for the same reason.
        """
        from kiro_crew.apps import manager as appmanager

        monkeypatch.setattr(appmanager, "config_path", lambda: cfg_file)
        monkeypatch.setattr(appmanager, "config_local_path", lambda: cfg_file.parent / "local.json")
        cfg_file.write_text('{"agent": {"apps_trus', encoding="utf-8")
        before = cfg_file.read_text(encoding="utf-8")

        with pytest.raises(RuntimeError, match="unreadable"):
            appmanager._drop_trust_grant("zibble-app")

        assert (
            cfg_file.read_text(encoding="utf-8") == before
        ), "the unreadable config was overwritten"


class TestSetupWizardWriters:
    """``cli_setup`` -- the widest read-to-write window in the tree.

    A wizard step reads the document to compute a prompt default, then blocks on
    the operator, then writes. Whatever lands during the prompt is inside that
    window, so this family is where a whole-document rewrite is most likely to
    revert a real setting.
    """

    def test_a_write_during_the_prompt_survives_the_slash_command_step(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write applies to the document as it stands, not to the pre-prompt read.

        Driven through the prompt itself rather than with threads: the competing
        locked write runs from inside ``_input_or_skip``, i.e. strictly after the
        step's own read and strictly before its write. That is the interleave in
        its exact worst case, with no timing to be flaky about.
        """
        from kiro_crew import cli_setup

        monkeypatch.setattr(cli_setup, "config_path", lambda: cfg_file)

        def _answer_after_a_competing_write(_prompt: str) -> str:
            # Lands while the wizard is between its read and its write.
            cfg_loader.update_config_locked(
                cfg_file,
                mutate=lambda data: {**data, "timezone": "Asia/Shanghai"},
                stamp_meta=False,
            )
            return "zibble-cmd"

        monkeypatch.setattr(cli_setup, "_input_or_skip", _answer_after_a_competing_write)

        cli_setup._setup_slash_command()

        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert on_disk["slack"]["command"] == "zibble-cmd", "the wizard's own edit was lost"
        assert on_disk["timezone"] == "Asia/Shanghai", (
            "the config write that landed while the operator was answering was "
            "reverted -- the wizard wrote back its pre-prompt snapshot"
        )
        assert on_disk["model"] == "sonnet"
        assert on_disk["session"]["timeout_secs"] == 7200

    def test_a_write_during_the_prompt_survives_the_timezone_step(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same window on the step with the most prompts, hence the widest one."""
        from kiro_crew import cli_setup

        monkeypatch.setattr(cli_setup, "config_path", lambda: cfg_file)

        def _answer_after_a_competing_write(_prompt: str) -> str:
            cfg_loader.update_config_locked(
                cfg_file,
                mutate=lambda data: {**data, "auto_update": True},
                stamp_meta=False,
            )
            return "America/Los_Angeles"

        monkeypatch.setattr(cli_setup, "_input_or_skip", _answer_after_a_competing_write)
        monkeypatch.setattr(cli_setup, "_detect_system_timezone", lambda: "")

        cli_setup._setup_timezone()

        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert on_disk["timezone"] == "America/Los_Angeles", "the wizard's own edit was lost"
        assert (
            on_disk["auto_update"] is True
        ), "the config write that landed while the operator was answering was reverted"
        assert on_disk["model"] == "sonnet"


class TestAForeignSectionIsNeverReplaced:
    """A mutate callback must ABORT on a non-dict section, not overwrite it.

    These live beside the lock tests because the callback is what the conversion
    introduced: the pre-lock code reached its section through
    ``dict.setdefault``, which on a scalar either raised (slash command) or was
    caught and reported as a save failure (dashboard URL). A callback that
    instead assigns a fresh ``{}`` over that scalar destroys an operator value
    the step does not own AND reports success -- silent config loss, in the exact
    shape #8032 exists to stop, reintroduced by the fix for it.

    A section that is genuinely ABSENT is still created; that is the ordinary
    path and must keep working, so each case is pinned in both directions.
    """

    @staticmethod
    def _seed(cfg_file: Path, section: object, key: str) -> None:
        cfg_file.write_text(json.dumps({"model": "sonnet", key: section}), encoding="utf-8")

    def test_the_slash_command_step_leaves_a_scalar_slack_section_alone(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kiro_crew import cli_setup

        monkeypatch.setattr(cli_setup, "config_path", lambda: cfg_file)
        monkeypatch.setattr(cli_setup, "_input_or_skip", lambda _prompt: "zibble-cmd")
        self._seed(cfg_file, "not-an-object", "slack")
        before = cfg_file.read_text(encoding="utf-8")

        cli_setup._setup_slash_command()

        assert cfg_file.read_text(encoding="utf-8") == before, (
            "the step replaced a non-object 'slack' section with a fresh one, "
            "destroying the operator's value"
        )
        out = capsys.readouterr().out
        assert "not an object" in out, "the refusal was silent"
        assert "✅" not in out, "the step reported success after writing nothing"

    def test_the_slash_command_step_still_creates_an_absent_slack_section(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import cli_setup

        monkeypatch.setattr(cli_setup, "config_path", lambda: cfg_file)
        monkeypatch.setattr(cli_setup, "_input_or_skip", lambda _prompt: "zibble-cmd")
        cfg_file.write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")

        cli_setup._setup_slash_command()

        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert on_disk["slack"]["command"] == "zibble-cmd"
        assert on_disk["model"] == "sonnet"

    def test_the_dashboard_url_step_leaves_a_scalar_dashboard_section_alone(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kiro_crew import cli_setup

        monkeypatch.setattr(cli_setup, "config_path", lambda: cfg_file)
        self._seed(cfg_file, "not-an-object", "dashboard")
        before = cfg_file.read_text(encoding="utf-8")

        loaded = unittest.mock.MagicMock()
        loaded.dashboard.url = "http://old:1234"
        loaded.load_credentials.return_value = {
            "SLACK_APP_TOKEN": "xapp-fake",
            "SLACK_BOT_TOKEN": "xoxb-fake",
        }
        monkeypatch.setattr(cli_setup.KiroCrewConfig, "load", staticmethod(lambda: loaded))
        monkeypatch.setattr(cli_setup.socket, "gethostbyname", lambda _h: "10.0.0.1")
        monkeypatch.setattr(cli_setup.socket, "gethostname", lambda: "zibble-host")
        monkeypatch.setattr("builtins.input", lambda _prompt: "http://zibble-host:5476")

        cli_setup._maybe_setup_dashboard_url()

        assert (
            cfg_file.read_text(encoding="utf-8") == before
        ), "the step replaced a non-object 'dashboard' section with a fresh one"
        out = capsys.readouterr().out
        assert "Failed to save" in out, "the refusal was silent"

    def test_the_dashboard_url_step_still_creates_an_absent_dashboard_section(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import cli_setup

        monkeypatch.setattr(cli_setup, "config_path", lambda: cfg_file)
        cfg_file.write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")

        loaded = unittest.mock.MagicMock()
        loaded.dashboard.url = "http://old:1234"
        loaded.load_credentials.return_value = {
            "SLACK_APP_TOKEN": "xapp-fake",
            "SLACK_BOT_TOKEN": "xoxb-fake",
        }
        monkeypatch.setattr(cli_setup.KiroCrewConfig, "load", staticmethod(lambda: loaded))
        monkeypatch.setattr(cli_setup.socket, "gethostbyname", lambda _h: "10.0.0.1")
        monkeypatch.setattr(cli_setup.socket, "gethostname", lambda: "zibble-host")
        monkeypatch.setattr("builtins.input", lambda _prompt: "http://zibble-host:5476")

        cli_setup._maybe_setup_dashboard_url()

        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert on_disk["dashboard"]["url"] == "http://zibble-host:5476"
        assert on_disk["model"] == "sonnet"


class TestTheSeedWritesNoMetaBlock:
    """``_ensure_default_agent_in_config`` must not stamp ``meta`` (#8032 round 2).

    The writer it replaced stamped nothing, and this conversion is about the lock
    rather than the document's shape. Without ``stamp_meta=False`` a fresh
    install's first chat would rewrite a key this function does not own, which is
    a shape change smuggled in by a locking fix.
    """

    def test_the_seed_adds_no_meta_key(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import cli_chat

        monkeypatch.setattr(cli_chat, "config_path", lambda: cfg_file)
        cfg_file.write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")

        cli_chat._ensure_default_agent_in_config()

        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert on_disk["agents"]["default"]["kiro_agent"] == "kirocrew"
        assert on_disk["default_agent"] == "default"
        assert "meta" not in on_disk, (
            "the default-agent seed stamped a meta block -- the writer it "
            "replaced stamped nothing, so this is an unrelated shape change"
        )
        assert on_disk["model"] == "sonnet"

    def test_the_seed_skips_entirely_when_agents_already_exist(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """mutate returns None, so the file is not rewritten at all."""
        from kiro_crew import cli_chat

        monkeypatch.setattr(cli_chat, "config_path", lambda: cfg_file)
        cfg_file.write_text(
            json.dumps({"agents": {"mine": {"kiro_agent": "zibble"}}}), encoding="utf-8"
        )
        before = cfg_file.read_text(encoding="utf-8")

        cli_chat._ensure_default_agent_in_config()

        assert (
            cfg_file.read_text(encoding="utf-8") == before
        ), "the seed rewrote a config that already had agents"


class TestARestoredGrantIsNotDuplicated:
    """``_restore_trust_grant`` must append under the lock, not on top of it.

    The pre-lock ``_has_trust_grant`` check answers "should this restore run at
    all". It is not the read the write is derived from, so a dashboard re-grant
    landing between that check and the advisory acquire is invisible to it -- and
    an unconditional append then persists the name twice. A duplicated entry in
    ``apps_trusted`` is a corrupted consent record: it is the durable list that
    decides whether a third-party app may execute, so it must hold each name
    once and exactly once.

    ``apps_trusted_local`` in the same callback was already guarded, which is
    what makes the base list's unconditional append an inconsistency rather than
    a deliberate choice.
    """

    def test_a_regrant_landing_before_the_lock_is_not_appended_twice(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The restore observes the re-grant under the lock and leaves one entry.

        Driven deterministically: the competing locked re-grant runs from inside
        ``_has_trust_grant``, i.e. strictly after the restore's own pre-lock check
        has been answered and strictly before its acquire. That is the window in
        its exact worst case, with no timing to be flaky about.
        """
        from kiro_crew.apps import manager as appmanager

        monkeypatch.setattr(appmanager, "config_path", lambda: cfg_file)
        monkeypatch.setattr(appmanager, "config_local_path", lambda: cfg_file.parent / "local.json")

        # No grant yet, so the restore has work to do.
        cfg_file.write_text(
            json.dumps({"model": "sonnet", "agent": {"apps_trusted": []}}), encoding="utf-8"
        )

        installed = object()
        monkeypatch.setattr(appmanager, "_read_installed", lambda _n: installed)

        def _regrant_then_report_absent(_name: str) -> bool:
            # The dashboard re-grants while the restore is between its check and
            # its acquire. Reports False so the restore proceeds, exactly as it
            # would have on the stale answer.
            cfg_loader.update_config_locked(
                cfg_file,
                mutate=lambda data: {
                    **data,
                    "agent": {**data.get("agent", {}), "apps_trusted": ["zibble-app"]},
                },
                stamp_meta=False,
            )
            return False

        monkeypatch.setattr(appmanager, "_has_trust_grant", _regrant_then_report_absent)

        appmanager._restore_trust_grant(
            "zibble-app", had_grant=True, expected_app=installed  # type: ignore[arg-type]
        )

        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert on_disk["agent"]["apps_trusted"] == ["zibble-app"], (
            "the restore appended a name the locked document already held -- the "
            f"persisted consent list is corrupted: {on_disk['agent']['apps_trusted']}"
        )
        assert on_disk["model"] == "sonnet"

    def test_a_restore_with_no_existing_grant_still_adds_the_name(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinary path is unchanged: absent means append."""
        from kiro_crew.apps import manager as appmanager

        monkeypatch.setattr(appmanager, "config_path", lambda: cfg_file)
        monkeypatch.setattr(appmanager, "config_local_path", lambda: cfg_file.parent / "local.json")
        cfg_file.write_text(
            json.dumps({"model": "sonnet", "agent": {"apps_trusted": ["other-app"]}}),
            encoding="utf-8",
        )

        installed = object()
        monkeypatch.setattr(appmanager, "_read_installed", lambda _n: installed)
        monkeypatch.setattr(appmanager, "_has_trust_grant", lambda _n: False)

        appmanager._restore_trust_grant(
            "zibble-app", had_grant=True, expected_app=installed  # type: ignore[arg-type]
        )

        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert on_disk["agent"]["apps_trusted"] == ["other-app", "zibble-app"]
        assert on_disk["model"] == "sonnet"
