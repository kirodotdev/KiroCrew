"""R38 regression (round-38 Codex finding on 39d084c).

find_site_by_tag() feeds destructive operations (sync --delete / empty /
delete-bucket). It must: (1) filter on BOTH site + managed tags; (2) reject
buckets outside the managed naming scheme; (3) fail loud on ambiguity.
"""
import json
from unittest.mock import patch

import pytest

from kiro_crew.deploy import engine


def _resp(arns):
    return json.dumps({"ResourceTagMappingList": [{"ResourceARN": a} for a in arns]})


def _find(arns):
    with patch.object(engine, "_checked", return_value=_resp(arns)) as m:
        result = engine.find_site_by_tag("mysite", "prof", "us-west-2")
    return result, m


class TestF1DiscoveryTrust:
    def test_filter_includes_managed_tag(self):
        _, m = _find([])
        argv = m.call_args[0][0]
        joined = " ".join(argv)
        assert f"Key={engine.TAG_SITE},Values=mysite" in joined
        assert f"Key={engine.TAG_MANAGED},Values=true" in joined

    def test_rejects_bucket_outside_naming_scheme(self):
        result, _ = _find(["arn:aws:s3:::victim-production-data"])
        assert result is None, "a tagged but non-managed-name bucket must never be trusted"

    def test_accepts_managed_bucket(self):
        result, _ = _find([f"arn:aws:s3:::{engine.BUCKET_PREFIX}abc123"])
        assert result["bucket"] == f"{engine.BUCKET_PREFIX}abc123"

    def test_ambiguous_buckets_fail_loud(self):
        with pytest.raises(engine.AWSError, match="ambiguous"):
            _find([
                f"arn:aws:s3:::{engine.BUCKET_PREFIX}aaa",
                f"arn:aws:s3:::{engine.BUCKET_PREFIX}bbb",
            ])

    def test_ambiguous_distributions_fail_loud(self):
        with pytest.raises(engine.AWSError, match="ambiguous"):
            _find([
                "arn:aws:cloudfront::123:distribution/E1AAA",
                "arn:aws:cloudfront::123:distribution/E2BBB",
            ])
