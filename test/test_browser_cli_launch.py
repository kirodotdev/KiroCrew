"""The launch config that selects the engine Kiro Crew actually installs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.browser_cli import launch as mod


def test_launch_config_path_is_under_the_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(home))

    assert mod.launch_config_path() == home / "playwright-cli-config.json"


def test_launch_config_path_is_independent_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent runs the CLI from wherever its turn happened to be.

    A cwd-derived config would apply to some turns and not others.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    first = tmp_path / "somewhere"
    second = tmp_path / "elsewhere"
    first.mkdir()
    second.mkdir()

    monkeypatch.chdir(first)
    from_first = mod.launch_config_path()
    monkeypatch.chdir(second)
    from_second = mod.launch_config_path()

    assert from_first == from_second
    assert first not in from_first.parents
    assert second not in from_second.parents


def test_config_names_the_engine_under_the_nested_browser_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The schema is nested. A flat top-level `browserName` parses and selects nothing.

    That is why this asserts the shape and not merely that the value appears
    somewhere in the file.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    path = mod.write_config()

    assert path is not None
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {"browser": {"browserName": "chromium"}}


def test_engine_is_the_one_the_capability_gate_requires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config must name the engine `install-browser` fetched and `browser_ok`
    gates on, or the product would install one browser and launch another --
    which is the whole defect this module exists to close."""
    from kiro_crew.browser_cli import install

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    assert mod.LAUNCH_ENGINE in install.BROWSER_ENGINES
    engine = mod.desired_config()["browser"]
    assert isinstance(engine, dict)
    assert engine["browserName"] == mod.LAUNCH_ENGINE


def test_config_does_not_touch_the_browser_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chromium's sandbox is a security boundary, so no generated default removes it.

    A host that cannot run it needs an operator decision, which is what deferring
    to an operator-set `PLAYWRIGHT_MCP_CONFIG` provides.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    body = json.dumps(mod.desired_config())

    assert "chromiumSandbox" not in body
    assert "no-sandbox" not in body


def test_cli_env_overrides_points_the_cli_at_the_generated_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(mod.CONFIG_ENV, raising=False)

    env = mod.cli_env_overrides()

    assert env == {"PLAYWRIGHT_MCP_CONFIG": str(mod.launch_config_path())}
    assert mod.launch_config_path().is_file()


def test_cli_env_override_value_is_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI resolves a relative config path against its own working directory."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(mod.CONFIG_ENV, raising=False)

    value = mod.cli_env_overrides()[mod.CONFIG_ENV]

    assert Path(value).is_absolute()


def test_an_operator_set_config_is_never_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator naming their own config chose an engine, an executablePath, or
    launch options for a host that cannot run the browser sandbox. Replacing it
    would both override that choice and remove the escape hatch these narrow
    defaults rely on."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(mod.CONFIG_ENV, "/operator/owned.json")

    assert mod.cli_env_overrides() == {}


def test_a_blank_operator_value_is_not_treated_as_a_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty or whitespace value selects no config, so it is not a preference
    to preserve -- honouring it would leave the CLI on its own default browser."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(mod.CONFIG_ENV, "   ")

    assert mod.cli_env_overrides() == {mod.CONFIG_ENV: str(mod.launch_config_path())}


def test_write_is_idempotent_and_converges_a_stale_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    first = mod.write_config()
    assert first is not None
    stamp = first.stat().st_mtime_ns
    assert mod.write_config() == first
    assert first.stat().st_mtime_ns == stamp, "an unchanged config is not rewritten"

    first.write_text('{"browser": {"browserName": "webkit"}}\n', encoding="utf-8")
    mod.write_config()

    assert json.loads(first.read_text(encoding="utf-8")) == mod.desired_config()


def test_an_unreadable_existing_config_is_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Undecodable bytes are a reason to write, not a reason to skip."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    path = mod.launch_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe not utf-8")

    assert mod.write_config() == path
    assert json.loads(path.read_text(encoding="utf-8")) == mod.desired_config()


def test_an_unwritable_config_yields_no_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing the CLI at a path that does not exist is worse than leaving it on
    its own default: it fails on the missing config instead of the missing
    browser, which is less diagnosable for the same broken outcome."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(mod.CONFIG_ENV, raising=False)
    monkeypatch.setattr(mod, "write_config", lambda: None)

    assert mod.cli_env_overrides() == {}


def test_the_launch_config_is_write_protected_from_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense-in-depth on both agent write paths, at parity with existing leaves.

    The config's schema accepts ``launchOptions.chromiumSandbox``, so an agent
    that rewrote it would turn the browser sandbox off for every later browse and
    the change would persist until the next gateway start. This does not create a
    capability the agent lacked -- it can point ``PLAYWRIGHT_MCP_CONFIG`` at a file
    of its own -- it removes the durable form of silently rewriting the config the
    product installed.

    Asserted on BOTH paths, because a leaf on only one is reachable through the
    other, and the two gates are separate matchers.
    """
    from kiro_crew import security

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    path = str(mod.launch_config_path())

    # 1. the file-edit gate
    assert security.is_sensitive_write_path(path) is True
    # 2. the shell gate, across the spellings it does cover
    for command in (
        "echo x > ~/.kiro/crew/playwright-cli-config.json",
        "echo x > $HOME/.kiro/crew/playwright-cli-config.json",
        "echo x > ~/.kirocrew/playwright-cli-config.json",  # legacy data home
        "tee ~/.kiro/crew/playwright-cli-config.json",
        "cp /tmp/evil.json ~/.kiro/crew/playwright-cli-config.json",
    ):
        assert security.is_sensitive_bash_command(command) is not None, command
    # Readable through Python: the CLI opens it on every invocation.
    assert security.is_sensitive_path(path) is False


def test_launch_config_shell_protection_matches_an_existing_protected_leaf() -> None:
    """The shell gate treats this leaf exactly as it treats a long-standing one.

    Parity is the honest assertion, and the durable one. The leaf is deliberately
    ANCHORED rather than bare-token: per the scope note on
    ``_BARE_TOKEN_PROTECTED_LEAVES``, a leaf earns anchor-independent matching only
    when the filename IS the grant, and here it is not -- the agent can point
    ``PLAYWRIGHT_MCP_CONFIG`` at a file of its own. So a ``cd``-relative write is
    the accepted residual, exactly as it is for the on-call schedule.

    Asserting parity is what protects the invariant: it fails if someone protects
    one leaf and not the other, and it does not pretend a gap is closed.
    """
    from kiro_crew import security

    ours = "playwright-cli-config.json"
    existing = "apps/ops-mission-control/data/rotation.yaml"
    for form in (
        "echo x > ~/.kiro/crew/{leaf}",
        "echo x > $HOME/.kiro/crew/{leaf}",
        "echo x > ~/.kirocrew/{leaf}",
        "tee ~/.kiro/crew/{leaf}",
        "cd ~/.kiro/crew && printf x > {leaf}",
    ):
        assert (
            security.is_sensitive_bash_command(form.format(leaf=ours)) is not None
        ) == (
            security.is_sensitive_bash_command(form.format(leaf=existing)) is not None
        ), form


def test_gateway_startup_merges_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The variable has to reach a command line Kiro Crew never constructs, so the
    wiring is what delivers the fix -- an unwired module changes nothing."""
    import inspect

    from kiro_crew.dashboard import server

    source = inspect.getsource(server)

    assert "browser_cli_launch.cli_env_overrides" in source


def test_gateway_startup_does_not_write_the_config_on_the_event_loop() -> None:
    """Computing the override writes a file, and the wiring site is `async def`.

    A bare call there blocks the loop during boot, so the dispatch must stay
    off-thread -- asserted on the source because the cost is invisible in a
    functional test.
    """
    import inspect

    from kiro_crew.dashboard import server

    source = inspect.getsource(server)

    assert "asyncio.to_thread(browser_cli_launch.cli_env_overrides)" in source
    assert "os.environ.update(browser_cli_launch.cli_env_overrides())" not in source
