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


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        # ── Stable: the tag is the version, `release.yml` tags `vX.Y.Z` ──
        ("0.7.0", "v0.7.0"),
        ("1.2.3", "v1.2.3"),
        # A distribution build stamp (BUILD_VERSION `0.7.0.5`) is a build OF
        # 0.7.0 — the reported shape in the cloud-launch parity refusal — and
        # folds onto the release's tag as changelog.release_of_build folds it
        # onto the release's notes.
        ("0.7.0.5", "v0.7.0"),
        ("0.6.0.12", "v0.6.0"),
        # ── Insider: wheel `rcN` and desktop `-insider.N` name one tag ──
        ("0.7.0rc5", "v0.7.0-insider.5"),
        ("0.7.0-insider.5", "v0.7.0-insider.5"),
        # release.yml publishes `-rc.N` tags to the insider feed too; the
        # desktop spelling still IS that tag.
        ("0.7.0-rc.1", "v0.7.0-rc.1"),
        # ── No tag exists for these; the launcher falls back to `main` ──
        ("0.8.0.dev123", None),
        ("0.7.0-nightly.20260807t061500", None),
        ("0.7.0rc4.dev20260807061500", None),
        # `a` / `b` segments belong to no release lane.
        ("0.7.0b1", None),
        ("0.7.0a2", None),
        ("0.7.0rc4.post1", None),
        # release.yml refuses a base that is not exactly x.y.z.
        ("0.7", None),
        ("0.7.0.5.1", None),
        ("v0.7.0", None),
        ("main", None),
        ("", None),
        ("0.7.0; rm -rf /", None),
    ],
)
def test_release_refs_maps_each_stamping_onto_its_tag(version: str, expected: str | None) -> None:
    """The likeliest tag comes first; a version that names no tag names nothing."""
    refs = release_channel.release_refs(version)
    assert (refs[0] if refs else None) == expected
    assert all(ref.startswith("v") for ref in refs)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        # release.yml writes `rcN` for a `-insider.N` AND a `-rc.N` tag, so the
        # wheel spelling names both — insider (the lane's own naming) first.
        ("0.7.0rc5", ("v0.7.0-insider.5", "v0.7.0-rc.5")),
        # Every other shape names at most one tag.
        ("0.7.0", ("v0.7.0",)),
        ("0.7.0.5", ("v0.7.0",)),
        ("0.7.0-insider.5", ("v0.7.0-insider.5",)),
        ("0.7.0-rc.1", ("v0.7.0-rc.1",)),
        ("0.8.0.dev123", ()),
        ("garbage", ()),
    ],
)
def test_release_refs_lists_every_tag_a_version_may_name(version, expected) -> None:
    assert release_channel.release_refs(version) == expected


def test_release_ref_is_a_safe_clone_ref() -> None:
    """Every answer must pass `cloud.ec2`'s ref charset: it is inlined into the
    instance's `git clone --branch` and a stray byte there would run as root."""
    from kiro_crew.cloud import ec2

    for version in ("0.7.0", "0.7.0.5", "0.7.0rc5", "0.7.0-insider.5", "0.7.0-rc.1"):
        refs = release_channel.release_refs(version)
        assert refs
        for ref in refs:
            assert ec2._REF_RE.match(ref), ref


def test_release_refs_default_to_this_build(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.release_channel.__version__", "9.9.9rc7")
    assert release_channel.release_refs() == ("v9.9.9-insider.7", "v9.9.9-rc.7")
    monkeypatch.setattr("kiro_crew.release_channel.__version__", "9.9.9.dev1")
    assert release_channel.release_refs() == ()
