"""Invocation-local git config classification tests for the git/tar self floor.

Split out of ``test_denied_commands_security_git_tar_self.py`` to keep each file
under the 2000-line cap. Same rule, own-host pin and helpers as that sibling;
this file holds the config-key classifier, the all-verb config self-dial scan,
and the ``GIT_CONFIG_PARAMETERS`` cases.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import (  # noqa: F401
    _denied_by,
    _rule_pattern,
)

from kiro_crew import security
from kiro_crew.security import BUILTIN_DENIED_RULES  # noqa: F401
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import compute_effective_denied, is_denied, is_safe_user_regex  # noqa: F401

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestGitTarSelfConfig:
    """Invocation-local git config classification cases for the git/tar floor."""

    _RULE = "sandbox-escape-git-tar-self"
    #: A command a git or tar transport runs is re-entered through every argv
    #: floor, and the refusal names the row that refused it: a carried ``ssh``
    #: that dials this host is reported under the ssh floor's own row.
    _SSH_RULE = "sandbox-escape-ssh-self"
    _DIAL_RULES = frozenset({_RULE, _SSH_RULE})

    @staticmethod
    def _effective():
        return list(compute_effective_denied(BUILTIN_DENIED_RULES, (), False, (), ()))

    @pytest.fixture(autouse=True)
    def _pin_own_host_cache(self, request, _floor_monkeypatch):
        # Same pin as the ssh-self suite: a fixed own-host cache with the
        # resolved-once latch set, so no command here spawns the real own-host
        # resolver thread or blocks on DNS, and the DNS-alias verdict layer is
        # stubbed to "not self" so remote-host allow cases stay allowed.
        own_mp = pytest.MonkeyPatch()
        request.addfinalizer(own_mp.undo)
        own = security.socket.gethostname().strip().lower()
        pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
        _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
        _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
        _floor_monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
        own_mp.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
        _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)

    @pytest.mark.parametrize(
        "cmd",
        [
            # git runs these config values as a command even on a local-only
            # verb, so a self-dialing value is refused regardless of the verb.
            "git -c core.sshCommand='ssh localhost' status",
            "git -c core.fsmonitor='ssh localhost' status",
            "git -c core.gitProxy='ssh localhost' status",
            "git -c core.fsmonitor='ssh localhost' commit -m x",
            # A post-verb ``-c`` override is read the same way.
            "git status -c core.sshCommand='ssh localhost'",
            # ``archive`` with no transport-opening ``--remote`` (the one here is
            # an operand after ``--``) is local, yet the ``-c`` still carries.
            "git -c core.sshCommand='ssh localhost' archive HEAD -- --remote",
            # git carries a ``-c`` through GIT_CONFIG_PARAMETERS into a nested
            # transport, so an inherited self-dialing entry is refused on a local verb.
            "GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost'\" git status",
            # Numbered environment config is the same config as ``-c``.
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.sshCommand "
            "GIT_CONFIG_VALUE_0='ssh localhost' git status",
        ],
        ids=[
            "c-sshcommand-local-verb",
            "c-fsmonitor-local-verb",
            "c-gitproxy-local-verb",
            "c-fsmonitor-commit",
            "post-verb-c-sshcommand-local-verb",
            "c-sshcommand-local-archive",
            "git-config-parameters-carry-local-verb",
            "numbered-config-env-local-verb",
        ],
    )
    def test_local_verb_self_dialing_config_is_denied(self, cmd):
        # The value carries ``ssh localhost``, so the ssh floor names the row.
        assert _denied_by(cmd) == self._SSH_RULE, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c core.editor=vim status",
            "git -c core.fsmonitor='ssh far.example.com' status",
            "git -c core.fsmonitor=/usr/bin/fsmonitor-daemon status",
            "git -c core.pager=less log",
        ],
        ids=["editor", "remote-fsmonitor", "path-fsmonitor", "pager"],
    )
    def test_local_verb_benign_config_is_allowed(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    _CONFIG_TRANSPORT_COMMANDS = [
        (
            "clone",
            "git {config} clone https://github.com/kirodotdev/KiroCrew.git",
            "git clone {config} https://github.com/kirodotdev/KiroCrew.git",
        ),
        ("fetch", "git {config} fetch origin", "git fetch {config} origin"),
        (
            "ls-remote",
            "git {config} ls-remote https://github.com/kirodotdev/KiroCrew.git",
            "git ls-remote {config} https://github.com/kirodotdev/KiroCrew.git",
        ),
        (
            "submodule-update",
            "git {config} submodule update",
            "git submodule update {config}",
        ),
        (
            "remote-add",
            "git {config} remote add origin https://github.com/kirodotdev/KiroCrew.git",
            "git remote add {config} origin https://github.com/kirodotdev/KiroCrew.git",
        ),
    ]
    _CONFIG_CASES = [
        ("http-proxy", "http.proxy", "localhost:3128", None),
        ("https-proxy", "https.proxy", "localhost:3128", None),
        ("remote-url", "remote.x.url", "localhost:x", _RULE),
        (
            "url-rewrite",
            "url.ssh://localhost/.insteadOf",
            "https://github.com/",
            _RULE,
        ),
        ("ssh-command", "core.sshCommand", "ssh localhost", _SSH_RULE),
    ]

    @pytest.mark.parametrize(("verb", "pre", "post"), _CONFIG_TRANSPORT_COMMANDS)
    @pytest.mark.parametrize(("case", "key", "value", "rule"), _CONFIG_CASES)
    @pytest.mark.parametrize("position", ["pre", "post"])
    @pytest.mark.parametrize("source", ["config", "config-env"])
    def test_git_config_values_are_classified_by_key(
        self, verb, pre, post, case, key, value, rule, position, source
    ):
        del verb, case
        if source == "config-env":
            prefix = f"CONFIG_VALUE='{value}' "
            config = f"--config-env {key}=CONFIG_VALUE"
        else:
            prefix = ""
            config = f"-c {key}='{value}'"
        cmd = prefix + (pre if position == "pre" else post).format(config=config)
        assert security._is_git_tar_to_self(cmd) is (rule is not None), cmd
        assert _denied_by(cmd) == rule, cmd

    def test_post_verb_proxy_config_is_not_a_remote_candidate(self):
        cmd = "git clone -c http.proxy=localhost:3128 https://github.com/org/repo"
        assert not security._is_git_tar_to_self(cmd), cmd
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        "template",
        ["-c {}", "-c{}", "--config {}", "--config={}"],
        ids=["short-detached", "short-attached", "long-detached", "long-attached"],
    )
    @pytest.mark.parametrize(
        ("config", "denied"),
        [
            ("http.proxy=localhost:3128", False),
            ("remote.x.url=localhost:x", True),
        ],
        ids=["http-proxy", "remote-url"],
    )
    def test_post_verb_config_spellings_use_the_key_aware_classifier(
        self, template, config, denied
    ):
        option = template.format(config)
        cmd = f"git clone {option} https://github.com/kirodotdev/KiroCrew.git"
        assert security._is_git_tar_to_self(cmd) is denied, cmd
        if denied:
            assert _denied_by(cmd) == self._RULE, cmd
        else:
            assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        ("key", "value", "denied"),
        [
            ("remote.x.url", "localhost:x", True),
            ("http.proxy", "localhost:3128", False),
        ],
        ids=["transport-url", "http-proxy"],
    )
    @pytest.mark.parametrize(
        "env_form",
        [
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0={key} GIT_CONFIG_VALUE_0={value} ",
            "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0={key} " "GIT_CONFIG_VALUE_0={value}; ",
            "declare -x GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0={key} " "GIT_CONFIG_VALUE_0={value}; ",
            "typeset -x GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0={key} " "GIT_CONFIG_VALUE_0={value}; ",
        ],
        ids=["prefix", "export", "declare-export", "typeset-export"],
    )
    def test_numbered_environment_config_uses_the_key_aware_classifier(
        self, key, value, denied, env_form
    ):
        cmd = env_form.format(key=key, value=value) + "git fetch origin"
        assert security._is_git_tar_to_self(cmd) is denied, cmd
        if denied:
            assert _denied_by(cmd) == self._RULE, cmd
        else:
            assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("git -c remote.selftarget.url=localhost:x ls-remote selftarget", _RULE),
            ("git -c remote.selftarget.pushurl=localhost:x ls-remote selftarget", _RULE),
            ("git -c alias.foo='ssh localhost' ls-remote far:x", _SSH_RULE),
        ],
        ids=["remote-url", "remote-pushurl", "command-value-key-agnostic"],
    )
    def test_transport_url_key_or_command_value_to_self_is_denied(self, cmd, rule):
        assert _denied_by(cmd) == rule, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost id #'\" " "git ls-remote far:x",
            "GIT_CONFIG_PARAMETERS=\"'core.sshCommand'='ssh localhost id #'\" "
            "git ls-remote far:x",
            "GIT_CONFIG_PARAMETERS=\"'alias.note=it'\\''s' "
            "'core.sshCommand=ssh localhost'\" git ls-remote far:x",
            "env GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost id #'\" "
            "git ls-remote far:x",
            "export GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost id #'\"; "
            "git ls-remote far:x",
            "declare -x GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost id #'\"; "
            "git ls-remote far:x",
            "typeset -x GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost id #'\"; "
            "git ls-remote far:x",
            "GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh localhost id #'\" "
            "sh -c 'git ls-remote far:x'",
        ],
        ids=[
            "inline",
            "split-key-value",
            "escaped-quote",
            "env",
            "export",
            "declare-export",
            "typeset-export",
            "nested-shell",
        ],
    )
    def test_git_config_parameters_sq_quote_to_self_is_denied(self, cmd):
        assert _denied_by(cmd) == self._SSH_RULE, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "GIT_CONFIG_PARAMETERS=\"'http.proxy=localhost:3128'\" git ls-remote far:x",
            "GIT_CONFIG_PARAMETERS=\"'core.sshCommand=ssh far.example.com'\" "
            "git ls-remote far:x",
        ],
        ids=["http-proxy", "remote-ssh-command"],
    )
    def test_git_config_parameters_non_self_values_are_allowed(self, cmd):
        assert _denied_by(cmd) not in self._DIAL_RULES, cmd

    def test_malformed_git_config_parameters_falls_back_to_the_config_classifier(self):
        cmd = 'GIT_CONFIG_PARAMETERS="remote.x.url=localhost:x \'" git ls-remote far:x'
        assert _denied_by(cmd) == self._RULE
