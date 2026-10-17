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


# ── Report provenance: what a bug report may CLAIM about its lane ────────────
#
# `channel()` classifies a string. A packaged build may carry two strings that
# disagree -- a repackager's four-part BUILD_VERSION stamp (which the stamp rule
# only admits over a bare base, so stamping strips any prerelease marker) beside
# distribution metadata that kept the `rc` -- plus a `$KIROCREW_HOME/channel`
# record. `report_channel` weighs those sources and answers None rather than
# guess, because the answer becomes a create-time label on a public issue.


@pytest.mark.parametrize(
    ("version", "distribution", "recorded", "expected"),
    [
        # The reported case: plain wrapper stamp, rc distribution, insider
        # record -- with and without the record, the rc marker decides.
        ("0.7.0.5", "0.7.0rc7", "insider", "insider"),
        ("0.7.0.5", "0.7.0rc7", None, "insider"),
        # A stamp alone is lane-less: no metadata, no record -> unknown, never
        # Stable. A record supplies the lane. PLAIN metadata does not: the
        # desktop lanes pip-install the checkout and stamp only __version__,
        # so plain dist-info is what an unstamped pyproject leaves behind on
        # any lane -- silence, not a stable claim. Only a marker there counts.
        ("0.7.0.5", None, None, None),
        ("0.7.0.5", None, "insider", "insider"),
        ("0.7.0.5", None, "stable", "stable"),
        ("0.7.0.5", "0.7.0", None, None),
        ("0.7.0.5", "0.7.0", "stable", "stable"),
        ("0.6.0.12", "0.6.0.12", None, None),
        ("0.6.0.12", "0.6.0.12", "stable", "stable"),
        # A desktop insider or nightly build: stamped __version__, plain
        # dist-info from the unstamped pyproject. Its own marker decides.
        ("0.7.0-insider.4", "0.7.0", None, "insider"),
        ("0.7.0-nightly.20260907t061500", "0.7.0", None, "nightly"),
        ("0.7.0rc7", "0.7.0", None, "insider"),
        # Contradictions answer None: a stable record over a prerelease build
        # (promotion never re-stamps; a lane switch not yet applied), a lane
        # record over a plain stable build, two markers that disagree, or a
        # bare literal over an rc distribution (the marker is IN the metadata).
        ("0.7.0rc7", "0.7.0rc7", "stable", None),
        ("0.7.0", "0.7.0", "insider", None),
        ("0.7.0-nightly.20260907t061500", "0.7.0rc7", None, None),
        ("0.7.0", "0.7.0rc7", None, None),
        # The release pipeline's own spellings need no corroboration.
        ("0.7.0", None, None, "stable"),
        ("0.7.0", "0.7.0", None, "stable"),
        ("0.7.0", "0.7.0", "stable", "stable"),
        ("0.7.0rc7", None, None, "insider"),
        ("0.7.0rc7", "0.7.0rc7", "insider", "insider"),
        ("0.7.0-insider.4", None, None, "insider"),
        ("0.7.0-insider.4", "0.7.0rc4", None, "insider"),
        ("0.7.0.dev20260907061500", "0.7.0.dev20260907061500", "nightly", "nightly"),
        ("0.7.0-nightly.20260907t061500", None, None, "nightly"),
        # Metadata describing ANOTHER release is stale dist-info (a checkout
        # ahead of its editable install) and says nothing about this build.
        ("0.8.0", "0.1.2", None, "stable"),
        ("0.8.0", "0.7.0rc7", None, "stable"),
        ("0.8.0rc1", "0.7.0", None, "insider"),
        # A record that names no lane is no record.
        ("0.7.0.5", None, "beta", None),
        ("0.7.0", None, "", "stable"),
    ],
)
def test_report_channel_weighs_every_provenance_source(
    version: str, distribution: str | None, recorded: str | None, expected: str | None
) -> None:
    assert release_channel.report_channel(version, distribution, recorded) == expected


def test_report_channel_never_invents_a_lane() -> None:
    """Every non-None answer is a real channel, so it can be labelled and prefilled."""
    for version in ("0.7.0.5", "0.7.0", "0.7.0rc7", "0.7.0-nightly.20260907t061500", "garbage"):
        for distribution in (None, "0.7.0", "0.7.0rc7", "0.7.0.5"):
            for recorded in (None, *release_channel.CHANNELS):
                answer = release_channel.report_channel(version, distribution, recorded)
                assert answer is None or answer in release_channel.CHANNELS, (
                    version,
                    distribution,
                    recorded,
                    answer,
                )


def test_recorded_channel_reads_the_install_record(tmp_path, monkeypatch) -> None:
    """Absent-aware, unlike `update_layout.release_channel`, which must always
    name a lane to follow: here an absent or junk record is NO evidence."""
    monkeypatch.setattr(release_channel, "data_home", lambda: tmp_path)
    assert release_channel.recorded_channel() is None
    (tmp_path / "channel").write_text(" Insider \n", encoding="utf-8")
    assert release_channel.recorded_channel() == "insider"
    (tmp_path / "channel").write_text("beta\n", encoding="utf-8")
    assert release_channel.recorded_channel() is None
    (tmp_path / "channel").write_text("", encoding="utf-8")
    assert release_channel.recorded_channel() is None


def test_recorded_channel_tolerates_an_unreadable_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(release_channel, "data_home", lambda: tmp_path)
    (tmp_path / "channel").mkdir()  # a directory where a file should be
    assert release_channel.recorded_channel() is None


def test_distribution_version_is_none_without_metadata(monkeypatch) -> None:
    from importlib.metadata import PackageNotFoundError

    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(release_channel, "_metadata_version", missing)
    assert release_channel.distribution_version() is None


def test_distribution_version_reads_the_installed_metadata(monkeypatch) -> None:
    monkeypatch.setattr(release_channel, "_metadata_version", lambda name: " 0.7.0rc7\n")
    assert release_channel.distribution_version() == "0.7.0rc7"


@pytest.mark.parametrize(
    ("version", "release"),
    [
        # Only a repackager's four-part build stamp folds; the release
        # pipeline's public spellings ARE the public identity and stay.
        ("0.7.0.5", "0.7.0"),
        ("0.6.0.12", "0.6.0"),
        ("0.7.0", "0.7.0"),
        ("0.7.0rc7", "0.7.0rc7"),
        ("0.7.0-insider.4", "0.7.0-insider.4"),
        ("0.7.0-nightly.20260907t061500", "0.7.0-nightly.20260907t061500"),
        ("0.7.0.dev20260907061500", "0.7.0.dev20260907061500"),
    ],
)
def test_provenance_folds_only_the_build_stamp_off_the_public_release(
    monkeypatch, version: str, release: str
) -> None:
    monkeypatch.setattr(release_channel, "distribution_version", lambda: None)
    monkeypatch.setattr(release_channel, "recorded_channel", lambda: None)
    found = release_channel.provenance(version)
    assert found.version == version
    assert found.release == release
    assert found.distribution is None
    assert found.recorded is None


def test_provenance_defaults_to_this_build_and_reads_both_probes(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.release_channel.__version__", "9.9.9.3")
    monkeypatch.setattr(release_channel, "distribution_version", lambda: "9.9.9rc2")
    monkeypatch.setattr(release_channel, "recorded_channel", lambda: "insider")
    found = release_channel.provenance()
    assert found == release_channel.Provenance(
        version="9.9.9.3",
        release="9.9.9",
        distribution="9.9.9rc2",
        recorded="insider",
        channel="insider",
    )
