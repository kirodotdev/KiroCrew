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

#: Tell ``_stub_serve`` to leave ``_marker_port`` alone (the test patched it itself).
KEEP = object()


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
    # ``dashboard.tailscale.enabled`` is a PRECONDITION of `up`, not something it sets:
    # the command checks the trust flag and refuses rather than writing config, so the
    # ordinary success path starts from a host where the operator already enabled it.
    path.write_text(
        json.dumps(
            {
                "timezone": "UTC",
                "dashboard": {
                    "url": "http://localhost:5476",
                    "tailscale": {"enabled": True},
                },
            }
        )
    )
    return path


def _stub_serve(
    monkeypatch, *, publish=None, unpublish=None, state=None, name="d.t.ts.net", marker=5476
):
    """Stub the serve backend, and by default a *running gateway*.

    The run-marker stub is part of the default arrangement because ``up`` now refuses
    to publish a port nothing is verified to be listening on — so "a gateway is up" is
    a precondition of the ordinary success path, not an extra. Tests that exercise port
    resolution itself pass ``marker=KEEP`` and install their own.
    """
    from kiro_crew import cli_commands
    from kiro_crew.dashboard import tailnet, tailnet_serve

    if marker is not KEEP:
        monkeypatch.setattr(cli_commands, "_marker_port", lambda: marker)

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
    """`up` checks the trust flag, then publishes — and never writes config.

    The ordering matters in the opposite direction from the earlier design. When the
    write came after publishing, a failed write left a published-but-untrusted
    dashboard: reachable on the tailnet and answering 403. Now the flag is a
    precondition, so a host that would 403 is refused before anything is mutated.
    """

    def test_publish_happens_and_the_url_is_printed(self, cfg_file, monkeypatch, capsys):
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch)
        _tailnet(_args("up"))
        assert "URL:        https://d.t.ts.net" in capsys.readouterr().out

    def test_a_failed_publish_exits_nonzero_and_leaves_the_flag_alone(
        self, cfg_file, monkeypatch
    ) -> None:
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch, publish=ServeResult(False, "no_permission", "access denied"))
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("up"))
        assert exc.value.code == 1
        assert _enabled(cfg_file) is True, "the operator's flag must be left as they set it"

    def test_a_pinned_host_is_refused(self, cfg_file, monkeypatch) -> None:
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(
            monkeypatch,
            publish=ServeResult(False, "governance_pinned", "pinned off by policy"),
        )
        with pytest.raises(SystemExit):
            _tailnet(_args("up"))
        assert _enabled(cfg_file) is True

    def test_other_settings_survive(self, cfg_file, monkeypatch) -> None:
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch)
        _tailnet(_args("up"))
        assert json.loads(cfg_file.read_text())["timezone"] == "UTC"


class TestRestartNotice:
    def test_said_on_every_successful_publish(self, cfg_file, monkeypatch, capsys) -> None:
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


class TestTheEffectiveValueIsWhatGates:
    """An overlay that disables the flag must stop `up`, not be discovered after it.

    ``config.local.json`` takes PRECEDENCE over the base file, so a host whose overlay
    sets this false would otherwise get a published dashboard and a cheerful message
    while the gateway still refuses the origin — the operator's next clue being a bare
    403 from their phone.
    """

    def test_an_overlay_that_disables_refuses_and_names_the_overlay(
        self, cfg_file, monkeypatch, capsys
    ) -> None:
        from kiro_crew.cli_commands import _tailnet
        from kiro_crew.dashboard import tailnet_serve

        (cfg_file.parent / "config.local.json").write_text(
            json.dumps({"dashboard": {"tailscale": {"enabled": False}}})
        )
        published: list[int] = []
        _stub_serve(monkeypatch)
        monkeypatch.setattr(
            tailnet_serve,
            "publish",
            lambda p, **k: published.append(p) or ServeResult(True, "ok", "published"),
        )
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("up"))
        assert exc.value.code == 1
        assert published == []
        assert "config.local.json" in capsys.readouterr().err

    def test_an_enabled_host_publishes(self, cfg_file, monkeypatch, capsys) -> None:
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch)
        _tailnet(_args("up"))
        assert "published" in capsys.readouterr().out


class TestPortResolution:
    """The port must be the one the gateway is actually bound to.

    A gateway started with `--port` (or `KIROCREW_PORT`) is not described by
    `dashboard.url`, so parsing the config would publish 443 in front of a port
    nothing is listening on — a publish that looks fine and 502s. This repo's own
    dev hosts run exactly that way.

    Worse than a 502: if the configured port was occupied and the gateway moved,
    the configured port now belongs to some *other* local service, and publishing in
    front of it would expose that service on the tailnet. So evidence (a verified run
    marker) outranks intent (`dashboard.url`).
    """

    def test_an_explicit_port_outranks_everything(self, cfg_file, monkeypatch) -> None:
        """The escape hatch for when discovery cannot decide.

        If the run marker is unreadable, or several gateways are up (where it
        deliberately refuses), the fallback is the configured port — which, if the
        gateway moved because that port was taken, now belongs to some OTHER local
        service that publishing would expose. Naming the port must beat every
        heuristic, including the marker and the env var.
        """
        from kiro_crew import cli_commands
        from kiro_crew.cli_commands import _tailnet
        from kiro_crew.dashboard import tailnet_serve

        monkeypatch.setenv("KIROCREW_PORT", "9001")
        monkeypatch.setattr(cli_commands, "_marker_port", lambda: 7788)
        seen: list[int] = []
        _stub_serve(monkeypatch, marker=KEEP)
        monkeypatch.setattr(
            tailnet_serve,
            "publish",
            lambda p, **k: seen.append(p) or ServeResult(True, "ok", "published"),
        )
        _tailnet(argparse.Namespace(tailnet_action="up", port=6000))
        assert seen == [6000]

    def test_the_run_marker_outranks_the_configured_url(self, cfg_file, monkeypatch) -> None:
        from kiro_crew import cli_commands
        from kiro_crew.cli_commands import _tailnet
        from kiro_crew.dashboard import tailnet_serve

        # config says 5476; a verified gateway is actually listening on 7788.
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        monkeypatch.setattr(cli_commands, "_marker_port", lambda: 7788)
        seen: list[int] = []
        _stub_serve(monkeypatch, marker=KEEP)
        monkeypatch.setattr(
            tailnet_serve,
            "publish",
            lambda p, **k: seen.append(p) or ServeResult(True, "ok", "published"),
        )
        _tailnet(_args("up"))
        assert seen == [7788], "published in front of the configured port, not the live one"

    def test_an_explicit_env_port_outranks_the_marker(self, cfg_file, monkeypatch) -> None:
        """An operator naming the port directly wins over discovery."""
        from kiro_crew import cli_commands
        from kiro_crew.cli_commands import _tailnet
        from kiro_crew.dashboard import tailnet_serve

        monkeypatch.setenv("KIROCREW_PORT", "9001")
        monkeypatch.setattr(cli_commands, "_marker_port", lambda: 7788)
        seen: list[int] = []
        _stub_serve(monkeypatch, marker=KEEP)
        monkeypatch.setattr(
            tailnet_serve,
            "publish",
            lambda p, **k: seen.append(p) or ServeResult(True, "ok", "published"),
        )
        _tailnet(_args("up"))
        assert seen == [9001]

    def test_marker_discovery_failure_refuses_instead_of_publishing_a_guess(
        self, cfg_file, monkeypatch, capsys
    ) -> None:
        """Discovery failing must not crash — and must not publish a guess either.

        An earlier revision fell back to the configured ``dashboard.url`` port here.
        That is the hazard, not the safe default: `tailscale serve` publishes whatever
        answers on the number it is given, so if the gateway is down or moved off its
        configured port, the fallback exposes an unrelated local service to every
        device on the tailnet. Refusing is recoverable; that exposure is not.
        """
        from kiro_crew import cli_commands
        from kiro_crew.cli_commands import _tailnet
        from kiro_crew.dashboard import tailnet_serve

        monkeypatch.delenv("KIROCREW_PORT", raising=False)

        def _boom():
            raise RuntimeError("marker directory unreadable")

        monkeypatch.setattr(cli_commands, "_marker_port", _boom)
        seen: list[int] = []
        _stub_serve(monkeypatch, marker=KEEP)
        monkeypatch.setattr(
            tailnet_serve,
            "publish",
            lambda p, **k: seen.append(p) or ServeResult(True, "ok", "published"),
        )
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("up"))
        assert exc.value.code == 1
        assert seen == [], "published a port nothing was verified to be listening on"
        err = capsys.readouterr().err
        assert "refusing to publish" in err
        assert "--port" in err, "must tell the operator how to proceed"


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

    def test_nothing_is_published_when_the_config_cannot_be_read(
        self, cfg_file, monkeypatch, capsys
    ):
        """The refusal happens BEFORE publishing, so there is no partial state.

        This is stronger than the behaviour it replaced. Previously `up` published
        first and only then discovered it could not record the setting, leaving the
        dashboard exposed with a config that said otherwise. The pre-load check makes
        the failure atomic: nothing published, nothing written, and a message naming
        the file.
        """
        from kiro_crew.cli_commands import _tailnet
        from kiro_crew.dashboard import tailnet_serve

        published: list[int] = []
        _stub_serve(monkeypatch)
        monkeypatch.setattr(
            tailnet_serve,
            "publish",
            lambda p, **k: published.append(p) or ServeResult(True, "ok", "published"),
        )
        cfg_file.write_text("{not json")
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("up"))
        assert exc.value.code == 1
        assert published == [], "must not expose the dashboard it cannot record"
        assert str(cfg_file) in capsys.readouterr().err


class TestACorruptConfigStopsEveryAction:
    """Validation runs before anything calls ``KiroCrewConfig.load()``.

    That ordering is the point: ``load()`` is itself destructive on a file it cannot
    parse — its migration write-back rewrites the file — so a section it chokes on is
    replaced by defaults merely because a read-only command was run. Validating first
    means a corrupt or wrongly-shaped config aborts the command instead of being
    quietly replaced.
    """

    @pytest.mark.parametrize("action", ["status", "down", "up"])
    def test_every_action_refuses_rather_than_rewriting(self, cfg_file, monkeypatch, action):
        """Even `status` must not rewrite the file just by reading it.

        `KiroCrewConfig.load()` migration-writes a parseable-but-wrongly-typed config,
        and both `status` and `down` need the dashboard port, which resolves through
        `load()` as well — so there is no version of them that reads without
        rewriting. Declining beats corrupting.
        """
        from kiro_crew.cli_commands import _tailnet

        bad = '{"dashboard": 5}'
        cfg_file.write_text(bad)
        _stub_serve(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args(action))
        assert exc.value.code == 1
        assert cfg_file.read_text() == bad, "the operator's file must be untouched"

    def test_withdrawal_stays_achievable_when_the_config_is_unusable(
        self, cfg_file, monkeypatch, capsys
    ):
        """Refusing `down` is only acceptable if withdrawal is still reachable."""
        from kiro_crew.cli_commands import _tailnet

        cfg_file.write_text('{"dashboard": 5}')
        _stub_serve(monkeypatch)
        with pytest.raises(SystemExit):
            _tailnet(_args("down"))
        err = capsys.readouterr().err
        assert "tailscale serve --https 443 --set-path=/ off" in err

    def test_the_key_path_is_real(self) -> None:
        """The writer sets a literal path, so a rename must fail here, not silently.

        Without this, renaming the field would leave `up` writing a key nothing
        reads — and the effective-value check would then blame a config.local.json
        overlay that does not exist.
        """
        from kiro_crew.config import KiroCrewConfig

        assert isinstance(KiroCrewConfig().dashboard.tailscale.enabled, bool)


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


class TestEverySectionIsGuardedNotJustOurs:
    """A read-only command must not be able to destroy an unrelated section.

    ``KiroCrewConfig.load()`` performs a migration write-back, so merely *loading* a
    file it cannot parse replaces that section with defaults. Guarding only the
    sections this command writes was not enough: the destructive write comes from
    ``load()``, which covers all of them, so ``{"slack": 5}`` plus a ``tailnet status``
    was sufficient to wipe the operator's Slack settings.
    """

    @pytest.mark.parametrize("section", ["slack", "memory", "telemetry", "instances"])
    @pytest.mark.parametrize("action", ["status", "up", "down"])
    def test_a_non_object_section_survives_every_action(
        self, cfg_file, monkeypatch, section, action, capsys
    ) -> None:
        from kiro_crew.cli_commands import _tailnet

        cfg_file.write_text(json.dumps({"timezone": "UTC", section: 5}))
        before = cfg_file.read_bytes()
        _stub_serve(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args(action))
        assert exc.value.code == 1
        assert cfg_file.read_bytes() == before, f"{action} rewrote a config it could not parse"
        assert section in capsys.readouterr().err

    def test_the_guarded_set_is_derived_from_the_model_not_a_hardcoded_list(self) -> None:
        """So a section added to the model later is covered without an edit here."""
        import dataclasses

        from kiro_crew.cli_commands import _container_valued_sections
        from kiro_crew.config.loader import KiroCrewConfig

        guarded = set(_container_valued_sections())
        expected = {
            f.name
            for f in dataclasses.fields(KiroCrewConfig)
            if dataclasses.is_dataclass(f.type) or getattr(f.type, "__origin__", None) is dict
        }
        missing = expected - guarded
        assert not missing, f"model sections left unguarded: {sorted(missing)}"


class TestUpNeverWritesConfig:
    """`up` reads the trust flag and refuses; it must never write the config file.

    This replaces four earlier tests that exercised a lock, a content digest and a
    dedicated busy error. All of that existed to make a read-modify-write of the shared
    config safe from a second process, which cannot be done from the caller side: the
    window between "compared" and "renamed" is only closable by a lock every writer
    takes (#2147). Removing the write removes the whole problem, so what needs locking
    in is the ABSENCE of the write.
    """

    @pytest.mark.parametrize("action", ["status", "up", "down"])
    def test_no_action_changes_a_single_operator_value(
        self, cfg_file, monkeypatch, action
    ) -> None:
        """Every value the operator set survives, and the trust flag is untouched.

        Deliberately NOT a byte-for-byte assertion, which would be a false claim: the
        first ``KiroCrewConfig.load()`` on a minimal config performs a one-time
        migration write-back that materialises every default key (98 bytes became 9187
        in a direct measurement), and that happens for `kirocrew status`, `config get`
        and every other command in this repo — it is not something this feature does or
        can stop. What this feature owes is narrower and checkable: it must not change
        any value the operator chose.
        """
        from kiro_crew.cli_commands import _tailnet

        _stub_serve(monkeypatch)
        before = json.loads(cfg_file.read_text())
        try:
            _tailnet(_args(action))
        except SystemExit:
            pass
        after = json.loads(cfg_file.read_text())
        assert after["timezone"] == before["timezone"]
        assert after["dashboard"]["url"] == before["dashboard"]["url"]
        assert (
            after["dashboard"]["tailscale"]["enabled"]
            == before["dashboard"]["tailscale"]["enabled"]
        ), f"{action} changed the trust flag"

    def test_no_write_helper_survives(self) -> None:
        """A future edit must not quietly reintroduce the write path."""
        from kiro_crew import cli_commands

        for gone in ("_record_tailnet_enabled", "_tailnet_config_lock", "TailnetConfigBusy"):
            assert not hasattr(cli_commands, gone), f"{gone} is back; the write returned"

    def test_a_disabled_flag_refuses_before_publishing(self, cfg_file, monkeypatch, capsys):
        """Refusal must come BEFORE the mutation, not after.

        Publishing first and writing after left a published-but-untrusted dashboard
        whenever the write failed — reachable on the tailnet and answering 403, the
        exact confusing state this command exists to remove.
        """
        from kiro_crew.cli_commands import _tailnet
        from kiro_crew.dashboard import tailnet_serve

        cfg_file.write_text(json.dumps({"dashboard": {"tailscale": {"enabled": False}}}))
        published: list[int] = []
        _stub_serve(monkeypatch)
        monkeypatch.setattr(
            tailnet_serve,
            "publish",
            lambda p, **k: published.append(p) or ServeResult(True, "ok", "published"),
        )
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args("up"))
        assert exc.value.code == 1
        assert published == [], "published a dashboard the gateway would refuse"
        err = capsys.readouterr().err
        assert "config set dashboard.tailscale.enabled true" in err


class TestListSectionsAreGuardedToo:
    """A scalar where the model expects an array is a crash, not just a lost merge.

    ``registries`` is a ``list[ExternalRegistryConfig]``, and the loader *iterates* it —
    so ``{"registries": 5}`` ends the command in an uncaught ``TypeError`` rather than
    a clean refusal. Guarding only object-valued sections missed this whole shape.
    """

    @pytest.mark.parametrize("action", ["status", "up", "down"])
    def test_a_scalar_array_section_is_refused_not_iterated(
        self, cfg_file, monkeypatch, action, capsys
    ) -> None:
        from kiro_crew.cli_commands import _tailnet

        cfg_file.write_text(json.dumps({"timezone": "UTC", "registries": 5}))
        before = cfg_file.read_bytes()
        _stub_serve(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args(action))
        assert exc.value.code == 1, "a TypeError would surface as a traceback, not exit 1"
        assert cfg_file.read_bytes() == before
        err = capsys.readouterr().err
        assert "registries" in err
        assert "not an array" in err

    def test_the_expected_shape_matches_the_model(self) -> None:
        import dataclasses

        from kiro_crew.cli_commands import _container_valued_sections
        from kiro_crew.config.loader import KiroCrewConfig

        shapes = _container_valued_sections()
        for field in dataclasses.fields(KiroCrewConfig):
            origin = getattr(field.type, "__origin__", None)
            if origin is list:
                assert shapes.get(field.name) is list, f"{field.name} must expect an array"
            elif origin is dict or dataclasses.is_dataclass(field.type):
                assert shapes.get(field.name) is dict, f"{field.name} must expect an object"


class TestTheOverlayIsGuardedToo:
    """`config.local.json` reaches the loader just as surely as the base file.

    ``load()`` merges the overlay over ``config.json``, so a wrongly-typed section
    there is loaded all the same — `config set --local registries 5` is enough — and a
    scalar where a list is expected gets iterated, ending the command in a traceback
    instead of a refusal. Guarding only the base file left the overlay as an open door
    to the exact failure the guard exists to prevent.
    """

    @pytest.mark.parametrize("action", ["status", "up", "down"])
    @pytest.mark.parametrize("bad", ['{"registries": 5}', '{"slack": 5}'])
    def test_a_malformed_overlay_is_refused_by_every_action(
        self, cfg_file, monkeypatch, action, bad, capsys
    ) -> None:
        from kiro_crew.cli_commands import _tailnet

        overlay = cfg_file.parent / "config.local.json"
        overlay.write_text(bad)
        before = overlay.read_bytes()
        _stub_serve(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _tailnet(_args(action))
        assert exc.value.code == 1, "a TypeError would surface as a traceback, not exit 1"
        assert overlay.read_bytes() == before

    def test_the_message_names_the_overlay_not_the_base_file(
        self, cfg_file, monkeypatch, capsys
    ) -> None:
        """Naming the wrong file sends the operator to edit a healthy one."""
        from kiro_crew.cli_commands import _tailnet

        overlay = cfg_file.parent / "config.local.json"
        overlay.write_text('{"registries": 5}')
        _stub_serve(monkeypatch)
        with pytest.raises(SystemExit):
            _tailnet(_args("status"))
        err = capsys.readouterr().err
        assert "config.local.json" in err
        assert "registries" in err

    def test_a_missing_overlay_is_not_an_error(self, cfg_file, monkeypatch) -> None:
        """The overlay is optional; absence must not be treated as malformed."""
        from kiro_crew.cli_commands import _tailnet

        assert not (cfg_file.parent / "config.local.json").exists()
        _stub_serve(monkeypatch)
        _tailnet(_args("status"))
