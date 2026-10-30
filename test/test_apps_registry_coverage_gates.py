"""Behaviour coverage for the small gates and helpers ``kiro_crew.apps.registry`` checks
before it fetches anything.

Streamed log lines, the child environment, URL shape, the clone sandbox mode, the
configured and trusted registry hosts, owner-designated repos, the SEL credential
grant, and loading the registry file with its edition rows.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from apps_registry_coverage_helpers import _explicit_registry_execution_admission  # noqa: F401
from apps_registry_coverage_helpers import _config_with, _reg

from kiro_crew.apps import registry
from kiro_crew.platform import PlatformCompositionError

# ---------------------------------------------------------------------------
# StreamingLogLines
# ---------------------------------------------------------------------------


class TestStreamingLogLines:
    def test_append_stores_and_forwards(self):
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        lines = registry.StreamingLogLines(queue)
        lines.append("hello")
        assert list(lines) == ["hello"]
        assert queue.get_nowait() == "hello"

    def test_extend_forwards_every_line(self):
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        lines = registry.StreamingLogLines(queue)
        lines.extend(["a", "b"])
        assert list(lines) == ["a", "b"]
        assert [queue.get_nowait(), queue.get_nowait()] == ["a", "b"]

    def test_full_queue_drops_without_raising(self):
        """A slow SSE consumer must not break the install it is watching."""
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1)
        lines = registry.StreamingLogLines(queue)
        lines.append("kept")
        lines.append("dropped")
        # The list keeps everything; only the queue drops the overflow.
        assert list(lines) == ["kept", "dropped"]
        assert queue.get_nowait() == "kept"
        assert queue.empty()


# ---------------------------------------------------------------------------
# Environment construction
# ---------------------------------------------------------------------------


class TestEnvHelpers:
    def test_minimal_env_keeps_allowlisted_and_drops_the_rest(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("MY_SECRET_TOKEN_VALUE", "hunter2")
        env = registry.minimal_env()
        assert env["PATH"] == "/usr/bin"
        assert "MY_SECRET_TOKEN_VALUE" not in env

    def test_minimal_env_applies_extras(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        assert registry.minimal_env(PATH="/opt/bin")["PATH"] == "/opt/bin"

    def test_anonymous_git_env_strips_credential_carriers(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /home/me/.ssh/id_ed25519")
        env = registry.anonymous_git_env()
        assert "SSH_AUTH_SOCK" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
        assert "id_ed25519" not in env["GIT_SSH_COMMAND"]


# ---------------------------------------------------------------------------
# URL shape helpers
# ---------------------------------------------------------------------------


class TestUrlHelpers:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ({"gitUrl": " https://example.com/a.git "}, "https://example.com/a.git"),
            ({"repo": "https://example.com/b.git"}, "https://example.com/b.git"),
            ({"gitUrl": "", "repo": "legacy-name"}, "legacy-name"),
            ({}, ""),
            ({"gitUrl": {"nested": "object"}}, ""),
            ({"gitUrl": 42}, ""),
        ],
    )
    def test_entry_git_url(self, raw, expected):
        assert registry._entry_git_url(raw) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://example.com/a.git", True),
            ("http://example.com/a.git", True),
            ("ssh://git@example.com/a.git", True),
            ("git://example.com/a.git", True),
            ("git+ssh://example.com/a.git", True),
            ("git@example.com:owner/a.git", True),
            ("bare-name", False),
            ("", False),
            ("/abs/path", False),
        ],
    )
    def test_looks_like_git_url(self, url, expected):
        assert registry._looks_like_git_url(url) is expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://Example.COM/a.git", "example.com"),
            ("ssh://git@Example.com:2222/a.git", "example.com"),
            ("git@example.com:owner/a.git", "example.com"),
            ("git+ssh://user@host.internal/a.git", "host.internal"),
            ("ssh://git@[2001:DB8:0:0::1]:2222/a.git", "2001:db8::1"),
            ("git@[2001:DB8::1]:owner/a.git", "2001:db8::1"),
            ("https://[2001:db8::1]/owner/a.git", "2001:db8::1"),
            ("ssh://git@[2001:db8::1/a.git", ""),
            ("ssh://git@[]/a.git", ""),
            ("ssh://git@[not-ipv6]/a.git", ""),
            ("ssh://git@[2001:db8::1]junk/a.git", ""),
            ("ssh://git@[2001:db8::1]:0/a.git", ""),
            ("ssh://git@[2001:db8::1]:65536/a.git", ""),
            ("git@[2001:db8::1]junk:owner/a.git", ""),
            ("ssh://git@2001:db8::1/a.git", ""),
            ("ssh://git@example.test:１２/a.git", ""),
            ("ssh://deploy:password@example.test/a.git", ""),
            ("git+ssh://deploy:password@example.test/a.git", ""),
            ("  ", ""),
            ("not a url", ""),
        ],
    )
    def test_git_url_host(self, url, expected):
        assert registry._git_url_host(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("ssh://git@github.com/a.git", True),
            ("git+ssh://git@github.com/a.git", True),
            ("git@github.com:owner/a.git", True),
            ("ssh://git@[2001:db8::1]/a.git", True),
            ("git@[2001:db8::1]:owner/a.git", True),
            ("git@[2001:db8::1]junk:owner/a.git", False),
            ("ssh://deploy:password@github.com/a.git", False),
            ("https://github.com/owner/a.git", False),
            ("", False),
        ],
    )
    def test_is_ssh_git_url(self, url, expected):
        assert registry._is_ssh_git_url(url) is expected


class TestCloneSandboxMode:
    def test_public_ssh_forge_gets_standard(self):
        assert registry._clone_sandbox_mode("git@github.com:owner/a.git") == "standard"

    def test_configured_host_is_added_to_the_trusted_set(self):
        mode = registry._clone_sandbox_mode(
            "git@gitea.internal:owner/a.git", frozenset({"gitea.internal"})
        )
        assert mode == "standard"

    def test_untrusted_ssh_host_stays_strict(self):
        assert registry._clone_sandbox_mode("git@evil.example:owner/a.git") == "strict"

    def test_colon_bearing_ssh_userinfo_never_receives_host_trust(self):
        target = "ssh://deploy:password@github.com/owner/a.git"

        assert registry.is_clone_host_trusted(target) is False
        assert registry._clone_sandbox_mode(target, frozenset({"github.com"})) == "strict"

    def test_https_never_needs_ssh_keys(self):
        assert registry._clone_sandbox_mode("https://github.com/owner/a.git") == "strict"

    def test_hostless_ssh_url_fails_closed(self, monkeypatch):
        monkeypatch.setattr(registry, "_is_ssh_git_url", lambda url: True)
        monkeypatch.setattr(registry, "_git_url_host", lambda url: "")
        assert registry._clone_sandbox_mode("nonsense") == "strict"


class TestConfiguredRegistryHosts:
    def test_collects_hosts_of_configured_registries(self, monkeypatch):
        _config_with(
            monkeypatch,
            [
                _reg("a", "https://gitea.internal/org/idx.git"),
                _reg("b", "bare-name-no-host"),
            ],
        )
        assert registry._configured_registry_hosts() == frozenset({"gitea.internal"})

    def test_ipv6_hosts_are_exact_for_trust_and_ssh_sandbox(self, monkeypatch):
        configured = "ssh://git@[2001:DB8::1]/owner/index.git"
        _config_with(monkeypatch, [_reg("ipv6", configured)])

        trusted = registry._configured_registry_hosts()
        assert trusted == frozenset({"2001:db8::1"})

        exact_uri = "ssh://git@[2001:db8::1]/owner/app.git"
        other_uri = "ssh://git@[2001:dead::2]/owner/app.git"
        exact_scp = "git@[2001:db8::1]:owner/app.git"
        other_scp = "git@[2001:dead::2]:owner/app.git"

        assert registry.is_clone_host_trusted(exact_uri) is True
        assert registry.is_clone_host_trusted(exact_scp) is True
        assert registry.is_clone_host_trusted(other_uri) is False
        assert registry.is_clone_host_trusted(other_scp) is False
        assert registry._clone_sandbox_mode(exact_uri, trusted) == "standard"
        assert registry._clone_sandbox_mode(exact_scp, trusted) == "standard"
        assert registry._clone_sandbox_mode(other_uri, trusted) == "strict"
        assert registry._clone_sandbox_mode(other_scp, trusted) == "strict"

    @pytest.mark.parametrize(
        "target",
        [
            "ssh://git@[2001:db8::1/owner/app.git",
            "ssh://git@[]/owner/app.git",
            "ssh://git@[not-ipv6]/owner/app.git",
            "git@[2001:db8::1]junk:owner/app.git",
        ],
    )
    def test_malformed_ipv6_hosts_fail_closed(self, monkeypatch, target):
        _config_with(
            monkeypatch,
            [_reg("ipv6", "ssh://git@[2001:db8::1]/owner/index.git")],
        )

        assert registry._git_url_host(target) == ""
        assert registry.is_clone_host_trusted(target) is False
        assert registry._clone_sandbox_mode(target) == "strict"

    def test_oversized_ports_fail_closed_without_integer_conversion_crash(self, monkeypatch):
        _config_with(
            monkeypatch,
            [_reg("ipv6", "ssh://git@[2001:db8::1]/owner/index.git")],
        )
        oversized = "9" * 5000
        targets = [
            f"ssh://git@[2001:db8::1]:{oversized}/owner/app.git",
            f"ssh://git@example.test:{oversized}/owner/app.git",
        ]

        for target in targets:
            assert registry._git_url_host(target) == ""
            assert registry.is_clone_host_trusted(target) is False
            assert registry._clone_sandbox_mode(target) == "strict"

    def test_config_load_failure_degrades_to_empty(self, monkeypatch):
        def _boom(cls):
            raise OSError("config unreadable")

        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(_boom))
        assert registry._configured_registry_hosts() == frozenset()


class TestContextCloneSandboxMode:
    def test_composition_error_is_never_swallowed(self, monkeypatch):
        def _boom():
            raise PlatformCompositionError("companion missing")

        monkeypatch.setattr(registry, "current_context", _boom)
        with pytest.raises(PlatformCompositionError):
            registry._context_clone_sandbox_mode("git@github.com:o/a.git")

    def test_adapter_failure_falls_back_to_the_module_decision(self, monkeypatch):
        def _boom():
            raise RuntimeError("adapter down")

        monkeypatch.setattr(registry, "current_context", _boom)
        monkeypatch.setattr(registry, "_configured_registry_hosts", frozenset)
        # The security gate must survive the adapter: a public forge still
        # resolves, an unknown host still fails closed.
        assert registry._context_clone_sandbox_mode("git@github.com:o/a.git") == "standard"
        assert registry._context_clone_sandbox_mode("git@evil.example:o/a.git") == "strict"


class TestIsCloneHostTrusted:
    def test_hostless_url_is_untrusted(self):
        assert registry.is_clone_host_trusted("bare-name") is False

    def test_composition_error_propagates(self, monkeypatch):
        def _boom():
            raise PlatformCompositionError("companion missing")

        monkeypatch.setattr(registry, "current_context", _boom)
        with pytest.raises(PlatformCompositionError):
            registry.is_clone_host_trusted("https://github.com/o/a.git")

    def test_adapter_failure_keeps_the_default_trust_set(self, monkeypatch):
        def _boom():
            raise RuntimeError("adapter down")

        monkeypatch.setattr(registry, "current_context", _boom)
        monkeypatch.setattr(registry, "_configured_registry_hosts", frozenset)
        assert registry.is_clone_host_trusted("https://github.com/o/a.git") is True
        assert registry.is_clone_host_trusted("https://127.0.0.1:8443/x.git") is False


class TestOwnerDesignatedRepo:
    def test_bundled_entry_is_not_index_originated(self):
        assert registry._is_owner_designated_repo({"gitUrl": "https://x/y.git"}) is False

    def test_entry_without_resolvable_url_is_refused(self, monkeypatch):
        assert registry._is_owner_designated_repo({"_registry": "mine"}) is False

    def test_byte_identical_url_is_owner_designated(self, monkeypatch):
        _config_with(monkeypatch, [_reg("mine", "https://gitea.internal/org/idx.git")])
        entry = {"_registry": "mine", "gitUrl": "https://gitea.internal/org/idx.git"}
        assert registry._is_owner_designated_repo(entry) is True

    def test_sibling_repo_on_the_same_host_is_not(self, monkeypatch):
        _config_with(monkeypatch, [_reg("mine", "https://gitea.internal/org/idx.git")])
        entry = {"_registry": "mine", "gitUrl": "https://gitea.internal/org/private.git"}
        assert registry._is_owner_designated_repo(entry) is False


class TestSelCredentialGrant:
    def test_audit_failure_is_swallowed(self, monkeypatch, caplog):
        sel_fn = MagicMock(side_effect=RuntimeError("sel down"))
        monkeypatch.setattr(registry, "_sel_fn", sel_fn)
        caplog.set_level("DEBUG", logger="kiro_crew.apps.registry")
        registry._sel_credential_grant("op", "https://x/y.git")
        sel_fn.assert_called_once_with()
        messages = [r.getMessage() for r in caplog.records if r.name == "kiro_crew.apps.registry"]
        assert messages == ["SEL audit log failed for op: sel down"]

    def test_grant_is_logged_when_sel_is_present(self, monkeypatch):
        sel_obj = MagicMock()
        monkeypatch.setattr(registry, "_sel_fn", lambda: sel_obj)
        registry._sel_credential_grant("install_from_registry", "https://x/y.git")
        assert sel_obj.log_api_access.call_args.kwargs["outcome"] == "granted"


# ---------------------------------------------------------------------------
# Registry file loading + edition rows
# ---------------------------------------------------------------------------


class TestLoadRegistryFile:
    def test_missing_file_yields_no_rows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "_REGISTRY_FILE", tmp_path / "absent.json")
        monkeypatch.setattr(registry, "_edition_registry_rows", list)
        assert registry._load_registry_file() == []

    def test_non_array_json_is_rejected(self, monkeypatch, tmp_path):
        path = tmp_path / "app-registry.json"
        path.write_text('{"not": "an array"}', encoding="utf-8")
        monkeypatch.setattr(registry, "_REGISTRY_FILE", path)
        monkeypatch.setattr(registry, "_edition_registry_rows", list)
        assert registry._load_registry_file() == []

    def test_invalid_json_is_rejected(self, monkeypatch, tmp_path):
        path = tmp_path / "app-registry.json"
        path.write_text("{{{ not json", encoding="utf-8")
        monkeypatch.setattr(registry, "_REGISTRY_FILE", path)
        monkeypatch.setattr(registry, "_edition_registry_rows", list)
        assert registry._load_registry_file() == []

    def test_edition_rows_are_add_only_and_never_repoint_a_core_row(self, monkeypatch, tmp_path):
        path = tmp_path / "app-registry.json"
        path.write_text(json.dumps([{"name": "core-app", "repo": "core/repo"}]), encoding="utf-8")
        monkeypatch.setattr(registry, "_REGISTRY_FILE", path)
        monkeypatch.setattr(
            registry,
            "_edition_registry_rows",
            lambda: [
                {"name": "core-app", "repo": "attacker/repo"},
                {"name": "edition-app", "repo": "edition/repo"},
            ],
        )
        rows = registry._load_registry_file()
        assert [r["name"] for r in rows] == ["core-app", "edition-app"]
        assert rows[0]["repo"] == "core/repo"


class TestEditionRegistryRows:
    def test_malformed_rows_are_dropped(self, monkeypatch):
        loader = SimpleNamespace(
            registry_rows=lambda: [
                {"name": "good"},
                {"name": 7},
                "not-a-dict",
                {},
            ]
        )
        monkeypatch.setattr(
            registry, "current_context", lambda: SimpleNamespace(apps_loader=loader)
        )
        assert registry._edition_registry_rows() == [{"name": "good"}]

    def test_seam_failure_falls_back_to_bundled_only(self, monkeypatch):
        def _boom():
            raise RuntimeError("loader down")

        monkeypatch.setattr(registry, "current_context", _boom)
        assert registry._edition_registry_rows() == []
