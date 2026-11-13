"""The per-site artifact-deploy bucket policy refuses non-TLS requests.

``put_oac_bucket_policy`` writes the whole policy document, so the TLS-only Deny
must be one of its statements, and a re-deploy writes the policy again so an
existing site carries the current statements.
"""

import json
from unittest import mock

from kiro_crew.deploy import engine

BUCKET = "kirocrew-web-example"
DIST_ARN = "arn:aws:cloudfront::123:distribution/EX1"


def _written_policies(checked: mock.MagicMock) -> list[dict]:
    out = []
    for call in checked.call_args_list:
        argv = call.args[0]
        if argv[:2] == ["s3api", "put-bucket-policy"]:
            out.append(json.loads(argv[argv.index("--policy") + 1]))
    return out


def test_policy_has_exact_tls_deny_statement():
    with mock.patch.object(engine, "_checked") as checked:
        engine.put_oac_bucket_policy(BUCKET, DIST_ARN, "p")
    (policy,) = _written_policies(checked)
    tls = [s for s in policy["Statement"] if s.get("Sid") == engine.DENY_INSECURE_TRANSPORT_SID]
    assert tls == [
        {
            "Sid": "KiroCrewDenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [f"arn:aws:s3:::{BUCKET}", f"arn:aws:s3:::{BUCKET}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        }
    ]
    # Existing statements stay.
    sids = [s["Sid"] for s in policy["Statement"]]
    assert sids == [
        "AllowCloudFrontOAC",
        "DenyCloudFrontControlManifests",
        "KiroCrewDenyInsecureTransport",
    ]


def test_redeploy_rewrites_policy_with_tls_deny(tmp_path):
    existing = {"bucket": BUCKET, "distribution_id": "EX1", "distribution_arn": DIST_ARN}
    with (
        mock.patch.object(engine, "find_site_by_tag", return_value=existing),
        mock.patch.object(engine, "_checked", return_value="") as checked,
        mock.patch.object(engine, "distribution_status", return_value=("Deployed", "d.net")),
    ):
        result = engine.deploy("site1", str(tmp_path), "p", "us-west-2")
    assert result["reused"] is True
    (policy,) = _written_policies(checked)
    allow = next(s for s in policy["Statement"] if s["Sid"] == "AllowCloudFrontOAC")
    assert allow["Condition"]["StringEquals"]["AWS:SourceArn"] == DIST_ARN
    assert any(s.get("Sid") == engine.DENY_INSECURE_TRANSPORT_SID for s in policy["Statement"])
    verbs = [c.args[0][:2] for c in checked.call_args_list]
    assert verbs.index(["s3api", "put-bucket-policy"]) < verbs.index(["s3", "sync"])
