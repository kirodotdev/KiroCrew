"""The feed monotonicity guard: a channel pointer never moves backward.

``scripts/check_feed_advance.py`` decides, at publish time, whether a
release run may rewrite its channel's mutable feed pointer. The scenario
it exists for is a hotfix cut on an OLD release line (``v0.4.1-insider.1``
while insider serves ``0.5.0-insider.1``): the run must publish its
immutable versioned assets, but rewriting the pointer would offer every
client on the channel a downgrade -- which electron-updater ACCEPTS when
the installed version carries a prerelease suffix. This happened live on
2026-08-28 and required an emergency ``v0.5.0-insider.2`` to undo.

Structural pins on the four publish workflows keep the guard wired to
every pointer write; a plausible refactor that drops one silently
re-opens the rollback.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_feed_advance.py"

_spec = importlib.util.spec_from_file_location("check_feed_advance", SCRIPT)
assert _spec is not None and _spec.loader is not None
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class TestParseVersion:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("0.4.1", (0, 4, 1, 1, 0)),
            ("v0.4.1", (0, 4, 1, 1, 0)),
            ("0.4.1-insider.1", (0, 4, 1, 0, 1)),
            ("0.4.1rc1", (0, 4, 1, 0, 1)),
            ("0.5.0-rc.2", (0, 5, 0, 0, 2)),
            # Desktop nightly: date AND time both order (same-day nightlies
            # must not compare equal, or a rerun of the earlier one could
            # advance over the later one).
            ("0.1.0-nightly.20260731t065756", (0, 1, 0, 0, 20260731065756)),
            ("0.1.0-nightly.20260731", (0, 1, 0, 0, 20260731)),
            # Nightly wheel (PEP 440): same run, same comparable number.
            ("0.1.0.dev20260731065756", (0, 1, 0, 0, 20260731065756)),
            # Free-form prerelease labels release.yml accepts and builds.
            # A hyphen in the label used to make the guard exit 2 with empty
            # stdout, which the desktop publishers read as "neither advance
            # nor hold" and fail -- a hard publish outage on a valid tag.
            ("0.5.0-beta-preview.1", (0, 5, 0, 0, 1)),
            ("v0.5.0-beta-preview.1", (0, 5, 0, 0, 1)),
            ("0.5.0-insider.4", (0, 5, 0, 0, 4)),
            # Dotless label: release.yml's trailing-number rule yields 0, so
            # the guard says 0 too rather than refusing to parse.
            ("0.5.0-beta", (0, 5, 0, 0, 0)),
            ("0.5.0-alpha.beta.3", (0, 5, 0, 0, 3)),
        ],
    )
    def test_spellings(self, text: str, expected: tuple) -> None:
        assert guard.parse_version(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "not-a-version",
            "0.4",
            "0.4.1.2",
            "0.5.0rc",
            "0.5.0_insider.1",
            "",
        ],
    )
    def test_malformed_is_rejected(self, text: str) -> None:
        # Broadening the prerelease surface must not make the base optional:
        # anything without a numeric X.Y.Z base is still unparseable.
        assert guard.parse_version(text) is None

    def test_hyphenated_label_matches_its_own_wheel_stamp(self) -> None:
        # release.yml maps 0.5.0-beta-preview.1 to the wheel 0.5.0rc1, and
        # publish-cli passes the wheel while the desktop jobs pass the raw
        # tag -- both must land on the same comparable key or one channel
        # holds while another advances.
        assert guard.parse_version("0.5.0-beta-preview.1") == guard.parse_version("0.5.0rc1")

    def test_release_yml_still_validates_only_the_base(self) -> None:
        # Parity pin: the guard is deliberately as permissive as the tag
        # surface. If release.yml ever starts validating the prerelease part
        # too, this assertion fails and the loose fallback can be narrowed.
        body = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        assert r'"$BASE" =~ ^[0-9]+\.[0-9]+\.[0-9]+$' in body
        assert 'N="${PRE##*.}"' in body

    def test_release_outranks_its_own_prereleases(self) -> None:
        assert guard.parse_version("0.5.0") > guard.parse_version("0.5.0-insider.9")

    def test_labels_are_lane_spellings_of_the_same_build(self) -> None:
        # The wheel stamp 0.4.1rc1 IS the tag 0.4.1-insider.1.
        assert guard.parse_version("0.4.1rc1") == guard.parse_version("0.4.1-insider.1")

    def test_nightly_wheel_and_desktop_stamps_are_the_same_run(self) -> None:
        assert guard.parse_version("0.1.0.dev20260830065756") == guard.parse_version(
            "0.1.0-nightly.20260830t065756"
        )

    def test_same_day_nightlies_order_by_time(self) -> None:
        earlier = guard.parse_version("0.1.0-nightly.20260830t010000")
        later = guard.parse_version("0.1.0-nightly.20260830t065756")
        assert earlier < later

    def test_the_live_incident_ordering(self) -> None:
        # v0.4.1-insider.1 must NOT outrank the 0.5.0 insider the channel served.
        assert guard.parse_version("0.4.1-insider.1") < guard.parse_version("0.5.0-insider.1")


class TestExtractFeedVersion:
    def test_json_feed(self) -> None:
        assert guard.extract_feed_version('{"version": "0.5.0rc1"}') == "0.5.0rc1"

    def test_yaml_feed(self) -> None:
        body = "version: 0.5.0-insider.1\nfiles:\n  - url: https://x/y.zip\n"
        assert guard.extract_feed_version(body) == "0.5.0-insider.1"

    def test_empty_and_unparseable(self) -> None:
        assert guard.extract_feed_version("") is None
        assert guard.extract_feed_version("<html>404</html>") is None

    def test_nested_yaml_keys_cannot_shadow(self) -> None:
        # Only a left-anchored top-level `version:` counts.
        body = "files:\n  version: 9.9.9\nversion: 0.5.0-insider.1\n"
        assert guard.extract_feed_version(body) == "0.5.0-insider.1"


def _run(args: list[str], feed: str = "", tags: str = "", tmp_path: Path | None = None):
    cmd = [sys.executable, str(SCRIPT)] + args
    files = []
    if tmp_path is not None:
        feed_file = tmp_path / "feed"
        feed_file.write_text(feed, encoding="utf-8")
        tags_file = tmp_path / "tags"
        tags_file.write_text(tags, encoding="utf-8")
        files = ["--current-file", str(feed_file), "--tags-file", str(tags_file)]
    return subprocess.run(cmd + files, capture_output=True, text=True, encoding="utf-8", timeout=30)


class TestVerdicts:
    def test_hotfix_on_old_line_holds_against_the_feed(self, tmp_path: Path) -> None:
        """The exact live incident: insider serves 0.5.0, hotfix publishes 0.4.1."""
        res = _run(
            ["--new", "0.4.1-insider.1", "--channel", "insider", "--self", "v0.4.1-insider.1"],
            feed="version: 0.5.0-insider.1\n",
            tmp_path=tmp_path,
        )
        assert res.returncode == 3
        assert res.stdout.strip() == "hold"

    def test_hotfix_holds_on_tags_even_when_the_cdn_feed_is_stale(self, tmp_path: Path) -> None:
        """The CDN copy lags up to max-age; the pushed tag does not."""
        res = _run(
            ["--new", "0.4.1-insider.1", "--channel", "insider", "--self", "v0.4.1-insider.1"],
            feed="version: 0.4.0-insider.14\n",  # stale CDN copy
            tags="v0.4.0\nv0.4.1-insider.1\nv0.5.0-insider.1\n",
            tmp_path=tmp_path,
        )
        assert res.returncode == 3
        assert res.stdout.strip() == "hold"

    def test_normal_release_advances(self, tmp_path: Path) -> None:
        res = _run(
            ["--new", "0.5.0-insider.2", "--channel", "insider", "--self", "v0.5.0-insider.2"],
            feed="version: 0.4.1-insider.1\n",
            tags="v0.4.1-insider.1\nv0.5.0-insider.1\nv0.5.0-insider.2\n",
            tmp_path=tmp_path,
        )
        assert res.returncode == 0
        assert res.stdout.strip() == "advance"

    def test_rerun_of_the_current_release_is_idempotent(self, tmp_path: Path) -> None:
        """Equality advances: a re-run recovering a half-finished publish
        must be able to complete the pointer write."""
        res = _run(
            ["--new", "0.5.0-insider.2", "--channel", "insider", "--self", "v0.5.0-insider.2"],
            feed="version: 0.5.0-insider.2\n",
            tags="v0.5.0-insider.2\n",
            tmp_path=tmp_path,
        )
        assert res.returncode == 0

    def test_first_publish_on_a_channel_advances(self, tmp_path: Path) -> None:
        res = _run(
            ["--new", "0.1.0-insider.1", "--channel", "insider", "--self", "v0.1.0-insider.1"],
            feed="",
            tags="v0.1.0-insider.1\n",
            tmp_path=tmp_path,
        )
        assert res.returncode == 0

    def test_hyphenated_label_tag_reaches_a_verdict(self, tmp_path: Path) -> None:
        """A tag release.yml accepts must never exit 2 with empty stdout.

        The desktop publishers capture stdout and fail the job on anything
        that is neither ``advance`` nor ``hold``, so an unparseable --new
        turns a valid-but-unusual tag into a hard publish failure.
        """
        res = _run(
            [
                "--new",
                "0.5.0-beta-preview.1",
                "--channel",
                "insider",
                "--self",
                "v0.5.0-beta-preview.1",
            ],
            feed="version: 0.5.0-insider.1\n",
            tags="v0.5.0-insider.1\nv0.5.0-beta-preview.1\n",
            tmp_path=tmp_path,
        )
        assert res.returncode == 0
        assert res.stdout.strip() == "advance"

    def test_hyphenated_label_tag_still_holds_on_an_old_line(self, tmp_path: Path) -> None:
        """Broadening the parser must not weaken the guard itself."""
        res = _run(
            [
                "--new",
                "0.4.1-beta-preview.1",
                "--channel",
                "insider",
                "--self",
                "v0.4.1-beta-preview.1",
            ],
            feed="version: 0.5.0-insider.1\n",
            tmp_path=tmp_path,
        )
        assert res.returncode == 3
        assert res.stdout.strip() == "hold"

    def test_unparseable_new_version_is_still_a_bad_invocation(self, tmp_path: Path) -> None:
        res = _run(
            ["--new", "not-a-version", "--channel", "insider"],
            tmp_path=tmp_path,
        )
        assert res.returncode == 2

    def test_stable_promotion_advances_past_its_own_rc_stamp(self, tmp_path: Path) -> None:
        """Promoting 0.4.1: the stable feed carries the previous release's
        wheel stamp and the repo carries the bare v0.4.1 tag (self)."""
        res = _run(
            ["--new", "0.4.1rc1", "--channel", "stable", "--self", "v0.4.1"],
            feed='{"version": "0.4.0rc14"}',
            tags="v0.4.0\nv0.4.1\nv0.4.1-insider.1\nv0.5.0-insider.1\n",
            tmp_path=tmp_path,
        )
        # 0.5.0-insider.1 is NOT a stable candidate; 0.4.1 is self. Advance.
        assert res.returncode == 0

    def test_stable_hotfix_holds_against_a_newer_stable_tag(self, tmp_path: Path) -> None:
        res = _run(
            ["--new", "0.4.2rc1", "--channel", "stable", "--self", "v0.4.2"],
            feed='{"version": "0.4.1rc1"}',  # stale CDN copy
            tags="v0.4.1\nv0.4.2\nv0.5.0\n",
            tmp_path=tmp_path,
        )
        assert res.returncode == 3

    def test_insider_line_is_capped_by_a_shipped_bare_release(self, tmp_path: Path) -> None:
        """Once 0.5.0 shipped stable, another 0.5.0-insider.N is a hotfix
        on an old line by definition."""
        res = _run(
            ["--new", "0.5.0-insider.3", "--channel", "insider", "--self", "v0.5.0-insider.3"],
            feed="version: 0.5.0-insider.2\n",
            tags="v0.5.0\nv0.5.0-insider.2\nv0.5.0-insider.3\n",
            tmp_path=tmp_path,
        )
        assert res.returncode == 3

    def test_nightly_ignores_tags(self, tmp_path: Path) -> None:
        """Nightly builds are untagged; a newer release tag must not block
        the nightly feed (nightly versions have their own base)."""
        res = _run(
            ["--new", "0.1.0-nightly.20260830", "--channel", "nightly", "--self", "main"],
            feed="version: 0.1.0-nightly.20260829\n",
            tags="v9.9.9\n",
            tmp_path=tmp_path,
        )
        assert res.returncode == 0

    def test_unparseable_new_version_fails_loud(self, tmp_path: Path) -> None:
        res = _run(
            ["--new", "garbage", "--channel", "insider"],
            tmp_path=tmp_path,
        )
        assert res.returncode == 2


class TestWorkflowsAreWired:
    """Every publish workflow's pointer writes sit behind the guard."""

    def test_every_pointer_writing_workflow_calls_the_guard(self) -> None:
        for name in (
            "publish-cli.yml",
            "publish-linux.yml",
            "publish-windows.yml",
            "sign-and-notarize.yml",
        ):
            body = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            assert "check_feed_advance.py" in body, f"{name} lost the feed guard"

    def test_guard_jobs_check_out_the_repository(self) -> None:
        """The guard needs repo CONTENTS on every run it gates.

        It shells out to ``scripts/check_feed_advance.py`` and to
        ``git ls-remote --tags origin``, so the job holding it must have
        checked the repository out whenever the guard step itself runs.
        ``sign-and-notarize.yml``'s publish job used to check out only on
        the promotion path (``if: inputs.promote``) -- with that gating the
        guard step red-fails on a normal nightly/insider publish, AFTER the
        immutable assets are already uploaded. So a checkout in a
        guard-bearing job must be either unconditional or gated no more
        narrowly than the guard step it feeds.
        """
        for name in (
            "publish-cli.yml",
            "publish-linux.yml",
            "publish-windows.yml",
            "sign-and-notarize.yml",
        ):
            doc = yaml.safe_load(
                (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            )
            for job_name, job in doc["jobs"].items():
                steps = job.get("steps") or []
                guard_steps = [
                    s for s in steps if "check_feed_advance.py" in str(s.get("run") or "")
                ]
                if not guard_steps:
                    continue
                checkouts = [s for s in steps if "actions/checkout" in str(s.get("uses") or "")]
                assert checkouts, f"{name}:{job_name} runs the guard with no checkout"
                guard_if = str(guard_steps[0].get("if") or "").strip()
                for co in checkouts:
                    co_if = str(co.get("if") or "").strip()
                    assert co_if in ("", guard_if), (
                        f"{name}:{job_name} checkout is gated on {co_if!r} while the guard "
                        f"step runs on {guard_if!r} -- the guard would run without "
                        f"scripts/ or a git repo"
                    )

    def test_signal_fetches_fail_closed_on_transport_errors(self) -> None:
        """Only confirmed absence (403/404) reads as "no feed yet". A guard
        that converted a fetch OUTAGE into empty evidence would advance on
        degraded signals -- reproducing the incident it exists to prevent."""
        for name in (
            "publish-cli.yml",
            "publish-linux.yml",
            "publish-windows.yml",
            "sign-and-notarize.yml",
        ):
            body = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            assert "403|404)" in body, f"{name}: absence must be explicit 403/404 only"
            assert (
                "channel feed fetch failed (transport)" in body
            ), f"{name}: a transport error must abort, never read as absence"
            assert (
                "channel feed fetch returned HTTP" in body
            ), f"{name}: an unexpected HTTP status must abort, never read as absence"
            # The old fail-open spellings must not come back.
            assert (
                "|| : > /tmp/current-feed" not in body
            ), f"{name}: feed fetch silently degrades to empty on error"
            assert (
                "|| : > /tmp/repo-tags.txt" not in body
            ), f"{name}: tag fetch silently degrades to empty on error"

    @pytest.mark.parametrize(
        ("name", "gated_steps"),
        [
            ("publish-linux.yml", ["Update latest artifact alias"]),
            ("publish-windows.yml", ["Update latest installer alias"]),
            (
                "sign-and-notarize.yml",
                ["Write legacy update feed", "Update latest DMG alias"],
            ),
        ],
    )
    def test_downstream_pointer_steps_are_gated_on_the_verdict(
        self, name: str, gated_steps: list[str]
    ) -> None:
        """The 'latest' aliases and the legacy feed are channel pointers
        too; skipping only the primary feed while still moving them would
        leave the downgrade reachable through the alias URL."""
        lines = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8").splitlines()
        for step in gated_steps:
            idx = next(
                i for i, line in enumerate(lines) if line.strip().startswith(f"- name: {step}")
            )
            window = "\n".join(lines[idx : idx + 3])
            assert (
                "steps.feed.outputs.advance == 'true'" in window
            ), f"{name} step {step!r} is not gated on the feed guard verdict"
