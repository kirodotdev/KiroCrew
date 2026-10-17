"""Commit permalinks survive the bare-secret pass; a key beside a commit does not."""

from __future__ import annotations

import random
import re
import string

import pytest

from kiro_crew.security import redact, redact_credentials
from kiro_crew.security import redaction as _redaction

# The canonical AWS documentation example secret key, never a live credential.
KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
# Example-derived keys carrying twelve hex digits at the start, at the end, after a
# `/` at the end, after a route word and `/` at the end, and before a `/` at the start.
HEAD_HEX_KEY = "3f9c0a7b1d4eI/K7MDENG/bPxRfiCYEXAMPLEKEY"
TAIL_HEX_KEY = "I/K7MDENG/bPxRfiCYEXAMPLEKEY3f9c0a7b1d4e"
SLASH_HEX_KEY = "wJalrXUtnFEMIQK7MDENGQbPxRf/3f9c0a7b1d4e"
ROUTE_HEX_KEY = "wJalrXUtnFEMIQK7MDENGQ/blob/3f9c0a7b1d4e"
ROUTE_HEX20_KEY = "yF8SCAKz70XBT8/blob/3f9c0a7b1d4e7c2b9a0f"
LEAD_SLASH_KEY = "3f9c0a7b1d4e/K7MDENGQbPxRfiCYEXAMPLEKEYw"
# A key-shaped token beginning with 28 hex digits and a `/`, so that twelve hex
# digits after a route complete a 40-digit commit inside it.
HEX_HEAD_KEY = "3f9c0a7b1d4e7c2b9a0f5d3e8c1b/RWG27KFEB2V"
COMMIT = "cc504736836c7c1905f86f8cd4e0c9b46b37347c"
TAG = "[REDACTED: credential]"
GATEWAY_LINK = (
    "https://github.com/kirodotdev/KiroCrew/blob/{}/src/kiro_crew/slack/gateway.py#L6257-L6274"
)

# Each fires the bare-secret heuristic when the commit ceiling is absent: the window
# straddling the repository name and the start of the commit reads as a key.
FIRING_LINK_TEMPLATES = [
    GATEWAY_LINK,
    "https://github.com/kirodotdev/KiroCrew/tree/{}/src/kiro_crew",
    "https://github.com/microsoft/TypeScript/blame/{}/src/compiler/checker.ts#L10",
    "https://github.com/microsoft/TypeScript/raw/{}/README.md",
    "https://bitbucket.org/atlassian/Docker-Builds/src/{}/README.md",
]
FIRING_COMMITS = [
    COMMIT,
    "c1cfc2376b8a578d55d083243c47dff503e02dc9",
    COMMIT,
    COMMIT,
    "2c73b114eeb1c7078f04d5a68f19a283f4d8c2fd",
]
FIRING_LINKS = [
    template.format(commit) for template, commit in zip(FIRING_LINK_TEMPLATES, FIRING_COMMITS)
]


def _without_the_commit_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_redaction, "_ROUTED_COMMIT_RE", re.compile(r"(?!)"))


def test_the_fixtures_are_what_they_claim() -> None:
    for key in (KEY, HEAD_HEX_KEY, TAIL_HEX_KEY, SLASH_HEX_KEY, ROUTE_HEX_KEY, LEAD_SLASH_KEY):
        assert len(key) == 40
        assert _redaction._looks_like_secret_key(key), key
    assert HEAD_HEX_KEY.startswith("3f9c0a7b1d4e")
    assert TAIL_HEX_KEY.endswith("3f9c0a7b1d4e")
    assert SLASH_HEX_KEY.endswith("/3f9c0a7b1d4e")
    assert ROUTE_HEX_KEY.endswith("/blob/3f9c0a7b1d4e")
    assert _redaction._looks_like_secret_key(ROUTE_HEX20_KEY)
    assert re.fullmatch(r".{14}/blob/[0-9a-f]{20}", ROUTE_HEX20_KEY)
    assert LEAD_SLASH_KEY.startswith("3f9c0a7b1d4e/")
    for commit in (COMMIT, COMMIT + COMMIT[:24]):
        assert _redaction._ROUTED_COMMIT_RE.fullmatch(f"blob/{commit}"), commit
    assert not _redaction._ROUTED_COMMIT_RE.search(f"blob/{COMMIT.upper()}/")
    assert not _redaction._ROUTED_COMMIT_RE.search(f"repo/{COMMIT}/")
    assert _redaction._looks_like_secret_key(HEX_HEAD_KEY)
    assert re.fullmatch(r"[0-9a-f]{28}/.{11}", HEX_HEAD_KEY)
    assert not _redaction._ROUTED_COMMIT_RE.search(f"repo/{COMMIT}/")


@pytest.mark.parametrize("url", FIRING_LINKS)
def test_the_fixture_link_trips_the_heuristic_without_the_ceiling(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_the_commit_ceiling(monkeypatch)
    assert _redaction._text_contains_bare_secret(url)
    assert redact_credentials(url)[0] != url


@pytest.mark.parametrize("url", FIRING_LINKS)
@pytest.mark.parametrize(
    "template",
    ["{}", "see {} for details.", "[gateway.py]({})", "<{}>", "`{}`", '{{"url":"{}"}}'],
)
def test_commit_permalink_survives(url: str, template: str) -> None:
    text = template.format(url)
    assert redact_credentials(text) == (text, [])
    assert redact(text) == text
    assert not _redaction._text_contains_bare_secret(text)


@pytest.mark.parametrize("template", FIRING_LINK_TEMPLATES)
def test_no_seeded_commit_fires(template: str) -> None:
    rng = random.Random(template)
    for _ in range(200):
        commit = "".join(rng.choice("0123456789abcdef") for _ in range(40))
        url = template.format(commit)
        assert redact_credentials(url) == (url, []), url


@pytest.mark.parametrize(
    "text",
    [
        f"{COMMIT}{KEY}",
        f"{KEY}{COMMIT}",
        f"{COMMIT.upper()}{KEY}",
        f"https://github.com/kirodotdev/KiroCrew/blob/{KEY}",
        f"https://github.com/kirodotdev/KiroCrew/blob/{COMMIT}/{KEY}",
        f"https://github.com/kirodotdev/KiroCrew/blob/{COMMIT}/src/{KEY}/gateway.py",
    ],
)
def test_a_key_beside_a_commit_is_still_redacted(text: str) -> None:
    result, warnings = redact_credentials(text)
    assert KEY not in result
    assert TAG in result
    assert warnings


def test_seeded_keys_glued_to_a_commit_are_redacted() -> None:
    """Every key-shaped window a key brings is still classified at its own offset."""
    rng = random.Random(7)
    alphabet = string.ascii_letters + string.digits + "+/"
    checked = 0
    while checked < 300:
        key = "".join(rng.choice(alphabet) for _ in range(40))
        if not _redaction._looks_like_secret_key(key):
            continue
        if key.count("/") > _redaction._SECRET_MAX_SLASHES:
            continue
        for text in (
            f"{COMMIT}{key}",
            f"https://github.com/kirodotdev/KiroCrew/blob/{COMMIT}/{key}",
            f"https://github.com/kirodotdev/KiroCrew/blob/{key}",
            f"{key}{COMMIT}",
        ):
            assert key not in redact_credentials(text)[0], text
        checked += 1


@pytest.mark.parametrize("text", [HEAD_HEX_KEY, f"secret: {HEAD_HEX_KEY}\n", f'["{HEAD_HEX_KEY}"]'])
def test_a_standalone_key_holding_hex_digits_is_still_redacted(text: str) -> None:
    """The ceiling reads only a fragment: a 40-char run IS the token somebody wrote."""
    result, _ = redact_credentials(text)
    assert HEAD_HEX_KEY not in result
    assert TAG in result


@pytest.mark.parametrize(
    ("key", "text"),
    [
        (HEAD_HEX_KEY, f"X{HEAD_HEX_KEY}"),
        (HEAD_HEX_KEY, f"{HEAD_HEX_KEY}A"),
        (HEAD_HEX_KEY, f"https://evil.example/collect/{HEAD_HEX_KEY}"),
        (HEAD_HEX_KEY, f"{COMMIT}/{HEAD_HEX_KEY}"),
        (HEAD_HEX_KEY, f"{COMMIT}{HEAD_HEX_KEY}"),
        (HEAD_HEX_KEY, f"https://github.com/kirodotdev/KiroCrew/blob/{HEAD_HEX_KEY}"),
        (TAIL_HEX_KEY, f"{TAIL_HEX_KEY}{COMMIT}"),
        (SLASH_HEX_KEY, f"{SLASH_HEX_KEY}{COMMIT}"),
        (SLASH_HEX_KEY, f"{SLASH_HEX_KEY}{COMMIT[:28]}/README.md"),
        (LEAD_SLASH_KEY, f"{COMMIT[:28]}{LEAD_SLASH_KEY}"),
        (
            LEAD_SLASH_KEY,
            f"https://github.com/kirodotdev/KiroCrew/blob/{COMMIT[:28]}{LEAD_SLASH_KEY}",
        ),
        (
            HEX_HEAD_KEY,
            f"https://github.com/kirodotdev/KiroCrew/blob/{COMMIT[:12]}{HEX_HEAD_KEY}",
        ),
    ],
)
def test_a_glued_key_holding_hex_digits_is_still_redacted(key: str, text: str) -> None:
    """Only hex right after a code-host route is a commit; hex a key carries is not."""
    result, warnings = redact_credentials(text)
    assert key not in result
    assert TAG in result
    assert warnings


class _CountingStarts(list[int]):
    """A list of commit starts that counts every element read, by index or by iteration."""

    def __init__(self, starts: list[int]) -> None:
        super().__init__(starts)
        self.reads = 0

    def __getitem__(self, index):  # type: ignore[no-untyped-def]
        self.reads += 1
        return super().__getitem__(index)

    def __iter__(self):  # type: ignore[no-untyped-def]
        for start in super().__iter__():
            self.reads += 1
            yield start


def test_a_window_reads_only_the_commit_it_can_cross() -> None:
    """Complexity guard: one lookup reads a logarithmic number of starts, not all.

    Reading every commit per window made a 108 KB run of routed commits take
    about 15 seconds. 4,096 commits allow 13 bisect reads and one index.
    """
    starts = _CountingStarts([i * 46 for i in range(4096)])
    assert _redaction._crosses_into_a_routed_commit(46 * 2048 - 20, starts)
    assert 0 < starts.reads <= 32, starts.reads


@pytest.mark.parametrize(
    "glued",
    [
        f"{ROUTE_HEX_KEY}{COMMIT[:28]}/README.md",
        f"{ROUTE_HEX20_KEY}{COMMIT[:20]}/README.md",
    ],
)
def test_the_accepted_residual_is_a_key_that_ends_in_a_routed_commit(glued: str) -> None:
    """A key ending in `/blob/` and twelve or more hex digits, glued to the rest of 40.

    The key then carries the route itself, so its window holds twelve or more digits
    of a routed commit, which at this layer is byte-identical to a window straddling
    a repository name and a commit. Pinned as a decision, not an accident.
    """
    assert redact_credentials(glued) == (glued, [])
