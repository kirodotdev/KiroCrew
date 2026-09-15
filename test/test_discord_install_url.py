"""Guards the Discord install (OAuth authorize) URL builder.

Discord publishes no app manifest, so the authorize URL IS the install surface,
and its permissions bitfield is a magic number nobody can check by eye. These
tests pin that number from two directions at once: to the named bits the module
builds it from, and to the number
``src/kiro_crew/docs/discord-integration.md`` publishes for operators to paste.
Either one drifting alone would hand somebody a bot that can read a thread but
not reply in it, with nothing going red.
"""

from __future__ import annotations

from functools import reduce
from operator import or_
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from kiro_crew.discord import install_url

_APP_ID = "123456789012345678"

_DOC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "docs" / "discord-integration.md"

#: The bitfield the setup doc publishes. Spelled out here rather than imported
#: so this file states the expected value independently of the code under test.
_DOCUMENTED_PERMISSIONS = 309237711936

#: Discord's SEND_MESSAGES bit, deliberately NOT requested: a Kiro Crew turn
#: runs in a thread, and the bot must not be able to post in a shared channel.
_PERM_SEND_MESSAGES = 1 << 11


def _doc_text() -> str:
    return _DOC.read_text(encoding="utf-8")


class TestPermissionBits:
    """The requested permission set, bit by bit."""

    def test_thread_permissions_equals_the_documented_number(self) -> None:
        assert install_url.THREAD_PERMISSIONS == _DOCUMENTED_PERMISSIONS

    @pytest.mark.parametrize(
        ("name", "bit"),
        [
            ("View Channel", 1 << 10),
            ("Read Message History", 1 << 16),
            ("Add Reactions", 1 << 6),
            ("Send Messages in Threads", 1 << 38),
        ],
    )
    def test_each_named_bit_is_the_right_value_and_is_requested(self, name: str, bit: int) -> None:
        # Both halves matter: the label must map to Discord's actual bit, and
        # that bit must survive into the OR the URL carries.
        assert install_url.THREAD_PERMISSION_BITS[name] == bit
        assert install_url.THREAD_PERMISSIONS & bit == bit

    def test_named_constants_match_the_labelled_table(self) -> None:
        assert install_url.PERM_VIEW_CHANNEL == 1 << 10
        assert install_url.PERM_READ_MESSAGE_HISTORY == 1 << 16
        assert install_url.PERM_ADD_REACTIONS == 1 << 6
        assert install_url.PERM_SEND_MESSAGES_IN_THREADS == 1 << 38
        assert install_url.PERM_CREATE_PUBLIC_THREADS == 1 << 35

    def test_no_unnamed_bit_rides_along(self) -> None:
        # Any bit in the OR that the labelled table does not name is a permission
        # the operator grants without ever seeing it named. Derived from the table
        # rather than hardcoded, so the property holds as the set changes instead
        # of the assertion going stale the next time a bit is legitimately added.
        named = install_url.THREAD_PERMISSION_BITS
        assert install_url.THREAD_PERMISSIONS.bit_count() == len(named)
        assert install_url.THREAD_PERMISSIONS == reduce(or_, named.values())
        assert all(bit.bit_count() == 1 for bit in named.values())

    def test_send_messages_in_channels_is_not_requested(self) -> None:
        assert install_url.THREAD_PERMISSIONS & _PERM_SEND_MESSAGES == 0

    def test_dm_install_permissions_are_none(self) -> None:
        assert install_url.DM_PERMISSIONS == 0


class TestDocAgreement:
    """The shipped setup doc and the builder must publish the same install."""

    def test_doc_publishes_the_same_bitfield(self) -> None:
        assert f"permissions={install_url.THREAD_PERMISSIONS}" in _doc_text()

    def test_doc_url_matches_the_built_url(self) -> None:
        # The doc's URL is the manual fallback an operator pastes; it has to be
        # the same URL, parameter for parameter, that Kiro Crew builds.
        doc_urls = [
            line.strip()
            for line in _doc_text().splitlines()
            if line.strip().startswith(f"{install_url.AUTHORIZE_ENDPOINT}?")
        ]
        assert doc_urls, "the setup doc no longer publishes a manual install URL"
        expected = install_url.build_install_url(_APP_ID)
        assert [u.replace("YOUR_APP_ID", _APP_ID) for u in doc_urls] == [expected]


class TestBuildInstallUrl:
    """The URL itself: endpoint, scopes, encoding, and the DM-only variant."""

    def test_endpoint_is_discord_oauth_authorize(self) -> None:
        parts = urlparse(install_url.build_install_url(_APP_ID))
        assert (parts.scheme, parts.netloc, parts.path) == (
            "https",
            "discord.com",
            "/oauth2/authorize",
        )

    def test_carries_both_scopes_url_encoded(self) -> None:
        url = install_url.build_install_url(_APP_ID)
        # Decoded: one space-separated scope list, both scopes present.
        assert parse_qs(urlparse(url).query)["scope"] == ["bot applications.commands"]
        # Encoded: the space is "+" and no raw space reaches the URL.
        assert "scope=bot+applications.commands" in url
        assert " " not in url

    def test_thread_variant_requests_the_documented_permissions(self) -> None:
        url = install_url.build_install_url(_APP_ID)
        query = parse_qs(urlparse(url).query)
        assert query["permissions"] == [str(_DOCUMENTED_PERMISSIONS)]
        assert query["client_id"] == [_APP_ID]

    def test_dm_only_requests_no_permissions(self) -> None:
        url = install_url.build_install_url(_APP_ID, dm_only=True)
        assert parse_qs(urlparse(url).query)["permissions"] == ["0"]
        assert str(_DOCUMENTED_PERMISSIONS) not in url

    def test_dm_only_still_asks_for_both_scopes(self) -> None:
        # The scopes say what the app IS; only the guild grant differs.
        url = install_url.build_install_url(_APP_ID, dm_only=True)
        assert parse_qs(urlparse(url).query)["scope"] == ["bot applications.commands"]

    def test_client_id_whitespace_is_tolerated(self) -> None:
        assert install_url.build_install_url(f"  {_APP_ID}\n") == (
            install_url.build_install_url(_APP_ID)
        )

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "YOUR_APP_ID",
            "12345",
            "1234567890123456789012345678",
            "1234567890123456a",
            # Would otherwise smuggle a second parameter into a URL the
            # operator is told to trust.
            "123456789012345678&permissions=8",
            "123456789012345678?x=1",
            # A terminal control sequence in a printed URL.
            "123456789012345678\x1b[31m",
        ],
    )
    def test_a_client_id_that_is_not_a_snowflake_is_refused(self, bad: str) -> None:
        with pytest.raises(ValueError):
            install_url.build_install_url(bad)
