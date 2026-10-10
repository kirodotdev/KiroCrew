"""``agent.env``: operator environment variables for everything the agent runs.

The contract under test:

* the mapping is empty by default, so nothing changes until an operator sets it;
* a name outside the POSIX identifier grammar, a name Kiro Crew, the sandbox or a
  runtime loader owns, a credential-shaped name, and a non-string or NUL-bearing
  value are dropped with a warning that names the key and never the value;
* a name the agent environment scrub strips is dropped at spawn, with a warning,
  instead of vanishing without one;
* the shared launch tail lays the map over the inherited gateway environment for
  every backend, on both drivers, so it wins over the gateway's own value -- and
  everything Kiro Crew sets for the session afterwards, and the scrub, still win
  over it.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import acp_launch_capture as capture_mod
import pytest

from kiro_crew.acp import agent_env as agent_env_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.agent_env import agent_env_overlay
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_ACP_RUNTIME, ACP_BACKENDS_KNOWN
from kiro_crew.config import sections as sections_mod
from kiro_crew.config.sections import AgentConfig, agent_env_refusal, coerce_agent_env

_PROXY = {"HTTPS_PROXY": "http://proxy.example:3128", "NO_PROXY": "localhost,127.0.0.1"}


class TestTheConfigKey:
    def test_default_is_empty(self) -> None:
        assert AgentConfig().env == {}

    def test_the_issue_examples_are_accepted_as_written(self) -> None:
        wanted = {
            "HTTP_PROXY": "http://proxy.example:3128",
            "HTTPS_PROXY": "http://proxy.example:3128",
            "NO_PROXY": "localhost,127.0.0.1,.corp.example",
            "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/corp-root.pem",
            "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/corp-root.pem",
            "SSL_CERT_FILE": "/etc/ssl/certs/corp-root.pem",
            "AWS_CA_BUNDLE": "/etc/ssl/certs/corp-root.pem",
            "AWS_PROFILE": "dev",
            "AWS_REGION": "us-west-2",
            "GIT_SSH_COMMAND": "ssh -o StrictHostKeyChecking=accept-new",
            "GIT_CONFIG_GLOBAL": "/home/me/.gitconfig-work",
            "GIT_TERMINAL_PROMPT": "0",
            "JAVA_HOME": "/opt/jdk",
            "NPM_CONFIG_REGISTRY": "https://registry.example/npm/",
            "PIP_INDEX_URL": "https://registry.example/pypi/simple",
            "EMPTY_IS_A_VALUE": "",
        }
        assert AgentConfig(env=dict(wanted)).env == wanted

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "1ABC",
            "HAS SPACE",
            "HAS-DASH",
            "TRAILING_NEWLINE\n",
            "PATH",
            "path",
            "HOME",
            "TMPDIR",
            "BASH_ENV",
            "NODE_OPTIONS",
            "JAVA_TOOL_OPTIONS",
            "_JAVA_OPTIONS",
            "__PYVENV_LAUNCHER__",
            "LD_PRELOAD",
            "ld_library_path",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONSTARTUP",
            "KIROCREW_SESSION_KEY",
            "KIRO_API_KEY",
            "DSH_HOME",
            "DBUS_SESSION_BUS_ADDRESS",
            "SYSTEMD_LOG_LEVEL",
            "GCONV_PATH",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "GITHUB_TOKEN",
            "NPM_PASSWORD",
            "MY_PRIVATE_THING",
            "api_key",
        ],
    )
    def test_a_refused_name_is_dropped(self, name: str) -> None:
        assert agent_env_refusal(name) is not None
        assert coerce_agent_env({name: "v", "AWS_PROFILE": "dev"}) == {"AWS_PROFILE": "dev"}

    def test_every_harness_home_override_is_refused(self) -> None:
        # The credential read gate anchors these from the gateway's environment,
        # so the agent child must not be handed a different value.
        from kiro_crew.agent_sdk.host_auth import home_override_env_vars

        overrides = home_override_env_vars()
        assert overrides
        for name in overrides:
            assert agent_env_refusal(name) is not None, name
            assert agent_env_refusal(name.lower()) is not None, name

    def test_the_deepseek_mapping_shares_the_name_grammar(self) -> None:
        from kiro_crew.acp.harness import deepseek
        from kiro_crew.config.sections import AGENT_ENV_NAME_GRAMMAR

        assert deepseek._DEEPSEEK_ENV_NAME_GRAMMAR is AGENT_ENV_NAME_GRAMMAR

    @pytest.mark.parametrize("value", [None, 3, ["a"], {"a": "b"}, "has\x00nul"])
    def test_a_bad_value_is_dropped(self, value: object) -> None:
        assert coerce_agent_env({"HTTPS_PROXY": value}) == {}

    def test_a_value_the_spawn_cannot_encode_is_dropped(self, monkeypatch) -> None:
        # A lone surrogate has no encoding in a POSIX environment; on Windows the
        # environment block is UTF-16 and holds it. Pin the strict encoder so the
        # drop is exercised on every host.
        def strict(value: str) -> bytes:
            return value.encode("utf-8")

        monkeypatch.setattr(sections_mod._os, "fsencode", strict)
        assert coerce_agent_env({"HTTPS_PROXY": "lone\ud800surrogate", "AWS_PROFILE": "dev"}) == {
            "AWS_PROFILE": "dev"
        }

    def test_a_non_mapping_is_empty(self) -> None:
        assert coerce_agent_env(["HTTPS_PROXY"]) == {}
        assert coerce_agent_env(None) == {}

    def test_the_warning_names_the_key_and_never_the_value(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.sections"):
            coerce_agent_env({"GITHUB_TOKEN": "ghp_do_not_log", "HTTPS_PROXY": "a\x00b"})
        text = caplog.text
        assert "GITHUB_TOKEN" in text and "HTTPS_PROXY" in text
        assert "ghp_do_not_log" not in text and "a\x00b" not in text

    def test_the_loader_reads_the_key(self) -> None:
        from kiro_crew.config.loader import _build_agent_config

        agent = _build_agent_config({"env": {**_PROXY, "PATH": "/evil"}})
        assert agent.env == _PROXY
        assert _build_agent_config({}).env == {}


class TestTheOverlay:
    def _configure(self, monkeypatch, mapping: dict[str, str]) -> None:
        loaded = SimpleNamespace(agent=SimpleNamespace(env=dict(mapping)))
        monkeypatch.setattr(
            "kiro_crew.config.KiroCrewConfig.load", staticmethod(lambda: loaded), raising=True
        )

    def test_the_configured_map_is_returned(self, monkeypatch) -> None:
        self._configure(monkeypatch, _PROXY)
        assert agent_env_overlay() == _PROXY

    def test_a_name_the_scrub_strips_is_dropped_with_a_warning(self, monkeypatch, caplog) -> None:
        self._configure(monkeypatch, {"GIT_ASKPASS": "/bin/askpass", **_PROXY})
        with caplog.at_level(logging.WARNING, logger=agent_env_mod.__name__):
            assert agent_env_overlay() == _PROXY
        assert "GIT_ASKPASS" in caplog.text and "/bin/askpass" not in caplog.text

    def test_an_unreadable_config_applies_nothing(self, monkeypatch) -> None:
        def _boom():
            raise OSError("unreadable")

        monkeypatch.setattr(
            "kiro_crew.config.KiroCrewConfig.load", staticmethod(_boom), raising=True
        )
        assert agent_env_overlay() == {}


def _overlay_patches(overlay: dict[str, str]) -> tuple:
    """Hand both drivers' launch tail *overlay* as the ``agent.env`` answer, and
    put the REAL agent environment scrub back (the golden capture stubs it), so the
    order of the overlay against the scrub is what is measured."""
    from unittest.mock import patch

    from kiro_crew.sandbox import scrub_agent_subprocess_env

    return (
        patch.object(client_mod, "agent_env_overlay", new=lambda: dict(overlay)),
        patch.object(runtime_mod, "agent_env_overlay", new=lambda: dict(overlay)),
        patch.object(client_mod, "scrub_agent_subprocess_env", new=scrub_agent_subprocess_env),
        patch.object(runtime_mod, "scrub_agent_subprocess_env", new=scrub_agent_subprocess_env),
    )


#: What the launch tail must do with each entry, whichever backend it starts.
#: ``HTTPS_PROXY`` overrides a gateway value; ``NO_PROXY`` is new; the two names
#: below reach the tail only if the config and overlay filters were bypassed, and
#: pin that the tail's own order still wins over them.
_OVERLAY = {
    **_PROXY,
    "KIROCREW_SPAWNED": "forged",
    "SLACK_BOT_TOKEN": "forged",
}


def _assert_overlay_landed(added: dict[str, str], golden_added: dict[str, str]) -> None:
    assert added.get("HTTPS_PROXY") == _PROXY["HTTPS_PROXY"]
    assert added.get("NO_PROXY") == _PROXY["NO_PROXY"]
    # Set by Crew after the overlay: the overlay cannot stand in for it.
    assert added.get("KIROCREW_SPAWNED") == golden_added.get("KIROCREW_SPAWNED")
    assert added.get("KIROCREW_SPAWNED") != "forged"
    # Removed by the scrub after the overlay: never reaches the child.
    assert "SLACK_BOT_TOKEN" not in added


@pytest.fixture
def gateway_proxy(monkeypatch):
    """A gateway environment that already carries a different HTTPS_PROXY."""
    monkeypatch.setitem(capture_mod._FIXED_PARENT_ENV, "HTTPS_PROXY", "http://gateway:1")


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN), ids=lambda b: b or "kiro")
def test_every_backend_launch_carries_agent_env(backend, tmp_path, gateway_proxy) -> None:
    """Every known backend's real launch lays ``agent.env`` over the gateway env."""
    golden = capture_mod.read_golden()[capture_mod.golden_key(backend)]
    answers = capture_mod.capture(backend, tmp_path, extra_patches=_overlay_patches(_OVERLAY))
    _assert_overlay_landed(answers["env_added"], golden["env_added"])


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_ACP_RUNTIME), ids=lambda b: b or "kiro")
def test_every_runtime_launch_carries_agent_env(backend, tmp_path, gateway_proxy) -> None:
    """The ``AcpRuntime`` driver goes through the same tail and gets the same map."""
    from test_acp_launch_goldens import _kiro_family_runtime_gate_patches

    answers = capture_mod._capture_runtime_served(
        backend,
        tmp_path,
        capture_mod.fixed_parent_env(),
        _overlay_patches(_OVERLAY) + _kiro_family_runtime_gate_patches(tmp_path),
    )
    assert answers["env_added"].get("HTTPS_PROXY") == _PROXY["HTTPS_PROXY"]
    assert answers["env_added"].get("NO_PROXY") == _PROXY["NO_PROXY"]
    assert answers["env_added"].get("KIROCREW_SPAWNED") != "forged"
    assert "SLACK_BOT_TOKEN" not in answers["env_added"]


class _StopReadback(Exception):
    """Raised from the env resolver: the read-back's environment is all we need."""


def _readback_calls(tmp_path):
    """Each env-sensitive tool-gate read-back, called on a client of its harness."""
    from kiro_crew.acp.harness import deepseek as deepseek_mod
    from kiro_crew.acp.harness import opencode as opencode_mod
    from kiro_crew.acp.harness import pi as pi_mod
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
    )

    def client(backend):
        return client_mod.AcpClient(
            work_dir=tmp_path, acp_backend=backend, extra_env={"NO_PROXY": "session-wins"}
        )

    argv = ["/opt/bin/harness"]
    return {
        "opencode": lambda: opencode_mod._verify_opencode_routing(
            client(ACP_BACKEND_OPENCODE), ACP_BACKEND_OPENCODE, argv, "{}"
        ),
        "pi": lambda: pi_mod._verify_pi_gate(client(ACP_BACKEND_PI), ACP_BACKEND_PI, argv, "/x"),
        "deepseek": lambda: deepseek_mod._verify_deepseek_gate(
            client(ACP_BACKEND_DEEPSEEK), ACP_BACKEND_DEEPSEEK, argv, "/x", "/m", "nonce"
        ),
    }


@pytest.mark.parametrize("harness", ["opencode", "pi", "deepseek"])
def test_a_tool_gate_read_back_sees_agent_env_like_the_spawn(harness, tmp_path, monkeypatch):
    """A read-back vouches for the session's config, and these harnesses find their
    config through the environment, so the read-back must carry ``agent.env`` in the
    launch tail's order: over the gateway env, under the per-session overlay."""
    seen: dict = {}

    def _resolve(env, *, kiro_api_key):
        seen.update(env)
        raise _StopReadback

    monkeypatch.setattr(client_mod, "_resolve_spawn_env", _resolve)
    monkeypatch.setattr(client_mod, "agent_env_overlay", lambda: dict(_PROXY))
    monkeypatch.setenv("HTTPS_PROXY", "http://gateway:1")
    with pytest.raises(_StopReadback):
        _readback_calls(tmp_path)[harness]()
    assert seen["HTTPS_PROXY"] == _PROXY["HTTPS_PROXY"]
    assert seen["NO_PROXY"] == "session-wins"


def test_one_spawn_reads_agent_env_once(tmp_path, monkeypatch) -> None:
    """The read-back and the launch tail of one spawn share one reading, so a
    config save between them cannot change what the child gets."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_OPENCODE

    answers = iter([dict(_PROXY), {"HTTPS_PROXY": "http://changed:9"}])
    monkeypatch.setattr(client_mod, "agent_env_overlay", lambda: next(answers))
    client = client_mod.AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
    first = client._agent_env_for_spawn()
    assert client._agent_env_for_spawn() is first
    assert first == _PROXY


@pytest.mark.parametrize("harness", ["opencode", "pi", "deepseek"])
def test_the_spawn_carries_the_values_its_read_back_checked(harness, tmp_path, gateway_proxy):
    """A config save between the read-back and the launch must not reach the child:
    the launch tail lays over the reading the read-back took, not a fresh one."""
    from unittest.mock import patch

    from kiro_crew.acp.harness import deepseek as deepseek_mod
    from kiro_crew.acp.harness import opencode as opencode_mod
    from kiro_crew.acp.harness import pi as pi_mod
    from kiro_crew.sandbox import scrub_agent_subprocess_env

    readings = iter([dict(_PROXY), {"HTTPS_PROXY": "http://changed:9"}])

    def read_back(session, *_args, **_kwargs):
        session._agent_env_for_spawn()
        return ("", "")

    read_backs = {
        "opencode": patch.object(opencode_mod, "_verify_opencode_routing", new=read_back),
        "pi": patch.object(pi_mod, "_verify_pi_gate", new=read_back),
        "deepseek": patch.object(deepseek_mod, "_verify_deepseek_gate", new=read_back),
    }
    answers = capture_mod.capture(
        harness,
        tmp_path,
        extra_patches=(
            patch.object(client_mod, "agent_env_overlay", new=lambda: next(readings)),
            patch.object(client_mod, "scrub_agent_subprocess_env", new=scrub_agent_subprocess_env),
            read_backs[harness],
        ),
    )
    assert answers["env_added"].get("HTTPS_PROXY") == _PROXY["HTTPS_PROXY"]


def test_a_lower_case_spelling_of_a_scrubbed_name_is_dropped(monkeypatch) -> None:
    loaded = SimpleNamespace(agent=SimpleNamespace(env={"git_askpass": "/bin/x", **_PROXY}))
    monkeypatch.setattr(
        "kiro_crew.config.KiroCrewConfig.load", staticmethod(lambda: loaded), raising=True
    )
    assert agent_env_overlay() == _PROXY


def test_on_windows_a_name_is_folded_to_the_inherited_spelling(monkeypatch) -> None:
    loaded = SimpleNamespace(agent=SimpleNamespace(env={"https_proxy": "http://p:1"}))
    monkeypatch.setattr(
        "kiro_crew.config.KiroCrewConfig.load", staticmethod(lambda: loaded), raising=True
    )
    monkeypatch.setattr(agent_env_mod.os, "name", "nt")
    assert agent_env_overlay() == {"HTTPS_PROXY": "http://p:1"}
