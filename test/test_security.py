"""Tests for kiro_crew.security: ``redact_credentials`` patterns, base64 detection and bare keys.

The rest of the family lives beside it: ``test_security_token_params.py``,
``test_security_bare_secrets.py``, ``test_security_denied_commands.py``,
``test_security_oauth_urls.py``, ``test_security_exfil_urls.py``,
``test_security_sensitive_paths.py``, ``test_security_keystone_leaves.py``,
``test_security_bash_gate.py``, ``test_security_stream_redaction.py``,
``test_security_resource_limits.py``, ``test_security_publish_floor.py`` and
``test_security_shell_grammar.py``.
"""

from __future__ import annotations

import base64
import json

import pytest
import security_helpers

from kiro_crew.security import (
    redact_credentials,
)


class TestRedactCredentials:
    """Tests for redact_credentials()."""

    def test_redacts_aws_access_key_id(self) -> None:
        text = "Found key AKIAIOSFODNN7EXAMPLE in output"
        result, warnings = redact_credentials(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED: credential]" in result
        assert len(warnings) == 1

    def test_redacts_asia_key(self) -> None:
        text = "ASIAXXXXXXXXXEXAMPLE"
        result, _ = redact_credentials(text)
        assert "ASIA" not in result

    def test_redacts_secret_access_key(self) -> None:
        text = "SecretAccessKey=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        result, _ = redact_credentials(text)
        assert "wJalrXUtnFEMI" not in result

    def test_redacts_aws_secret_access_key_ini(self) -> None:
        text = "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG"
        result, _ = redact_credentials(text)
        assert "wJalrXUtnFEMI" not in result

    def test_redacts_session_token(self) -> None:
        text = "SessionToken=FwoGZXIvYXdzEBYaDH+longtoken"
        result, _ = redact_credentials(text)
        assert "FwoGZXIvYXdzEBYaDH" not in result

    def test_redacts_private_key_header(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQ"
        result, _ = redact_credentials(text)
        assert "BEGIN RSA PRIVATE KEY" not in result

    def test_redacts_openssh_private_key(self) -> None:
        text = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1r"
        result, _ = redact_credentials(text)
        assert "BEGIN OPENSSH PRIVATE KEY" not in result

    def test_redacts_full_private_key_body(self) -> None:
        """The base64 BODY (not just the header) must be redacted."""
        body_a = "MIIEpAIBAAKCAQEA1234567890abcdefghijklmnopqrstuvwxyzABCDEF"
        body_b = "GHIJKLMNOPQRSTUVWXYZ0987654321zyxwvutsrqponmlkjihgfedcba"
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            f"{body_a}\n{body_b}\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result, warnings = redact_credentials(text)
        assert body_a not in result
        assert body_b not in result
        assert "BEGIN RSA PRIVATE KEY" not in result
        assert "END RSA PRIVATE KEY" not in result
        assert "[REDACTED: credential]" in result
        assert warnings

    def test_redacts_truncated_private_key_body(self) -> None:
        """A key block missing the END marker still has its body redacted."""
        body = "MIIEpAIBAAKCAQEAtruncatedbodybytes1234567890abcdef"
        text = f"-----BEGIN EC PRIVATE KEY-----\n{body}"
        result, _ = redact_credentials(text)
        assert body not in result
        assert "BEGIN EC PRIVATE KEY" not in result

    def test_redacts_encrypted_private_key_body(self) -> None:
        """Encrypted PEM: Proc-Type/DEK-Info headers carry ':'/',' — body must
        still be fully redacted (a base64-only body class would stop short)."""
        body = "MIIEpAIBAAKCAQEAencryptedbodybytes0987654321zyxwvu"
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "Proc-Type: 4,ENCRYPTED\n"
            "DEK-Info: AES-128-CBC,DDEA6208BB09B295E4C9BA85D2E85CD1\n\n"
            f"{body}\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result, _ = redact_credentials(text)
        assert body not in result
        assert "DEK-Info" not in result
        assert "BEGIN RSA PRIVATE KEY" not in result

    def test_redacts_two_private_key_blocks(self) -> None:
        """Two adjacent key blocks: each body redacted, intervening prose kept."""
        body1 = "MIIEpAIBAAKCAQEAfirstkeybody1234567890abcdefghij"
        body2 = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAA"
        text = (
            f"-----BEGIN RSA PRIVATE KEY-----\n{body1}\n-----END RSA PRIVATE KEY-----\n"
            "middle prose stays\n"
            f"-----BEGIN OPENSSH PRIVATE KEY-----\n{body2}\n-----END OPENSSH PRIVATE KEY-----"
        )
        result, _ = redact_credentials(text)
        assert body1 not in result
        assert body2 not in result
        assert "middle prose stays" in result

    def test_private_key_prose_not_over_redacted(self) -> None:
        """A full key block followed by prose: the END anchor stops the span so
        the trailing prose is preserved (no over-redaction)."""
        body = "MIIEpAIBAAKCAQEAbodybytes1234567890abcdefghijklmn"
        text = (
            f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----\n"
            "Contact ops@example.com if this key is expired."
        )
        result, _ = redact_credentials(text)
        assert body not in result
        assert "Contact ops@example.com if this key is expired." in result

    def test_no_false_positive_on_private_key_prose(self) -> None:
        """Prose mentioning 'PRIVATE KEY' without the PEM markers is untouched."""
        text = "See the PRIVATE KEY handling section of the runbook."
        result, warnings = redact_credentials(text)
        assert result == text
        assert not warnings

    def test_pem_header_in_prose_without_end_keeps_trailing_lines(self) -> None:
        """A PEM BEGIN header mentioned inline in prose (no body, no END marker)
        must not swallow trailing lines to end-of-string. Guards the `$`
        end-of-string over-redaction regression."""
        text = (
            "For example, a PEM key starts with "
            "-----BEGIN RSA PRIVATE KEY----- and contains base64 data.\n"
            "Line 2 of docs.\n"
            "Line 3."
        )
        result, _ = redact_credentials(text)
        assert "Line 2 of docs." in result
        assert "Line 3." in result
        assert "and contains base64 data." in result

    def test_redacts_encrypted_private_key_across_dek_info_blank_line(self) -> None:
        """RFC 1421 ENCRYPTED PEM (no END): the mandatory blank line between the
        DEK-Info header and the base64 body must NOT terminate the run — the
        whole body is redacted. Guards the leak where a
        single blank line ended the continuation and emitted the body verbatim."""
        body_line1 = "MIIEpQIBAAKCAQEAencryptedbodybytesABCDEF1234567890zyxwv"
        body_line2 = "secondencryptedbodylineGHIJKL0987654321mnopqrABCDEF"
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "Proc-Type: 4,ENCRYPTED\n"
            "DEK-Info: DES-EDE3-CBC,ABCD1234EF567890\n"
            "\n"
            f"{body_line1}\n"
            f"{body_line2}"
        )
        result, _ = redact_credentials(text)
        assert body_line1 not in result
        assert body_line2 not in result
        assert "DEK-Info" not in result
        assert "BEGIN RSA PRIVATE KEY" not in result

    def test_two_blank_lines_terminate_private_key_run(self) -> None:
        """TWO+ consecutive blank lines terminate the truncated-key run so
        trailing prose is preserved (no over-redaction). The single-blank-line
        lookahead must not extend across a paragraph break."""
        body = "MIIEpQIBAAKCAQEAbodybytes1234567890abcdefghijklmnop"
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            f"{body}\n"
            "\n"
            "\n"
            "ThisProseAfterTwoBlankLinesMustSurvive and stay intact."
        )
        result, _ = redact_credentials(text)
        assert body not in result
        assert "ThisProseAfterTwoBlankLinesMustSurvive and stay intact." in result

    def test_redacts_slack_token(self) -> None:
        text = "Token is xoxb-1234567890-abcdefghij"
        result, _ = redact_credentials(text)
        assert "xoxb-" not in result

    # ── Third-party developer credentials (pentest issue 2) ──

    # NOTE: each fixture below is written as two adjacent string literals that
    # Python concatenates at parse time, so the runtime secret value is exactly
    # the intended token (the redaction test is unchanged). The split keeps any
    # single source literal from being a complete provider token, so GitHub
    # push-protection / secret scanners don't flag these synthetic fixtures.
    #
    # The explicit ``ids=`` labels exist for the same reason one level up:
    # without them pytest derives each test ID from the REASSEMBLED value, and
    # the full key-shaped string then lands verbatim in every derived artifact
    # (.test_durations, junit XML, CI logs). Push protection rejects any branch
    # carrying such an artifact — that is what kept the Update Test Durations
    # workflow from ever landing its PR. Keep these labels secret-shape-free.
    @pytest.mark.parametrize(
        "secret",
        [
            "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12",  # GitHub classic PAT
            "gho_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234",  # GitHub OAuth
            "github_pat_"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij1234567890ABCDEFGHIJ",  # fine-grained
            "glpat-" "xxxx1234xxxx5678xxxx",  # GitLab PAT
            "sk_live_" "51HG7aBcDeFgHiJkLmNoPqRsTuVwXyZ",  # Stripe live
            "sk_test_" "51HG7aBcDeFgHiJkLmNoPqRsTuVwXyZ",  # Stripe test
            "rk_live_" "51HG7aBcDeFgHiJkLmNoPqRsTuVwXyZ",  # Stripe restricted
            "SG." "abcdefghijklmnop.qrstuvwxyz1234567890ABCDEFGHIJKLMNOPQR",  # SendGrid
            "sk-proj-" "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234",  # OpenAI
            "sk-ant-api03-" "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP",  # Anthropic
            "npm_" "abcdefghijklmnopqrstuvwxyz123456",  # npm
            "pypi-" "AgEIcHlwaS5vcmcCJGI2YzRlYjYwLWExYmUtNDgxZi04",  # PyPI
            "dop_v1_" "abcdefghijklmnopqrstuvwxyz1234567890abcdefghijklmnopqrst",  # DigitalOcean
            "GOCSPX-" "abcdefghijklmnopqrstuvwx",  # Google OAuth
        ],
        ids=[
            "github-classic-pat",
            "github-oauth",
            "github-fine-grained-pat",
            "gitlab-pat",
            "stripe-live",
            "stripe-test",
            "stripe-restricted",
            "sendgrid",
            "openai-project",
            "anthropic",
            "npm-token",
            "pypi-token",
            "digitalocean",
            "google-oauth",
        ],
    )
    def test_redacts_third_party_credentials(self, secret: str) -> None:
        text = f"KEY={secret}"
        result, warnings = redact_credentials(text)
        assert secret not in result
        assert "[REDACTED: credential]" in result
        assert len(warnings) == 1

    @pytest.mark.parametrize(
        "secret",
        [
            "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12",  # GitHub classic PAT
            "sk-ant-api03-" "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP",  # Anthropic
            "sk-proj-" "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234",  # OpenAI
            "sk_live_" "51HG7aBcDeFgHiJkLmNoPqRsTuVwXyZ",  # Stripe live
            "xoxb-" "1234567890-abcdefghijklmnop",  # Slack bot token
        ],
        # Safe display labels: pytest would otherwise derive the ID from the
        # reassembled token — see the note on the parametrize above.
        ids=["github-classic-pat", "anthropic", "openai-project", "stripe-live", "slack-bot"],
    )
    def test_warning_does_not_leak_secret_prefix(self, secret: str) -> None:
        """The warnings list must carry NO secret bytes — only length metadata.

        The bug this guards: the plaintext branch
        emitted ``matched[:20]``, leaking a 12-16 char slice of the real secret
        (a fingerprint of exactly which key matched) into a list that sinks
        expect to be safe to log/surface. High-entropy API-key prefixes
        (``ghp_``, ``sk-ant-``, ``sk-proj-``, ``sk_live_``, ``xoxb-``) are the
        worst case; assert none of the raw secret survives in any warning.
        """
        text = f"KEY={secret}"
        _, warnings = redact_credentials(text)
        assert len(warnings) == 1
        joined = " ".join(warnings)
        # The full secret must not appear, and neither may any leading slice of
        # it beyond the (non-secret) provider prefix — assert the whole value
        # and its first 20 chars (the old leak window) are both absent.
        assert secret not in joined
        assert secret[:20] not in joined
        # Positive: the warning still reports the redaction with a length.
        assert "Redacted credential pattern" in joined
        assert f"{len(secret)} chars" in joined

    def test_redacts_db_uri_with_embedded_password(self) -> None:
        text = "DATABASE_URL=postgres://admin:SuperSecret123@db.example.com:5432/prod"
        result, _ = redact_credentials(text)
        assert "SuperSecret123" not in result
        assert "admin" not in result
        # host after @ may remain — only the credential prefix is redacted
        assert "[REDACTED: credential]" in result

    def test_every_redaction_tag_constant_is_registered(self) -> None:
        """A new credential tag must be added to ``CREDENTIAL_REDACTION_TAGS``.

        Consumers ask that tuple "did the redactor replace something here" -- the
        dashboard chat notice counts it to tell the user their text
        was rewritten. A tag that exists but is not registered is invisible to
        every such consumer, which is exactly how the encoded-credential tag came
        to be missed. This ratchet makes that omission fail here instead of
        silently degrading a user-facing warning.
        """
        from kiro_crew.security import redaction

        declared = {
            name: value
            for name, value in vars(redaction).items()
            if name.startswith("_REDACTED_") and name.endswith("_TAG")
            if isinstance(value, str)
        }
        assert declared, "tag-constant naming changed; this ratchet no longer sees them"

        unregistered = {
            name: value
            for name, value in declared.items()
            if value not in redaction.CREDENTIAL_REDACTION_TAGS
        }
        assert not unregistered, (
            "redaction tag(s) not in CREDENTIAL_REDACTION_TAGS: "
            f"{sorted(unregistered)} -- add them there so consumers that ask "
            "'was anything redacted' (e.g. the dashboard chat notice) can see them"
        )

    def test_pass_two_emits_a_registered_tag(self) -> None:
        """The base64 pass must substitute a tag consumers actually look for."""
        import base64

        from kiro_crew.security import (
            _REDACTED_ENCODED_CREDENTIAL_TAG,
            CREDENTIAL_REDACTION_TAGS,
        )

        blob = base64.b64encode(b"postgresql://user:pass@host:5432/db").decode()
        result, warnings = redact_credentials(f"blob: {blob}")

        assert _REDACTED_ENCODED_CREDENTIAL_TAG in result
        assert _REDACTED_ENCODED_CREDENTIAL_TAG in CREDENTIAL_REDACTION_TAGS
        assert any("base64-encoded" in w for w in warnings)

    @pytest.mark.parametrize(
        "mongo",
        [
            "mongodb://user:p%40ss@cluster0.example.com",
            "mongodb+srv://user:pw@cluster0.example.com",
            "mysql://root:toor@localhost:3306/db",
            "redis://default:secret@redis.example.com:6379",
            # URL userinfo is a credential on fetch schemes too (a
            # token-bearing artifact CDN base quoted by update-failure text).
            "https://user:tok-SECRET99@cdn.example.com/w.whl",
            "ftp://anon:pw@mirror.example.com/f",
            # A password containing an unencoded @ must redact through the
            # FINAL authority separator, not stop at the first @.
            "https://user:p@ss@cdn.example.com/w.whl",
            "redis://default:se@cret@redis.example.com:6379",
        ],
    )
    def test_redacts_various_db_uris(self, mongo: str) -> None:
        result, _ = redact_credentials(mongo)
        assert "[REDACTED: credential]" in result

    def test_no_false_positive_on_benign_strings(self) -> None:
        """Non-credential strings that superficially resemble prefixes stay intact."""
        for benign in [
            "npm_config_cache=/home/u/.npm",  # npm_ env var, too short + underscores
            "git sha 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",  # 40-hex git SHA
            "postgresql://localhost:5432/db",  # no user:pass@
            "https://example.com:8080/path",  # port is not userinfo
            "https://example.com/a@b",  # @ in the path, not the authority
            "SG.short.x",  # segments too short
            "the ghp_ prefix on its own",  # no token body
        ]:
            result, warnings = redact_credentials(benign)
            assert result == benign, f"false positive on {benign!r}"
            assert warnings == []

    def test_bare_hex_not_redacted_by_design(self) -> None:
        """A bare 32-hex token (e.g. Twilio) is intentionally NOT redacted.

        A generic 32-hex string collides with MD5 hashes, git object ids, and
        dash-less UUIDs, so redacting it would be high false-positive. Matches
        the pentest recommendation, which omitted Twilio from the pattern set.
        """
        text = "TWILIO_AUTH=a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        result, _ = redact_credentials(text)
        assert result == text

    def test_preserves_normal_text(self) -> None:
        text = "The deployment succeeded. 42 pods running."
        result, warnings = redact_credentials(text)
        assert result == text
        assert len(warnings) == 0

    def test_preserves_aws_cli_output(self) -> None:
        text = '{"Account": "123456789012", "Arn": "arn:aws:iam::123:user/dev"}'
        result, warnings = redact_credentials(text)
        assert result == text
        assert len(warnings) == 0

    def test_preserves_ada_update_success(self) -> None:
        text = "Successfully refreshed aws credentials for default"
        result, warnings = redact_credentials(text)
        assert result == text
        assert len(warnings) == 0

    def test_preserves_git_output(self) -> None:
        text = "Cloning into 'KiroCrew'...\nremote: Enumerating objects: 1234"
        result, warnings = redact_credentials(text)
        assert result == text

    def test_preserves_kubectl_output(self) -> None:
        text = "NAME       READY   STATUS    RESTARTS   AGE\nnginx-pod  1/1     Running   0          5m"
        result, warnings = redact_credentials(text)
        assert result == text

    # ── JSON-form credential redaction (regression) ──
    # The key-value patterns required the key name to be immediately followed by
    # `[:=]`, so JSON (`"aws_secret_access_key": "..."`) — where a closing quote
    # sits between the key and the colon — was NOT matched and the secret leaked.
    # JSON is one of the most common shapes credentials take in tool output/logs.

    def test_redacts_json_secret_access_key(self) -> None:
        text = '{"aws_secret_access_key": "ABCverysecret123"}'
        result, warnings = redact_credentials(text)
        assert "ABCverysecret123" not in result
        assert warnings

    def test_redacts_json_secret_no_space(self) -> None:
        text = '{"aws_secret_access_key":"ABCverysecret123"}'
        result, _ = redact_credentials(text)
        assert "ABCverysecret123" not in result

    def test_redacts_json_session_token(self) -> None:
        text = '{"aws_session_token": "XYZtokenvalue789"}'
        result, _ = redact_credentials(text)
        assert "XYZtokenvalue789" not in result

    def test_redacts_json_access_key_id(self) -> None:
        text = '{"aws_access_key_id": "someAccessKeyIdValue"}'
        result, _ = redact_credentials(text)
        assert "someAccessKeyIdValue" not in result

    def test_bare_keyvalue_still_redacted(self) -> None:
        # Regression guard: the original bare forms must still work.
        for text, secret in [
            ("aws_secret_access_key=BAREsecret1", "BAREsecret1"),
            ("aws_secret_access_key: BAREsecret2", "BAREsecret2"),
            ("SecretAccessKey=BAREsecret3", "BAREsecret3"),
        ]:
            result, _ = redact_credentials(text)
            assert secret not in result, f"bare form leaked: {text!r}"

    def test_prose_mentioning_key_not_overredacted(self) -> None:
        # The key name as ordinary prose (followed by a space/word, not [:=]) must
        # not trigger redaction — guards against over-redaction from the new pattern.
        text = "The aws_secret_access_key field is required for auth."
        result, _ = redact_credentials(text)
        assert result == text

    def test_redacts_json_compact_no_overcapture(self) -> None:
        """Compact JSON: only the secret value is redacted, not adjacent fields."""
        text = '{"aws_secret_access_key":"SECRET","region":"us-east-1"}'
        result, _ = redact_credentials(text)
        assert "SECRET" not in result
        assert '"region":"us-east-1"' in result  # adjacent field preserved

    def test_multi_credential_json_both_redacted(self) -> None:
        """Multiple credentials in one compact JSON object — both must be redacted."""
        text = '{"aws_secret_access_key":"SECRET1","aws_session_token":"TOKEN2","region":"x"}'
        result, _ = redact_credentials(text)
        assert "SECRET1" not in result
        assert "TOKEN2" not in result
        assert '"region":"x"' in result

    # ── JWT / Authorization: Bearer tokens ──
    # JWTs and OAuth bearer tokens can leak in tool output / logs. They are
    # redacted here. `eyJ` is the base64url of every JWT header's `{"` prefix.

    _JWT = security_helpers._JWT

    def test_redacts_jwt(self) -> None:
        text = f"token={self._JWT}"
        result, warnings = redact_credentials(text)
        assert self._JWT not in result
        assert "[REDACTED: credential]" in result
        assert len(warnings) == 1

    def test_redacts_jwt_in_prose(self) -> None:
        text = f"Here is the id_token: {self._JWT} — do not log it."
        result, _ = redact_credentials(text)
        assert "eyJhbGci" not in result
        assert "do not log it." in result  # trailing prose preserved (no over-capture)

    # A JWE (RFC 7516) is a five-segment compact-serialization token
    # (header.encrypted_key.iv.ciphertext.tag). The three-segment JWT pattern
    # would only redact the first three segments and leak the ciphertext + tag,
    # so the segment quantifier accepts 5-segment tokens as a whole.
    _JWE = (
        "eyJhbGciOiJSU0EtT0FFUCIsImVuYyI6IkExMjhHQ00ifQ"
        ".OKOawDo13gRp2ojaHV7LFpZcgV7T6DVZKTyKOMTYUmKoTCVJRgckCL9kiMT03JGe"
        ".48V1_ALb6US04U3b"
        ".5eym8TW_c8SuK0ltJ3rpYIzOeDQz7TALvtu6UG9oMo4vpzs9tX_EFShS8iB7j6ji"
        ".XFBoMYUZodetZdvTiFvSkQ"
    )

    def test_redacts_jwe_five_segments(self) -> None:
        """A 5-segment JWE must redact as one token, not leak ciphertext+tag."""
        text = f"token={self._JWE}"
        result, warnings = redact_credentials(text)
        assert self._JWE not in result
        assert "XFBoMYUZodetZdvTiFvSkQ" not in result  # trailing tag segment gone
        assert "[REDACTED: credential]" in result
        assert len(warnings) == 1

    # RFC 7516 compact JWE with direct (`alg:dir`) or key-agreement (`ECDH-ES`)
    # key management: the Encrypted Key (2nd) segment is EMPTY, giving two
    # consecutive dots -> `header..iv.ciphertext.tag`. A `+` quantifier on the
    # post-header segments would fail to match this and leak ciphertext + tag.
    _JWE_DIR = security_helpers._JWE_DIR

    def test_the_jose_samples_are_the_shared_helpers_objects(self) -> None:
        """``TestStreamRedactor`` reads these two samples from ``security_helpers``.

        Binding the same objects here keeps one copy of each token for both modules.
        """
        assert TestRedactCredentials._JWT is security_helpers._JWT
        assert TestRedactCredentials._JWE_DIR is security_helpers._JWE_DIR

    def test_redacts_jwe_direct_empty_key_segment(self) -> None:
        """A dir/ECDH-ES JWE (empty 2nd segment) must redact whole, not leak."""
        text = f"token={self._JWE_DIR}"
        result, warnings = redact_credentials(text)
        assert self._JWE_DIR not in result
        assert "XFBoMYUZodetZdvTiFvSkQ" not in result  # trailing tag segment gone
        assert "5eym8TW_c8SuK0ltJ3rpYIzOeDQz7TALvtu6UG9oMo4vpzs9tX_EFShS8iB7j6ji" not in result
        assert "[REDACTED: credential]" in result
        assert len(warnings) == 1

    def test_redacts_authorization_bearer(self) -> None:
        text = "Authorization: Bearer abc123.def-456_ghi/jkl+mno=="
        result, warnings = redact_credentials(text)
        assert "abc123.def-456_ghi/jkl+mno==" not in result
        assert "[REDACTED: credential]" in result
        assert len(warnings) == 1

    def test_redacts_json_shaped_authorization_bearer(self) -> None:
        """A serialized JSON header `{"Authorization": "Bearer <tok>"}` redacts.

        The quote before the `:` and
        the quote before the token defeated the old `Authorization:\\s*Bearer`
        prefix, leaking the token in structured logs / JSON request dumps.
        """
        text = '{"Authorization": "Bearer abc123.def-456_ghi/jkl+mno=="}'
        result, warnings = redact_credentials(text)
        assert "abc123.def-456_ghi/jkl+mno==" not in result
        assert "[REDACTED: credential]" in result
        assert len(warnings) == 1

    def test_redacts_authorization_bearer_no_space(self) -> None:
        text = "Authorization:Bearer   opaque-token-value"
        result, _ = redact_credentials(text)
        assert "opaque-token-value" not in result

    def test_redacts_lowercase_authorization_bearer(self) -> None:
        """HTTP/2 + requests/net/http logs emit a lowercase header/scheme.

        Header names are case-insensitive (RFC 7230 §3.2), HTTP/2 mandates
        lowercase, and the `Bearer` scheme is case-insensitive (RFC 6750 §2.1),
        so the case-sensitive prefix would otherwise leak the token.
        """
        text = "authorization: bearer opaque-token-value"
        result, warnings = redact_credentials(text)
        assert "opaque-token-value" not in result
        assert "[REDACTED: credential]" in result
        assert len(warnings) == 1

    def test_redacts_bearer_jwt_single_match(self) -> None:
        """A Bearer header carrying a JWT redacts as one match, not two."""
        text = f"Authorization: Bearer {self._JWT}"
        result, warnings = redact_credentials(text)
        assert self._JWT not in result
        assert "Bearer" not in result
        assert len(warnings) == 1

    def test_jwt_prefix_without_structure_not_redacted(self) -> None:
        """A bare `eyJ` token with no `.`-separated segments must not over-redact."""
        text = "The variable eyJson holds parsed JSON output."
        result, warnings = redact_credentials(text)
        assert result == text
        assert warnings == []

    def test_hostname_containing_eyj_not_redacted(self) -> None:
        """A dotted name with `eyJ` inside has JWT shape but no JSON-object header."""
        from kiro_crew.security import _contains_fixed_credential, _decode_b64_safe

        for text in ("ssh honeyJar.example.com", "https://api.honeyJar.co.uk/v1/x"):
            assert redact_credentials(text) == (text, []), text
            assert not _contains_fixed_credential(text), text
            assert _decode_b64_safe(base64.b64encode(text.encode()).decode()) == "", text

    def test_jose_validated_jwt_redacted_on_every_batch_path(self) -> None:
        from kiro_crew.security import (
            _contains_fixed_credential,
            _decode_b64_chunk,
            _decode_b64_safe,
        )

        for token in (self._JWT, self._JWE, self._JWE_DIR):
            text = f"host honeyJar.example.com token {token}"
            result, warnings = redact_credentials(text)
            assert result == "host honeyJar.example.com token [REDACTED: credential]"
            assert len(warnings) == 1
            assert _contains_fixed_credential(token)
            encoded = base64.b64encode(token.encode()).decode()
            assert _decode_b64_chunk(encoded) == token
            assert _decode_b64_safe(encoded) == token

    def test_credential_inside_rejected_jwt_shape_still_redacted(self) -> None:
        """A rejected JWT-shaped span is rescanned, so nothing inside it leaks."""
        result, _ = redact_credentials("eyJx.AKIAIOSFODNN7EXAMPLE.y")
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        result, _ = redact_credentials(f"eyJfoo.{self._JWT}")
        assert result == "eyJfoo.[REDACTED: credential]"
        # a link token matched by the same start: the other branches are retried there
        link = "eyJ" + "e" * 100 + "." + "S" * 43
        assert redact_credentials(f"{link}.json")[0] == "[REDACTED: credential].json"

    def test_non_jose_json_header_still_redacted(self) -> None:
        """itsdangerous / Flask-session tokens carry a JSON header with no `alg`/`enc`."""
        typ_only = base64.urlsafe_b64encode(b'{"typ":"JWT"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(b'{"user_id":7}').decode().rstrip("=")
        for token in (f"{typ_only}.payload.sig", f"{payload}.ZsT9kA.sig-x_Y"):
            assert redact_credentials(f"cookie {token} end")[0] == (
                "cookie [REDACTED: credential] end"
            ), token

    def test_dense_eyj_header_fails_closed(self) -> None:
        """A header holding a second `eyJ` is redacted whole instead of rescanned per `eyJ`."""
        from kiro_crew.security import _partial_jwt_tail

        dense = "eyJ" * 30000 + ".x.y"
        assert redact_credentials(dense)[0] == "[REDACTED: credential]"
        assert redact_credentials(f"monkeyJ{self._JWT}")[0] == "monk[REDACTED: credential]"
        assert _partial_jwt_tail(dense).start() == 0

    # ── Two-segment dashboard link token ──
    # `dashboard.token_auth.generate_token` emits `base64url(payload).base64url(
    # hmac_sig)` — TWO segments, so the JWT alternative's old `{2,4}` segment
    # floor never matched it. The token then fell through to the bare-secret
    # entropy pass, whose run class is STANDARD base64 (`[A-Za-z0-9+/]`) and
    # excludes base64url's `-`/`_`. Redaction therefore depended on which
    # characters a random signature happened to contain.

    # Same payload; signatures differ only in whether they contain a `-`.
    _LINK_PAYLOAD = (
        "eyJzdWIiOiJsb2NhbC1hcHAiLCJleHAiOjE3ODU0MTc2MDYsInNlc3Npb25fZXhwIjoxNzg1NDg5MzA2"
        "LCJpYXQiOjE3ODU0MTczMDYsIm5vbmNlIjoiOTM5YzE3MGQ5ZjBiNmEyMiIsImdlbiI6MH0"
    )
    _SIG_PLAIN = "gVhM4aKLA8dyFHoZlQx6SpYSNPkXA07kpDhWd6UhZIa"  # no `-`/`_`
    _SIG_URLSAFE = "gVhM4aKLA8dyFH-oZlQx6SpYSNPkXA07kpDhWd6UhZI"  # contains `-`

    def test_redacts_two_segment_dashboard_link_token(self) -> None:
        """The whole two-segment token is replaced, not just its signature."""
        token = f"{self._LINK_PAYLOAD}.{self._SIG_URLSAFE}"
        text = f"https://host.example.com/?token={token}"
        result, warnings = redact_credentials(text)
        assert result == "https://host.example.com/?token=[REDACTED: credential]"
        # The payload segment carries the claims (sub/exp/nonce) and must not
        # survive: a partially-redacted token still looks like a usable URL.
        assert "eyJzdWIi" not in result
        assert len(warnings) == 1

    def test_two_segment_token_redaction_independent_of_signature_alphabet(self) -> None:
        """Redaction must not depend on `-`/`_` appearing in the signature.

        Before the dedicated two-segment alternative, only signatures free of
        base64url's `-`/`_` formed a 40+ run for the bare-secret pass, so
        `(62/64)^42` = 26.4% of minted tokens were partially redacted and the
        remaining ~74% streamed out verbatim.
        """
        for sig in (self._SIG_PLAIN, self._SIG_URLSAFE):
            token = f"{self._LINK_PAYLOAD}.{sig}"
            result, warnings = redact_credentials(f"?token={token}")
            assert result == "?token=[REDACTED: credential]", sig
            assert len(warnings) == 1, sig

    def test_identifier_containing_eyj_not_redacted(self) -> None:
        """An `eyJ`-containing identifier followed by attribute access is code.

        The two-segment alternative needs a left boundary. Without one, the
        substring `eyJson.get` inside `keyJson.get` matches and the line is
        rewritten to `k[REDACTED: credential](raw)`. `redact_credentials` feeds
        persisted diff bodies, saved artifacts and compressed history, so a false
        positive is written to disk with no way to recover the original.
        """
        for text in (
            "keyJson.get(raw)",
            "surveyJson.title",
            "serviceAccountKeyJson.load(path)",
            "monkeyJson.dumps(x)",
        ):
            result, warnings = redact_credentials(text)
            assert result == text, text
            assert warnings == [], text

    def test_short_two_segment_base64url_not_redacted(self) -> None:
        """A short `eyJ…` value with one dot is a filename or a quoted claim set.

        The per-segment length floors carry this: `eyJ2IjoxfQ` is 7 chars past the
        prefix (far under the 40-char payload floor) and `json` is under the 20-char
        signature floor. A real link token clears both by a wide margin.
        """
        for text in (
            "cache file eyJ2IjoxfQ.json written",
            "See https://example.com/path?q=eyJhbGciOiJIUzI1NiJ9.",
        ):
            result, warnings = redact_credentials(text)
            assert result == text, text
            assert warnings == [], text

    def test_boundary_position_identifier_not_redacted(self) -> None:
        """A dotted identifier that BEGINS with `eyJ` must survive.

        The left boundary cannot help at offset 0, and a length FLOOR alone is
        beatable by a verbose enough identifier, so the segment lengths are taken
        from the generator instead: exactly 43 chars of HMAC signature, and a payload
        floor no real identifier reaches. Without that, these collapse to
        `[REDACTED: credential](x)` inside a persisted diff chip body.
        """
        for text in (
            "eyJsonSerializer.deserializeFromStringValue(x)",
            "eyJsonDocument.deserializeConfiguration(raw)",
            "obj.eyJsonReader.readValueFromInputStream(x)",
            "eyJargonized.intercontinentalization",
            # exactly 40 chars past `eyJ`, which cleared an earlier `{40,}` floor
            "eyJsonSerializerConfigurationFactoryBuilder.deserializeFromStringValue(x)",
            # long enough to clear any plausible payload floor on the first component
            "eyJsonSerializerConfigurationFactoryBuilderRegistryProviderDelegating"
            "InterceptorFactoryAdapterHandler.deserializeFromStringValueUsing"
            "ConfiguredObjectMapperInstance(x)",
        ):
            result, warnings = redact_credentials(text)
            assert result == text, text
            assert warnings == [], text

    def test_link_token_signature_is_43_chars(self) -> None:
        """Pin the assumption the 2-segment alternative encodes as `{43}`.

        `token_auth._sign` is HMAC-SHA256 base64url-unpadded, so the signature is
        always exactly 43 chars. The redaction pattern hard-codes that width. If the
        digest ever changes, this fails loudly here rather than silently disabling
        redaction of the link token in production.
        """
        from kiro_crew.dashboard.token_auth import _sign

        for payload in (b'{"sub":"x"}', b"", b"a" * 4096):
            assert len(_sign(payload)) == 43, payload[:16]

    def test_link_token_payload_clears_the_96_char_floor(self) -> None:
        """Pin the `{96,}` payload floor against the generator's own claim set.

        The floor must stay BELOW the shortest payload a mint can produce, or the
        pattern silently stops matching live tokens. That is a leak, not a
        cosmetic miss, so it is pinned rather than asserted in a comment.

        Both the floor and the claim set are read from source instead of restated
        here: the floor comes from the compiled pattern, and the claim KEYS come
        from a real mint, so dropping a claim or raising the floor fails loudly.
        """
        import re

        from kiro_crew.dashboard.token_auth import generate_token
        from kiro_crew.security import _CREDENTIAL_PATTERNS

        floors = re.findall(r"eyJ\[A-Za-z0-9_-\]\{(\d+),\}", _CREDENTIAL_PATTERNS.pattern)
        assert len(floors) == 1, f"expected one bounded eyJ floor, got {floors}"
        floor = int(floors[0])

        payload = generate_token("local-app", 300, register_nonce=False).split(".")[0]
        assert payload.startswith("eyJ")
        assert len(payload) - 3 > floor, "a real mint no longer clears the floor"

        # Derived worst case: the narrowest `sub` a caller could pass, with every
        # float claim at its shortest repr (an exactly-integral `time.time()`).
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        # `gen` is normalised alongside `sub` because it mirrors the persisted
        # counter behind `revocation_gen.current_revocation_gen()`, LOADED FROM
        # DISK on first use. Left ambient, the
        # derived floor would depend on how many times this machine has revoked:
        # the repr widens at 10, moving the floor 145 -> 147, so the pin below would
        # fail on a clean checkout with no code change.
        shortest = {"sub": "x", "gen": 0}
        minimal = {
            k: shortest.get(k, 1785543020.0 if isinstance(v, float) else v)
            for k, v in claims.items()
        }
        raw = json.dumps(minimal, separators=(",", ":")).encode()
        worst = len(base64.urlsafe_b64encode(raw).decode().rstrip("=")) - 3
        assert worst > floor, f"derived floor {worst} no longer clears {{{floor},}}"
        # Pinned so the figure quoted in `security.py` cannot rot silently.
        assert worst == 145, f"derived floor moved to {worst}; update security.py"

    def test_bearer_word_alone_not_redacted(self) -> None:
        """The word `Bearer` without the `Authorization:` header prefix is prose."""
        text = "The bond is a bearer instrument, not registered."
        result, warnings = redact_credentials(text)
        assert result == text
        assert warnings == []


class TestRedactCredentialsBase64:
    """Tests for base64-encoded credential detection."""

    def test_detects_base64_encoded_access_key(self) -> None:
        secret = "AccessKeyId=AKIAIOSFODNN7EXAMPLE SecretAccessKey=wJalrXUtnFEMI"
        encoded = base64.b64encode(secret.encode()).decode()
        text = f"Output: {encoded}"
        result, warnings = redact_credentials(text)
        assert encoded not in result
        assert "[REDACTED:" in result

    def test_detects_base64_encoded_secret_key(self) -> None:
        secret = "SecretAccessKey=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        encoded = base64.b64encode(secret.encode()).decode()
        text = f"Result: {encoded}"
        result, warnings = redact_credentials(text)
        assert encoded not in result

    def test_detects_base64_private_key(self) -> None:
        secret = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA"
        encoded = base64.b64encode(secret.encode()).decode()
        text = f"Data: {encoded}"
        result, warnings = redact_credentials(text)
        assert encoded not in result

    def test_ignores_benign_base64(self) -> None:
        # Normal base64 that doesn't decode to credentials
        text = "aW1wb3J0IHRoaXM=  # import this"
        result, warnings = redact_credentials(text)
        assert result == text

    def test_ignores_short_base64(self) -> None:
        text = "SGVsbG8="  # "Hello" — too short to trigger (< 40 chars)
        result, warnings = redact_credentials(text)
        assert result == text


class TestBareSecretKeyRedaction:
    """Label-independent 40-char AWS secret-key redaction.

    A bare 40-char base64 secret (the value paired with an AKIA/ASIA access key
    ID) carries no distinctive prefix and no ``key=`` label, so the labelled
    patterns miss it when it appears standalone. These tests prove the
    entropy + structural heuristic catches real secret shapes WITHOUT
    over-redacting git SHAs, hex digests, UUIDs, code identifiers, or file paths.
    """

    # ── TRUE POSITIVES: real 40-char secret-key shapes must be redacted ──

    def test_redacts_bare_aws_example_secret_key(self) -> None:
        # The canonical AWS documentation example secret access key, standalone
        # (no label, no AKIA sibling) — the exact gap the finding describes.
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        result, warnings = redact_credentials(secret)
        assert secret not in result
        assert "[REDACTED: credential]" in result
        assert warnings

    def test_redacts_bare_secret_in_prose_context(self) -> None:
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        text = f"Here is the key: {secret} — keep it safe"
        result, _ = redact_credentials(text)
        assert secret not in result
        assert "keep it safe" in result  # surrounding prose preserved

    def test_redacts_bare_secret_in_json_array(self) -> None:
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        text = f'{{"keys": ["{secret}"]}}'
        result, _ = redact_credentials(text)
        assert secret not in result

    def test_redacts_duplicate_bare_secret_occurrences(self) -> None:
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        text = f"{secret} and again {secret}"
        result, _ = redact_credentials(text)
        assert secret not in result  # BOTH copies gone

    @pytest.mark.parametrize(
        "secret",
        [
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # AWS doc example (40 chars)
            "Kx3Q51tPusV/D0URlGfMmNbVc7Z8yJhLpQrStUwZ",  # random, with '/' (40 chars)
            "Kx3Q51tPusVkD0URlGfMmNbVc7Z8yJhLpQrStUwZ",  # random alnum (40 chars)
            "Zx9Kq2Wm7Vn4Bc1Xz8Lp5Rt3Yd6Fg0Hj2Ns4QwYt",  # random alnum (40 chars)
        ],
    )
    def test_redacts_various_bare_secret_shapes(self, secret: str) -> None:
        assert len(secret) == 40  # guard: AWS secret-key length
        result, _ = redact_credentials(secret)
        assert secret not in result, f"bare secret leaked: {secret!r}"

    def test_redacts_secret_glued_to_adjacent_base64_char(self) -> None:
        # A real 40-char secret glued to an adjacent base64 char with NO delimiter
        # produces a 41+ char run that the exact-40 length gate would miss, leaking
        # the key verbatim. The sliding 40-char window must still catch it. Covers:
        # X+secret, secret+A, SECRET=+secret+ABC, and secret+X+secret.
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        for label, text in [
            ("prefix char", "X" + secret),
            ("suffix char", secret + "A"),
            ("labelled + trailing", "SECRET=" + secret + "ABC"),
            ("two secrets joined by one char", secret + "X" + secret),
        ]:
            result, warnings = redact_credentials(text)
            assert secret not in result, f"glued secret leaked ({label}): {result!r}"
            assert "[REDACTED: credential]" in result, label
            assert warnings, label

    # ── TRUE NEGATIVES: high-FP-risk lookalikes must NOT be redacted ──

    def test_git_sha_not_redacted(self) -> None:
        # 40-char hex git commit SHA — must survive untouched.
        for sha in [
            "da39a3ee5e6b4b0d3255bfef95601890afd80709",
            "356a192b7913b04c54574d18c28d46e6395428ab",
            "DA39A3EE5E6B4B0D3255BFEF95601890AFD80709",  # upper hex
            "Da39A3ee5E6b4B0d3255BfeF95601890AfD80709",  # mixed hex
        ]:
            result, warnings = redact_credentials(sha)
            assert result == sha, f"git SHA over-redacted: {sha!r}"
            assert not warnings

    def test_sha256_hex_not_redacted(self) -> None:
        digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        result, warnings = redact_credentials(digest)
        assert result == digest
        assert not warnings

    def test_md5_hex_not_redacted(self) -> None:
        digest = "d41d8cd98f00b204e9800998ecf8427e"
        result, warnings = redact_credentials(digest)
        assert result == digest
        assert not warnings

    def test_uuid_not_redacted(self) -> None:
        for u in [
            "550e8400-e29b-41d4-a716-446655440000",
            "550E8400-E29B-41D4-A716-446655440000",
        ]:
            result, _ = redact_credentials(u)
            assert result == u, f"UUID over-redacted: {u!r}"

    def test_ordinary_prose_not_redacted(self) -> None:
        text = "The quick brown fox jumps over the lazy dog once more today."
        result, warnings = redact_credentials(text)
        assert result == text
        assert not warnings

    def test_camelcase_identifier_not_redacted(self) -> None:
        # 40-char camelCase/PascalCase code identifiers with digits — the class
        # that overlaps real keys on entropy alone. The structural gates
        # (longest-lowercase-run + vowel-ratio) must keep them intact.
        for ident in [
            "AbstractSingletonProxyFactoryBean2Impl3",
            "getUserProfileByIdAndReturnJsonV2Respon",
            "configLoaderV3ParseYamlAndMergeDefaults1",
            "ThisIsA40CharacterCamelCaseIdentifier12T",
            "React2ComponentWithHooksAndStateManager1",
            "HTTPResponseHandlerV2ForJsonAndXmlData12",
        ]:
            result, warnings = redact_credentials(ident)
            assert result == ident, f"identifier over-redacted: {ident!r}"
            assert not warnings

    def test_long_camelcase_identifier_run_not_over_redacted(self) -> None:
        # The sliding 40-char window must not turn a benign >40-char camelCase
        # identifier run into a false positive: NO window within it may look like
        # a secret. Regression guard for the glued-secret fix.
        for ident in [
            "getUserProfileByIdAndReturnJsonV2ResponseHandlerFactoryImpl",
            "AbstractSingletonProxyFactoryBeanConfigurationLoaderV3Parser",
        ]:
            assert len(ident) > 40
            result, warnings = redact_credentials(ident)
            assert result == ident, f"identifier run over-redacted: {ident!r}"
            assert not warnings

    def test_slash_delimited_file_paths_not_redacted(self) -> None:
        # 40-char mixed-case file/package paths contain '/' (a base64 char) but
        # are benign. Regression guard: the heuristic must NOT treat '/' as a
        # free pass to redact — every '/' token still has to clear the structural
        # gates, and dictionary-word path segments fail them.
        for path in [
            "src/main/java/com/Example/FooBarBazClas1",  # exactly 40 chars
            "MyClass1/MyOther2/MyThird3/MyFourthClas4",  # exactly 40 chars
        ]:
            assert len(path) == 40  # guard: same length as an AWS secret key
            result, warnings = redact_credentials(path)
            assert result == path, f"file path over-redacted: {path!r}"
            assert not warnings

    def test_base32_and_digit_runs_not_redacted(self) -> None:
        for token in [
            "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXPJBSWY3DP",  # base32 (no lowercase)
            "1234567890123456789012345678901234567890",  # digits only
            "abcdefghijklmnopqrstuvwxyzabcdefghijklmn",  # lowercase only
        ]:
            result, warnings = redact_credentials(token)
            assert result == token, f"token over-redacted: {token!r}"
            assert not warnings

    def test_base64_of_readable_text_not_over_redacted_as_bare(self) -> None:
        # A base64 blob that decodes to printable text is handled by the
        # encoded-credential path, not the bare-secret heuristic; a benign one
        # must survive untouched.
        blob = base64.b64encode(b"the quick brown fox jumps over lazyy").decode()[:40]
        result, warnings = redact_credentials(blob)
        assert result == blob
        assert not warnings
