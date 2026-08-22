"""Tests for the off-host snapshot destination.

Every AWS call goes through ``engine.run_aws``, so these stub that one chokepoint
and assert on the argv that would have been executed. Nothing here touches AWS.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import snapshot_remote as remote
from kiro_crew.deploy import engine


class FakeAws:
    """Records argv and answers from a scripted table keyed by the s3 verb."""

    def __init__(self, answers: dict[str, tuple[int, str, str]] | None = None):
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, args, profile, timeout=30):
        self.calls.append(list(args))
        self.timeouts = getattr(self, "timeouts", [])
        self.timeouts.append(timeout)
        key = " ".join(args[:2])
        return self.answers.get(key, (0, "{}", ""))

    def argv_for(self, prefix: str) -> list[list[str]]:
        return [c for c in self.calls if " ".join(c[:2]).startswith(prefix)]


@pytest.fixture
def fake(monkeypatch):
    f = FakeAws()
    monkeypatch.setattr(engine, "run_aws", f)
    return f


class TestParseS3Url:
    """Parses the object a restore names. Writing no longer takes a URL at all — the
    destination is provisioned once and recorded — so this only has to resolve one
    bundle and refuse everything that is not one."""

    def test_a_bundle_key(self):
        obj = remote.parse_s3_url("s3://my-backups/backups/laptop/snap.tar.gz")
        assert obj.bucket == "my-backups"
        assert obj.key == "backups/laptop/snap.tar.gz"

    @pytest.mark.parametrize(
        "bad",
        [
            "my-backups/x.tar.gz",  # no scheme
            "https://my-backups/x.tar.gz",
            "s3://UPPERCASE/x.tar.gz",
            "s3://ab/x.tar.gz",  # bucket too short
            "s3://has_underscore/x.tar.gz",
            "s3://has.dots/x.tar.gz",  # dots break virtual-host TLS
            "s3://my-backups",  # names no object
            "s3://my-backups/backups/laptop/",  # a prefix, not an object
            "s3://my-backups/notes.txt",  # not a bundle
        ],
    )
    def test_rejected_before_any_aws_call(self, bad, fake):
        with pytest.raises(remote.DestinationError):
            remote.parse_s3_url(bad)
        assert fake.calls == [], "validation must not reach the AWS CLI"


class TestTimeoutSizing:
    def test_small_bundle_gets_the_floor(self):
        assert remote.timeout_for_bytes(1024) == 120

    def test_large_bundle_is_not_left_at_the_control_plane_default(self):
        """A 1.5 GB bundle must not inherit the 30s default that suits an s3api call.

        This is the concrete failure the sizing exists to prevent: a killed upload
        is indistinguishable from a failed backup.
        """
        one_and_a_half_gb = int(1.5 * 1024 * 1024 * 1024)
        assert remote.timeout_for_bytes(one_and_a_half_gb) > 1500

    def test_monotonic_in_size(self):
        assert remote.timeout_for_bytes(10**9) > remote.timeout_for_bytes(10**6)


class TestDownload:
    def test_refuses_a_url_that_names_no_bundle(self, tmp_path, fake):
        # A bare bucket, a prefix, and a non-bundle key are each refused before any
        # transfer. The trailing-slash case is judged on the RAW url, because a parser
        # that normalises the key would make `daily/` look like an object named `daily`.
        with pytest.raises(remote.DestinationError) as e:
            remote.download("s3://my-backups", tmp_path, "p")
        assert "does not name a bundle" in str(e.value)

        with pytest.raises(remote.DestinationError) as e:
            remote.download("s3://my-backups/daily/", tmp_path, "p")
        assert "names a prefix" in str(e.value)

        with pytest.raises(remote.DestinationError) as e:
            remote.download("s3://my-backups/daily/notes.txt", tmp_path, "p")
        assert "does not name a bundle" in str(e.value)

        assert fake.argv_for("s3 cp") == []

    def test_reports_a_success_that_produced_no_file(self, tmp_path, monkeypatch):
        """A zero exit with no local file must fail loudly rather than continue into
        the restore path with a missing bundle."""
        monkeypatch.setattr(remote.shutil, "which", lambda _n: "/usr/bin/aws")
        fake = FakeAws()  # exit 0, but writes nothing
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            remote.download("s3://my-bucket/k/bundle.tar.gz", tmp_path, "p")
        assert "missing" in str(e.value)

    def test_downloads_to_the_named_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(remote.shutil, "which", lambda _n: "/usr/bin/aws")
        target = tmp_path / "snaps"

        def fake_run(args, profile, timeout=30):
            Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(args[-1]).write_bytes(b"bundle")
            return 0, "", ""

        monkeypatch.setattr(engine, "run_aws", fake_run)
        local = remote.download("s3://my-bucket/k/bundle.tar.gz", target, "p")
        assert local == target / "bundle.tar.gz"
        assert local.read_bytes() == b"bundle"

    def test_missing_aws_cli_is_reported_plainly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(remote.shutil, "which", lambda _n: None)
        with pytest.raises(remote.DestinationError) as e:
            remote.download("s3://my-bucket/k/bundle.tar.gz", tmp_path, "p")
        assert "aws CLI" in str(e.value)
