"""Reclaiming the browser a dead agent process left open."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kiro_crew.browser_cli import reap as mod


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _registry(entries: dict[str, int]) -> None:
    path = mod.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


class _FakeCli:
    """Records the CLI invocations a sweep makes, answering `list --json`."""

    def __init__(self, browsers: list[dict[str, object]], fail_on: str = "") -> None:
        self._browsers = browsers
        self._fail_on = fail_on
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):  # noqa: ANN001, ANN204 - subprocess.run shim
        self.calls.append(list(argv))
        if self._fail_on and self._fail_on in argv:
            raise subprocess.SubprocessError("boom")
        if "list" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"browsers": self._browsers}), ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def verbs(self) -> list[str]:
        return [c[-1] for c in self.calls if c[-1] in ("close", "detach")]


def _patch_cli(monkeypatch: pytest.MonkeyPatch, fake: _FakeCli) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: "/fake/playwright-cli")
    monkeypatch.setattr(mod, "cli_env", lambda: {})
    monkeypatch.setattr(mod.subprocess, "run", fake)


def _owner_alive(monkeypatch: pytest.MonkeyPatch, alive: set[int]) -> None:
    monkeypatch.setattr(mod.platform_compat, "pgroup_exists", lambda pid: pid in alive)


# ── recording ──


def test_a_generated_name_is_recorded_with_its_owner() -> None:
    mod.record_session("kc-1234abcd", 4242)

    assert json.loads(mod.registry_path().read_text(encoding="utf-8")) == {"kc-1234abcd": 4242}


def test_an_operator_named_session_is_never_claimed() -> None:
    """An operator's ``PLAYWRIGHT_CLI_SESSION`` names THEIR browser.

    Recording it would make this module close a browser it does not own.
    """
    mod.record_session("chrome", 4242)

    assert not mod.registry_path().exists()


def test_a_bogus_pid_is_not_recorded() -> None:
    mod.record_session("kc-1234abcd", 0)

    assert not mod.registry_path().exists()


# ── sweeping ──


def test_a_live_owners_browser_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry({"kc-live": 111})
    _owner_alive(monkeypatch, {111})
    fake = _FakeCli([{"name": "kc-live", "attached": False}])
    _patch_cli(monkeypatch, fake)

    assert mod.sweep_dead_sessions() == 0
    assert fake.calls == []  # not even a list: liveness is decided first


def test_a_dead_owners_launched_browser_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry({"kc-dead": 222})
    _owner_alive(monkeypatch, set())
    fake = _FakeCli([{"name": "kc-dead", "attached": False}])
    _patch_cli(monkeypatch, fake)

    assert mod.sweep_dead_sessions() == 1
    assert fake.verbs == ["close"]
    assert json.loads(mod.registry_path().read_text(encoding="utf-8")) == {}


def test_an_attached_browser_is_detached_not_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``close`` on an attached session closes the operator's own windows."""
    _registry({"kc-attached": 333})
    _owner_alive(monkeypatch, set())
    fake = _FakeCli([{"name": "kc-attached", "attached": True}])
    _patch_cli(monkeypatch, fake)

    assert mod.sweep_dead_sessions() == 1
    assert fake.verbs == ["detach"]


def test_a_session_the_cli_no_longer_knows_costs_no_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry({"kc-gone": 444})
    _owner_alive(monkeypatch, set())
    fake = _FakeCli([])
    _patch_cli(monkeypatch, fake)

    assert mod.sweep_dead_sessions() == 0
    assert fake.verbs == []
    assert json.loads(mod.registry_path().read_text(encoding="utf-8")) == {}


def test_a_failed_release_keeps_the_claim_for_the_next_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser that could not be released must not be forgotten."""
    _registry({"kc-stuck": 555})
    _owner_alive(monkeypatch, set())
    fake = _FakeCli([{"name": "kc-stuck", "attached": False}], fail_on="close")
    _patch_cli(monkeypatch, fake)

    assert mod.sweep_dead_sessions() == 0
    assert json.loads(mod.registry_path().read_text(encoding="utf-8")) == {"kc-stuck": 555}


def test_a_missing_cli_keeps_every_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropping the entries would lose the record of what to release later."""
    _registry({"kc-dead": 666})
    _owner_alive(monkeypatch, set())
    monkeypatch.setattr(mod, "cli_path", lambda: None)

    assert mod.sweep_dead_sessions() == 0
    assert json.loads(mod.registry_path().read_text(encoding="utf-8")) == {"kc-dead": 666}


def test_an_empty_registry_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []
    monkeypatch.setattr(mod, "cli_path", lambda: called.append("cli") or "/fake/cli")

    assert mod.sweep_dead_sessions() == 0
    assert called == []


def test_a_corrupt_registry_is_not_fatal() -> None:
    path = mod.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert mod.sweep_dead_sessions() == 0


# ── wiring ──


def test_both_spawn_paths_record_their_browser_owner() -> None:
    """An unrecorded browser is never swept, so the anchor must be wired."""
    import inspect

    from kiro_crew.acp import client, runtime

    for module in (client, runtime):
        source = inspect.getsource(module)
        assert "record_browser_session, self._browser_session, self._pid" in source


def test_the_sweep_runs_on_the_existing_maintenance_cadence() -> None:
    """Liveness-keyed reclamation needs a sweep; an unwired one never runs.

    It shares the scratch sweep's hourly task deliberately: same doctrine, same
    "no file-count-scaled work on the gateway boot path" reasoning.
    """
    import inspect

    from kiro_crew.slack import gateway

    source = inspect.getsource(gateway)

    assert "browser_cli_reap.sweep_dead_sessions" in source
    assert "asyncio.to_thread(browser_cli_reap.sweep_dead_sessions)" in source
