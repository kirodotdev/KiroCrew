"""A short configured header value must not split a credential inside an MCP error.

The configured-value scrub and the generic credential scanners both read the
ORIGINAL error text; their spans are merged and replaced once. Running the
scrub first let a one-character value (``X-Api-Version: 1``) cut a valid token
into pieces the generic scanner cannot recognise (synthetic values only).
"""

from __future__ import annotations

import kiro_crew.mcp_discovery as disc
from kiro_crew.mcp_discovery import MCP_REDACTED_HEADER_VALUE, McpServerInfo
from kiro_crew.testing.synthetic_credentials import github_classic


def _token_with(char: str) -> str:
    for seed in range(1, 200):
        token = github_classic(seed=seed)
        if char in token[4:]:
            return token
    raise AssertionError(f"no synthetic token contains {char!r}")


def _assert_masked(text: str, token: str) -> None:
    assert token not in text
    body = token[4:]
    # No run of 6+ token characters survives, so no readable fragment is left.
    for start in range(len(body) - 5):
        assert body[start : start + 6] not in text
    # Putting the configured value back into the tags cannot rebuild the token.
    assert token not in text.replace(MCP_REDACTED_HEADER_VALUE, "1")


def test_short_configured_value_does_not_split_a_github_token() -> None:
    token = _token_with("1")
    error = f"initialize failed: server rejected {token} as invalid"

    redacted = disc.redact_mcp_error(error, {"X-Api-Version": "1"})

    _assert_masked(redacted, token)
    assert redacted.startswith("initialize failed: server rejected ")
    assert redacted.endswith(" as invalid")


def test_configured_value_inside_token_still_masks_both() -> None:
    token = _token_with("1")
    error = f"bad {token} and version 1 here"

    redacted = disc.redact_mcp_error(error, {"X-Api-Version": "1"})

    _assert_masked(redacted, token)


def test_to_dict_error_masks_token_with_short_header_value() -> None:
    token = _token_with("1")
    server = McpServerInfo(name="s", url="https://example.invalid/mcp")
    server.headers = {"X-Api-Version": "1"}
    server.sent_headers = {"X-Api-Version": "1"}
    server.error = f"initialize failed: {token}"

    dumped = server.to_dict()

    _assert_masked(str(dumped.get("error", "")), token)
