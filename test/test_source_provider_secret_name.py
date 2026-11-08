"""A provider error's secret name: its own field, never text the scrub removed.

A source provider can name the vault secret its remedy needs (Bitbucket Server's
per-host token) as ``SourceProviderError.secret_name``; the source API ships it
as ``secretName`` so the panel can show it as copyable text. That field is the
one piece of a plugin's error the client shows OUTSIDE the scrubbed sentence, so
the seam keeps it only in a Bitbucket Server token-name shape and only while the
redacted message still names it, and the source API ships it only when it is a
name Bitbucket Server code derives from the allowlist: a shape alone cannot tell
a name from an opaque token a provider repeated in its own text.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.dashboard.source_providers import bitbucket_server, hosts, plugins, sanitize
from kiro_crew.dashboard.source_providers.contract import (
    SourceProviderError,
    SourceProviderNotConfigured,
    SourceRef,
    safe_secret_name,
)

NAME = "BITBUCKET_SERVER_TOKEN_" + "AB" * 20
#: An upper-case value the credential scrub does not recognize: what an opaque
#: token in a format no redaction rule knows looks like to the shape check.
OPAQUE = "QX7ZK2M9WV4OPAQUE5TOKEN8R3D"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "lower_case",
        "1LEADING_DIGIT",
        "HAS SPACE",
        "HAS-DASH",
        "A" * 641,
        None,
        42,
        OPAQUE,
        "BITBUCKET_SERVER_TOKENX",
        "BITBUCKET_SERVER_TOKEN_",
        "BITBUCKET_SERVER_TOKEN_ABC",
        "BITBUCKET_SERVER_TOKEN_ab12",
        "BITBUCKET_SERVER_TOKEN_" + "AB" * 260,
    ],
)
def test_a_malformed_secret_name_is_dropped_at_construction(value) -> None:
    assert safe_secret_name(value) == ""
    assert SourceProviderError("x", secret_name=value).secret_name == ""  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [bitbucket_server.GLOBAL_TOKEN_NAME, NAME])
def test_a_well_formed_secret_name_is_kept(value) -> None:
    assert SourceProviderError("x", secret_name=value).secret_name == value


def test_the_per_host_name_for_the_longest_listable_host_fits_the_shape() -> None:
    # A 253-character hostname plus a port, hex-encoded: 23 + 2 x 259 characters.
    host = ".".join(["a" * 63] * 3 + ["b" * 61]) + ":65535"
    assert len(host) == 259
    name = "BITBUCKET_SERVER_TOKEN_" + host.encode().hex().upper()
    assert safe_secret_name(name) == name


def test_the_seam_keeps_a_long_name_the_display_cut_would_split() -> None:
    # The sentence is cut to its display length, which here falls inside the
    # name; the field is then the only place the reader gets the name whole.
    long_name = "BITBUCKET_SERVER_TOKEN_" + "AB" * 259
    message = (
        "Bitbucket Server at a host needs an HTTP access token with read access. "
        f"Store it in Settings > Secrets as {long_name}."
    )
    assert long_name not in sanitize._safe_error_text(message)
    with pytest.raises(SourceProviderNotConfigured) as caught:
        with plugins._plugin_errors("acme"):
            raise SourceProviderNotConfigured(message, secret_name=long_name)
    assert caught.value.secret_name == long_name
    replaced = plugins._plugin_setup_error(_Plugin(""), caught.value)  # type: ignore[arg-type]
    assert replaced.secret_name == long_name


@pytest.mark.parametrize("error_type", [SourceProviderError, SourceProviderNotConfigured])
def test_the_seam_keeps_a_secret_name_the_scrubbed_message_still_names(error_type) -> None:
    with pytest.raises(error_type) as caught:
        with plugins._plugin_errors("acme"):
            raise error_type(f"Store a token as {NAME}.", secret_name=NAME)
    assert caught.value.secret_name == NAME
    assert type(caught.value) is error_type


def test_the_seam_drops_a_secret_name_the_message_does_not_name() -> None:
    with pytest.raises(SourceProviderError) as caught:
        with plugins._plugin_errors("acme"):
            raise SourceProviderError("Store a token in Settings.", secret_name=NAME)
    assert caught.value.secret_name == ""


def test_the_seam_drops_a_secret_name_only_part_of_a_longer_word_matches() -> None:
    with pytest.raises(SourceProviderError) as caught:
        with plugins._plugin_errors("acme"):
            raise SourceProviderError(f"Store {NAME}_EXTRA.", secret_name=NAME)
    assert caught.value.secret_name == ""


def test_the_seam_drops_a_secret_name_the_scrub_removed_from_the_message() -> None:
    # A credential the scrub recognizes: the shape check already refuses this
    # one, and the seam's own rule refuses it again on its own, so the field
    # never carries it to the client after the scrub took it out of the sentence.
    leaked = "AKIAIOSFODNN7EXAMPLE"
    message = f"Store {leaked} as the token."
    assert leaked not in sanitize._safe_error_text(message)
    assert not plugins._secret_name_survives_scrub(leaked)
    with pytest.raises(SourceProviderError) as caught:
        with plugins._plugin_errors("acme"):
            raise SourceProviderError(message, secret_name=leaked)
    assert caught.value.secret_name == ""
    assert leaked not in str(caught.value)


class _Plugin:
    id = "acme"

    def __init__(self, setup: str) -> None:
        self._setup = setup

    def setup_message(self) -> str:
        return self._setup


def test_setup_guidance_keeps_the_name_when_the_plugin_falls_back_to_its_own_text() -> None:
    exc = SourceProviderNotConfigured(f"Store a token as {NAME}.", secret_name=NAME)
    replaced = plugins._plugin_setup_error(_Plugin(""), exc)  # type: ignore[arg-type]
    assert replaced.secret_name == NAME


def test_setup_guidance_drops_the_name_a_replacing_setup_message_does_not_name() -> None:
    exc = SourceProviderNotConfigured(f"Store a token as {NAME}.", secret_name=NAME)
    replaced = plugins._plugin_setup_error(  # type: ignore[arg-type]
        _Plugin("Ask your administrator to connect Acme."), exc
    )
    assert str(replaced) == "Ask your administrator to connect Acme."
    assert replaced.secret_name == ""


# --- the source API ships only a name Bitbucket Server code derives ----------

BB_HOST = "bitbucket.acme.example:7990"
OTHER_HOST = "other.acme.example"
ACME_URL = "https://review.acme.example/cr/7"


def test_the_shippable_names_are_the_ones_the_allowlist_derives(monkeypatch) -> None:
    monkeypatch.setattr(hosts, "_bitbucket_server_hosts_snapshot", frozenset())
    assert bitbucket_server.token_secret_names() == frozenset()
    monkeypatch.setattr(hosts, "_bitbucket_server_hosts_snapshot", frozenset({BB_HOST}))
    assert bitbucket_server.token_secret_names() == {bitbucket_server.GLOBAL_TOKEN_NAME}
    monkeypatch.setattr(hosts, "_bitbucket_server_hosts_snapshot", frozenset({BB_HOST, OTHER_HOST}))
    names = bitbucket_server.token_secret_names()
    assert names == {
        bitbucket_server.host_token_name(BB_HOST),
        bitbucket_server.host_token_name(OTHER_HOST),
    }
    # Every name the provider can tell the reader about passes the shape check.
    assert all(safe_secret_name(name) == name for name in names)


class _AcmeReview:
    """A registered provider whose read fails, naming ``secret_name`` in its text."""

    id = "acme"

    def __init__(self) -> None:
        self.secret_name = ""

    def message(self) -> str:
        return f"Acme rejected the token. Replace {self.secret_name} in Settings > Secrets."

    def parse(self, raw_url: str) -> SourceRef | None:
        if raw_url != ACME_URL:
            return None
        return SourceRef("acme", ACME_URL, "review.acme.example", "", "acme", 7, kind="change")

    async def fetch_full(self, ref: SourceRef, *, refresh: bool = False) -> dict[str, Any]:
        raise SourceProviderError(self.message(), secret_name=self.secret_name)

    async def fetch_checks(self, ref: SourceRef) -> list[dict[str, Any]]:
        return []

    def setup_message(self) -> str:
        return ""


@pytest.fixture
def acme_route(tmp_path, monkeypatch):
    """The real pull-request route, a registered provider, one Bitbucket host listed."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"dashboard": {"bitbucket_server_hosts": [BB_HOST]}}), encoding="utf-8"
    )
    for name in (
        "_github_hosts_snapshot",
        "_gitlab_hosts_snapshot",
        "_jira_hosts_snapshot",
        "_bitbucket_server_hosts_snapshot",
        "_gerrit_hosts_snapshot",
    ):
        monkeypatch.setattr(hosts, name, frozenset())
    monkeypatch.setattr(hosts, "_gitlab_hosts_loaded_at", 0.0)
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())
    for name in (
        "_CACHE",
        "_FULL_FETCH_INFLIGHT",
        "_FULL_FETCH_TASKS",
        "_FULL_FETCH_GENERATIONS",
        "_CHECKS_FETCH_INFLIGHT",
    ):
        monkeypatch.setattr(source, name, {})
    source.reset_source_providers_for_tests()
    provider = _AcmeReview()
    source.register_source_provider(provider)  # type: ignore[arg-type]
    with patch("kiro_crew.config.loader.config_path", return_value=path):
        yield provider
    source.reset_source_providers_for_tests()


async def _read_pull_request() -> dict[str, Any]:
    @web.middleware
    async def owner(request, handler):
        request["user"] = "U_OWNER"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    state = MagicMock()
    state.owner_id = "U_OWNER"
    app["state"] = state
    app.router.add_post("/api/source/pull-request", source.api_pull_request_source)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/source/pull-request", json={"url": ACME_URL})
        assert resp.status == 503
        return await resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opaque",
    [OPAQUE, "BITBUCKET_SERVER_TOKEN_" + OPAQUE.encode().hex().upper()],
    ids=["bare", "in-the-per-host-name-form"],
)
async def test_an_opaque_token_a_provider_repeats_never_ships_as_the_secret_name(
    acme_route, opaque
) -> None:
    # The scrub knows no rule for this value, so it survives in the sentence and
    # passes the seam's name-in-text check. Its shape cannot prove it is a name,
    # so the structured field must not declassify it. The second form passes the
    # token-name shape too, and is refused only because no listed host derives it.
    acme_route.secret_name = opaque
    assert opaque in sanitize._safe_error_text(acme_route.message())
    body = await _read_pull_request()
    assert body["code"] == "provider_error"
    assert "secretName" not in body


@pytest.mark.asyncio
async def test_a_name_the_allowlist_derives_still_ships_as_the_secret_name(acme_route) -> None:
    acme_route.secret_name = bitbucket_server.GLOBAL_TOKEN_NAME
    body = await _read_pull_request()
    assert body["secretName"] == bitbucket_server.GLOBAL_TOKEN_NAME
