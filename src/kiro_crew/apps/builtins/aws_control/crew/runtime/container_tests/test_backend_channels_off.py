"""The container serves no messaging channel, and says so positively.

A crew in a container has nobody to reach on Slack or Telegram: the only caller it
serves is the customer's HTTP turn through the front process. A transport that comes
up hands that crew an outbound channel into an owner's workspace that nobody chose to
grant it.

Two mechanisms, because neither alone is complete. The config file turns every
transport off by name, which is the only thing that reaches ``imessage`` and
``whatsapp`` -- their registry descriptors carry no credential at all, so they start
on their config flag. The credential strip is what reaches ``slack``, whose config
section has no ``enabled`` key and which starts on its tokens.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from container.common import ConfigError
from container.supervisor import backend as backend_mod

from ._settings_helper import make_settings


def _written(tmp_path: Path) -> dict:
    settings = make_settings(tmp_path)
    path = backend_mod.write_backend_config(settings)
    assert path == settings.config_dir / "config.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_transport_is_off_in_the_written_config(tmp_path: Path) -> None:
    config = _written(tmp_path)
    on = [name for name in backend_mod.CHANNEL_SECTIONS if config.get(name, {}).get("enabled")]
    assert not on, f"these transports are not disabled in the config the backend boots with: {on}"


def test_the_transport_list_is_not_empty() -> None:
    """Non-vacuity: an empty tuple would make the assertions above pass silently."""
    assert len(backend_mod.CHANNEL_SECTIONS) >= 10


def test_the_sandbox_cannot_be_turned_off_by_an_existing_config(tmp_path: Path) -> None:
    """The refusal to run unsandboxed must not be defeatable by a config file.

    ``verify_sandbox`` refuses to start where the model subprocess cannot be
    sandboxed, and the container offers no unsandboxed posture. The gateway reads its
    sandbox mode and two fallback flags from this same file, so a file arriving with
    them relaxed would let the worker run with no backend while the supervisor's
    refusal reported nothing wrong.
    """
    settings = make_settings(tmp_path)
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    (settings.config_dir / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "sandbox": "off",
                    "sandbox_allow_no_isolation": True,
                    "sandbox_allow_unsandboxed_exec": True,
                    "default_agent": "kept",
                }
            }
        ),
        encoding="utf-8",
    )

    config = json.loads(backend_mod.write_backend_config(settings).read_text(encoding="utf-8"))

    assert config["agent"]["sandbox"] == "auto"
    assert config["agent"]["sandbox_allow_no_isolation"] is False
    assert config["agent"]["sandbox_allow_unsandboxed_exec"] is False
    assert config["agent"]["default_agent"] == "kept", "only the forced keys are overwritten"


def test_the_sandbox_settings_are_written_even_with_no_existing_config(tmp_path: Path) -> None:
    """Written, not left to a default.

    ``sandbox_allow_unsandboxed_exec`` resolves an undeclared value through a PLATFORM
    default, so what silence means is not a constant and the container states its
    answer rather than inheriting one.
    """
    config = _written(tmp_path)
    for key, value in backend_mod.FORCED_AGENT_SETTINGS.items():
        assert config["agent"][key] == value, key


def test_a_flag_only_transport_is_covered() -> None:
    """``imessage`` and ``whatsapp`` need no credential, so only the config reaches them.

    Named individually because they are the reason this file exists as well as the
    credential strip. Stripping secrets cannot disable a transport that never needed
    one.
    """
    assert "imessage" in backend_mod.CHANNEL_SECTIONS
    assert "whatsapp" in backend_mod.CHANNEL_SECTIONS


def test_a_config_that_enables_a_transport_loses(tmp_path: Path) -> None:
    """The container's posture must not be a default a shipped file can outvote."""
    settings = make_settings(tmp_path)
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    path = settings.config_dir / "config.json"
    path.write_text(
        json.dumps({"telegram": {"enabled": True, "bot_token": "t"}, "keep": {"me": 1}}),
        encoding="utf-8",
    )

    backend_mod.write_backend_config(settings)
    config = json.loads(path.read_text(encoding="utf-8"))

    assert config["telegram"]["enabled"] is False
    assert config["telegram"]["bot_token"] == "t", "only `enabled` is forced"
    assert config["keep"] == {"me": 1}, "unrelated config survives"


def test_a_non_dict_section_is_replaced_rather_than_merged(tmp_path: Path) -> None:
    """A section of the wrong shape cannot make the section unwritable.

    ``dict(current)`` on a string would raise, and a transport left unwritten because
    the file was malformed is the failure this container cannot have.
    """
    settings = make_settings(tmp_path)
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    (settings.config_dir / "config.json").write_text(
        json.dumps({"discord": "not a section"}), encoding="utf-8"
    )

    config = json.loads(backend_mod.write_backend_config(settings).read_text(encoding="utf-8"))
    assert config["discord"] == {"enabled": False}


def test_the_write_refuses_a_symlink_at_the_destination(tmp_path: Path) -> None:
    """A pre-planted link must not redirect this write outside the data home.

    Same exposure as the bundle install, and the same primitive answers it.
    """
    settings = make_settings(tmp_path)
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (settings.config_dir / "config.json").symlink_to(outside)

    with pytest.raises(ConfigError):
        backend_mod.write_backend_config(settings)
    assert outside.read_text(encoding="utf-8") == "{}", "the link target was written through"


def test_no_channel_credential_reaches_the_backend(tmp_path: Path) -> None:
    base = {name: "secret" for name in backend_mod.CHANNEL_CRED_ENV}
    base["KIRO_API_KEY"] = "k"

    env = backend_mod.build_backend_env(make_settings(tmp_path), base=base)

    present = sorted(name for name in backend_mod.CHANNEL_CRED_ENV if name in env)
    assert not present, f"these channel credentials reach the auto-approving worker: {present}"


def test_the_credential_list_names_only_variables_the_gateway_reads(tmp_path: Path) -> None:
    """Every name here must be one the gateway actually reads.

    A name nothing reads is worse than a missing one: it reads as coverage while
    stripping nothing, and it makes the list look longer than its reach. The
    authoritative set lives in the gateway's channel registry, and
    ``test/test_crew_container_config_isolation.py`` compares the two -- this only
    pins the shape those names have, which is what makes that comparison possible.
    """
    assert backend_mod.CHANNEL_CRED_ENV
    for name in backend_mod.CHANNEL_CRED_ENV:
        assert name == name.upper(), name
        assert not name.startswith("KIROCREW_"), (
            f"{name} is a Kiro Crew-namespaced spelling; the gateway reads the channel's "
            "own variable, so this name strips nothing"
        )


def test_the_config_is_written_before_the_backend_starts(tmp_path: Path, monkeypatch) -> None:
    """Order, not just presence: the gateway reads this file at boot.

    A config written after ``start_backend`` is a config the running backend already
    ignored, and a transport it started is connected by then.
    """
    from container.supervisor import __main__ as entry

    calls: list[str] = []
    settings = make_settings(tmp_path)

    class _Fake:
        def terminate(self, *a, **k):
            return None

        def kill(self, *a, **k):
            return None

    def _record(name: str):
        """A stub that records the call and returns nothing.

        Each stub is a ``def`` rather than ``lambda ...: calls.append(name)`` because
        ``list.append`` returns ``None``, so the lambda form types as a function
        returning a value it does not have. Spelling it ``... or Path()`` would satisfy
        the type checker by relying on that same ``None``, which is the reading a
        checker is right to refuse.
        """

        def stub(*args, **kwargs):
            calls.append(name)

        return stub

    def _record_returning(name: str, value):
        def stub(*args, **kwargs):
            calls.append(name)
            return value

        return stub

    monkeypatch.setattr(entry, "verify_layout", _record("layout"))
    monkeypatch.setattr(entry, "verify_sandbox", _record("sandbox"))
    monkeypatch.setattr(entry.bundle_mod, "install_bundle", _record("bundle"))
    monkeypatch.setattr(entry.backend_mod, "build_backend_env", lambda s: {"KIRO_API_KEY": "k"})
    monkeypatch.setattr(entry.backend_mod, "require_api_key", lambda env: None)
    monkeypatch.setattr(
        entry.backend_mod, "write_backend_config", _record_returning("config", Path())
    )
    monkeypatch.setattr(entry.backend_mod, "start_backend", _record_returning("backend", _Fake()))
    monkeypatch.setattr(entry.backend_mod, "wait_until_ready", lambda *a, **kw: None)
    monkeypatch.setattr(entry, "_start_front", _record_returning("front", _Fake()))
    monkeypatch.setattr(entry, "_teardown", lambda front, backend: None)

    entry.run(settings, wait_for_shutdown=lambda children: "signal")

    assert calls.index("config") < calls.index("backend"), calls
    assert calls.index("bundle") < calls.index("config"), calls
