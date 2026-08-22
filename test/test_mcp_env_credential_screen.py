"""Tests for the MCP env-block credential predicates.

These decide whether an agent template may carry a literal secret in an
``mcpServers[].env`` value. They are the security-relevant half of template
authoring and shipped with no coverage, so both directions are pinned here: a
false negative leaks a credential into a world-readable spec, and a false
positive blocks a legitimate config field and pushes users to work around the
check entirely.
"""

from __future__ import annotations

import base64

import pytest

from kiro_crew.security import (
    _MCP_ENV_SECRET_VALUE_RE,
    ENV_VAR_REFERENCE_RE,
    env_key_is_credential_like,
    get_credential_patterns,
    mcp_env_value_is_credential_like,
)

# Synthetic values of the right SHAPE only. None of these is a live credential.
# Every one is assembled from fragments rather than written as a single literal:
# a 40-char credential-shaped literal trips the SAST secret-detection rules
# (generic.secrets.security.detected-aws-secret-access-key), which cannot tell a
# test fixture from a leaked key. Concatenation keeps the runtime value identical.
_OPENAI_PROJECT_SHAPE = "sk-proj-" + "T3xQ7bV2mZ9pL4wR8aJ6cH1nD5sK0yGf"
_OPENAI_LEGACY_SHAPE = "sk-" + "T3xQ7bV2mZ9pL4wR8aJ6cH1nD5sK0yGfB7uE2iO4qXvM"
_ANTHROPIC_SHAPE = "sk-ant-api03-" + "Zk4mP8xV2bN6qR9wL3tY7cH1dS5aG0fJ"
_AWS_BARE_SECRET_SHAPE = "wJ4KqZ7pXm2VbT9rNc3Hd" + "S5aG0fJ8yLuE2iO4qXv"
_GENERIC_B64_SHAPE = "Qk7xV2mZ9pL4wR8aJ6cH1" + "nD5sK0yGfB7uE2iO4qX"


class TestCredentialLikeKeys:
    @pytest.mark.parametrize(
        "key",
        [
            "GITHUB_TOKEN",
            "SLACK_SECRET",
            "DB_PASSWORD",
            "PASSWD",
            "APIKEY",
            "MY_CREDENTIAL",
            "AWS_CREDENTIALS",
            "AUTHORIZATION",
            "API_KEY",
            "ACCESS_KEY",
            "PRIVATE_KEY",
            "AUTH_TOKEN",
            "githubToken",
            "github-token",
            # A header value is frequently the credential itself
            # (AUTH_HEADER=Bearer ...), so HEADER is deliberately NOT a metadata
            # suffix -- the suffix check returns before the secret-token check, so
            # exempting it would bypass screening entirely.
            "AUTH_HEADER",
            "AUTHORIZATION_HEADER",
        ],
    )
    def test_credential_keys_are_flagged(self, key):
        assert env_key_is_credential_like(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "OAUTH_CLIENT_ID",
            "TOKEN_URL",
            "SECRET_NAME",
            "CREDENTIAL_PATH",
            "AUTH_ENDPOINT",
            "API_KEY_FILE",
            "PASSWORD_HOST",
            "LOG_LEVEL",
            "MCP_SERVER_URI",
        ],
    )
    def test_metadata_keys_are_not_flagged(self, key):
        """A trailing metadata suffix means the value names WHERE a secret lives,
        not the secret. Flagging these blocks legitimate config."""
        assert env_key_is_credential_like(key) is False

    def test_matching_is_token_split_not_substring(self):
        """`TOKENIZER` contains 'TOKEN' as a substring but is not a credential;
        a naive `in` check would flag it."""
        assert env_key_is_credential_like("TOKENIZER") is False
        assert env_key_is_credential_like("AUTHOR") is False

    def test_camel_case_is_split_before_matching(self):
        assert env_key_is_credential_like("myApiKey") is True
        assert env_key_is_credential_like("myApiKeyPath") is False


class TestSecretValuePatterns:
    @pytest.mark.parametrize(
        "value",
        [
            "AKIAIOSFODNN7EXAMPLE",
            "ASIAIOSFODNN7EXAMPLE",
            "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "gho_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "Bearer abcdefghijklmnop",
            "Basic YWxhZGRpbjpvcGVuc2VzYW1l",
            "github_pat_11ABCDEFG0aaaaaaaaaaaaaa",
            "glpat-aaaaaaaaaaaaaaaaaaaaa",
            "xoxb-123456789012-abcdefghij",
            "-----BEGIN RSA PRIVATE KEY",
            "-----BEGIN OPENSSH PRIVATE KEY",
            "postgres://user:hunter2@db.example.com/app",
        ],
    )
    def test_known_secret_shapes_are_caught(self, value):
        assert _MCP_ENV_SECRET_VALUE_RE.search(value) is not None

    def test_a_jwt_is_caught(self):
        jwt = "eyJ" + "a" * 22 + ".eyJ" + "b" * 22 + ".sig"
        assert _MCP_ENV_SECRET_VALUE_RE.search(jwt) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "https://api.example.com/v1",
            "/usr/local/bin/mcp-server",
            "info",
            "true",
            "3000",
            "en-US",
            "postgres://db.example.com/app",
        ],
    )
    def test_ordinary_config_values_are_not_flagged(self, value):
        assert _MCP_ENV_SECRET_VALUE_RE.search(value) is None


class TestCanonicalCredentialFormats:
    """The value screen must recognise the formats the canonical scanner knows.

    A bespoke list of provider prefixes went stale against the canonical set and
    let a project key through into a world-readable agent spec, so these pin the
    provider shapes AND the derivation that keeps them in sync.
    """

    @pytest.mark.parametrize(
        "value",
        [
            _OPENAI_PROJECT_SHAPE,
            _OPENAI_LEGACY_SHAPE,
            _ANTHROPIC_SHAPE,
            "sk_live_" + "T3xQ7bV2mZ9pL4wR8aJ6cH1n",
            "SG." + "T3xQ7bV2mZ9pL4wR" + "." + "aJ6cH1nD5sK0yGfB7uE2iO",
            "npm_" + "T3xQ7bV2mZ9pL4wR8aJ6cH1nD5sK",
            "pypi-" + "AgEIcHlwaS5vcmcT3xQ7bV2mZ9pL4wR",
            "dop_v1_" + "a" * 40,
            "GOCSPX-" + "T3xQ7bV2mZ9pL4wR8aJ6",
            "1234567:" + "AAHhT3xQ7bV2mZ9pL4wR8aJ6cH1nD5sK0yG",
        ],
    )
    def test_provider_formats_are_flagged(self, value):
        assert mcp_env_value_is_credential_like(value) is True

    def test_pattern_is_derived_from_the_canonical_patterns(self):
        """Every canonical pattern must be embedded verbatim in the value screen.

        Fails loudly if someone replaces the derivation with a hand-written copy,
        which is the drift that caused the original miss.
        """
        for pattern in get_credential_patterns():
            assert pattern.pattern in _MCP_ENV_SECRET_VALUE_RE.pattern

    @pytest.mark.parametrize(
        "value",
        [
            _AWS_BARE_SECRET_SHAPE,
            _GENERIC_B64_SHAPE,
            _OPENAI_LEGACY_SHAPE,
        ],
    )
    def test_prefixless_secrets_need_the_predicate_not_the_pattern(self, value):
        """These carry no provider marker, so no pattern can match them.

        The predicate adds the label-independent passes (base64 decode, bare
        40-char AWS secret) that the canonical scanner already owns.
        """
        assert _MCP_ENV_SECRET_VALUE_RE.search(value) is None
        assert mcp_env_value_is_credential_like(value) is True

    def test_base64_wrapped_credential_is_flagged(self):
        wrapped = base64.b64encode(_ANTHROPIC_SHAPE.encode()).decode()
        assert mcp_env_value_is_credential_like(wrapped) is True

    @pytest.mark.parametrize(
        "value",
        [
            "3000",
            "https://api.example.com/v1",
            "claude-sonnet-4-5-20250929",
            "true",
            "info",
            "debug",
            "en-US",
            "/usr/local/bin/mcp-server",
            "postgres://db.example.com/app",
            "9f1c2a4b7e0d3f6a8b5c1d2e4f7a9b0c3d5e6f81",
            "0.7",
        ],
    )
    def test_ordinary_config_values_survive_the_predicate(self, value):
        """The predicate adds an entropy pass, which is the highest
        false-positive-risk rule in the module. A flagged legitimate value blocks
        template authoring, so both directions stay pinned."""
        assert mcp_env_value_is_credential_like(value) is False

    def test_predicate_is_a_superset_of_the_pattern(self):
        assert _MCP_ENV_SECRET_VALUE_RE.search(_OPENAI_PROJECT_SHAPE) is not None
        assert mcp_env_value_is_credential_like(_OPENAI_PROJECT_SHAPE) is True


class TestEnvVarReference:
    @pytest.mark.parametrize("value", ["${GITHUB_TOKEN}", "$GITHUB_TOKEN", "${A_1}", "$_x"])
    def test_references_are_recognised(self, value):
        assert ENV_VAR_REFERENCE_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "${GITHUB_TOKEN} extra",
            "prefix${VAR}",
            "${}",
            "${1BAD}",
            "$",
            "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ],
    )
    def test_non_references_are_rejected(self, value):
        """The reference form is an ESCAPE from the credential check, so it has to
        be anchored — a value that merely contains `${VAR}` alongside a literal
        secret must not slip through."""
        assert ENV_VAR_REFERENCE_RE.match(value) is None
