"""Regression: a disabled MCP server must not defeat the probe cache.

``GET /api/mcp`` re-probes when a configured server is absent from
``_mcp_probe_cache``, so a freshly installed server flips from "Unknown" to a
real status on the next page load instead of waiting out the TTL.

The cache is filled from ``probe_all()``, which deliberately excludes
consent-disabled rows — probing spawns the server process, and that is exactly
what consent gates. ``list_servers()`` deliberately INCLUDES them so the UI can
render the row. Comparing the unfiltered list against the cache therefore
classified every disabled server as "new" on every single request, so
``should_reprobe`` was permanently true and a full spawn fan-out was always in
flight for anyone with one disabled server. The freshness check must apply
probe_all's own filter.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest

from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_discovery import (
    MCP_REDACTED_HEADER_VALUE,
    McpServerInfo,
)


def _request(state) -> object:
    """Minimal stand-in for the aiohttp request the handler reads."""

    class _App(dict):
        pass

    class _Req:
        def __init__(self, app):
            self.app = app

    app = _App()
    app["state"] = state
    return _Req(app)


class _State:
    def __init__(self) -> None:
        self._background_tasks: set = set()


def _arrange(monkeypatch, tmp_path, servers, cache):
    """Point the handler at a synthetic server list and a warm probe cache."""
    import kiro_crew.mcp_discovery as disc

    monkeypatch.setattr(disc, "list_servers", lambda *a, **k: list(servers))
    # No mcp.json on disk: the disabled/kirocrewManaged overlay is a no-op, so
    # `disabled` comes purely from the McpServerInfo rows above.
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "absent.json")
    monkeypatch.setattr(mcp_mod, "_kirocrew_mcp_json", lambda: tmp_path / "absent-kc.json")
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", list(cache))
    # Warm cache: the TTL branch must NOT be the thing that triggers a reprobe.
    monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
    monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)

    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", probe)
    return probe


class TestDisabledServerDoesNotDefeatProbeCache:
    @pytest.mark.asyncio
    async def test_disabled_server_does_not_force_a_reprobe(
        self, monkeypatch, tmp_path
    ) -> None:
        """A disabled row absent from the cache must not re-arm the probe.

        Reverted (the check iterating every server rather than only probeable
        ones), this schedules a background probe on a warm cache.
        """
        servers = [
            McpServerInfo(name="enabled-one", command="/bin/true", status="ok"),
            McpServerInfo(name="turned-off", command="/bin/true", disabled=True),
        ]
        # probe_all() would only ever have returned the enabled row.
        cache = [{"name": "enabled-one", "status": "ok", "tools": [], "error": ""}]
        probe = _arrange(monkeypatch, tmp_path, servers, cache)

        state = _State()
        await mcp_mod.api_mcp_servers(_request(state))

        probe.assert_not_awaited()
        assert not state._background_tasks
        assert mcp_mod._mcp_probe_in_progress is False

    @pytest.mark.asyncio
    async def test_new_enabled_server_still_forces_a_reprobe(
        self, monkeypatch, tmp_path
    ) -> None:
        """The feature the check exists for must survive the fix.

        An ENABLED server missing from the cache is genuinely new and still has
        to trigger a probe — the fix narrows the check, it does not remove it.
        """
        servers = [
            McpServerInfo(name="enabled-one", command="/bin/true", status="ok"),
            McpServerInfo(name="just-installed", command="/bin/true"),
        ]
        cache = [{"name": "enabled-one", "status": "ok", "tools": [], "error": ""}]
        _arrange(monkeypatch, tmp_path, servers, cache)

        state = _State()
        await mcp_mod.api_mcp_servers(_request(state))

        # The handler creates a real asyncio task around the patched coroutine.
        assert state._background_tasks, "a genuinely new server must re-arm the probe"
        assert mcp_mod._mcp_probe_in_progress is True
        for task in list(state._background_tasks):
            await task

    @pytest.mark.asyncio
    async def test_disabled_row_is_still_returned_to_the_ui(
        self, monkeypatch, tmp_path
    ) -> None:
        """Skipping disabled rows in the freshness check must not hide them.

        The row still has to reach the response, otherwise the fix would trade
        the spawn treadmill for a missing table entry.
        """
        servers = [
            McpServerInfo(name="enabled-one", command="/bin/true", status="ok"),
            McpServerInfo(name="turned-off", command="/bin/true", disabled=True),
        ]
        cache = [{"name": "enabled-one", "status": "ok", "tools": [], "error": ""}]
        _arrange(monkeypatch, tmp_path, servers, cache)

        resp = await mcp_mod.api_mcp_servers(_request(_State()))

        import json as _json

        rows = _json.loads(resp.text or "[]")
        names = [r["name"] for r in rows]
        assert "turned-off" in names, "a disabled server must still render a row"
        assert "enabled-one" in names


class TestMcpHeaderRedaction:
    @pytest.mark.asyncio
    async def test_servers_list_preserves_header_names_without_values(
        self, monkeypatch, tmp_path
    ) -> None:
        raw_headers = {
            "Authorization": "Bearer list-secret",
            "X-Api-Key": "list-api-key",
        }
        server = McpServerInfo(
            name="remote",
            url="https://mcp.example.com/sse",
            headers=raw_headers,
            status="ok",
        )
        cache = [{"name": "remote", "status": "ok", "tools": [], "error": ""}]
        _arrange(monkeypatch, tmp_path, [server], cache)

        resp = await mcp_mod.api_mcp_servers(_request(_State()))
        body = json.loads(resp.text or "[]")

        assert set(body[0]["headers"]) == set(raw_headers)
        assert set(body[0]["headers"].values()) == {MCP_REDACTED_HEADER_VALUE}
        assert "list-secret" not in (resp.text or "")
        assert "list-api-key" not in (resp.text or "")

    @pytest.mark.asyncio
    async def test_live_probe_preserves_header_names_without_values(
        self, monkeypatch, tmp_path
    ) -> None:
        import kiro_crew.mcp_discovery as disc

        server = McpServerInfo(
            name="remote",
            url="https://mcp.example.com/sse",
            headers={"Authorization": "Bearer probe-secret"},
            status="ok",
        )
        monkeypatch.setattr(disc, "probe_all", AsyncMock(return_value=[server]))
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "absent.json")
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])

        resp = await mcp_mod.api_mcp_probe(_request(_State()))
        body = json.loads(resp.text or "[]")

        assert body[0]["headers"] == {
            "Authorization": MCP_REDACTED_HEADER_VALUE
        }
        assert "probe-secret" not in (resp.text or "")

    @pytest.mark.asyncio
    async def test_cached_probe_redacts_legacy_raw_header_values(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            mcp_mod,
            "_mcp_probe_cache",
            [
                {
                    "name": "remote",
                    "headers": {"Authorization": "Bearer cached-secret"},
                }
            ],
        )
        monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)

        resp = await mcp_mod.api_mcp_probe_cached(_request(_State()))
        body = json.loads(resp.text or "[]")

        assert body[0]["headers"] == {
            "Authorization": MCP_REDACTED_HEADER_VALUE
        }
        assert "cached-secret" not in (resp.text or "")

    @pytest.mark.asyncio
    async def test_live_probe_redacts_reflected_header_credential_suffix(
        self, monkeypatch, tmp_path
    ) -> None:
        import kiro_crew.mcp_discovery as disc

        credential = "live-fake-credential-7e91"
        secret = f"Bearer {credential}"
        server = McpServerInfo(
            name="remote",
            url="https://mcp.example.com/sse",
            headers={"Authorization": secret, "X-Blank": " \t "},
            status="error",
            error=f"Remote reflected {credential}; keep this context",
        )
        monkeypatch.setattr(disc, "probe_all", AsyncMock(return_value=[server]))
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "absent.json")
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])

        resp = await mcp_mod.api_mcp_probe(_request(_State()))
        body = json.loads(resp.text or "[]")

        assert body[0]["error"] == (
            f"Remote reflected {MCP_REDACTED_HEADER_VALUE}; keep this context"
        )
        assert body[0]["headers"] == {
            "Authorization": MCP_REDACTED_HEADER_VALUE,
            "X-Blank": MCP_REDACTED_HEADER_VALUE,
        }
        assert credential.casefold() not in (resp.text or "").casefold()

    @pytest.mark.asyncio
    async def test_cached_probe_redacts_reflected_header_credential_suffix(
        self, monkeypatch
    ) -> None:
        credential = "cached-fake-credential-4c82"
        secret = f"Token {credential}"
        monkeypatch.setattr(
            mcp_mod,
            "_mcp_probe_cache",
            [
                {
                    "name": "remote",
                    "headers": {
                        "Authorization": secret,
                        "X-Whitespace": "   ",
                    },
                    "error": (
                        f"Legacy server echoed {credential} while initializing"
                    ),
                }
            ],
        )
        monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)

        resp = await mcp_mod.api_mcp_probe_cached(_request(_State()))
        body = json.loads(resp.text or "[]")

        assert body[0]["error"] == (
            f"Legacy server echoed {MCP_REDACTED_HEADER_VALUE} while initializing"
        )
        assert body[0]["headers"] == {
            "Authorization": MCP_REDACTED_HEADER_VALUE,
            "X-Whitespace": MCP_REDACTED_HEADER_VALUE,
        }
        assert credential.casefold() not in (resp.text or "").casefold()

    def test_full_authorization_value_is_redacted_exactly_once(self) -> None:
        import kiro_crew.mcp_discovery as disc

        credential = "full-fake-credential-2a63"
        value = f"Bearer {credential}"
        error = f"Remote reflected {value}; keep this context"

        redacted = disc.redact_mcp_error(error, {"Authorization": value})

        assert redacted == (
            f"Remote reflected {MCP_REDACTED_HEADER_VALUE}; keep this context"
        )
        assert redacted.count(MCP_REDACTED_HEADER_VALUE) == 1
        assert credential not in redacted

    def test_long_credential_glued_to_word_characters_is_redacted(self) -> None:
        """A long credential cannot occur inside prose by chance, so it is
        masked as a bare substring even when a server reflects it with no
        surrounding whitespace. Boundary anchoring would suppress this."""
        import kiro_crew.mcp_discovery as disc

        credential = "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"
        headers = {"Authorization": f"Bearer {credential}"}

        for error in (
            f"remote said prefix{credential} bad",
            f"remote said {credential}suffix bad",
            f"a{credential}b",
        ):
            redacted = disc.redact_mcp_error(error, headers)
            assert credential not in redacted
            assert MCP_REDACTED_HEADER_VALUE in redacted

    def test_short_basic_credential_reflected_alone_is_redacted(self) -> None:
        import kiro_crew.mcp_discovery as disc

        credential = "YTpi"
        value = f"Basic {credential}"

        redacted = disc.redact_mcp_error(
            credential,
            {"Authorization": value},
        )

        assert redacted == MCP_REDACTED_HEADER_VALUE
        assert credential not in redacted

    def test_padded_base64_credential_is_redacted_before_punctuation(self) -> None:
        import kiro_crew.mcp_discovery as disc

        credential = "YTpiYw=="
        error = f"Remote reflected {credential}."

        redacted = disc.redact_mcp_error(
            error,
            {"Authorization": f"Basic {credential}"},
        )

        assert redacted == f"Remote reflected {MCP_REDACTED_HEADER_VALUE}."
        assert credential not in redacted

    def test_percent_encoded_padded_basic_token_is_redacted(self) -> None:
        """A URL-encoded reflection must not bypass the literal-value pattern.

        A remote server embedding the configured value in an error URL returns
        it percent-encoded — space as %20, base64 padding as %3D — which
        contains no literal substring of the credential at all.
        """
        import kiro_crew.mcp_discovery as disc

        headers = {"Authorization": "Basic YTpiYw=="}
        error = "GET https://mcp.example.com/auth?hdr=Basic%20YTpiYw%3D%3D failed"

        redacted = disc.redact_mcp_error(error, headers)

        assert "YTpiYw" not in redacted
        assert "%3D" not in redacted
        assert MCP_REDACTED_HEADER_VALUE in redacted

    def test_partially_percent_encoded_credential_is_redacted(self) -> None:
        """Only the padding encoded: a mixed literal/%XX reflection is caught."""
        import kiro_crew.mcp_discovery as disc

        redacted = disc.redact_mcp_error(
            "Remote reflected YTpiYw%3D%3D in its error",
            {"Authorization": "Basic YTpiYw=="},
        )

        assert "YTpiYw" not in redacted
        assert MCP_REDACTED_HEADER_VALUE in redacted

    def test_fully_percent_encoded_credential_is_redacted(self) -> None:
        import kiro_crew.mcp_discovery as disc

        credential = "full-fake-credential-2a63"
        encoded = "".join(f"%{byte:02X}" for byte in credential.encode())
        headers = {"Authorization": f"Bearer {credential}"}

        redacted = disc.redact_mcp_error(f"remote said {encoded}", headers)

        assert encoded not in redacted
        assert MCP_REDACTED_HEADER_VALUE in redacted

    def test_double_percent_encoded_credential_is_redacted(self) -> None:
        """A nested reflection re-encodes the escape's own percent sign."""
        import kiro_crew.mcp_discovery as disc

        headers = {"Authorization": "Basic YTpiYw=="}

        for reflected in ("YTpiYw%253D%253D", "YTpiYw%25253D%25253D"):
            redacted = disc.redact_mcp_error(
                f"nested URL carried {reflected} back", headers
            )
            assert "YTpiYw" not in redacted, reflected
            assert MCP_REDACTED_HEADER_VALUE in redacted

    def test_cached_error_is_redacted_with_probe_time_headers(self) -> None:
        """A rotated credential must not unmask a cached error.

        ``list_servers()`` re-attaches cached errors to server objects built
        from the CURRENT config, so an error cached raw and redacted only at
        serialization time would be masked against the NEW credential while
        still carrying the OLD one.
        """
        import kiro_crew.mcp_discovery as disc

        old_credential = "old-rotated-secret-9f27"
        server = disc.McpServerInfo(
            name="rotating",
            url="https://mcp.example.com/v1",
            headers={"Authorization": f"Bearer {old_credential}"},
            status="error",
            error=f"remote reflected {old_credential} in its failure",
            source="discovered",
        )
        disc._cache_probe(server)
        try:
            status, tools, error, *_rest = disc._get_cached("rotating")
            assert status == "error"
            assert old_credential not in error
            assert MCP_REDACTED_HEADER_VALUE in error
        finally:
            disc._probe_cache.pop("rotating", None)

    def test_plus_encoded_space_in_reflected_value_is_redacted(self) -> None:
        """Form-urlencoding spells the space in ``Basic <token>`` as ``+``."""
        import kiro_crew.mcp_discovery as disc

        redacted = disc.redact_mcp_error(
            "auth failed for Basic+YTpiYw%3d%3d (lowercase hex)",
            {"Authorization": "Basic YTpiYw=="},
        )

        assert "YTpiYw" not in redacted
        assert MCP_REDACTED_HEADER_VALUE in redacted

    def test_credential_shaped_reflection_is_masked_without_configured_match(
        self,
    ) -> None:
        """The generic scanners run on every error, not only configured values.

        A remote server can reflect a credential the config never mentions
        (an access-key id, a connection string); the site-wide scrubbers must
        catch it before serialization.
        """
        import kiro_crew.mcp_discovery as disc

        fake_key = "AKIA" + "IOSFODNN7EXAMPLE"
        redacted = disc.redact_mcp_error(
            f"remote rejected key {fake_key} during handshake",
            {"Authorization": "Bearer unrelated-configured-value"},
        )

        assert fake_key not in redacted

    def test_lone_surrogate_header_value_does_not_crash(self) -> None:
        """JSON permits unpaired surrogate escapes; building the redaction
        pattern must never raise and turn a listing request into a 500."""
        import kiro_crew.mcp_discovery as disc

        headers = {
            "Authorization": "Bearer bad\ud800token",
            "X-Api-Key": "other-real-credential-77",
        }

        redacted = disc.redact_mcp_error(
            "remote reflected other-real-credential-77 and more", headers
        )

        assert "other-real-credential-77" not in redacted
        assert MCP_REDACTED_HEADER_VALUE in redacted

    def test_short_credential_substring_in_unrelated_word_is_not_redacted(
        self,
    ) -> None:
        import kiro_crew.mcp_discovery as disc

        error = "Remote returned prefixYTpicalSuffix as ordinary prose"

        assert disc.redact_mcp_error(
            error,
            {"Authorization": "Basic YTpi"},
        ) == error

    def test_short_authorization_suffix_leaves_ordinary_error_untouched(self) -> None:
        import kiro_crew.mcp_discovery as disc

        error = "A remote server returned a generic failure"

        assert disc.redact_mcp_error(error, {"Authorization": "Bearer a"}) == error

    def test_scheme_less_header_value_is_still_redacted(self) -> None:
        import kiro_crew.mcp_discovery as disc

        credential = "scheme-less-fake-credential-8b14"
        error = f"Remote reflected {credential}"

        assert disc.redact_mcp_error(error, {"X-Api-Key": credential}) == (
            f"Remote reflected {MCP_REDACTED_HEADER_VALUE}"
        )
