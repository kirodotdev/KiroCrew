"""``kirocrew tailnet`` — the command that makes exposing the dashboard one step.

Reaching the dashboard from another device on a tailnet takes two independent
changes: publish it with ``tailscale serve``, and tell the gateway to trust the
resulting origin. Either one alone is a dead end — publish without trust and every
request is refused by the Origin check, trust without publish and there is nothing
listening. So the ORDERING is the contract these tests defend:

* :class:`TestUpOrdering` — the config is recorded only *after* publishing
  succeeded. A host that records "tailnet access on" while nothing is served is
  precisely the working-looking-switch-that-does-nothing this feature removes.
* :class:`TestRestartNotice` — the restart note is unconditional, including when
  the switch was already on, because the origin is resolved once at startup.
* :class:`TestDown` — withdrawing does not silently flip the config too.
"""

from __future__ import annotations

import argparse
import json

import pytest

from kiro_crew.dashboard.tailnet_serve import ServeResult, ServeState


def _args(action: str) -> argparse.Namespace:
    return argparse.Namespace(tailnet_action=action)


@pytest.fixture()
def cfg_file(tmp_path, monkeypatch):
    """An isolated data home, so no test touches a real config.

    Isolation is by ``KIROCREW_HOME`` rather than by patching ``config_path`` in
    each module that imports it. Same mechanism the sibling governance suite uses,
    and it is the one that composes: patching the symbol reaches only the modules
    you remembered, and the ones you did not keep resolving the real home — which
    then leaks into whatever test runs next in the same process.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    path = home / "config.json"
    path.write_text(json.dumps({"timezone": "UTC", "dashboard": {"url": "http://localhost:5476"}}))
    return path


def _stub_serve(monkeypatch, *, publish=None, unpublish=None, state=None, name="d.t.ts.net"):
    from kiro_crew.dashboard import tailnet, tailnet_serve

    monkeypatch.setattr(
        tailnet_serve, "publish", lambda *a, **k: publish or ServeResult(True, "ok", "published")
    )
    monkeypatch.setattr(
        tailnet_serve,
        "unpublish",
        lambda *a, **k: unpublish or ServeResult(True, "ok", "withdrawn"),
    )
    monkeypatch.setattr(
        tailnet_serve, "serve_state", lambda *a, **k: state or ServeState(True, True, "serving")
    )
    monkeypatch.setattr(tailnet, "self_dns_name", lambda: name)
    monkeypatch.setattr(tailnet, "is_governance_pinned_off", lambda *a, **k: False)


def _enabled(path) -> object:
    return json.loads(path.read_text()).get("dashboard", {}).get("tailscale", {}).get("enabled")


class TestUpOrdering:
    def test_publish_then_record(self, cfg_file, monkeypatch, capsys) -> None:
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch)
        assert _enabled(cfg_file) in (None, False)
        _tailnet(_args("up"))
        assert _enabled(cfg_file) is True
        assert "URL:        https://d.t.ts.net" in capsys.readouterr().out

    def test_a_failed_publish_records_nothing(self, cfg_file, monkeypatch) -> None:
        """The half-state this ordering exists to prevent.

        If the config were written first, a refused publish would leave a host
        claiming tailnet access is enabled with nothing serving it — and the
        operator's next clue would be a bare 403 from another device.
        """
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(
            monkeypatch, publish=ServeResult(False, "no_permission", "access denied")
        )
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("up"))
        assert exc.value.code == 1
        assert _enabled(cfg_file) in (None, False)

    def test_a_pinned_host_is_refused_and_records_nothing(self, cfg_file, monkeypatch) -> None:
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(
            monkeypatch,
            publish=ServeResult(False, "governance_pinned", "pinned off by policy"),
        )
        with pytest.raises(SystemExit):
            _tailnet(_args("up"))
        assert _enabled(cfg_file) in (None, False)

    def test_other_settings_survive_the_write(self, cfg_file, monkeypatch) -> None:
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch)
        _tailnet(_args("up"))
        assert json.loads(cfg_file.read_text())["timezone"] == "UTC"


class TestRestartNotice:
    def test_said_even_when_already_enabled(self, cfg_file, monkeypatch, capsys) -> None:
        """The origin set is built once, at startup.

        A gateway that booted before this command has an allowlist that does not
        contain the name, so "already on" is not "already working".
        """
        from kiro_crew.cli_commands import _tailnet

        cfg_file.write_text(
            json.dumps(
                {
                    "dashboard": {
                        "url": "http://localhost:5476",
                        "tailscale": {"enabled": True},
                    }
                }
            )
        )
        _stub_serve(monkeypatch)
        _tailnet(_args("up"))
        out = capsys.readouterr().out
        assert "Restart the gateway" in out
        assert "already on" in out

    def test_unresolvable_name_is_flagged_not_hidden(self, cfg_file, monkeypatch, capsys) -> None:
        """Published, but the gateway will trust nothing — say so.

        This is the boot-race case: serve is up, the daemon has no name for us yet,
        so a restart alone will not fix it. Printing only the restart note would
        send the operator in circles.
        """
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch, name=None)
        _tailnet(_args("up"))
        out = capsys.readouterr().out
        assert "No tailnet name is resolvable" in out


class TestEffectiveValueNotJustTheWrite:
    """`config.local.json` takes PRECEDENCE over the base file.

    So a successful write to config.json can change nothing at all. Printing
    "= true" there would be the same false promise the whole feature exists to
    remove, and the operator's next clue would be a bare 403 from their phone.
    """

    def test_an_overlay_that_still_disables_is_reported(self, cfg_file, monkeypatch, capsys):
        from kiro_crew.cli_commands import _tailnet

        (cfg_file.parent / "config.local.json").write_text(
            json.dumps({"dashboard": {"tailscale": {"enabled": False}}})
        )
        _stub_serve(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("up"))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "config.local.json" in err
        # The base write DID happen — the failure is about the effective value, so
        # the message must not imply the write was refused.
        assert json.loads(cfg_file.read_text())["dashboard"]["tailscale"]["enabled"] is True

    def test_no_overlay_reports_success(self, cfg_file, monkeypatch, capsys) -> None:
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch)
        _tailnet(_args("up"))
        assert "dashboard.tailscale.enabled = true" in capsys.readouterr().out


class TestPortResolution:
    """The port must be the one the gateway is actually bound to.

    A gateway started with `--port` (or `KIROCREW_PORT`) is not described by
    `dashboard.url`, so parsing the config would publish 443 in front of a port
    nothing is listening on — a publish that looks fine and 502s. This repo's own
    dev hosts run exactly that way.
    """

    def test_env_port_wins_over_the_config_url(self, cfg_file, monkeypatch) -> None:
        from kiro_crew.cli_commands import _tailnet
        from kiro_crew.dashboard import tailnet_serve

        monkeypatch.setenv("KIROCREW_PORT", "7788")
        seen: list[int] = []
        _stub_serve(monkeypatch)
        monkeypatch.setattr(
            tailnet_serve,
            "publish",
            lambda p, **k: seen.append(p) or ServeResult(True, "ok", "published"),
        )
        _tailnet(_args("up"))
        assert seen == [7788]

    def test_a_non_string_dashboard_url_does_not_crash(self, cfg_file, monkeypatch) -> None:
        """`dashboard.url` is user-editable JSON and may be any type.

        `urlparse` raises TypeError on a non-str — not a ValueError — so an
        unguarded parse crashed every action, including withdrawal. The guard lives
        in `resolve_client_port`, which is why this command goes through it.
        """
        from kiro_crew.cli_commands import _tailnet

        cfg_file.write_text(json.dumps({"dashboard": {"url": 123}}))
        _stub_serve(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("down"))
        assert exc.value.code == 0


class TestCorruptConfigIsNeverReplaced:
    """A malformed config.json must abort the write, not get replaced by defaults.

    `KiroCrewConfig.load()` swallows a bad file and returns DEFAULTS, and the writer
    persists a full serialisation — so without a guard, `tailnet up` on a host with
    a hand-edited (or half-written) config.json silently replaces every setting the
    user has and prints success. The repo's own `read_config_for_update` docstring
    calls this shape a data-loss bug; this pins that the guard is wired up here.
    """

    @pytest.mark.parametrize("raw", ["{not json", '["an", "array"]', '"a string"'])
    def test_the_file_survives(self, cfg_file, monkeypatch, raw: str) -> None:
        from kiro_crew.cli_commands import _tailnet

        cfg_file.write_text(raw)
        _stub_serve(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("up"))
        assert exc.value.code == 1
        assert cfg_file.read_text() == raw, "the operator's file must be untouched"

    def test_the_publish_that_succeeded_is_still_reported(self, cfg_file, monkeypatch, capsys):
        """Two things are true and both must be said.

        Serve IS published; the setting is NOT recorded. Reporting only the failure
        would leave the operator thinking nothing happened and re-running it.
        """
        from kiro_crew.cli_commands import _tailnet

        cfg_file.write_text("{not json")
        _stub_serve(monkeypatch)
        with pytest.raises(SystemExit):
            _tailnet(_args("up"))
        captured = capsys.readouterr()
        assert "Published" in captured.err
        assert "kirocrew config set dashboard.tailscale.enabled true" in captured.err


class TestTheWriteIsMinimalAndSingleRead:
    """`tailnet up` owns its writer instead of sharing `config set`'s.

    `config set` reads the file twice on the way to a write — validate, then
    `KiroCrewConfig.load()` to build the full serialisation it persists. Another
    writer truncating between those reads makes the second one observe defaults,
    which then get written over everything the user has. The property pinned here is
    the one that makes that unreachable: the write payload comes from a SINGLE
    `read_config_for_update`, and `load()` is not consulted to build it.

    Deliberately NOT asserted: that the file never gains default-filled sections.
    `KiroCrewConfig.load()` performs its own migration write-back, so defaults are
    materialised by the *first* `load()` in the command, independently of this
    writer. That is pre-existing product behaviour, not something this writer causes
    or can fix in scope — claiming otherwise in a test would be encoding a property
    the code does not have.
    """

    def test_the_write_payload_never_comes_from_load(self, cfg_file, monkeypatch) -> None:
        """The whole point of the private writer, asserted directly.

        If `load()` were consulted to build the payload, a torn write between the
        validating read and that call would serialise defaults over the user's
        config — the race this writer exists to avoid.
        """
        from kiro_crew.cli_commands import _record_tailnet_enabled
        from kiro_crew.config import KiroCrewConfig

        def _boom(*_a, **_k):
            raise AssertionError("load() must not build the write payload")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
        _record_tailnet_enabled()
        assert json.loads(cfg_file.read_text())["dashboard"]["tailscale"]["enabled"] is True

    def test_unrelated_settings_survive(self, cfg_file, monkeypatch) -> None:
        from kiro_crew.cli_commands import _tailnet

        cfg_file.write_text(
            json.dumps(
                {
                    "timezone": "UTC",
                    "slack": {"command": "kirocrew"},
                    "dashboard": {"url": "http://localhost:5476", "auto_open_browser": False},
                }
            )
        )
        _stub_serve(monkeypatch)
        _tailnet(_args("up"))
        after = json.loads(cfg_file.read_text())
        assert after["slack"]["command"] == "kirocrew"
        assert after["timezone"] == "UTC"
        assert after["dashboard"]["auto_open_browser"] is False
        assert after["dashboard"]["tailscale"]["enabled"] is True

    @pytest.mark.parametrize("bad", ['{"dashboard": 5}', '{"dashboard": {"tailscale": []}}'])
    def test_a_non_object_section_is_refused_not_coerced(self, cfg_file, bad: str) -> None:
        """Replacing it with `{}` would discard the operator's data silently.

        Exercised against the writer directly rather than through the command,
        because the command's first `load()` normalises a parseable-but-wrong-typed
        file before the writer ever sees it. The guard still matters — it is what
        holds when that write-back does not happen (a read-only data home, or a
        future `load()` that stops rewriting).
        """
        from kiro_crew.cli_commands import _record_tailnet_enabled
        from kiro_crew.config.loader import ConfigReadError

        cfg_file.write_text(bad)
        with pytest.raises(ConfigReadError):
            _record_tailnet_enabled()
        assert cfg_file.read_text() == bad

    def test_an_unwritable_config_reports_partial_success_not_a_traceback(
        self, cfg_file, monkeypatch, capsys
    ) -> None:
        """The write can fail for ordinary filesystem reasons, not just corruption.

        A read-only data home, a symlink pointing somewhere unwritable, a full disk —
        all `OSError`, none `ConfigReadError`. Letting that escape ends the command in
        a traceback **with the dashboard already published**, so the operator sees a
        crash and no statement of what took effect.
        """
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.cli_commands.write_config_atomically",
            lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("read-only file system")),
        )
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("up"))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Published" in err
        assert "read-only file system" in err
        assert "kirocrew config set dashboard.tailscale.enabled true" in err

    def test_the_key_path_is_real(self) -> None:
        """The writer sets a literal path, so a rename must fail here, not silently.

        Without this, renaming the field would leave `up` writing a key nothing
        reads — and the effective-value check would then blame a config.local.json
        overlay that does not exist.
        """
        from kiro_crew.config import KiroCrewConfig

        assert isinstance(KiroCrewConfig().dashboard.tailscale.enabled, bool)


class TestConcurrentWriteIsNotClobbered:
    """A read-modify-write can lose another writer's update.

    A dashboard config save landing between the read and the write would be replaced
    by our older snapshot. The fingerprint re-check turns that into a re-read, so the
    other writer's content survives and our one key is applied on top.

    What this does NOT claim: closing the window. A writer can still land between the
    final stat and the rename. Every config read-modify-write in this repo has that
    gap and there is no shared compare-and-swap to use, so this narrows the window
    for this caller and the class is tracked separately.
    """

    def test_a_concurrent_save_survives(self, cfg_file, monkeypatch) -> None:
        from kiro_crew.cli_commands import _record_tailnet_enabled

        # Simulate another writer landing after our read: the first fingerprint
        # comparison sees a change, so the update re-reads and re-applies.
        original = json.loads(cfg_file.read_text())
        state = {"fired": False}
        real_read = __import__("kiro_crew.cli_commands", fromlist=["x"]).read_config_for_update

        def racing_read(path):
            data = real_read(path)
            if not state["fired"]:
                state["fired"] = True
                merged = dict(original)
                merged["slack"] = {"command": "written-by-someone-else"}
                cfg_file.write_text(json.dumps(merged))
            return data

        monkeypatch.setattr("kiro_crew.cli_commands.read_config_for_update", racing_read)
        _record_tailnet_enabled()
        after = json.loads(cfg_file.read_text())
        assert after["slack"] == {"command": "written-by-someone-else"}, "their write was lost"
        assert after["dashboard"]["tailscale"]["enabled"] is True, "ours was not applied"

    def test_a_continuously_changing_file_aborts_instead_of_looping(
        self, cfg_file, monkeypatch
    ) -> None:
        """One retry, then a clear failure — looping would be worse."""
        from kiro_crew.cli_commands import _record_tailnet_enabled
        from kiro_crew.config.loader import ConfigReadError

        real_read = __import__("kiro_crew.cli_commands", fromlist=["x"]).read_config_for_update

        def always_racing(path):
            data = real_read(path)
            payload = json.loads(cfg_file.read_text())
            payload["nonce"] = payload.get("nonce", 0) + 1
            cfg_file.write_text(json.dumps(payload))
            return data

        monkeypatch.setattr("kiro_crew.cli_commands.read_config_for_update", always_racing)
        with pytest.raises(ConfigReadError, match="kept changing"):
            _record_tailnet_enabled()

    def test_the_quiet_path_writes_once(self, cfg_file, monkeypatch) -> None:
        """No collision → no retry, and the write still lands."""
        from kiro_crew.cli_commands import _record_tailnet_enabled

        writes: list[object] = []
        real_write = __import__("kiro_crew.cli_commands", fromlist=["x"]).write_config_atomically

        def counting_write(path, data):
            writes.append(path)
            return real_write(path, data)

        monkeypatch.setattr("kiro_crew.cli_commands.write_config_atomically", counting_write)
        _record_tailnet_enabled()
        assert len(writes) == 1
        assert json.loads(cfg_file.read_text())["dashboard"]["tailscale"]["enabled"] is True


class TestDown:
    def test_withdraw_leaves_the_config_alone(self, cfg_file, monkeypatch, capsys) -> None:
        from kiro_crew.cli_commands import _tailnet

        cfg_file.write_text(
            json.dumps(
                {
                    "dashboard": {
                        "url": "http://localhost:5476",
                        "tailscale": {"enabled": True},
                    }
                }
            )
        )
        _stub_serve(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("down"))
        assert exc.value.code == 0
        assert _enabled(cfg_file) is True
        assert "unchanged" in capsys.readouterr().out

    def test_a_failed_withdraw_exits_nonzero(self, cfg_file, monkeypatch) -> None:
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch, unpublish=ServeResult(False, "no_permission", "denied"))
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("down"))
        assert exc.value.code == 1


class TestStatus:
    def test_reports_all_three_axes(self, cfg_file, monkeypatch, capsys) -> None:
        """Trust, name and published are independent, so all three are shown.

        Any one of them being wrong produces the same symptom from the user's
        chair (the dashboard does not open), which is why a single "on/off" line
        would not be diagnostic.
        """
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch)
        _tailnet(_args("status"))
        out = capsys.readouterr().out
        assert "Trust:" in out
        assert "Name:" in out
        assert "Published:" in out
        assert "URL:        https://d.t.ts.net" in out

    def test_unknown_published_state_is_not_shown_as_no(self, cfg_file, monkeypatch, capsys):
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch, state=ServeState(None, None, "could not tell"))
        _tailnet(_args("status"))
        assert "Published:  unknown" in capsys.readouterr().out

    def test_a_pin_is_named(self, cfg_file, monkeypatch, capsys) -> None:
        from kiro_crew.cli_commands import _tailnet
        from kiro_crew.dashboard import tailnet

        _stub_serve(monkeypatch)
        monkeypatch.setattr(tailnet, "is_governance_pinned_off", lambda *a, **k: True)
        _tailnet(_args("status"))
        assert "PINNED OFF" in capsys.readouterr().out

    def test_unknown_action_exits_nonzero(self, cfg_file, monkeypatch) -> None:
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("sideways"))
        assert exc.value.code == 1
