"""A credential-server keyword joined to its neighbours with ``_``.

Sibling of ``test_deny_guidance.py``, which sits at its line target; the
keyword-policy tests that already exist stay there.

The keywords are matched against an identifier a third party chose, where
snake_case is as idiomatic as kebab-case, so ``aws_credentials`` must be found
exactly as ``aws-credentials`` is. The class anchors keep ``_`` as a word
character: they read a refusal reason plus the refused command, and a separator
there would turn silence into wrong advice.
"""

from __future__ import annotations

import pytest

from kiro_crew import deny_guidance as dg

_AWS_REASON = "Blocked: command accesses sensitive credential path (.aws/credentials)"
_VENDOR_HINT_MARK = "may vend credentials"


class TestSnakeCaseServerKeywords:
    @pytest.mark.parametrize(
        "row",
        [
            {"server_id": "vault-mcp", "description": "vends aws_credentials for the account"},
            {"server_id": "role-tools", "name": "iam_role_helper"},
            {"server_id": "broker", "title": "corp_sso_broker"},
            {"server_id": "token-desk", "description": "mints sts_session tokens"},
            {"server_id": "keyring", "description": "returns my_creds on demand"},
        ],
    )
    def test_a_keyword_delimited_by_underscores_is_found(self, row):
        """The id carries no keyword, so only the snake_case field can match."""
        assert dg.credential_vendor_server_ids([row]) == [row["server_id"]]
        assert _VENDOR_HINT_MARK in dg.credential_tool_hint([row])

    @pytest.mark.parametrize(
        "row",
        [
            {"server_id": "vault-mcp", "description": "vends aws-credentials for the account"},
            {"server_id": "role-tools", "name": "iam-role-helper"},
            {"server_id": "broker", "title": "corp.sso.broker"},
        ],
    )
    def test_the_other_separators_still_match(self, row):
        assert dg.credential_vendor_server_ids([row]) == [row["server_id"]]

    @pytest.mark.parametrize(
        "description",
        [
            "posts_messages to a channel",
            "renders williams_charts",
            "renders picasso_prints",
            "lists my_iambic_scans",
        ],
    )
    def test_an_underscore_does_not_open_a_keyword_inside_a_word(self, description):
        """Only ``_`` became a separator: a keyword inside a word stays shut.

        Each row puts a keyword's letters right against the ``_`` that is now a
        boundary, on one side or the other, so each half of the matcher is held:
        ``sts`` in "posts" and ``sso`` ending "picasso" need the LEFT lookaround,
        and ``iam`` opening "iambic" after the ``_`` needs the RIGHT one.
        """
        assert (
            dg.credential_vendor_server_ids(
                [{"server_id": "note-taker", "description": description}]
            )
            == []
        )

    def test_the_vendor_sentence_reaches_a_credential_refusal(self):
        hint = dg.credential_tool_hint(
            [{"server_id": "vault-mcp", "description": "vends aws_credentials"}]
        )
        assert dg.classify_deny(_AWS_REASON) == dg.DENY_CLASS_AWS_CREDENTIAL
        assert _VENDOR_HINT_MARK in dg.remediation_for(_AWS_REASON, credential_tool_hint=hint)

    def test_a_snake_case_server_id_passes_the_unchanged_id_filter(self):
        """``_SAFE_SERVER_ID_RE`` admits ``_`` (its class is ``[A-Za-z0-9._:/-]``).

        So the keyword boundary, not the id filter, was what dropped
        ``aws_credentials_server``, and this fix names it. The filter itself is
        pinned as it stands: an id it refuses is still dropped before the scan.
        """
        assert dg._SAFE_SERVER_ID_RE.pattern == r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$"
        assert dg.credential_vendor_server_ids([{"server_id": "aws_credentials_server"}]) == [
            "aws_credentials_server"
        ]
        assert dg.credential_vendor_server_ids([{"server_id": "aws_credentials server"}]) == []


class TestClassAnchorsKeepTheirBoundary:
    def test_the_class_matchers_still_treat_underscore_as_a_word_character(self):
        """A refused ``@db/drop_sso_sessions`` is not an SSO login refusal."""
        assert dg.classify_deny("Blocked by security policy: @db/drop_sso_sessions") == ""
        for _deny_class, matchers in dg._CLASS_MATCHERS:
            for matcher in matchers:
                assert "[0-9a-z_]" in matcher.pattern, matcher.pattern
