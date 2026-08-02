"""Emergency release-control state and feed rewrite contracts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_feed_control.py"
SPEC = importlib.util.spec_from_file_location("release_feed_control", SCRIPT)
assert SPEC and SPEC.loader
release_control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_control)

BYTE_BASE = "https://download.crew.kiro.dev"
_SHA512 = "A" * 86 + "=="


def _control(**updates):
    body = release_control.bootstrap_control(
        "stable", "initialize emergency controls", now="2026-08-01T00:00:00Z"
    )
    body.update(updates)
    return release_control.validate_control(body)


def _mac_yaml(version: str = "1.2.3") -> str:
    return f"""version: {version}
files:
  - url: {BYTE_BASE}/desktop/stable/{version}/KiroCrew.zip
    sha512: '{_SHA512}'
    size: 123
  - url: {BYTE_BASE}/desktop/stable/{version}/KiroCrew.dmg
    sha512: '{_SHA512}'
    size: 456
path: {BYTE_BASE}/desktop/stable/{version}/KiroCrew.zip
sha512: '{_SHA512}'
releaseDate: '2026-08-01T00:00:00Z'
"""


def _legacy_mac(version: str = "1.2.3") -> dict:
    return {
        "version": version,
        "url": f"{BYTE_BASE}/desktop/stable/{version}/KiroCrew.zip",
        "dmg": f"{BYTE_BASE}/desktop/stable/{version}/KiroCrew.dmg",
        "name": version,
        "pub_date": "2026-08-01T00:00:00Z",
    }


def _cli_feed(version: str = "1.2.3") -> dict:
    return {
        "channel": "stable",
        "version": version,
        "wheel_url": f"{BYTE_BASE}/cli/stable/{version}/kirocrew-{version}-py3-none-any.whl",
        "sha256": "a" * 64,
        "python_requires": ">=3.10",
        "pub_date": "2026-08-01T00:00:00Z",
    }


class TestControlSchema:
    def test_bootstrap_is_frozen_and_generation_one(self):
        control = _control()
        assert control["frozen"] is True
        assert control["generation"] == 1
        assert control["minimum_supported_version"] == ""
        assert control["withdrawn_versions"] == []

    @pytest.mark.parametrize(
        "mutation",
        [
            {"schema_version": 2},
            {"channel": "production"},
            {"frozen": 1},
            {"minimum_supported_version": "latest"},
            {"generation": 0},
            {"withdrawn_versions": ["1.2.3", "1.2.3"]},
        ],
    )
    def test_invalid_control_fails_closed(self, mutation):
        body = _control()
        body.update(mutation)
        with pytest.raises(release_control.ReleaseControlError):
            release_control.validate_control(body, expected_channel="stable")

    def test_unknown_key_fails_closed(self):
        body = _control()
        body["freezed"] = True
        with pytest.raises(release_control.ReleaseControlError, match="unknown"):
            release_control.validate_control(body)

    def test_duplicate_json_keys_fail_closed(self):
        with pytest.raises(release_control.ReleaseControlError, match="repeats"):
            release_control._decode_json(
                b'{"channel":"stable","channel":"nightly"}',
                source="control",
            )

    def test_reason_rejects_log_control_characters(self):
        body = _control()
        body["reason"] = "incident\n::warning::forged"
        with pytest.raises(release_control.ReleaseControlError, match="controls"):
            release_control.validate_control(body)

    def test_channel_mismatch_fails_closed(self):
        with pytest.raises(release_control.ReleaseControlError, match="does not match"):
            release_control.validate_control(_control(), expected_channel="insider")

    def test_semver_prerelease_order_matches_release_rules(self):
        assert release_control._semver_lt("1.2.3-insider.2", "1.2.3")
        assert release_control._semver_lt("1.2.3-insider.2", "1.2.3-insider.10")
        assert not release_control._semver_lt("1.2.4-nightly.20260801t000000", "1.2.3")


class TestMutations:
    def test_freeze_and_unfreeze_are_separate_audited_generations(self):
        unfrozen = release_control.mutate_control(
            _control(),
            "unfreeze",
            reason="release window opened",
            now="2026-08-01T00:01:00Z",
        )
        frozen = release_control.mutate_control(
            unfrozen,
            "freeze",
            reason="incident response",
            now="2026-08-01T00:02:00Z",
        )
        assert unfrozen["frozen"] is False
        assert frozen["frozen"] is True
        assert frozen["generation"] == 3
        assert frozen["reason"] == "incident response"

    def test_withdraw_is_idempotent_and_always_freezes(self):
        first = release_control.mutate_control(
            _control(frozen=False),
            "withdraw",
            version="1.2.3",
            reason="known-bad updater",
            now="2026-08-01T00:02:00Z",
        )
        second = release_control.mutate_control(
            first,
            "withdraw",
            version="1.2.3",
            reason="retry same incident",
            now="2026-08-01T00:03:00Z",
        )
        assert first["frozen"] is True
        assert second["withdrawn_versions"] == ["1.2.3"]
        assert second["generation"] == first["generation"] + 1

    def test_restore_stays_frozen_until_explicit_unfreeze(self):
        restored = release_control.mutate_control(
            _control(frozen=False),
            "restore",
            reason="restore previous pointers",
            now="2026-08-01T00:02:00Z",
        )
        assert restored["frozen"] is True

    def test_minimum_version_can_be_set_and_cleared(self):
        pinned = release_control.mutate_control(
            _control(),
            "set-minimum",
            version="1.2.3",
            reason="critical security floor",
            now="2026-08-01T00:02:00Z",
        )
        cleared = release_control.mutate_control(
            pinned,
            "clear-minimum",
            reason="floor no longer required",
            now="2026-08-01T00:03:00Z",
        )
        assert pinned["minimum_supported_version"] == "1.2.3"
        assert cleared["minimum_supported_version"] == ""


class TestFeedRewrites:
    def test_desktop_yaml_gets_control_metadata_without_changing_artifacts(self, tmp_path):
        source = tmp_path / "in.yml"
        output = tmp_path / "out.yml"
        source.write_text(_mac_yaml(), encoding="utf-8")
        control = _control(
            minimum_supported_version="1.2.0",
            withdrawn_versions=["1.1.9"],
        )

        info = release_control.rewrite_feed(
            source,
            output,
            "latest-mac.yml",
            control,
            artifact_base=BYTE_BASE,
        )

        text = output.read_text(encoding="utf-8")
        assert 'minimumSupportedVersion: "1.2.0"' in text
        assert 'withdrawnVersions: ["1.1.9"]' in text
        assert f"{BYTE_BASE}/desktop/stable/1.2.3/KiroCrew.zip" in text
        assert info["version"] == "1.2.3"
        assert len(info["urls"]) == 2

    @pytest.mark.parametrize(
        "feed_name,body",
        [
            ("latest-mac.json", _legacy_mac()),
            ("latest-cli.json", _cli_feed()),
        ],
    )
    def test_json_feeds_get_the_same_control_metadata(self, tmp_path, feed_name, body):
        source = tmp_path / "in.json"
        output = tmp_path / "out.json"
        source.write_text(json.dumps(body), encoding="utf-8")
        release_control.rewrite_feed(
            source,
            output,
            feed_name,
            _control(
                minimum_supported_version="1.2.0",
                withdrawn_versions=["1.1.9"],
            ),
            artifact_base=BYTE_BASE,
        )
        rewritten = json.loads(output.read_text(encoding="utf-8"))
        assert rewritten["minimumSupportedVersion"] == "1.2.0"
        assert rewritten["withdrawnVersions"] == ["1.1.9"]
        assert rewritten["version"] == body["version"]

    def test_withdrawn_target_is_never_restored_live(self, tmp_path):
        source = tmp_path / "in.yml"
        source.write_text(_mac_yaml("1.2.3"), encoding="utf-8")
        with pytest.raises(release_control.ReleaseControlError, match="withdrawn"):
            release_control.rewrite_feed(
                source,
                tmp_path / "out.yml",
                "latest-mac.yml",
                _control(withdrawn_versions=["1.2.3"]),
                artifact_base=BYTE_BASE,
            )

    def test_target_below_minimum_is_never_restored_live(self, tmp_path):
        source = tmp_path / "in.yml"
        source.write_text(_mac_yaml("1.2.2"), encoding="utf-8")
        with pytest.raises(release_control.ReleaseControlError, match="below minimum"):
            release_control.rewrite_feed(
                source,
                tmp_path / "out.yml",
                "latest-mac.yml",
                _control(minimum_supported_version="1.2.3"),
                artifact_base=BYTE_BASE,
            )

    def test_uncomparable_cli_version_fails_closed_against_the_floor(self, tmp_path):
        # A PEP 440 dev stamp cannot be SemVer-compared to the floor. Failing
        # OPEN here would let `rewrite` republish exactly the build the
        # emergency floor was set to retire (1.1.0.dev1 dodging a 1.2.0
        # floor that plain 1.1.0 would trip).
        source = tmp_path / "latest-cli.json"
        source.write_text(json.dumps(_cli_feed("1.1.0.dev1")), encoding="utf-8")
        with pytest.raises(
            release_control.ReleaseControlError, match="cannot compare"
        ):
            release_control.rewrite_feed(
                source,
                tmp_path / "out.json",
                "latest-cli.json",
                _control(minimum_supported_version="1.2.0"),
                artifact_base=BYTE_BASE,
            )

    def test_comparable_cli_version_above_the_floor_still_publishes(self, tmp_path):
        source = tmp_path / "latest-cli.json"
        source.write_text(json.dumps(_cli_feed("1.2.3")), encoding="utf-8")
        info = release_control.rewrite_feed(
            source,
            tmp_path / "out.json",
            "latest-cli.json",
            _control(minimum_supported_version="1.2.0"),
            artifact_base=BYTE_BASE,
        )
        assert info["version"] == "1.2.3"

    def test_artifact_host_escape_is_rejected(self, tmp_path):
        source = tmp_path / "in.yml"
        source.write_text(
            _mac_yaml().replace(BYTE_BASE, "https://attacker.invalid"),
            encoding="utf-8",
        )
        with pytest.raises(release_control.ReleaseControlError, match="outside"):
            release_control.feed_info(
                source,
                "latest-mac.yml",
                artifact_base=BYTE_BASE,
                expected_channel="stable",
            )

    def test_malformed_digest_is_rejected_before_recovery_capture(self, tmp_path):
        source = tmp_path / "bad.yml"
        source.write_text(_mac_yaml().replace(_SHA512, "hex-not-base64"), encoding="utf-8")
        with pytest.raises(release_control.ReleaseControlError, match="base64"):
            release_control.feed_info(
                source,
                "latest-mac.yml",
                artifact_base=BYTE_BASE,
                expected_channel="stable",
            )

    def test_channel_escape_is_rejected(self, tmp_path):
        source = tmp_path / "wrong-channel.yml"
        source.write_text(
            _mac_yaml().replace("/desktop/stable/", "/desktop/insider/"),
            encoding="utf-8",
        )
        with pytest.raises(release_control.ReleaseControlError, match="channel"):
            release_control.feed_info(
                source,
                "latest-mac.yml",
                artifact_base=BYTE_BASE,
                expected_channel="stable",
            )

    @pytest.mark.parametrize(
        "feed_name,body",
        [
            ("latest-mac.yml", _mac_yaml("2.0.0")),
            ("latest-linux.yml", _mac_yaml("2.0.0")),
            ("latest-mac.json", json.dumps(_legacy_mac("2.0.0"))),
            ("latest-cli.json", json.dumps(_cli_feed("2.0.0"))),
        ],
    )
    def test_artifact_version_must_match_feed_version(
        self, tmp_path, feed_name, body
    ):
        source = tmp_path / feed_name
        source.write_text(
            body.replace("/2.0.0/", "/1.0.0/"),
            encoding="utf-8",
        )
        with pytest.raises(release_control.ReleaseControlError, match="version"):
            release_control.feed_info(
                source,
                feed_name,
                artifact_base=BYTE_BASE,
                expected_channel="stable",
            )

    @pytest.mark.parametrize(
        "traversal",
        [
            "/2.0.0/../2.0.0/",
            "/2.0.0/%2E%2E/2.0.0/",
            "/2.0.0/x%5C../",
        ],
        ids=["dot-dot", "encoded-dot-dot", "encoded-backslash"],
    )
    def test_artifact_url_traversal_is_rejected(self, tmp_path, traversal):
        # The raw path still starts with the expected /desktop/stable/2.0.0/
        # prefix, so only the traversal guard can catch these: consumers
        # normalize dot-segments and backslashes AFTER this validation.
        source = tmp_path / "latest-mac.yml"
        source.write_text(
            _mac_yaml("2.0.0").replace("/2.0.0/", traversal),
            encoding="utf-8",
        )
        with pytest.raises(
            release_control.ReleaseControlError, match="traversal"
        ):
            release_control.feed_info(
                source,
                "latest-mac.yml",
                artifact_base=BYTE_BASE,
                expected_channel="stable",
            )

    def test_oversized_numerics_fail_closed_not_traceback(self, tmp_path):
        # Python 3.12 raises a plain ValueError for int() conversions past
        # 4,300 digits. A bounded (<=128 KiB) document can carry a 5,000-digit
        # number, so every numeric parse must surface ReleaseControlError,
        # never an uncaught conversion traceback.
        huge = "1" * 5000

        # yml files[].size
        source = tmp_path / "latest-mac.yml"
        source.write_text(
            _mac_yaml("2.0.0").replace("size: 123", f"size: {huge}"),
            encoding="utf-8",
        )
        with pytest.raises(release_control.ReleaseControlError, match="size"):
            release_control.feed_info(
                source,
                "latest-mac.yml",
                artifact_base=BYTE_BASE,
                expected_channel="stable",
            )

        # yml withdrawnVersions flow array (json.loads int conversion)
        source = tmp_path / "withdrawn.yml"
        source.write_text(
            _mac_yaml("2.0.0") + f"withdrawnVersions: [{huge}]\n",
            encoding="utf-8",
        )
        with pytest.raises(
            release_control.ReleaseControlError, match="withdrawnVersions"
        ):
            release_control.feed_info(
                source,
                "latest-mac.yml",
                artifact_base=BYTE_BASE,
                expected_channel="stable",
            )

        # JSON feed body (shared _decode_json path)
        source = tmp_path / "latest-cli.json"
        body = json.dumps(_cli_feed("2.0.0"))[:-1] + f', "x": {huge}}}'
        source.write_text(body, encoding="utf-8")
        with pytest.raises(release_control.ReleaseControlError):
            release_control.feed_info(
                source,
                "latest-cli.json",
                artifact_base=BYTE_BASE,
                expected_channel="stable",
            )

    def test_overlong_version_string_fails_closed(self):
        with pytest.raises(release_control.ReleaseControlError, match="128"):
            release_control._parse_semver(
                "1.0.0-" + "9" * 5000, field="version"
            )

    def test_path_digest_must_match_its_file_entry(self, tmp_path):
        source = tmp_path / "mismatched-digest.yml"
        replacement = "B" * 86 + "=="
        text = _mac_yaml().rsplit(f"sha512: '{_SHA512}'", 1)[0]
        text += f"sha512: '{replacement}'\nreleaseDate: '2026-08-01T00:00:00Z'\n"
        source.write_text(text, encoding="utf-8")
        with pytest.raises(release_control.ReleaseControlError, match="does not match"):
            release_control.feed_info(
                source,
                "latest-mac.yml",
                artifact_base=BYTE_BASE,
                expected_channel="stable",
            )

    def test_cli_feed_channel_must_match_control_channel(self, tmp_path):
        source = tmp_path / "cli.json"
        source.write_text(json.dumps(_cli_feed()), encoding="utf-8")
        with pytest.raises(release_control.ReleaseControlError, match="does not match"):
            release_control.feed_info(
                source,
                "latest-cli.json",
                artifact_base=BYTE_BASE,
                expected_channel="insider",
            )

    def test_rewrite_replaces_old_metadata_instead_of_duplicating_it(self, tmp_path):
        source = tmp_path / "in.yml"
        output = tmp_path / "out.yml"
        source.write_text(
            _mac_yaml()
            + 'minimumSupportedVersion: "1.0.0"\n'
            + 'withdrawnVersions: ["1.0.1"]\n',
            encoding="utf-8",
        )
        release_control.rewrite_feed(
            source,
            output,
            "latest-mac.yml",
            _control(minimum_supported_version="1.2.0"),
            artifact_base=BYTE_BASE,
        )
        text = output.read_text(encoding="utf-8")
        assert text.count("minimumSupportedVersion:") == 1
        assert text.count("withdrawnVersions:") == 1
        assert "1.0.1" not in text


class TestRecoverySnapshots:
    def test_valid_feed_is_promoted_atomically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live_feed = _mac_yaml("2.1.0").encode()
        monkeypatch.setattr(
            release_control,
            "fetch_bytes",
            lambda _url, *, limit: live_feed,
        )
        destination = tmp_path / "recovery" / "latest-mac.yml"

        assert release_control.snapshot_feed(
            "https://updates.example.invalid/latest-mac.yml",
            destination,
            "latest-mac.yml",
            "2.2.0",
            artifact_base=BYTE_BASE,
            expected_channel="stable",
        )
        assert destination.read_bytes() == live_feed
        assert not list(destination.parent.glob(f".{destination.name}.*"))

    def test_same_version_keeps_existing_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live_feed = _mac_yaml("2.2.0").encode()
        monkeypatch.setattr(
            release_control,
            "fetch_bytes",
            lambda _url, *, limit: live_feed,
        )
        destination = tmp_path / "latest-mac.yml"
        destination.write_bytes(b"existing recovery candidate")

        assert not release_control.snapshot_feed(
            "https://updates.example.invalid/latest-mac.yml",
            destination,
            "latest-mac.yml",
            "2.2.0",
            artifact_base=BYTE_BASE,
            expected_channel="stable",
        )
        assert destination.read_bytes() == b"existing recovery candidate"
        assert not list(tmp_path.glob(f".{destination.name}.*"))

    def test_invalid_feed_never_replaces_existing_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            release_control,
            "fetch_bytes",
            lambda _url, *, limit: b"not: a desktop release feed\n",
        )
        destination = tmp_path / "latest-mac.yml"
        destination.write_bytes(b"existing recovery candidate")

        with pytest.raises(release_control.ReleaseControlError):
            release_control.snapshot_feed(
                "https://updates.example.invalid/latest-mac.yml",
                destination,
                "latest-mac.yml",
                "2.2.0",
                artifact_base=BYTE_BASE,
                expected_channel="stable",
            )
        assert destination.read_bytes() == b"existing recovery candidate"
        assert not list(tmp_path.glob(f".{destination.name}.*"))

    def test_invalid_new_version_fails_before_fetch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetched = False

        def fetch(_url, *, limit):
            nonlocal fetched
            fetched = True
            return b""

        monkeypatch.setattr(release_control, "fetch_bytes", fetch)
        with pytest.raises(release_control.ReleaseControlError, match="SemVer"):
            release_control.snapshot_feed(
                "https://updates.example.invalid/latest-mac.yml",
                tmp_path / "latest-mac.yml",
                "latest-mac.yml",
                "",
                artifact_base=BYTE_BASE,
                expected_channel="stable",
            )
        assert fetched is False

    @pytest.mark.parametrize(
        "argv",
        [
            [
                "guard",
                "--url",
                "https://updates.example.invalid/control.json",
                "--channel",
                "stable",
                "--save",
                "control.json",
                "--github-env",
                "github.env",
            ],
            [
                "snapshot",
                "--url",
                "https://updates.example.invalid/latest-mac.yml",
                "--feed-name",
                "latest-mac.yml",
                "--new-version",
                "2.2.0",
                "--output",
                "recovery.yml",
            ],
        ],
    )
    def test_operational_inputs_are_required(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit):
            release_control._parser().parse_args(argv)


class TestPublisherGuard:
    def test_cross_origin_redirect_is_rejected_before_following(self):
        handler = release_control._SameOriginRedirectHandler(
            ("https", "updates.example", 443)
        )
        request = release_control.urllib.request.Request(
            "https://updates.example/control.json"
        )
        with pytest.raises(release_control.ReleaseControlError, match="cross-origin"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://attacker.invalid/control.json",
            )

    def test_frozen_guard_fails_before_writing_environment(self, tmp_path, monkeypatch):
        control = _control(frozen=True)
        monkeypatch.setattr(
            release_control,
            "load_control_url",
            lambda url, expected_channel: control,
        )
        env = tmp_path / "env"
        rc = release_control.main(
            [
                "guard",
                "--url",
                "https://updates.example/feed/stable/release-control.json",
                "--channel",
                "stable",
                "--candidate-version",
                "1.2.3",
                "--save",
                str(tmp_path / "saved.json"),
                "--github-env",
                str(env),
            ]
        )
        assert rc == 2
        assert not env.exists()

    def test_unfrozen_guard_exports_bounded_values(self, tmp_path, monkeypatch):
        control = _control(
            frozen=False,
            minimum_supported_version="1.2.0",
            withdrawn_versions=["1.1.9"],
        )
        monkeypatch.setattr(
            release_control,
            "load_control_url",
            lambda url, expected_channel: control,
        )
        env = tmp_path / "env"
        rc = release_control.main(
            [
                "guard",
                "--url",
                "https://updates.example/feed/stable/release-control.json",
                "--channel",
                "stable",
                "--candidate-version",
                "1.2.3",
                "--save",
                str(tmp_path / "saved.json"),
                "--github-env",
                str(env),
            ]
        )
        assert rc == 0
        assert env.read_text(encoding="utf-8").splitlines() == [
            "MINIMUM_SUPPORTED_VERSION=1.2.0",
            'WITHDRAWN_VERSIONS=["1.1.9"]',
            "RELEASE_CONTROL_GENERATION=1",
        ]

    def test_guard_rejects_empty_candidate_version(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            release_control,
            "load_control_url",
            lambda url, expected_channel: _control(frozen=False),
        )
        env = tmp_path / "empty.env"
        rc = release_control.main(
            [
                "guard",
                "--url",
                "https://updates.example/feed/stable/release-control.json",
                "--channel",
                "stable",
                "--candidate-version",
                "",
                "--save",
                str(tmp_path / "saved.json"),
                "--github-env",
                str(env),
            ]
        )
        assert rc == 2
        assert not env.exists()

    def test_guard_rejects_withdrawn_or_below_floor_candidate(self, tmp_path, monkeypatch):
        control = _control(
            frozen=False,
            minimum_supported_version="1.2.3",
            withdrawn_versions=["1.2.4"],
        )
        monkeypatch.setattr(
            release_control,
            "load_control_url",
            lambda url, expected_channel: control,
        )
        for candidate in ("1.2.2", "1.2.4"):
            rc = release_control.main(
                [
                    "guard",
                    "--url",
                    "https://updates.example/feed/stable/release-control.json",
                    "--channel",
                    "stable",
                    "--candidate-version",
                    candidate,
                    "--save",
                    str(tmp_path / f"{candidate}.json"),
                    "--github-env",
                    str(tmp_path / f"{candidate}.env"),
                ]
            )
            assert rc == 2
