"""Tests for operator-registered OAuth clients (``connections/oauth_clients``).

Pure-function coverage: the three-source precedence for the client id and the
secret, the two validators, the dashboard view (which must never carry a secret
value), the surgical wire-shape writer, and the slug+URL binding that keeps an
operator's client off a stranger's endpoint. No config file, no vault, no HTTP:
every input is passed in explicitly.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from kiro_crew.connections import get_provider
from kiro_crew.connections.oauth_clients import (
    CONFIG_CLIENTS_KEY,
    CONFIG_ROOT_KEY,
    ResolvedOAuthClient,
    apply_preregistered_oauth_client,
    client_id_env_name,
    client_secret_env_name,
    client_secret_name,
    managed_client_secret_names,
    oauth_client_view,
    provider_for_server,
    resolve_oauth_client,
    validate_client_id,
    validate_client_secret,
)
from kiro_crew.mcp_utils import KIRO_OAUTH_KEY

GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
GITHUB_REDIRECT_URI = "http://127.0.0.1:48101/callback"
ENV_ID = "KIROCREW_CONNECTIONS_GITHUB_CLIENT_ID"
ENV_SECRET = "KIROCREW_CONNECTIONS_GITHUB_CLIENT_SECRET"
VAULT_NAME = "CONNECTIONS_GITHUB_CLIENT_SECRET"


def _provider(slug: str = "github", *, confidential: bool = True, client_id: str | None = None):
    """A pre-registered provider copied from the shipped registry, then adjusted."""
    item = deepcopy(get_provider(slug))
    assert item is not None
    item["auth"] = dict(item["auth"])
    item["auth"]["confidential"] = confidential
    if client_id is None:
        item.pop("client_id", None)
    else:
        item["client_id"] = client_id
    return item


def _config(slug: str, client_id: object) -> dict:
    return {CONFIG_ROOT_KEY: {CONFIG_CLIENTS_KEY: {slug: {"client_id": client_id}}}}


class _Held:
    """The shape ``SecretVault.get`` hands back: a value behind ``reveal()``."""

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value


class _Vault:
    def __init__(self, entries: dict[str, object] | None = None) -> None:
        self.entries = entries or {}
        self.asked: list[str] = []

    def get(self, name: str):
        self.asked.append(name)
        return self.entries.get(name)


class _BrokenVault:
    def get(self, name: str):
        raise OSError("vault unreadable")


# ── naming ──


@pytest.mark.parametrize(
    ("slug", "token"),
    [("github", "GITHUB"), ("google-drive", "GOOGLE_DRIVE"), ("microsoft-365", "MICROSOFT_365")],
)
def test_env_and_vault_names_upper_case_the_slug_and_swap_hyphens(slug, token):
    assert client_id_env_name(slug) == f"KIROCREW_CONNECTIONS_{token}_CLIENT_ID"
    assert client_secret_env_name(slug) == f"KIROCREW_CONNECTIONS_{token}_CLIENT_SECRET"
    assert client_secret_name(slug) == f"CONNECTIONS_{token}_CLIENT_SECRET"


def test_managed_secret_names_cover_every_shipped_preregistered_provider():
    names = managed_client_secret_names()
    assert names["CONNECTIONS_GITHUB_CLIENT_SECRET"] == "github"
    assert names["CONNECTIONS_ASANA_CLIENT_SECRET"] == "asana"
    assert set(names.values()) == {"github", "asana"}


# ── validators ──


def test_client_id_is_trimmed_and_returned():
    assert validate_client_id("  Iv1.abc123  ") == "Iv1.abc123"
    assert validate_client_id("Iv1.abc123") == "Iv1.abc123"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "has space",
        "tab\tinside",
        "ctrl\x01char",
        "\x7fdel",
        "caf\u00e9",
        "a" * 513,
        None,
        42,
        ["Iv1.abc"],
        {"client_id": "x"},
    ],
)
def test_unusable_client_ids_are_refused(bad):
    assert validate_client_id(bad) is None


def test_client_id_length_bound_is_inclusive():
    assert validate_client_id("a" * 512) == "a" * 512
    assert validate_client_id("a" * 513) is None


def test_client_id_accepts_the_full_printable_ascii_range():
    assert validate_client_id("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~") is not None


def test_client_secret_is_returned_verbatim_not_trimmed():
    """A vendor secret may carry surrounding whitespace; trimming would corrupt it."""
    assert validate_client_secret("  s3cr3t  ") == "  s3cr3t  "
    assert validate_client_secret("a\tb") == "a\tb"
    assert validate_client_secret("caf\u00e9") == "caf\u00e9"


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "\t\n", "line\nbreak", "carriage\rreturn", "nul\x00byte", "x" * 4097, None, 7, []],
)
def test_unusable_client_secrets_are_refused(bad):
    assert validate_client_secret(bad) is None


def test_client_secret_length_bound_is_inclusive():
    assert validate_client_secret("x" * 4096) == "x" * 4096


# ── resolve_oauth_client: client id precedence ──


def test_env_client_id_outranks_config_and_registry():
    provider = _provider(confidential=False, client_id="from-registry")
    resolved = resolve_oauth_client(
        provider,
        config=_config("github", "from-config"),
        vault=_Vault(),
        environ={ENV_ID: "from-env"},
    )
    assert resolved is not None
    assert (resolved.client_id, resolved.client_id_source) == ("from-env", "env")


def test_config_client_id_outranks_registry_when_env_is_unset():
    provider = _provider(confidential=False, client_id="from-registry")
    resolved = resolve_oauth_client(
        provider, config=_config("github", "from-config"), vault=_Vault(), environ={}
    )
    assert resolved is not None
    assert (resolved.client_id, resolved.client_id_source) == ("from-config", "config")


def test_registry_client_id_is_the_last_resort():
    provider = _provider(confidential=False, client_id="from-registry")
    resolved = resolve_oauth_client(provider, config={}, vault=_Vault(), environ={})
    assert resolved is not None
    assert (resolved.client_id, resolved.client_id_source) == ("from-registry", "registry")


def test_an_unusable_env_client_id_falls_through_to_config():
    provider = _provider(confidential=False)
    resolved = resolve_oauth_client(
        provider,
        config=_config("github", "from-config"),
        vault=_Vault(),
        environ={ENV_ID: "has a space"},
    )
    assert resolved is not None
    assert resolved.client_id_source == "config"


def test_env_client_id_is_trimmed_on_the_way_in():
    provider = _provider(confidential=False)
    resolved = resolve_oauth_client(
        provider, config=None, vault=_Vault(), environ={ENV_ID: "  padded  "}
    )
    assert resolved is not None
    assert resolved.client_id == "padded"


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {CONFIG_ROOT_KEY: "not-a-mapping"},
        {CONFIG_ROOT_KEY: {CONFIG_CLIENTS_KEY: []}},
        {CONFIG_ROOT_KEY: {CONFIG_CLIENTS_KEY: {"github": "not-a-record"}}},
        {CONFIG_ROOT_KEY: {CONFIG_CLIENTS_KEY: {"github": {"client_id": "   "}}}},
        {CONFIG_ROOT_KEY: {CONFIG_CLIENTS_KEY: {"asana": {"client_id": "other-slug"}}}},
    ],
)
def test_no_client_id_anywhere_resolves_to_none(config):
    provider = _provider(confidential=False)
    assert resolve_oauth_client(provider, config=config, vault=_Vault(), environ={}) is None


def test_a_dcr_provider_never_resolves_even_with_values_present():
    notion = deepcopy(get_provider("notion"))
    assert notion is not None and "auth" not in notion
    resolved = resolve_oauth_client(
        notion,
        config=_config("notion", "cfg"),
        vault=_Vault({"CONNECTIONS_NOTION_CLIENT_SECRET": _Held("s")}),
        environ={"KIROCREW_CONNECTIONS_NOTION_CLIENT_ID": "env"},
    )
    assert resolved is None


# ── resolve_oauth_client: secret precedence ──


def test_env_secret_outranks_the_vault():
    vault = _Vault({VAULT_NAME: _Held("from-vault")})
    resolved = resolve_oauth_client(
        _provider(),
        config=_config("github", "cid"),
        vault=vault,
        environ={ENV_SECRET: "from-env"},
    )
    assert resolved is not None
    assert (resolved.client_secret, resolved.client_secret_source) == ("from-env", "env")


def test_vault_secret_is_used_when_env_is_unset():
    vault = _Vault({VAULT_NAME: _Held("from-vault")})
    resolved = resolve_oauth_client(
        _provider(), config=_config("github", "cid"), vault=vault, environ={}
    )
    assert resolved is not None
    assert (resolved.client_secret, resolved.client_secret_source) == ("from-vault", "vault")
    assert vault.asked == [VAULT_NAME]


def test_vault_secret_is_read_verbatim():
    vault = _Vault({VAULT_NAME: _Held("  spaced  ")})
    resolved = resolve_oauth_client(
        _provider(), config=_config("github", "cid"), vault=vault, environ={}
    )
    assert resolved is not None
    assert resolved.client_secret == "  spaced  "


def test_a_vault_returning_a_bare_string_is_accepted():
    """Duck-typed: a dict-backed stub without ``reveal()`` still works."""
    vault = _Vault({VAULT_NAME: "plain"})
    resolved = resolve_oauth_client(
        _provider(), config=_config("github", "cid"), vault=vault, environ={}
    )
    assert resolved is not None
    assert resolved.client_secret == "plain"


def test_confidential_client_without_a_secret_resolves_to_none():
    resolved = resolve_oauth_client(
        _provider(confidential=True), config=_config("github", "cid"), vault=_Vault(), environ={}
    )
    assert resolved is None


def test_confidential_client_with_an_unusable_env_secret_and_empty_vault_is_none():
    resolved = resolve_oauth_client(
        _provider(confidential=True),
        config=_config("github", "cid"),
        vault=_Vault(),
        environ={ENV_SECRET: "multi\nline"},
    )
    assert resolved is None


def test_a_raising_vault_reads_as_no_secret():
    resolved = resolve_oauth_client(
        _provider(confidential=True),
        config=_config("github", "cid"),
        vault=_BrokenVault(),
        environ={},
    )
    assert resolved is None


def test_a_none_vault_reads_as_no_secret():
    resolved = resolve_oauth_client(
        _provider(confidential=True), config=_config("github", "cid"), vault=None, environ={}
    )
    assert resolved is None


def test_public_client_without_a_secret_resolves_with_secret_none():
    resolved = resolve_oauth_client(
        _provider(confidential=False), config=_config("github", "cid"), vault=_Vault(), environ={}
    )
    assert resolved == ResolvedOAuthClient(
        slug="github",
        client_id="cid",
        client_id_source="config",
        client_secret=None,
        client_secret_source=None,
        redirect_uri=GITHUB_REDIRECT_URI,
    )


def test_public_client_still_carries_a_secret_when_one_is_stored():
    vault = _Vault({VAULT_NAME: _Held("optional")})
    resolved = resolve_oauth_client(
        _provider(confidential=False), config=_config("github", "cid"), vault=vault, environ={}
    )
    assert resolved is not None
    assert resolved.client_secret == "optional"


def test_resolved_redirect_uri_is_derived_from_the_registry():
    resolved = resolve_oauth_client(
        _provider(confidential=False), config=_config("github", "cid"), vault=_Vault(), environ={}
    )
    assert resolved is not None
    assert resolved.redirect_uri == GITHUB_REDIRECT_URI


def test_hyphenated_slug_reads_its_own_env_and_vault_names():
    provider = _provider(confidential=True)
    provider["slug"] = "google-drive"
    vault = _Vault({"CONNECTIONS_GOOGLE_DRIVE_CLIENT_SECRET": _Held("gd-secret")})
    resolved = resolve_oauth_client(
        provider,
        config=None,
        vault=vault,
        environ={"KIROCREW_CONNECTIONS_GOOGLE_DRIVE_CLIENT_ID": "gd-id"},
    )
    assert resolved is not None
    assert resolved.slug == "google-drive"
    assert (resolved.client_id, resolved.client_id_source) == ("gd-id", "env")
    assert (resolved.client_secret, resolved.client_secret_source) == ("gd-secret", "vault")
    assert vault.asked == ["CONNECTIONS_GOOGLE_DRIVE_CLIENT_SECRET"]


# ── oauth_client_view ──


def _flat_values(view: dict) -> list[str]:
    return [str(v) for v in view.values()]


def test_view_never_carries_a_secret_value():
    view = oauth_client_view(
        _provider(),
        config=_config("github", "cid"),
        vault_names={VAULT_NAME},
        environ={ENV_SECRET: "ENV-SECRET-VALUE"},
    )
    assert "ENV-SECRET-VALUE" not in " ".join(_flat_values(view))
    assert set(view) == {
        "slug",
        "confidential",
        "redirect_uri",
        "registration_guide",
        "client_id",
        "client_id_source",
        "client_secret_set",
        "client_secret_source",
        "configured",
    }
    assert view["client_secret_set"] is True
    assert view["client_secret_source"] == "env"


def test_view_reports_static_provider_facts():
    view = oauth_client_view(_provider(), config=None, vault_names=set(), environ={})
    assert view["slug"] == "github"
    assert view["confidential"] is True
    assert view["redirect_uri"] == GITHUB_REDIRECT_URI
    assert view["registration_guide"] == "oauth-app-registration/github.md"


def test_view_with_nothing_stored_is_unconfigured():
    view = oauth_client_view(_provider(), config=None, vault_names=set(), environ={})
    assert view["client_id"] is None
    assert view["client_id_source"] is None
    assert view["client_secret_set"] is False
    assert view["client_secret_source"] is None
    assert view["configured"] is False


def test_view_client_id_precedence_matches_resolve():
    provider = _provider(client_id="from-registry")
    env_view = oauth_client_view(
        provider,
        config=_config("github", "from-config"),
        vault_names=set(),
        environ={ENV_ID: "from-env"},
    )
    cfg_view = oauth_client_view(
        provider, config=_config("github", "from-config"), vault_names=set(), environ={}
    )
    reg_view = oauth_client_view(provider, config=None, vault_names=set(), environ={})
    assert (env_view["client_id"], env_view["client_id_source"]) == ("from-env", "env")
    assert (cfg_view["client_id"], cfg_view["client_id_source"]) == ("from-config", "config")
    assert (reg_view["client_id"], reg_view["client_id_source"]) == ("from-registry", "registry")


def test_view_secret_source_prefers_env_over_vault_name():
    view = oauth_client_view(
        _provider(),
        config=None,
        vault_names={VAULT_NAME},
        environ={ENV_SECRET: "x"},
    )
    assert view["client_secret_source"] == "env"


def test_view_vault_name_presence_marks_the_secret_set():
    view = oauth_client_view(_provider(), config=None, vault_names={VAULT_NAME}, environ={})
    assert view["client_secret_set"] is True
    assert view["client_secret_source"] == "vault"


def test_view_ignores_vault_names_belonging_to_other_providers():
    view = oauth_client_view(
        _provider(), config=None, vault_names={"CONNECTIONS_ASANA_CLIENT_SECRET"}, environ={}
    )
    assert view["client_secret_set"] is False


def test_view_ignores_an_unusable_env_secret():
    view = oauth_client_view(
        _provider(), config=None, vault_names=set(), environ={ENV_SECRET: "bad\nsecret"}
    )
    assert view["client_secret_set"] is False


@pytest.mark.parametrize(
    ("confidential", "has_id", "has_secret", "configured"),
    [
        (True, True, True, True),
        (True, True, False, False),
        (True, False, True, False),
        (True, False, False, False),
        (False, True, True, True),
        (False, True, False, True),
        (False, False, True, False),
        (False, False, False, False),
    ],
)
def test_view_configured_needs_an_id_and_for_confidential_a_secret(
    confidential, has_id, has_secret, configured
):
    view = oauth_client_view(
        _provider(confidential=confidential),
        config=_config("github", "cid") if has_id else None,
        vault_names={VAULT_NAME} if has_secret else set(),
        environ={},
    )
    assert view["configured"] is configured


# ── apply_preregistered_oauth_client ──


def _resolved(secret: str | None = "s3cr3t") -> ResolvedOAuthClient:
    return ResolvedOAuthClient(
        slug="github",
        client_id="operator-id",
        client_id_source="config",
        client_secret=secret,
        client_secret_source="vault" if secret is not None else None,
        redirect_uri=GITHUB_REDIRECT_URI,
    )


def test_apply_writes_the_three_owned_keys_in_wire_shape():
    entry = {"url": GITHUB_MCP_URL}
    out = apply_preregistered_oauth_client(entry, _resolved())
    assert out[KIRO_OAUTH_KEY] == {
        "clientId": "operator-id",
        "clientSecret": "s3cr3t",
        "redirectUri": GITHUB_REDIRECT_URI,
    }
    assert out["url"] == GITHUB_MCP_URL


def test_apply_is_surgical_on_the_oauth_block():
    entry = {
        "url": GITHUB_MCP_URL,
        "scopes": ["read:user"],
        KIRO_OAUTH_KEY: {
            "issuer": "https://github.com/login/oauth",
            "scopes": ["read:user"],
            "clientId": "stale-store-id",
        },
    }
    out = apply_preregistered_oauth_client(entry, _resolved())
    oauth = out[KIRO_OAUTH_KEY]
    assert oauth["issuer"] == "https://github.com/login/oauth"
    assert oauth["scopes"] == ["read:user"]
    assert oauth["clientId"] == "operator-id"  # the operator's record outranks the store
    assert oauth["clientSecret"] == "s3cr3t"
    assert oauth["redirectUri"] == GITHUB_REDIRECT_URI
    assert out["scopes"] == ["read:user"]


def test_apply_pops_a_stale_client_secret_for_a_public_client():
    entry = {
        "url": GITHUB_MCP_URL,
        KIRO_OAUTH_KEY: {"issuer": "https://github.com/login/oauth", "clientSecret": "old"},
    }
    out = apply_preregistered_oauth_client(entry, _resolved(secret=None))
    oauth = out[KIRO_OAUTH_KEY]
    assert "clientSecret" not in oauth
    assert oauth["issuer"] == "https://github.com/login/oauth"
    assert oauth["clientId"] == "operator-id"
    assert oauth["redirectUri"] == GITHUB_REDIRECT_URI


def test_apply_does_not_mutate_its_input():
    oauth = {"issuer": "https://github.com/login/oauth", "clientId": "stale"}
    entry = {"url": GITHUB_MCP_URL, KIRO_OAUTH_KEY: oauth}
    out = apply_preregistered_oauth_client(entry, _resolved())
    assert entry[KIRO_OAUTH_KEY] is oauth
    assert oauth == {"issuer": "https://github.com/login/oauth", "clientId": "stale"}
    assert out is not entry
    assert out[KIRO_OAUTH_KEY] is not oauth


def test_apply_replaces_a_non_object_oauth_value():
    entry = {"url": GITHUB_MCP_URL, KIRO_OAUTH_KEY: "garbage"}
    out = apply_preregistered_oauth_client(entry, _resolved())
    assert out[KIRO_OAUTH_KEY]["clientId"] == "operator-id"


# ── provider_for_server ──


def test_provider_for_server_requires_slug_and_url_to_match():
    match = provider_for_server("github", {"url": GITHUB_MCP_URL})
    assert match is not None and match["slug"] == "github"


@pytest.mark.parametrize(
    "entry",
    [
        {"url": "https://api.githubcopilot.com/mcp"},  # trailing slash differs
        {"url": "https://mcp.example.com/mcp"},
        {"url": "HTTPS://API.GITHUBCOPILOT.COM/mcp/"},
        {},
        None,
        "https://api.githubcopilot.com/mcp/",
    ],
)
def test_provider_for_server_refuses_a_same_name_server_pointing_elsewhere(entry):
    assert provider_for_server("github", entry) is None


def test_provider_for_server_refuses_a_dcr_provider_even_on_its_own_url():
    notion = get_provider("notion")
    assert notion is not None
    assert provider_for_server("notion", {"url": notion["mcp_url"]}) is None


def test_provider_for_server_refuses_an_unknown_name():
    assert provider_for_server("not-a-provider", {"url": GITHUB_MCP_URL}) is None
    assert provider_for_server("", {"url": GITHUB_MCP_URL}) is None


def test_provider_for_server_returns_a_copy_not_the_registry_object():
    first = provider_for_server("github", {"url": GITHUB_MCP_URL})
    second = provider_for_server("github", {"url": GITHUB_MCP_URL})
    assert first is not None and second is not None
    assert first == second and first is not second


# ── strip_preregistered_oauth_client ──


def test_strip_removes_only_the_three_owned_keys_and_keeps_siblings():
    from kiro_crew.connections.oauth_clients import strip_preregistered_oauth_client

    entry = {
        "url": GITHUB_MCP_URL,
        "oauth": {
            "clientId": "old",
            "clientSecret": "retired",
            "redirectUri": "http://127.0.0.1:48101/callback",
            "issuer": "https://github.com/login/oauth",
        },
    }
    out = strip_preregistered_oauth_client(entry)
    assert out["oauth"] == {"issuer": "https://github.com/login/oauth"}
    # The input is not mutated.
    assert entry["oauth"]["clientSecret"] == "retired"


def test_strip_drops_the_oauth_mapping_when_nothing_is_left():
    from kiro_crew.connections.oauth_clients import strip_preregistered_oauth_client

    out = strip_preregistered_oauth_client(
        {"url": GITHUB_MCP_URL, "oauth": {"clientId": "old", "clientSecret": "retired"}}
    )
    assert "oauth" not in out


def test_strip_is_a_no_op_without_an_oauth_block():
    from kiro_crew.connections.oauth_clients import strip_preregistered_oauth_client

    assert strip_preregistered_oauth_client({"url": GITHUB_MCP_URL}) == {"url": GITHUB_MCP_URL}


# ── agent-config read redaction (mcp_utils) ──


def test_redaction_masks_every_client_secret_and_nothing_else():
    from kiro_crew.mcp_utils import OAUTH_CLIENT_SECRET_REDACTED, redact_oauth_client_secrets

    spec = {
        "name": "kirocrew",
        "mcpServers": {
            "github": {"url": GITHUB_MCP_URL, "oauth": {"clientId": "id", "clientSecret": "s3"}},
            "notion": {"url": "https://mcp.notion.com/mcp"},
            "stdio": {"command": "x", "env": {"TOKEN": "keep"}},
        },
    }
    out = redact_oauth_client_secrets(spec)
    assert out["mcpServers"]["github"]["oauth"] == {
        "clientId": "id",
        "clientSecret": OAUTH_CLIENT_SECRET_REDACTED,
    }
    assert out["mcpServers"]["notion"] == spec["mcpServers"]["notion"]
    assert out["mcpServers"]["stdio"] == spec["mcpServers"]["stdio"]
    assert "s3" not in repr(out)
    # Source untouched.
    assert spec["mcpServers"]["github"]["oauth"]["clientSecret"] == "s3"


def test_restoring_a_marker_takes_the_on_disk_value_or_drops_it():
    from kiro_crew.mcp_utils import (
        OAUTH_CLIENT_SECRET_REDACTED,
        redact_oauth_client_secrets,
        restore_redacted_oauth_client_secrets,
    )

    on_disk = {"mcpServers": {"github": {"url": GITHUB_MCP_URL, "oauth": {"clientSecret": "s3"}}}}
    round_tripped = redact_oauth_client_secrets(on_disk)
    # A new server whose marker has no on-disk counterpart loses the key rather
    # than gaining the marker string as its secret.
    round_tripped["mcpServers"]["asana"] = {
        "url": "https://mcp.asana.com/v2/mcp",
        "oauth": {"clientSecret": OAUTH_CLIENT_SECRET_REDACTED, "clientId": "a"},
    }
    restored = restore_redacted_oauth_client_secrets(round_tripped, on_disk)
    assert restored["mcpServers"]["github"]["oauth"]["clientSecret"] == "s3"
    assert restored["mcpServers"]["asana"]["oauth"] == {"clientId": "a"}
    # A real (non-marker) value written by the editor is left alone.
    edited = {"mcpServers": {"github": {"oauth": {"clientSecret": "rotated"}}}}
    assert restore_redacted_oauth_client_secrets(edited, on_disk) == edited


def test_restoring_a_marker_refuses_when_the_url_moved():
    """A secret is bound to the endpoint it was issued for: an entry whose ``url``
    was edited alongside the marker drops the key instead of inheriting the secret
    stored for the old endpoint."""
    from kiro_crew.mcp_utils import (
        OAUTH_CLIENT_SECRET_REDACTED,
        restore_redacted_oauth_client_secrets,
    )

    on_disk = {"mcpServers": {"github": {"url": GITHUB_MCP_URL, "oauth": {"clientSecret": "s3"}}}}
    moved = {
        "mcpServers": {
            "github": {
                "url": "https://attacker.example/mcp",
                "oauth": {"clientSecret": OAUTH_CLIENT_SECRET_REDACTED, "clientId": "id"},
            }
        }
    }
    restored = restore_redacted_oauth_client_secrets(moved, on_disk)
    assert restored["mcpServers"]["github"]["oauth"] == {"clientId": "id"}


# ── agent._apply_operator_oauth_client: ownership gate ──


def test_rebuild_leaves_an_unmanaged_hand_authored_client_untouched(monkeypatch):
    """A user-owned ``github`` server at the provider URL keeps its own client.

    The rebuild merges onto the previous spec, so for a server defined only there
    (``kiro-cli mcp add --agent kirocrew``) the hand-authored ``oauth`` block is
    the only copy -- and, GitHub refusing DCR, the only way that server can
    authenticate at all. Unmanaged means verbatim, for apply and strip alike; the
    vault must not even be opened.
    """
    from kiro_crew import agent

    def _must_not_resolve(*_a, **_k):  # pragma: no cover - the assertion
        raise AssertionError("an unmanaged entry must not consult the operator client")

    monkeypatch.setattr(
        "kiro_crew.connections.oauth_clients.resolve_oauth_client", _must_not_resolve
    )
    hand_authored = {
        "url": GITHUB_MCP_URL,
        "oauth": {
            "clientId": "users-own-app",
            "clientSecret": "users-own-secret",
            "redirectUri": "http://127.0.0.1:9000/callback",
        },
    }
    out = agent._apply_operator_oauth_client("github", deepcopy(hand_authored), managed=False)
    assert out == hand_authored


def test_rebuild_strips_a_cleared_client_only_from_a_managed_entry(monkeypatch):
    from kiro_crew import agent

    monkeypatch.setattr(
        "kiro_crew.connections.oauth_clients.resolve_oauth_client", lambda *a, **k: None
    )
    monkeypatch.setattr(agent, "_load_json", lambda *_a, **_k: {})
    monkeypatch.setattr("kiro_crew.secrets.SecretVault", lambda *_a, **_k: object())
    stale = {
        "url": GITHUB_MCP_URL,
        "oauth": {"clientId": "retired", "clientSecret": "retired", "issuer": "https://i"},
    }
    out = agent._apply_operator_oauth_client("github", deepcopy(stale), managed=True)
    assert out == {"url": GITHUB_MCP_URL, "oauth": {"issuer": "https://i"}}


def test_rebuild_binds_the_client_at_write_time_not_at_the_server_pass(tmp_path, monkeypatch):
    """A rotation that lands between the server pass and the spec write wins.

    Two rebuilds are not serialized against each other, so binding the client in
    the server pass would let "resolve old secret -> rotation commits new secret
    -> stale rebuild writes" re-emit a retired secret. The client is therefore
    read inside the locked write section. Simulated here by flipping the vault's
    answer when the spec-write lock is taken: the written spec must carry NEW.
    """
    import contextlib
    import json

    from kiro_crew import agent as agent_mod
    from kiro_crew.apps import bridges
    from kiro_crew.connections import oauth_clients as oc

    project = tmp_path / "project" / "agents"
    project.mkdir(parents=True)
    (project / "defaults.json").write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    (project / "prompt.md").write_text("prompt", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # The dashboard store owns `github`, so the entry is managed.
    (home / "mcp.json").write_text(
        json.dumps({"mcpServers": {"github": {"url": GITHUB_MCP_URL}}}), encoding="utf-8"
    )
    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    spec_path = kiro_dir / "kirocrew.json"
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "absent-kiro.json")
    monkeypatch.setattr(agent_mod, "_CC_MCP_JSON", tmp_path / "absent-cc.json")
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: spec_path)

    vault = {"secret": "OLD"}
    monkeypatch.setattr(
        oc,
        "resolve_oauth_client",
        lambda *_a, **_k: ResolvedOAuthClient(
            slug="github",
            client_id="app-id",
            client_id_source="config",
            client_secret=vault["secret"],
            client_secret_source="vault",
            redirect_uri=GITHUB_REDIRECT_URI,
        ),
    )

    @contextlib.contextmanager
    def _lock_then_rotate():
        # The rotation commits its new secret just as this rebuild takes the
        # spec-write lock -- after the server pass has already run.
        vault["secret"] = "NEW"
        yield

    monkeypatch.setattr(bridges, "_mcp_lock", _lock_then_rotate)

    agent_mod.rebuild_agent_config(refresh_forks=False)

    written = json.loads(spec_path.read_text(encoding="utf-8"))
    assert written["mcpServers"]["github"]["oauth"]["clientSecret"] == "NEW"


def test_rebuild_strips_the_client_from_a_managed_provider_whose_url_moved(monkeypatch):
    """A managed `github` whose URL does not match the registry loses the client.

    The previous render carried the operator's client for the registry endpoint;
    the rebuild merges onto that render, so the URL edit alone would ship the
    old secret to the replacement endpoint. The client is bound to the endpoint
    it was registered for, so a moved URL retires it from this entry.
    """
    from kiro_crew import agent

    def _must_not_resolve(*_a, **_k):  # pragma: no cover - the assertion
        raise AssertionError("a moved URL must not consult the operator client")

    monkeypatch.setattr(
        "kiro_crew.connections.oauth_clients.resolve_oauth_client", _must_not_resolve
    )
    moved = {
        "url": "https://mcp.example.test/github",
        "oauth": {"clientId": "op", "clientSecret": "op-secret", "issuer": "https://i"},
    }
    out = agent._apply_operator_oauth_client("github", deepcopy(moved), managed=True)
    assert out == {"url": "https://mcp.example.test/github", "oauth": {"issuer": "https://i"}}
    # A managed name that is not a Connections provider is not this module's business.
    other = {"url": "https://x/mcp", "oauth": {"clientSecret": "mine"}}
    assert agent._apply_operator_oauth_client("mine", deepcopy(other), managed=True) == other


# ── dashboard/handlers/mcp.py: the rendered spec is a projection, not a source ──


def test_find_server_spec_strips_the_projected_client_from_the_rendered_spec(tmp_path, monkeypatch):
    """A scope toggle copying the rendered `github` entry must not carry the secret."""
    import json

    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "kirocrew.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "url": GITHUB_MCP_URL,
                        "oauth": {
                            "clientId": "op",
                            "clientSecret": "op-secret",
                            "redirectUri": GITHUB_REDIRECT_URI,
                            "issuer": "https://i",
                        },
                        "oauthScopes": ["repo"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_handlers, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(mcp_handlers, "_read_agent_spec", lambda p, **_k: json.loads(p.read_text()))

    found = mcp_handlers._find_server_spec_anywhere("github")
    assert found == {
        "url": GITHUB_MCP_URL,
        "oauth": {"issuer": "https://i"},
        "oauthScopes": ["repo"],
    }


def test_scrub_removes_a_projected_client_copy_from_every_scope(tmp_path, monkeypatch):
    """Copies made before the strip-on-copy rule are purged on the next mutation."""
    import json

    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    store = tmp_path / "mcp.json"
    kiro_global = tmp_path / "kiro-mcp.json"
    projected = {
        "url": GITHUB_MCP_URL,
        "oauth": {"clientId": "op", "clientSecret": "op-secret", "issuer": "https://i"},
    }
    store.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": deepcopy(projected),
                    # Same name, different endpoint: a stranger's server, untouched.
                    "other": {"url": "https://x/mcp", "oauth": {"clientSecret": "mine"}},
                }
            }
        ),
        encoding="utf-8",
    )
    kiro_global.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {"url": "https://elsewhere/mcp", "oauth": {"clientSecret": "mine"}}
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_handlers, "_kirocrew_mcp_json", lambda: store)
    monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", kiro_global)
    monkeypatch.setattr(mcp_handlers, "_extra_mcp_scopes", lambda: [])

    actions = mcp_handlers._scrub_preregistered_oauth_copies("github")

    assert actions == {"kirocrew": "scrubbed", "kiroGlobal": "noop"}
    after = json.loads(store.read_text(encoding="utf-8"))["mcpServers"]
    assert after["github"] == {"url": GITHUB_MCP_URL, "oauth": {"issuer": "https://i"}}
    assert after["other"] == {"url": "https://x/mcp", "oauth": {"clientSecret": "mine"}}
    # A same-named entry at another endpoint keeps its own client.
    assert json.loads(kiro_global.read_text(encoding="utf-8"))["mcpServers"]["github"]["oauth"] == {
        "clientSecret": "mine"
    }


def test_scrub_refuses_to_call_an_unreadable_scope_clean(tmp_path, monkeypatch):
    """A scope that exists but cannot be parsed may still hold the secret: raise."""
    import json

    import pytest

    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    store = tmp_path / "mcp.json"
    store.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(mcp_handlers, "_kirocrew_mcp_json", lambda: store)
    monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", tmp_path / "absent.json")
    monkeypatch.setattr(mcp_handlers, "_extra_mcp_scopes", lambda: [])

    with pytest.raises(json.JSONDecodeError):
        mcp_handlers._scrub_preregistered_oauth_copies("github")
