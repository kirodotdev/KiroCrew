"""Jira beyond the markdown converter: credential lookup, Cloud detection, linked
changes, and fix versions as the milestone equivalent."""

from __future__ import annotations

import json

import pytest
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401

from kiro_crew.dashboard.handlers import source_providers as source


class TestGetJiraAuth:
    """Credential resolution for Jira hosts."""

    def test_returns_none_when_no_config(self, monkeypatch):
        """No entries → None."""

        class FakeDashboard:
            jira_auth = []

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        assert source._get_jira_auth("acme.atlassian.net") is None

    def test_matches_host_case_insensitively(self, monkeypatch):
        class FakeEntry:
            host = "Acme.atlassian.net"
            email = "user@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": "secret123"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("user@acme.com", "secret123")

    def test_strips_port_443(self, monkeypatch):
        class FakeEntry:
            host = "jira.internal:443"
            email = ""

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": "pat-token"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        result = source._get_jira_auth("jira.internal")
        assert result == ("", "pat-token")

    def test_raises_value_error_on_config_load_failure(self, monkeypatch):
        """Config errors propagate as ValueError, not silent None."""

        class BrokenConfig:
            @classmethod
            def load(cls):
                raise RuntimeError("corrupt config.json")

        monkeypatch.setattr(source, "KiroCrewConfig", BrokenConfig)
        with pytest.raises(ValueError, match="jira_config_error"):
            source._get_jira_auth("acme.atlassian.net")

    def test_per_host_token_takes_precedence(self, monkeypatch):
        """JIRA_TOKEN_<hex> is preferred over global JIRA_API_TOKEN."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                host_key = "acme.atlassian.net".encode().hex().upper()
                return {
                    "JIRA_API_TOKEN": "global-fallback",
                    f"JIRA_TOKEN_{host_key}": "per-host-secret",
                }

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "per-host-secret")

    def test_seeded_global_env_does_not_bypass_per_host_vault(self, monkeypatch):
        """A global JIRA_API_TOKEN that load_credentials merely SEEDED into
        os.environ (setdefault), not a real pre-existing operator override,
        must NOT be treated as a live override: a host with its own per-host
        vault token still gets that per-host token. The snapshot is captured
        BEFORE load_credentials runs, so a value that did not exist in the
        environment beforehand is not seen as an override."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        # No REAL operator override present before the call.
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                # Simulate load_credentials' setdefault seeding the .env global
                # into the process environment during the call. Use monkeypatch
                # so the seed is auto-reverted at test teardown and cannot leak
                # into later tests (a raw os.environ.setdefault would persist).
                monkeypatch.setenv("JIRA_API_TOKEN", "seeded-global")
                return {"JIRA_API_TOKEN": "seeded-global"}

        host_key = "acme.atlassian.net".encode().hex().upper()
        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "per-host-vault" if name == f"JIRA_TOKEN_{host_key}" else "",
        )
        result = source._get_jira_auth("acme.atlassian.net")
        # Per-host vault token wins; the seeded global is ignored.
        assert result == ("dev@acme.com", "per-host-vault")

    def test_vault_token_preferred_over_env(self, monkeypatch):
        """A vault secret wins over the legacy .env value for the same host."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": "env-token"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        host_key = "acme.atlassian.net".encode().hex().upper()
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "vault-token" if name == f"JIRA_TOKEN_{host_key}" else "",
        )
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "vault-token")

    def test_vault_miss_falls_back_to_env(self, monkeypatch):
        """When the vault has no entry, the .env / environ value is used."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": "env-token"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(source, "_resolve_jira_token_from_vault", lambda name: "")
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "env-token")

    def test_vault_single_host_global_token(self, monkeypatch):
        """Single host with no per-host vault entry uses the global vault secret."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "vault-global" if name == "JIRA_API_TOKEN" else "",
        )
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "vault-global")

    def test_global_env_override_beats_stale_global_vault(self, monkeypatch):
        """A nonempty process-environment JIRA_API_TOKEN overrides even a stale
        global vault entry.

        `load_credentials` overlays `os.environ` over the .env for this key, so
        a live env var is the effective credential — and `secrets import` skips
        migrating the key while such an override is set. A vault entry left by an
        EARLIER migration must NOT shadow that override under vault-first
        resolution. Per-host keys are unaffected (not env-overlaid)."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                # env overlay would also place it here; the resolver reads the
                # override directly from os.environ before the global vault.
                return {"JIRA_API_TOKEN": "env-override"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        # Stale global vault entry that must NOT win.
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "stale-vault" if name == "JIRA_API_TOKEN" else "",
        )
        monkeypatch.setenv("JIRA_API_TOKEN", "env-override")
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "env-override")

    def test_catalog_slots_match_runtime_precedence(self, monkeypatch):
        """Catalog output names the same vault slot the runtime resolves."""
        from kiro_crew.dashboard.handlers.secrets import _managed_secret_catalog

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        host_name = source.jira_host_token_name(FakeEntry.host)
        cases = (
            ([host_name], {host_name: "host-value"}, host_name),
            ([], {"JIRA_API_TOKEN": "global-value"}, "JIRA_API_TOKEN"),
        )
        for stored_names, vault_values, expected_name in cases:
            monkeypatch.setattr(
                source,
                "_resolve_jira_token_from_vault",
                lambda name, values=vault_values: values.get(name, ""),
            )
            assert source._get_jira_auth(FakeEntry.host) == (
                FakeEntry.email,
                vault_values[expected_name],
            )
            catalog = _managed_secret_catalog(
                stored_names,
                [FakeEntry.host],
                jira_global_applicable=True,
                wakatime_enabled=False,
            )
            assert expected_name in {entry["name"] for entry in catalog}

    def test_migrated_secret_ref_in_env_resolves_from_vault_not_uri(self, monkeypatch):
        """After `secrets import --apply`, the .env line is
        `JIRA_API_TOKEN=secret://JIRA_API_TOKEN` and `load_credentials`
        propagates that ref into os.environ AND the creds dict. The resolver
        must NOT hand the `secret://` URI to Jira as the token — it must treat
        it as a vault reference and resolve the real secret from the vault."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                # load_credentials overlays the migrated secret:// ref here too.
                return {"JIRA_API_TOKEN": "secret://JIRA_API_TOKEN"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "real-vault-secret" if name == "JIRA_API_TOKEN" else "",
        )
        # The migrated ref is propagated into the environment by load_credentials.
        monkeypatch.setenv("JIRA_API_TOKEN", "secret://JIRA_API_TOKEN")
        result = source._get_jira_auth("acme.atlassian.net")
        # The vault secret is used — NOT the secret:// URI.
        assert result == ("dev@acme.com", "real-vault-secret")

    def test_returns_none_when_no_token_anywhere(self, monkeypatch):
        """Configured host but neither vault nor env holds a token → None."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(source, "_resolve_jira_token_from_vault", lambda name: "")
        assert source._get_jira_auth("acme.atlassian.net") is None

    def test_env_override_equal_to_env_file_is_not_genuine_override(self, monkeypatch):
        """When os.environ['JIRA_API_TOKEN'] equals the .env file value (i.e. it
        was seeded there by GatewayOrchestrator's startup load_credentials call),
        it must NOT beat a vault entry — the vault's rotated value should win.

        This is the Finding 2 fix: a value that merely came from .env via
        load_credentials' setdefault is NOT a genuine operator override.
        """

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        _ENV_FILE_TOKEN = "stale-env-token"
        _VAULT_TOKEN = "fresh-vault-token"

        monkeypatch.setenv("JIRA_API_TOKEN", _ENV_FILE_TOKEN)

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": _ENV_FILE_TOKEN}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        # Per-host vault returns nothing; global vault has the rotated token.
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: (_VAULT_TOKEN if name == "JIRA_API_TOKEN" else ""),
        )
        # .env file contains the same value as os.environ (startup-seeded).
        monkeypatch.setattr(
            source,
            "read_env_file_credential",
            lambda key: _ENV_FILE_TOKEN if key == "JIRA_API_TOKEN" else "",
        )
        result = source._get_jira_auth("acme.atlassian.net")
        # Vault token must win; the .env-seeded env value must NOT override it.
        assert result == ("dev@acme.com", _VAULT_TOKEN), (
            "Vault token should win when env value equals .env file value "
            f"(startup-seeded); got {result}"
        )

    def test_env_override_differing_from_env_file_is_genuine_override(self, monkeypatch):
        """When os.environ['JIRA_API_TOKEN'] DIFFERS from the .env file value,
        the operator explicitly set it at runtime — it must beat the vault entry.
        """

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        _ENV_FILE_TOKEN = "stale-env-token"
        _OPERATOR_TOKEN = "operator-set-at-runtime"
        _VAULT_TOKEN = "vault-token"

        monkeypatch.setenv("JIRA_API_TOKEN", _OPERATOR_TOKEN)

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": _OPERATOR_TOKEN}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: _VAULT_TOKEN if name == "JIRA_API_TOKEN" else "",
        )
        # .env file contains a different (older) value — operator set a new one.
        monkeypatch.setattr(
            source,
            "read_env_file_credential",
            lambda key: _ENV_FILE_TOKEN if key == "JIRA_API_TOKEN" else "",
        )
        result = source._get_jira_auth("acme.atlassian.net")
        # The differing env value is a genuine override; it must win over vault.
        assert result == ("dev@acme.com", _OPERATOR_TOKEN), (
            "Operator runtime override should win over vault when it differs "
            f"from .env file value; got {result}"
        )


class TestJiraIsCloud:
    def test_cloud_host(self):
        assert source._jira_is_cloud("acme.atlassian.net") is True

    def test_server_host(self):
        assert source._jira_is_cloud("jira.internal.corp") is False

    def test_subdomain_required_for_cloud(self):
        # The URL parser already rejects bare .atlassian.net, but _jira_is_cloud
        # is about suffix matching, not URL parsing. Any valid Cloud host has a
        # non-empty org prefix.
        assert source._jira_is_cloud("x.atlassian.net") is True


@pytest.mark.asyncio
async def test_fetch_jira_issue_no_credentials_raises_value_error(monkeypatch):
    """When no credentials are configured, a ValueError with the expected prefix is raised."""
    monkeypatch.setattr(source, "_get_jira_auth", lambda host: None)
    ref = source.SourceRef(
        provider="jira",
        url="https://acme.atlassian.net/browse/PROJ-123",
        host="acme.atlassian.net",
        owner="",
        repo="PROJ",
        number=123,
        kind="issue",
    )
    with pytest.raises(ValueError, match="jira_no_credentials"):
        await source._fetch_jira_issue(ref)


def test_parse_jira_cloud_url() -> None:
    """Jira Cloud URLs parse correctly."""
    ref = source.parse_source_url("https://acme.atlassian.net/browse/PROJ-42")
    assert ref.provider == "jira"
    assert ref.host == "acme.atlassian.net"
    assert ref.repo == "PROJ"
    assert ref.number == 42
    assert ref.kind == "issue"
    assert ref.url == "https://acme.atlassian.net/browse/PROJ-42"


def test_parse_jira_self_hosted_url(monkeypatch) -> None:
    """Self-hosted Jira URLs parse when host is in the allowlist."""
    monkeypatch.setattr(source, "_allowed_jira_hosts", lambda: frozenset({"jira.internal.corp"}))
    ref = source.parse_source_url("https://jira.internal.corp/browse/TEAM-99")
    assert ref.provider == "jira"
    assert ref.host == "jira.internal.corp"
    assert ref.repo == "TEAM"
    assert ref.number == 99


# --- Jira linked issues ---


class TestJiraLinkedChanges:
    """Tests for _jira_linked_changes parsing."""

    def test_outward_link_parsed(self) -> None:
        """An outward issue link is parsed with the correct relation label."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                    "outwardIssue": {
                        "key": "PROJ-456",
                        "fields": {
                            "summary": "Blocked task",
                            "status": {"statusCategory": {"key": "new"}},
                        },
                    },
                }
            ]
        }
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert len(result) == 1
        assert result[0]["provider"] == "jira"
        assert result[0]["url"] == "https://acme.atlassian.net/browse/PROJ-456"
        assert result[0]["number"] == 456
        assert result[0]["title"] == "Blocked task"
        assert result[0]["state"] == "open"
        assert result[0]["relation"] == "blocks"
        assert result[0]["issueKey"] == "PROJ-456"

    def test_inward_link_parsed(self) -> None:
        """An inward issue link is parsed with the inward relation label."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                    "inwardIssue": {
                        "key": "TEAM-10",
                        "fields": {
                            "summary": "Upstream dep",
                            "status": {"statusCategory": {"key": "indeterminate"}},
                        },
                    },
                }
            ]
        }
        result = source._jira_linked_changes(fields, "https://jira.corp/jira")
        assert len(result) == 1
        assert result[0]["relation"] == "is blocked by"
        assert result[0]["url"] == "https://jira.corp/jira/browse/TEAM-10"
        assert result[0]["state"] == "open"
        assert result[0]["issueKey"] == "TEAM-10"

    def test_done_status_maps_to_closed(self) -> None:
        """A linked issue with statusCategory 'done' maps to state 'closed'."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
                    "outwardIssue": {
                        "key": "FIX-7",
                        "fields": {
                            "summary": "Done fix",
                            "status": {"statusCategory": {"key": "done"}},
                        },
                    },
                }
            ]
        }
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert result[0]["state"] == "closed"

    def test_duplicate_keys_deduped(self) -> None:
        """Duplicate issue keys are folded."""
        link = {
            "type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
            "outwardIssue": {
                "key": "DUP-1",
                "fields": {"summary": "Dup", "status": {"statusCategory": {"key": "new"}}},
            },
        }
        fields = {"issuelinks": [link, link]}
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert len(result) == 1

    def test_empty_issuelinks(self) -> None:
        """Empty or missing issuelinks returns an empty list."""
        assert source._jira_linked_changes({}, "https://x") == []
        assert source._jira_linked_changes({"issuelinks": []}, "https://x") == []

    def test_malformed_link_skipped(self) -> None:
        """A link with neither inwardIssue nor outwardIssue is skipped."""
        fields = {
            "issuelinks": [
                {"type": {"name": "Bad", "inward": "x", "outward": "y"}},
                "not a dict",
                None,
            ]
        }
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert result == []

    def test_missing_summary_uses_key_as_title(self) -> None:
        """When summary is missing, the issue key is used as the title."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Rel", "inward": "r", "outward": "r"},
                    "outwardIssue": {
                        "key": "NO-SUM-1",
                        "fields": {"status": {"statusCategory": {"key": "new"}}},
                    },
                }
            ]
        }
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert result[0]["title"] == "NO-SUM-1"

    def test_multiple_links(self) -> None:
        """Multiple links are all returned in order."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                    "outwardIssue": {
                        "key": "A-1",
                        "fields": {
                            "summary": "First",
                            "status": {"statusCategory": {"key": "new"}},
                        },
                    },
                },
                {
                    "type": {
                        "name": "Duplicates",
                        "inward": "is duplicated by",
                        "outward": "duplicates",
                    },
                    "inwardIssue": {
                        "key": "B-2",
                        "fields": {
                            "summary": "Second",
                            "status": {"statusCategory": {"key": "done"}},
                        },
                    },
                },
            ]
        }
        result = source._jira_linked_changes(fields, "https://x.atlassian.net")
        assert len(result) == 2
        assert result[0]["issueKey"] == "A-1"
        assert result[0]["relation"] == "blocks"
        assert result[1]["issueKey"] == "B-2"
        assert result[1]["relation"] == "is duplicated by"
        assert result[1]["state"] == "closed"


# --- Jira fix versions as the milestone equivalent ---


class TestJiraFixVersionMilestone:
    """Tests for _jira_fix_versions and _jira_fix_version_milestone."""

    def test_unreleased_version_maps_to_open_milestone(self) -> None:
        """name -> title, releaseDate -> dueOn, unreleased -> open."""
        fields = {
            "fixVersions": [
                {"name": "2.4.0", "releaseDate": "2026-09-30", "released": False},
            ]
        }
        versions = source._jira_fix_versions(fields)
        assert len(versions) == 1
        assert source._jira_fix_version_milestone(versions[0]) == {
            "title": "2.4.0",
            "state": "open",
            "dueOn": "2026-09-30",
        }

    def test_released_version_maps_to_closed(self) -> None:
        """A shipped release is a closed milestone."""
        version = {"name": "2.3.0", "releaseDate": "2026-06-30", "released": True}
        assert source._jira_fix_version_milestone(version)["state"] == "closed"

    def test_archived_version_maps_to_closed(self) -> None:
        """An archived version takes no work, so it is not open."""
        version = {"name": "1.0.0", "released": False, "archived": True}
        assert source._jira_fix_version_milestone(version)["state"] == "closed"

    def test_missing_release_date_keeps_the_name(self) -> None:
        """A version with no release date still carries its target release."""
        version = {"name": "Next", "released": False}
        assert source._jira_fix_version_milestone(version) == {
            "title": "Next",
            "state": "open",
            "dueOn": "",
        }

    def test_locale_formatted_release_date_is_not_used(self) -> None:
        """userReleaseDate is locale-formatted, so only releaseDate feeds dueOn."""
        version = {"name": "3.0", "releaseDate": "2026-12-01", "userReleaseDate": "01/Dec/26"}
        assert source._jira_fix_version_milestone(version)["dueOn"] == "2026-12-01"

    def test_first_usable_version_wins_over_malformed_head(self) -> None:
        """A malformed or nameless leading entry does not hide a usable one."""
        fields = {
            "fixVersions": [
                "not a dict",
                None,
                {"name": "   "},
                {"name": "", "releaseDate": "2026-01-01"},
                {"name": "4.1", "releaseDate": "2026-11-05"},
            ]
        }
        versions = source._jira_fix_versions(fields)
        assert len(versions) == 1
        assert source._jira_fix_version_milestone(versions[0])["title"] == "4.1"

    def test_name_is_trimmed(self) -> None:
        """Surrounding whitespace in a version name is not rendered."""
        version = {"name": "  2.5.0  "}
        assert source._jira_fix_version_milestone(version)["title"] == "2.5.0"

    def test_no_fix_versions_yields_nothing_usable(self) -> None:
        """Missing, empty, and non-list fixVersions all yield no milestone."""
        assert source._jira_fix_versions({}) == []
        assert source._jira_fix_versions({"fixVersions": []}) == []
        assert source._jira_fix_versions({"fixVersions": None}) == []
        assert source._jira_fix_versions({"fixVersions": "1.0"}) == []

    def test_multiple_versions_are_all_returned(self) -> None:
        """The filter keeps every usable version so the caller can flag partial."""
        fields = {
            "fixVersions": [
                {"name": "2.4.0"},
                {"name": "2.5.0"},
            ]
        }
        assert [v["name"] for v in source._jira_fix_versions(fields)] == ["2.4.0", "2.5.0"]

    def test_milestone_keys_match_the_frontend_contract(self) -> None:
        """The mapped shape is exactly IssueMilestone: title, state, dueOn."""
        version = {"name": "2.4.0", "releaseDate": "2026-09-30"}
        assert set(source._jira_fix_version_milestone(version)) == {"title", "state", "dueOn"}


class TestJiraPickFixVersion:
    """Tests for _jira_pick_fix_version."""

    def test_mixed_versions_prefer_the_unreleased_one(self) -> None:
        """A pending release wins over an already-shipped one ahead of it."""
        fix_versions = [
            {"name": "2.3.0", "released": True},
            {"name": "2.4.0", "released": False},
        ]
        chosen = source._jira_pick_fix_version(fix_versions)
        assert chosen is not None
        assert chosen["name"] == "2.4.0"

    def test_all_released_falls_back_to_the_first(self) -> None:
        """A ticket whose versions all shipped still shows one milestone."""
        fix_versions = [
            {"name": "2.2.0", "released": True},
            {"name": "2.3.0", "archived": True},
        ]
        chosen = source._jira_pick_fix_version(fix_versions)
        assert chosen is not None
        assert chosen["name"] == "2.2.0"

    def test_empty_list_yields_none(self) -> None:
        """No usable fix versions means no milestone."""
        assert source._jira_pick_fix_version([]) is None

    def test_archived_is_not_pending(self) -> None:
        """An archived-but-unreleased version does not count as pending."""
        fix_versions = [
            {"name": "1.0.0", "released": False, "archived": True},
            {"name": "1.1.0", "released": False},
        ]
        chosen = source._jira_pick_fix_version(fix_versions)
        assert chosen is not None
        assert chosen["name"] == "1.1.0"


class _JiraFakeContent:
    """Minimal ``StreamReader`` stand-in for the capped-response reader."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _n: int):
        if self._body:
            yield self._body


class _JiraFakeResponse:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self.content = _JiraFakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _JiraRecordingSession:
    """Fake ``aiohttp.ClientSession`` that records the URL it was asked for."""

    def __init__(self, body: bytes, seen: list[str]) -> None:
        self._body = body
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def get(self, url, **_kwargs):
        self._seen.append(url)
        return _JiraFakeResponse(self._body)


def _jira_recording_session(monkeypatch, fields: dict) -> list[str]:
    """Arm a fake Jira endpoint serving *fields*; return the recorded URL list."""
    body = json.dumps({"fields": fields}).encode("utf-8")
    seen: list[str] = []
    monkeypatch.setattr(
        source.aiohttp, "ClientSession", lambda *a, **kw: _JiraRecordingSession(body, seen)
    )
    monkeypatch.setattr(source, "_get_jira_auth", lambda host: ("e@example.com", "tok"))
    return seen


async def _jira_fetch(monkeypatch, fields: dict) -> tuple[dict, list[str]]:
    """Drive ``_fetch_jira_issue`` over a canned payload; return it and the URLs."""
    seen = _jira_recording_session(monkeypatch, fields)
    ref = source.parse_source_url("https://acme.atlassian.net/browse/PROJ-123")
    return await source._fetch_jira_issue(ref), seen


class TestJiraFixVersionInPayload:
    """End-to-end pins: the field is requested and reaches the payload."""

    @pytest.mark.asyncio
    async def test_fix_versions_is_requested_from_the_rest_api(self, monkeypatch) -> None:
        """An unrequested field is absent from the response, so it must be asked for."""
        _issue, seen = await _jira_fetch(monkeypatch, {"summary": "x"})
        assert len(seen) == 1
        assert "fixVersions" in seen[0]

    @pytest.mark.asyncio
    async def test_milestone_carries_the_fix_version(self, monkeypatch) -> None:
        """The Issues panel milestone chip is fed by the first fix version."""
        issue, _seen = await _jira_fetch(
            monkeypatch,
            {
                "summary": "Ship it",
                "fixVersions": [{"name": "2.4.0", "releaseDate": "2026-09-30"}],
            },
        )
        assert issue["milestone"] == {
            "title": "2.4.0",
            "state": "open",
            "dueOn": "2026-09-30",
        }
        assert "fix versions" not in issue["partialSections"]

    @pytest.mark.asyncio
    async def test_multiple_fix_versions_are_declared_partial(self, monkeypatch) -> None:
        """Dropping the tail of a multi-release ticket is disclosed, not silent."""
        issue, _seen = await _jira_fetch(
            monkeypatch,
            {
                "summary": "Ship it twice",
                "fixVersions": [{"name": "2.4.0"}, {"name": "2.5.0"}],
            },
        )
        assert issue["milestone"]["title"] == "2.4.0"
        assert "fix versions" in issue["partialSections"]

    @pytest.mark.asyncio
    async def test_pending_version_wins_over_a_shipped_one(self, monkeypatch) -> None:
        """A released version ahead of a pending one does not steal the chip."""
        issue, _seen = await _jira_fetch(
            monkeypatch,
            {
                "summary": "Fixed in the next release",
                "fixVersions": [
                    {"name": "2.3.0", "releaseDate": "2026-06-30", "released": True},
                    {"name": "2.4.0", "releaseDate": "2026-09-30", "released": False},
                ],
            },
        )
        assert issue["milestone"] == {
            "title": "2.4.0",
            "state": "open",
            "dueOn": "2026-09-30",
        }
        assert "fix versions" in issue["partialSections"]

    @pytest.mark.asyncio
    async def test_no_fix_version_leaves_milestone_null(self, monkeypatch) -> None:
        """A ticket with no fix version keeps the panel's 'no milestone' state."""
        issue, _seen = await _jira_fetch(monkeypatch, {"summary": "Unscheduled"})
        assert issue["milestone"] is None
        assert "fix versions" not in issue["partialSections"]
