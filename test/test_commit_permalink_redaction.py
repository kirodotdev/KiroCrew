"""Commit permalinks pass through credential redaction unchanged."""

from __future__ import annotations

import random

import pytest

from kiro_crew.security import redact, redact_credentials

COMMIT = "cc504736836c7c1905f86f8cd4e0c9b46b37347c"
GATEWAY_LINK = (
    "https://github.com/kirodotdev/KiroCrew/blob/{}/src/kiro_crew/slack/gateway.py#L6257-L6274"
)

LINK_TEMPLATES = [
    GATEWAY_LINK,
    "https://github.com/kirodotdev/KiroCrew/tree/{}/src/kiro_crew",
    "https://github.com/microsoft/TypeScript/blame/{}/src/compiler/checker.ts#L10",
    "https://github.com/microsoft/TypeScript/raw/{}/README.md",
    "https://bitbucket.org/atlassian/Docker-Builds/src/{}/README.md",
    # Hosts that spell their routes in the plural.
    "https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct/blobs/{}",
    "https://gitlab.example.com/OpsCrew/BuildTooling/-/trees/{}/src/kiro_crew",
]
LINK_COMMITS = [
    COMMIT,
    "c1cfc2376b8a578d55d083243c47dff503e02dc9",
    COMMIT,
    COMMIT,
    "2c73b114eeb1c7078f04d5a68f19a283f4d8c2fd",
    COMMIT,
    COMMIT,
]
LINKS = [template.format(commit) for template, commit in zip(LINK_TEMPLATES, LINK_COMMITS)]


@pytest.mark.parametrize("url", LINKS)
@pytest.mark.parametrize(
    "template",
    ["{}", "see {} for details.", "[gateway.py]({})", "<{}>", "`{}`", '{{"url":"{}"}}'],
)
def test_commit_permalink_survives(url: str, template: str) -> None:
    text = template.format(url)
    assert redact_credentials(text) == (text, [])
    assert redact(text) == text


@pytest.mark.parametrize("template", LINK_TEMPLATES)
def test_no_seeded_commit_fires(template: str) -> None:
    rng = random.Random(template)
    for _ in range(200):
        commit = "".join(rng.choice("0123456789abcdef") for _ in range(40))
        url = template.format(commit)
        assert redact_credentials(url) == (url, []), url
