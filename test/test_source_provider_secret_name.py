"""A provider error's secret name: its own field, never text the scrub removed.

A source provider can name the vault secret its remedy needs (Bitbucket Server's
per-host token) as ``SourceProviderError.secret_name``; the source API ships it
as ``secretName`` so the panel can show it as copyable text. That field is the
one piece of a plugin's error the client shows OUTSIDE the scrubbed sentence, so
the seam keeps it only in its identifier shape and only while the redacted
message still names it.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.source_providers import plugins, sanitize
from kiro_crew.dashboard.source_providers.contract import (
    SourceProviderError,
    SourceProviderNotConfigured,
    safe_secret_name,
)

NAME = "BITBUCKET_SERVER_TOKEN_" + "AB" * 20


@pytest.mark.parametrize(
    "value",
    ["", "lower_case", "1LEADING_DIGIT", "HAS SPACE", "HAS-DASH", "A" * 641, None, 42],
)
def test_a_malformed_secret_name_is_dropped_at_construction(value) -> None:
    assert safe_secret_name(value) == ""
    assert SourceProviderError("x", secret_name=value).secret_name == ""  # type: ignore[arg-type]


def test_a_well_formed_secret_name_is_kept() -> None:
    assert SourceProviderError("x", secret_name=NAME).secret_name == NAME


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
    # An identifier-shaped credential would pass the shape check; the field must
    # not carry it to the client after the scrub took it out of the sentence.
    leaked = "AKIAIOSFODNN7EXAMPLE"
    message = f"Store {leaked} as the token."
    assert leaked not in sanitize._safe_error_text(message)
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
