"""``agent.env``: operator environment variables for everything the agent runs.

The contract under test:

* the mapping is empty by default, so nothing changes until an operator sets it;
* a name outside the POSIX identifier grammar, a name Kiro Crew, the sandbox or a
  runtime loader owns, a credential-shaped name, and a non-string or NUL-bearing
  value are dropped with a warning that names the key and never the value;
* a name the agent environment scrub strips is dropped at spawn, with a warning,
  instead of vanishing without one;
* a config write refuses every one of those entries up front, naming the key and
  the reason and writing nothing, unless the write leaves a mapping that already
  held it exactly as it found it;
* the shared launch tail lays the map over the inherited gateway environment for
  every backend, on both drivers, so it wins over the gateway's own value -- and
  everything Kiro Crew sets for the session afterwards, and the scrub, still win
  over it.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import acp_launch_capture as capture_mod
import pytest
from _hot_reload_helpers import change as _change
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.acp import agent_env as agent_env_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.agent_env import agent_env_overlay
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_ACP_RUNTIME, ACP_BACKENDS_KNOWN
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config import sections as sections_mod
from kiro_crew.config.sections import AgentConfig, agent_env_refusal, coerce_agent_env
from kiro_crew.kiro_prerequisite import identity_park_grace_remaining
from kiro_crew.session import SessionManager

_PROXY = {"HTTPS_PROXY": "http://proxy.example:3128", "NO_PROXY": "localhost,127.0.0.1"}

#: Names that make a shell, the loader, a runtime or a tool run code the command
#: did not name. None is an owned, home-override or credential-shaped name, so the
#: code-running layers are the only check that refuses each one.
_EXEC_NAMES = [
    # Named in review: bash options and prompts, zsh start-up, git.
    "SHELLOPTS",
    "BASHOPTS",
    "PS4",
    "ZDOTDIR",
    "GIT_SSH_COMMAND",
    "GIT_SSH",
    "GIT_EXTERNAL_DIFF",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_VALUE_0",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_ASKPASS",
    "GIT_PAGER",
    "GIT_EDITOR",
    "GIT_SEQUENCE_EDITOR",
    "GIT_PROXY_COMMAND",
    "GIT_EXEC_PATH",
    "GIT_TEMPLATE_DIR",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_A_FUTURE_RELEASE_NAME",
    # Shell start-up, options, prompts and word splitting.
    "PS0",
    "PS1",
    "PS2",
    "PS3",
    "IFS",
    "CDPATH",
    "FPATH",
    "FCEDIT",
    "BASH_LOADABLES_PATH",
    "BASH_XTRACEFD",
    "CONFIG_SHELL",
    # A helper program a tool runs from its environment.
    "EDITOR",
    "VISUAL",
    "PAGER",
    "MANPAGER",
    "AWS_PAGER",
    "GH_PAGER",
    "GH_EDITOR",
    "SUDO_ASKPASS",
    "SSH_ASKPASS",
    "SSH_ASKPASS_REQUIRE",
    "BROWSER",
    "LESS",
    "LESSOPEN",
    "LESSCLOSE",
    "CVS_RSH",
    "CVS_SERVER",
    "RSYNC_RSH",
    "RSYNC_CONNECT_PROG",
    "SVN_SSH",
    "HGMERGE",
    "HGRCPATH",
    "WGETRC",
    "AWS_CONFIG_FILE",
    "KUBECONFIG",
    "DOCKER_CONFIG",
    "CLAUDE_CODE_EXECUTABLE",
    "NPM_CONFIG_SCRIPT_SHELL",
    "NPM_CONFIG_NODE_OPTIONS",
    "NPM_CONFIG_USERCONFIG",
    "NPM_CONFIG_GLOBALCONFIG",
    "NPM_CONFIG_GIT",
    "NPM_CONFIG_ONLOAD_SCRIPT",
    "RUSTC_WRAPPER",
    "CARGO_BUILD_RUSTC_WRAPPER",
    "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUNNER",
    "CARGO_HOME",
    "GOENV",
    "GOFLAGS",
    "MAKEFLAGS",
    "CFLAGS",
    "RUSTFLAGS",
    # Interpreter and runtime hooks.
    "PERL5DB",
    "PERL5SHELL",
    "RUBYSHELL",
    "GEM_PATH",
    "GEM_HOME",
    "BUNDLE_GEMFILE",
    "LUA_INIT",
    "LUA_PATH",
    "LUA_CPATH",
    "NODE_REPL_EXTERNAL_MODULE",
    "CLASSPATH",
    "JAVA_OPTS",
    "MAVEN_OPTS",
    "GRADLE_OPTS",
    "ERL_FLAGS",
    "ERL_LIBS",
    "PHPRC",
    "PHP_INI_SCAN_DIR",
    "R_PROFILE",
    "R_PROFILE_USER",
    "R_ENVIRON",
    "R_ENVIRON_USER",
    "R_LIBS",
    "R_LIBS_USER",
    "R_LIBS_SITE",
    "DOTNET_STARTUP_HOOKS",
    "DOTNET_ADDITIONAL_DEPS",
    "COR_PROFILER",
    "COR_ENABLE_PROFILING",
    "CORECLR_PROFILER",
    "VIMINIT",
    "GVIMINIT",
    "EXINIT",
    "JULIA_LOAD_PATH",
    "TCLLIBPATH",
    # The loader, the C library and the libraries it loads plugins for.
    "OPENSSL_CONF",
    "OPENSSL_ENGINES",
    "OPENSSL_MODULES",
    "GETCONF_DIR",
    "NLSPATH",
    "GSS_MECH_CONFIG",
    "KRB5_CONFIG",
    "KRB5_KTNAME",
    "GTK_MODULES",
    "GIO_EXTRA_MODULES",
    "QT_PLUGIN_PATH",
    "LIBPATH",
    # The per-user base directories git and shells read config from.
    "XDG_CONFIG_DIRS",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    # A harness Kiro Crew launches, pointed at a config file whose MCP commands
    # it runs at start-up, and the variable Kiro Crew hands it its own config in.
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_CONFIG_CONTENT",
    "OPENCODE_A_FUTURE_RELEASE_NAME",
    # The location of a config file a tool reads a program from: an alias, a
    # credential helper, a plugin.
    "GH_CONFIG_DIR",
    "TF_CLI_CONFIG_FILE",
    "HELM_CONFIG_HOME",
    "AZURE_CONFIG_DIR",
    "CLOUDSDK_CONFIG",
    "BOTO_CONFIG",
]

#: Plain data whose name merely CONTAINS a credential word. A substring match would
#: refuse every one, and then the private-registry case the map exists for could
#: not set ``GOPRIVATE`` at all.
_NOT_CREDENTIAL_NAMES = [
    "GOPRIVATE",
    "TOKENIZERS_PARALLELISM",
    "KEYCLOAK_URL",
    "KEYBOARD_LAYOUT",
    "PASSWORDLESS_LOGIN",
    "SECRETARY_EMAIL",
    "goprivate",
]

#: Credential-shaped names: a name segment that is, or ends in, a credential word,
#: optionally plural or numbered, in any case.
_CREDENTIAL_NAMES = [
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "API_KEY",
    "api_key",
    "APIKEY",
    "API_KEY_2",
    "API_KEY2",
    "PRIVATEKEY",
    "DEPLOY_PRIVATE_KEY",
    "NPM_PASSWORD",
    "DB_PASSWD",
    "CLIENT_SECRET",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AZURE_CREDENTIAL_FILE",
    "SIGNING_KEYS",
    "AUTHTOKEN",
]


class TestTheConfigKey:
    def test_default_is_empty(self) -> None:
        assert AgentConfig().env == {}

    def test_the_issue_examples_are_accepted_as_written(self) -> None:
        # Every data name the issue lists. Its two git names that run or inject
        # code (GIT_SSH_COMMAND, GIT_CONFIG_GLOBAL) are refused below instead.
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
            "AWS_DEFAULT_REGION": "us-west-2",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSL_CAINFO": "/etc/ssl/certs/corp-root.pem",
            "JAVA_HOME": "/opt/jdk",
            "NPM_CONFIG_REGISTRY": "https://registry.example/npm/",
            "PIP_INDEX_URL": "https://registry.example/pypi/simple",
            "SSL_CERT_DIR": "/etc/ssl/certs",
            "CURL_CA_BUNDLE": "/etc/ssl/certs/corp-root.pem",
            "LANG": "C.UTF-8",
            "EMPTY_IS_A_VALUE": "",
        }
        assert AgentConfig(env=dict(wanted)).env == wanted

    def test_the_data_only_git_names_are_inside_a_refused_family(self) -> None:
        # The exemption is what keeps them: without it the GIT_ prefix refuses them.
        for name in sections_mod.AGENT_ENV_DATA_ONLY_NAMES:
            assert name.startswith(sections_mod.AGENT_ENV_DENIED_PREFIXES), name
            assert agent_env_refusal(name) is None, name
            assert agent_env_refusal(name.lower()) is None, name

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
            "DEPLOY_PRIVATE_KEY",
            "api_key",
        ],
    )
    def test_a_refused_name_is_dropped(self, name: str) -> None:
        assert agent_env_refusal(name) is not None
        assert coerce_agent_env({name: "v", "AWS_PROFILE": "dev"}) == {"AWS_PROFILE": "dev"}

    @pytest.mark.parametrize("name", _NOT_CREDENTIAL_NAMES)
    def test_a_name_that_only_contains_a_credential_word_is_accepted(self, name: str) -> None:
        assert agent_env_refusal(name) is None, name
        assert coerce_agent_env({name: "corp.example/*"}) == {name: "corp.example/*"}

    @pytest.mark.parametrize("name", _CREDENTIAL_NAMES)
    def test_a_credential_shaped_name_is_refused_as_one(self, name: str) -> None:
        reason = agent_env_refusal(name)
        assert reason is not None and "looks like a credential" in reason, name
        assert coerce_agent_env({name: "v"}) == {}

    def test_every_variable_the_opencode_harness_is_handed_is_refused(self) -> None:
        # Kiro Crew hands the opencode harness its permission routing through this
        # variable; the map must not be able to replace it, or point the harness at
        # a different config file through a sibling of it.
        from kiro_crew.acp.harness import opencode

        name = opencode._ENV_OPENCODE_CONFIG_CONTENT
        assert name.startswith(sections_mod.AGENT_ENV_DENIED_PREFIXES)
        assert agent_env_refusal(name) is not None

    @pytest.mark.parametrize("name", _EXEC_NAMES)
    def test_a_name_that_can_run_code_is_dropped(self, name: str) -> None:
        # Each of these makes a shell, the loader, a runtime or a tool the agent
        # runs execute code the command did not name, so set once here it would
        # ride along with every later command, approved ones included.
        assert agent_env_refusal(name) is not None, name
        assert agent_env_refusal(name.lower()) is not None, name
        assert coerce_agent_env({name: "v", "AWS_PROFILE": "dev"}) == {"AWS_PROFILE": "dev"}

    @pytest.mark.parametrize("prefix", sections_mod.AGENT_ENV_DENIED_PREFIXES)
    def test_every_later_name_in_a_refused_namespace_is_dropped(self, prefix: str) -> None:
        assert agent_env_refusal(f"{prefix}A_LATER_RELEASE_NAME") is not None
        assert agent_env_refusal(f"{prefix}a_later_release_name".lower()) is not None

    @pytest.mark.parametrize("suffix", sections_mod.AGENT_ENV_DENIED_SUFFIXES)
    def test_every_name_of_a_refused_shape_is_dropped(self, suffix: str) -> None:
        assert agent_env_refusal(f"SOME_TOOL{suffix}") is not None
        assert agent_env_refusal(f"some_tool{suffix}".lower()) is not None

    def test_every_name_the_command_floor_says_decides_what_runs_is_refused(self) -> None:
        # The command floor keeps its own list of assignments that decide WHICH
        # code runs. A name it distrusts for one command must not be settable for
        # every command, so its list is pinned as a subset here.
        from kiro_crew import name_grant

        for name in name_grant._EXEC_ENV_VARS:
            assert agent_env_refusal(name) is not None, name
        for prefix in name_grant._EXEC_ENV_PREFIXES:
            assert agent_env_refusal(f"{prefix}X") is not None, prefix
        for suffix in name_grant._EXEC_ENV_SUFFIXES:
            assert agent_env_refusal(f"X{suffix}") is not None, suffix

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


class TestTheSharedBackgroundRuntime:
    """An ``agent.env`` change frees the shared background runtime's slot.

    That runtime is one long-lived process every background and cron session
    demuxes onto, so it must not serve the old map until it goes stale (up to
    its 6 h age ceiling).
    """

    @staticmethod
    def _manager() -> SessionManager:
        return SessionManager(KiroCrewConfig(), provider_factory=lambda *a, **kw: AsyncMock())

    @staticmethod
    def _runtime(*, busy: bool) -> AsyncMock:
        rt = AsyncMock()
        rt.pid = _UNALLOCATABLE_PID
        rt.is_alive = lambda: True
        rt.has_active_or_initializing_sessions = lambda: busy
        rt.kill = AsyncMock()
        return rt

    @staticmethod
    async def _apply(mgr: SessionManager, *paths: str) -> None:
        with patch.object(mgr, "refresh_defaults", AsyncMock()):
            await mgr._on_config_change(_change(KiroCrewConfig(), *paths))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
    async def test_an_agent_env_change_parks_the_runtime_and_frees_the_slot(self, busy) -> None:
        mgr = self._manager()
        rt = self._runtime(busy=busy)
        mgr._bg_runtime = rt

        await self._apply(mgr, "agent.env.HTTPS_PROXY")

        # The next get_bg_session spawns a replacement, which reads the new map.
        assert mgr._bg_runtime is None
        assert mgr._draining_bg_runtimes == [rt]
        # Parked, not killed: its in-flight sessions finish on it, and a claim
        # pinned just before the change has the park grace to open its scope.
        rt.kill.assert_not_awaited()
        assert identity_park_grace_remaining(rt, time.monotonic()) > 0.0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_another_factory_change_leaves_the_runtime_alone(self) -> None:
        mgr = self._manager()
        rt = self._runtime(busy=False)
        mgr._bg_runtime = rt

        await self._apply(mgr, "agent.model")

        assert mgr._bg_runtime is rt
        assert mgr._draining_bg_runtimes == []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_runtime_and_a_closing_manager_park_nothing(self) -> None:
        mgr = self._manager()
        assert await mgr._background_runtime.retire_for_agent_env_change() is False

        rt = self._runtime(busy=False)
        mgr._bg_runtime = rt
        mgr._closing = True
        assert await mgr._background_runtime.retire_for_agent_env_change() is False
        assert mgr._bg_runtime is rt
        mgr._closing = False
        await mgr.close_all()


#: One entry per refusal class, each with the phrase its reason carries. The
#: values are distinct markers so a message that echoed one would be caught.
_REFUSED_AT_SAVE: dict[str, tuple[object, str]] = {
    "OPENCODE_CONFIG": ("/value/not/printed/1", "run code the command did not name"),
    "GH_CONFIG_DIR": ("/value/not/printed/2", "run code the command did not name"),
    "GITHUB_TOKEN": ("value-not-printed-3", "looks like a credential"),
    "CODEX_HOME": ("/value/not/printed/4", "sign-in directory"),
    "HAS-DASH": ("value-not-printed-5", "not an environment-variable name"),
    "GNUPGHOME": ("/value/not/printed/6", "scrub removes that name"),
    "BAD_VALUE_TYPE": (3, "must be a string"),
    "BAD_VALUE_NUL": ("value-not-printed-7\x00", "must be a string with no NUL"),
}


class TestTheWriteFloor:
    """A refused entry is turned away when it is SAVED, not dropped at load.

    Load and spawn drop such an entry with only a gateway-log line, so a write
    that stored it would succeed, the entry would sit in ``config.json``, and the
    variable would never reach the agent. Every config writer -- ``kirocrew
    config set`` in each form, every dashboard write, ``save()`` -- lands through
    the publish floor, so the refusal is made there.
    """

    @staticmethod
    def _seed(path, agent: dict) -> bytes:
        path.write_text(json.dumps({"agent": agent}), encoding="utf-8")
        return path.read_bytes()

    @staticmethod
    def _put_env(path, mapping: object) -> None:
        from kiro_crew.config.loader import update_config_locked

        def put(existing: dict) -> dict:
            existing.setdefault("agent", {})["env"] = mapping
            return existing

        update_config_locked(path, mutate=put)

    def test_the_scrub_case_is_one_the_config_layer_alone_accepts(self) -> None:
        # So the save-time refusal of GNUPGHOME below is the scrub check's doing.
        assert agent_env_refusal("GNUPGHOME") is None
        assert sections_mod.agent_env_scrub_removes("GNUPGHOME")
        assert sections_mod.agent_env_scrub_removes("gnupghome")

    @pytest.mark.parametrize("name", sorted(_REFUSED_AT_SAVE))
    def test_a_refused_entry_is_refused_at_save_and_nothing_is_written(
        self, tmp_path, name: str
    ) -> None:
        from kiro_crew.config.loader import ConfigWriteRefused

        path = tmp_path / "config.json"
        before = self._seed(path, {"yolo": True})
        value, reason = _REFUSED_AT_SAVE[name]

        with pytest.raises(ConfigWriteRefused) as refused:
            self._put_env(path, {**_PROXY, "GOPRIVATE": "corp.example/*", name: value})

        message = str(refused.value)
        assert repr(name) in message and reason in message, message
        # Only the refused entry is named, and no value ever is.
        assert "'HTTPS_PROXY'" not in message and "'GOPRIVATE'" not in message
        if isinstance(value, str):
            assert value.rstrip("\x00") not in message
        assert path.read_bytes() == before, "a refused write must leave the file as it was"
        assert not list(tmp_path.glob("*.tmp*")), "no temp file may survive a refused publish"

    def test_an_accepted_mapping_is_written_as_given(self, tmp_path) -> None:
        path = tmp_path / "config.json"
        self._seed(path, {})
        wanted = {**_PROXY, "GOPRIVATE": "corp.example/*", "TOKENIZERS_PARALLELISM": "false"}

        self._put_env(path, wanted)

        assert json.loads(path.read_text(encoding="utf-8"))["agent"]["env"] == wanted

    def test_a_non_mapping_is_refused_at_save(self, tmp_path) -> None:
        from kiro_crew.config.loader import ConfigWriteRefused

        path = tmp_path / "config.json"
        before = self._seed(path, {})
        with pytest.raises(ConfigWriteRefused) as refused:
            self._put_env(path, "HTTPS_PROXY=http://proxy.example:3128")
        assert "must be an object" in str(refused.value)
        assert path.read_bytes() == before

    def test_a_refused_entry_already_on_disk_does_not_refuse_an_unrelated_write(
        self, tmp_path, caplog
    ) -> None:
        # Most writers have nothing to do with agent.env and no handler for the
        # refusal; a hand-edited entry must not turn each of them into an error.
        from kiro_crew.config.loader import update_config_locked

        path = tmp_path / "config.json"
        stale = {"OPENCODE_CONFIG": "/value/not/printed", **_PROXY}
        self._seed(path, {"env": stale})

        def unrelated(existing: dict) -> dict:
            existing["agent"]["yolo"] = True
            return existing

        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            update_config_locked(path, mutate=unrelated)

        written = json.loads(path.read_text(encoding="utf-8"))["agent"]
        assert written["yolo"] is True and written["env"] == stale
        warned = [r.getMessage() for r in caplog.records if "agent.env" in r.getMessage()]
        assert warned and "'OPENCODE_CONFIG'" in warned[0]
        assert "/value/not/printed" not in warned[0]

    def test_changing_the_mapping_while_a_refused_entry_stays_is_refused(self, tmp_path) -> None:
        from kiro_crew.config.loader import ConfigWriteRefused, update_config_locked

        path = tmp_path / "config.json"
        before = self._seed(path, {"env": {"OPENCODE_CONFIG": "/x"}})

        def add_beside_it(existing: dict) -> dict:
            existing["agent"]["env"]["HTTPS_PROXY"] = "http://proxy.example:3128"
            return existing

        with pytest.raises(ConfigWriteRefused) as refused:
            update_config_locked(path, mutate=add_beside_it)
        assert "'OPENCODE_CONFIG'" in str(refused.value)
        assert path.read_bytes() == before

    def test_with_no_readable_prior_document_a_refused_entry_is_refused(self, tmp_path) -> None:
        from kiro_crew.config.loader import ConfigWriteRefused, write_config_atomically

        data = {"agent": {"env": {"GITHUB_TOKEN": "v"}}}
        with pytest.raises(ConfigWriteRefused):
            write_config_atomically(tmp_path / "new.json", data)
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{ not json", encoding="utf-8")
        with pytest.raises(ConfigWriteRefused):
            write_config_atomically(corrupt, data)
        assert corrupt.read_text(encoding="utf-8") == "{ not json"

    def test_the_whole_document_save_is_gated_too(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.config.loader import ConfigWriteRefused

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        cfg = KiroCrewConfig()
        cfg.agent.env = {"OPENCODE_CONFIG_DIR": "/x"}
        with pytest.raises(ConfigWriteRefused) as refused:
            cfg.save()
        assert "'OPENCODE_CONFIG_DIR'" in str(refused.value)

    @staticmethod
    def _config_set(key: str, value: str, *, local: bool) -> None:
        from kiro_crew.cli_config import _config_cmd

        args = argparse.Namespace(config_action="set", key=key, value=value, file=None, local=local)
        with patch("kiro_crew.cli_config.sel"):
            _config_cmd(args)

    def test_config_set_refuses_by_key_and_reason_and_writes_nothing(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        base = home / "config.json"
        self._seed(base, {"yolo": True})
        value = json.dumps({"OPENCODE_CONFIG": "/value/not/printed", **_PROXY})

        with pytest.raises(SystemExit) as exited:
            self._config_set("agent.env", value, local=False)

        assert exited.value.code == 1
        err = capsys.readouterr().err
        assert "'OPENCODE_CONFIG'" in err and "run code the command did not name" in err
        assert "/value/not/printed" not in err
        # Loading the config may stamp it; the refused mapping never lands.
        stored = json.loads(base.read_text(encoding="utf-8"))
        assert stored["agent"]["yolo"] is True
        assert not stored["agent"].get("env")

    def test_config_set_local_refuses_and_does_not_create_the_overlay(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        with pytest.raises(SystemExit) as exited:
            self._config_set("agent.env", json.dumps({"GITHUB_TOKEN": "v"}), local=True)

        assert exited.value.code == 1
        assert "'GITHUB_TOKEN'" in capsys.readouterr().err
        assert not (home / "config.local.json").exists()

    def test_config_set_writes_an_accepted_entry(self, tmp_path, monkeypatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        self._seed(home / "config.json", {})

        wanted = {"GOPRIVATE": "corp.example/*", "TOKENIZERS_PARALLELISM": "false"}
        self._config_set("agent.env", json.dumps(wanted), local=False)

        written = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert written["agent"]["env"] == wanted
