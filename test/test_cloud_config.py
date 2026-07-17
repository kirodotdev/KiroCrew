"""Unit tests for the cloud config store (cloud/config.py) — no secrets stored."""

from __future__ import annotations

from kiro_crew.cloud.config import DEFAULT_REGION, CloudConfig


class TestCloudConfig:
    def test_defaults(self, tmp_path):
        cfg = CloudConfig.load(tmp_path / "cloud.json")
        assert cfg.profile == ""
        assert cfg.region == DEFAULT_REGION
        assert cfg.last_tag == ""

    def test_roundtrip(self, tmp_path):
        p = tmp_path / "cloud.json"
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="kc-abc")
        cfg.save(p)
        loaded = CloudConfig.load(p)
        assert loaded.profile == "dev"
        assert loaded.region == "us-west-2"
        assert loaded.last_tag == "kc-abc"

    def test_over_long_last_tag_sanitized_to_empty(self, tmp_path):
        # A 52-63 char last_tag must be sanitized to "" on load — NOT carried
        # into the resume path where validate_tag (cap 51) would raise. The
        # sanitizer's job is "no last launch", not a crash. Keep _TAG_RE in
        # lockstep with ec2._TAG_RE.
        from kiro_crew.cloud import ec2

        assert ec2._TAG_RE.pattern == r"^[a-zA-Z0-9-]{1,51}$"  # the cap we mirror
        p = tmp_path / "cloud.json"
        p.write_text('{"profile": "dev", "region": "us-east-1", "last_tag": "%s"}' % ("a" * 60))
        cfg = CloudConfig.load(p)
        assert cfg.last_tag == ""  # too long -> dropped, no ValidationError later
        # A malformed-charset tag is likewise dropped.
        p.write_text('{"last_tag": "bad tag!"}')
        assert CloudConfig.load(p).last_tag == ""
        # A valid 51-char tag is kept.
        p.write_text('{"last_tag": "%s"}' % ("k" * 51))
        assert CloudConfig.load(p).last_tag == "k" * 51

    def test_never_stores_credentials(self, tmp_path):
        p = tmp_path / "cloud.json"
        CloudConfig(profile="dev", region="us-east-1", last_tag="t").save(p)
        text = p.read_text()
        # Only profile/region/last_tag — no secret-shaped keys.
        for forbidden in ("secret", "access_key", "aws_access", "token", "password"):
            assert forbidden not in text.lower()

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path):
        p = tmp_path / "cloud.json"
        p.write_text("not json{{{")
        cfg = CloudConfig.load(p)
        assert cfg.region == DEFAULT_REGION

    def test_non_object_json_falls_back_to_defaults(self, tmp_path):
        # Valid JSON that isn't an object ("hello", [1,2], 42, null) must not
        # raise AttributeError out of load() — that would give a raw traceback on
        # every `kirocrew cloud` command (handle_cloud only catches AWS/validation
        # errors). Honor the tolerate-a-corrupt-file promise.
        for body in ('"hello"', "[1, 2, 3]", "42", "null", "true"):
            p = tmp_path / "cloud.json"
            p.write_text(body)
            cfg = CloudConfig.load(p)
            assert cfg.region == DEFAULT_REGION
            assert cfg.profile == ""
            assert cfg.last_tag == ""

    def test_missing_region_coerced_to_default(self, tmp_path):
        p = tmp_path / "cloud.json"
        p.write_text('{"profile": "dev", "region": ""}')
        cfg = CloudConfig.load(p)
        assert cfg.region == DEFAULT_REGION
