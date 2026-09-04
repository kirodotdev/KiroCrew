"""The release-channel classifier — the one Python answer, tested directly.

Why this file exists separately from ``test_diagnostics.py``: three surfaces
depend on this rule agreeing (the bug-report label, the dashboard status
payload, and the issue-triage workflow's label vocabulary), and a disagreement
between them is SILENT — a prerelease build classified stable simply stops
producing distinguishable bug reports, with no error anywhere.
"""

from __future__ import annotations

import pytest

from kiro_crew import release_channel


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        # ── Desktop / SemVer, as stamped by nightly.yml and release.yml ──
        ("0.1.4", "stable"),
        ("1.2.3", "stable"),
        ("0.1.4-nightly.20260807t061500", "nightly"),
        ("0.1.4-insider.2", "insider"),
        # release.yml maps -rc.N tags onto the INSIDER feed, so -rc is insider.
        ("0.1.4-rc.1", "insider"),
        # ── Wheel / PEP 440, as rewritten into __version__ by build-wheel.yml ──
        # Neither spelling contains a `-`. A hyphen-only rule (what this module
        # replaced) called both "stable", which silently made every prerelease
        # CLI install's bug report look like it came from a supported build.
        ("0.1.4rc4", "insider"),
        ("0.1.4b1", "insider"),
        ("0.1.4a2", "insider"),
        ("0.1.4.dev20260807061500", "nightly"),
        # A post-release of a prerelease is still that prerelease.
        ("0.1.4rc4.post1", "insider"),
    ],
)
def test_channel_covers_both_stamping_conventions(version: str, expected: str) -> None:
    assert release_channel.channel(version) == expected


def test_nightly_wins_over_a_prerelease_segment() -> None:
    """`.dev` is checked first, so a dev build off an rc base is still nightly.

    Nightly builds come off main HEAD, which is the more useful answer for
    triage than "rc" — a nightly report is usually a PR that merged hours ago.
    """
    assert release_channel.channel("0.1.4rc4.dev20260807061500") == "nightly"


@pytest.mark.parametrize(
    "version", ["0.1.4-nightly.20260807t0615", "0.1.4-insider.1", "0.1.4rc4", "0.1.4.dev1"]
)
def test_is_prerelease_agrees_with_channel(version: str) -> None:
    assert release_channel.is_prerelease(version) is True
    assert release_channel.channel(version) != "stable"


def test_stable_is_not_a_prerelease() -> None:
    assert release_channel.is_prerelease("1.2.3") is False


def test_every_channel_has_a_label_and_a_form_option() -> None:
    """A channel with no label cannot be triaged; one with no option cannot be
    prefilled into the issue form. Both maps must cover the full vocabulary."""
    assert set(release_channel.CHANNEL_LABELS) == set(release_channel.CHANNELS)
    assert set(release_channel.CHANNEL_FORM_OPTIONS) == set(release_channel.CHANNELS)


def test_labels_share_one_prefix() -> None:
    """`issue-triage.yml` detects an existing channel label by the `channel: `
    prefix, and the model's allowlist excludes that prefix by construction. A
    label that broke the convention would slip both controls."""
    for label in release_channel.CHANNEL_LABELS.values():
        assert label.startswith("channel: "), label


def test_channel_defaults_to_this_build(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.release_channel.__version__", "9.9.9-insider.7")
    assert release_channel.channel() == "insider"
    assert release_channel.is_prerelease() is True
