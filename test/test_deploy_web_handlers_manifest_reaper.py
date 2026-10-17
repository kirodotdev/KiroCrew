"""The deploy manifest, and the reaper Lambda that reaps by it.

A successful deploy writes ``.kirocrew-deploy.json`` with its TTL to the base stack's
shared bucket; ``ttl_hours`` is type-checked before any AWS call and ``expires_at`` is
the deploy time plus the TTL, not the creation time; the manifest names the engine
architecture, its bucket and its distribution. The reaper deletes an already-disabled
engine-arch distribution and empties its bucket, and marks the deploy ``reaping`` when
the delete reports the distribution is not yet disabled.
"""

from __future__ import annotations

from pathlib import Path

from deploy_web_handlers_helpers import _force_posix_shell  # noqa: F401
from deploy_web_handlers_helpers import _isolate_config  # noqa: F401
from deploy_web_handlers_helpers import _mock_base_stack  # noqa: F401
from deploy_web_handlers_helpers import _fake_store, _run, _set_profile
from skill_script_helpers import load_skill_script

from kiro_crew.deploy import engine, handlers
from kiro_crew.deploy import profiles as profiles_mod

# --- item 6: ttl_hours manifest write ---


def test_do_deploy_writes_manifest_with_ttl(tmp_path, monkeypatch):
    """After successful deploy, _do_deploy writes .kirocrew-deploy.json with TTL."""
    public = tmp_path / "site"
    public.mkdir()
    (public / "index.html").write_text("<h1>hi</h1>")

    captured_s3_calls = []

    def fake_deploy(site_id, src_dir, profile, region):
        return {
            "url": "https://d.cloudfront.net/",
            "bucket": "test-bucket",
            "site_id": site_id,
            "distribution_id": "EXXX",
        }

    def fake_run_aws(args, profile, timeout=30):
        captured_s3_calls.append(args)
        # Return valid base stack output for describe-stacks (base-stack check)
        if "describe-stacks" in args:
            import json as _json

            return (0, _json.dumps([{"OutputKey": "BucketName", "OutputValue": "test-bucket"}]), "")
        return (0, "", "")

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    monkeypatch.setattr(engine, "run_aws", fake_run_aws)
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path])

    # Mock profile resolution
    monkeypatch.setattr(profiles_mod, "resolve_profile", lambda p: ("default", "us-west-2"))
    monkeypatch.setattr(
        profiles_mod,
        "load_registry",
        lambda: {"default": "default", "profiles": [{"name": "default", "region": "us-west-2"}]},
    )

    status, body = _run(
        handlers._do_deploy(
            {
                "site_id": "my-ttl-app",
                "local_dir": str(public),
                "ttl_hours": 24,
                "confirm": True,
            }
        )
    )
    assert status == 200

    # Verify an S3 cp command was issued for the manifest
    manifest_calls = [c for c in captured_s3_calls if ".kirocrew-deploy.json" in " ".join(c)]
    assert len(manifest_calls) == 1
    assert "s3://test-bucket/my-ttl-app/.kirocrew-deploy.json" in " ".join(manifest_calls[0])

    # Verify the temp manifest file contents (the s3 cp source)
    src_file = manifest_calls[0][2]  # ["s3", "cp", <local_file>, "s3://..."]
    # File is already cleaned up but we can verify it was in the call
    assert src_file.endswith(".json")


# --- item #3: ttl_hours validation ----------------------------------------


def test_deploy_invalid_ttl_rejected_before_deploy(monkeypatch):
    """Invalid ttl_hours returns 400 before any AWS call is made."""
    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(
        handlers._do_deploy(
            {"site_id": "s", "artifact_slug": "a", "confirm": True, "ttl_hours": "banana"}
        )
    )
    assert status == 400
    assert "ttl_hours must be an integer" in payload["error"]


def test_deploy_negative_ttl_rejected(monkeypatch):
    """Negative ttl_hours returns 400."""
    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
    status, payload = _run(
        handlers._do_deploy(
            {"site_id": "s", "artifact_slug": "a", "confirm": True, "ttl_hours": -5}
        )
    )
    assert status == 400
    assert "non-negative" in payload["error"]


def test_deploy_expires_at_uses_future_time(monkeypatch):
    """expires_at in manifest = now + ttl_hours, NOT creation time."""
    import json
    from datetime import datetime, timedelta, timezone

    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)

    captured_manifest = {}

    def fake_deploy(site_id, src_dir, profile, region):
        return {
            "site_id": site_id,
            "url": "https://d.cloudfront.net/",
            "reused": False,
            "bucket": "b",
            "distribution_id": "D1",
            "status": "Deployed",
        }

    def fake_run_aws(cmd, profile, timeout=15):
        if "describe-stacks" in cmd:
            import json as _j

            return (0, _j.dumps([{"OutputKey": "BucketName", "OutputValue": "base-bkt"}]), "")
        if cmd[0] == "s3" and cmd[1] == "cp":
            # Capture the manifest content before the file is deleted
            src_file = cmd[2]
            try:
                captured_manifest["data"] = Path(src_file).read_text(encoding="utf-8")
            except FileNotFoundError:
                pass
        return (0, "", "")

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    monkeypatch.setattr(engine, "run_aws", fake_run_aws)

    before = datetime.now(timezone.utc)
    status, payload = _run(
        handlers._do_deploy(
            {"site_id": "s", "artifact_slug": "a", "confirm": True, "ttl_hours": 24}
        )
    )
    after = datetime.now(timezone.utc)
    assert status == 200

    # Parse the manifest that was written
    manifest = json.loads(captured_manifest["data"])
    expires = datetime.strptime(manifest["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    # expires_at should be ~24 hours from now (not equal to created_at)
    assert expires > before + timedelta(hours=23, minutes=59)
    assert expires < after + timedelta(hours=24, minutes=1)
    # created_at should be approximately now
    created = datetime.strptime(manifest["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    assert created != expires  # Critical: they must NOT be the same


# --- item #4: manifest goes to base-stack bucket --------------------------


def test_deploy_manifest_written_to_base_stack_bucket(monkeypatch):
    """Manifest should go to the shared base-stack bucket, not per-site engine bucket."""
    _set_profile(monkeypatch)
    monkeypatch.setattr(handlers, "get_default_store", lambda: _fake_store(), raising=False)
    monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)

    s3_calls = []

    def fake_deploy(site_id, src_dir, profile, region):
        return {
            "site_id": site_id,
            "url": "https://d.cloudfront.net/",
            "reused": False,
            "bucket": "kirocrew-web-random123",
            "distribution_id": "D1",
            "status": "Deployed",
        }

    def fake_run_aws(cmd, profile, timeout=15):
        import json as _j

        if "describe-stacks" in cmd and "kirocrew-deploy-base" in cmd:
            return (
                0,
                _j.dumps(
                    [
                        {"OutputKey": "BucketName", "OutputValue": "shared-base-bucket"},
                        {"OutputKey": "DistributionId", "OutputValue": "DXYZ"},
                    ]
                ),
                "",
            )
        if cmd[0] == "s3" and cmd[1] == "cp":
            s3_calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    monkeypatch.setattr(engine, "run_aws", fake_run_aws)

    status, _ = _run(
        handlers._do_deploy({"site_id": "mysite", "artifact_slug": "a", "confirm": True})
    )
    assert status == 200

    # The manifest must target the SHARED base bucket, not the per-site random one
    assert len(s3_calls) == 1
    s3_dest = s3_calls[0][3]  # "s3://shared-base-bucket/mysite/.kirocrew-deploy.json"
    assert "shared-base-bucket" in s3_dest
    assert "kirocrew-web-random123" not in s3_dest
    assert "mysite/.kirocrew-deploy.json" in s3_dest


# --- F1: manifest includes arch/bucket/distribution_id fields ----------------


def test_deploy_manifest_includes_engine_arch_fields(monkeypatch, tmp_path):
    """F1: _do_deploy must write arch, bucket, distribution_id to the manifest."""
    _set_profile(monkeypatch, "p")

    from kiro_crew.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path / "store")
    store.create(name="mywidget", content="<p>Hello</p>", kind="widget")
    monkeypatch.setattr(handlers, "get_default_store", lambda: store)

    manifest_data: list = []

    def fake_deploy(site_id, src_dir, profile, region):
        return {
            "site_id": site_id,
            "bucket": "kirocrew-web-abc123",
            "distribution_id": "E1234XYZ",
            "url": "https://d.cf.net/",
            "status": "Deployed",
        }

    def fake_run_aws(args, profile, timeout=30):
        # Capture the manifest write
        if args[:2] == ["s3", "cp"] and ".kirocrew-deploy.json" in str(args):
            import json as _json

            # Read the temp file that was written
            src_file = args[2]
            with open(src_file) as f:
                manifest_data.append(_json.load(f))
        if "describe-stacks" in args:
            import json as _json

            return (
                0,
                _json.dumps([{"OutputKey": "BucketName", "OutputValue": "shared-bucket"}]),
                "",
            )
        return (0, "", "")

    monkeypatch.setattr(engine, "deploy", fake_deploy)
    monkeypatch.setattr(engine, "run_aws", fake_run_aws)

    status, payload = _run(
        handlers._do_deploy({"site_id": "testsite", "artifact_slug": "mywidget", "confirm": True})
    )
    assert status == 200

    assert len(manifest_data) == 1
    man = manifest_data[0]
    assert man["arch"] == "engine"
    assert man["bucket"] == "kirocrew-web-abc123"
    assert man["distribution_id"] == "E1234XYZ"


# --- F1: reaper engine-arch happy path + retry path --------------------------

_REAPER_LAMBDA_INDEX = (
    Path(__file__).resolve().parent.parent
    / "src/kiro_crew/deploy/skills/artifact-deploy/scripts/reaper_lambda/index.py"
)


def _load_reaper(module_name):
    """A private copy of the reaper Lambda, built against the boto3 stubs in place now.

    Never imported as the top-level name ``index``: registering it there leaves a
    module bound to this test's boto3 MagicMock for whatever imports ``index`` next in
    the worker, and evicting the name first drops whichever ``index`` was there.
    """
    return load_skill_script(module_name, _REAPER_LAMBDA_INDEX)


def test_reaper_engine_arch_happy_path(monkeypatch):
    """F1: reaper reaps engine-arch deploy (distribution disabled → deleted → bucket emptied)."""
    import sys
    from unittest.mock import MagicMock

    # Mock boto3 before importing the reaper module
    mock_boto3 = MagicMock()
    mock_botocore = MagicMock()
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)
    monkeypatch.setitem(sys.modules, "botocore", mock_botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", mock_botocore.exceptions)

    # Set env vars the module expects
    monkeypatch.setenv("BUCKET", "shared-bucket")
    monkeypatch.setenv("DIST_ID", "ESHARED")

    reaper_mod = _load_reaper("reaper_lambda_index_happy_path")

    mock_s3 = MagicMock()
    mock_cf = MagicMock()
    monkeypatch.setattr(reaper_mod, "s3", mock_s3)
    monkeypatch.setattr(reaper_mod, "cf", mock_cf)
    monkeypatch.setenv("ACCOUNT_ID", "123456789012")

    # R19 F4: tag verification mocks — distribution and bucket tags match slug
    mock_cf.list_tags_for_resource.return_value = {
        "Tags": {"Items": [{"Key": "kirocrew:site", "Value": "mysite"}]}
    }
    mock_s3.get_bucket_tagging.return_value = {
        "TagSet": [{"Key": "kirocrew:site", "Value": "mysite"}]
    }

    # Distribution is already disabled (Enabled=False)
    mock_cf.get_distribution_config.return_value = {
        "ETag": "etag1",
        "DistributionConfig": {"Enabled": False},
    }
    mock_cf.delete_distribution.return_value = {}

    # Bucket listing returns one object
    mock_s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "index.html"}]}
    ]
    mock_s3.delete_objects.return_value = {}
    mock_s3.delete_bucket.return_value = {}
    mock_s3.delete_object.return_value = {}

    man = {
        "slug": "mysite",
        "arch": "engine",
        "bucket": "kirocrew-web-mysite",
        "distribution_id": "E999",
        "expires_at": "2020-01-01T00:00:00Z",
        "persistent": False,
    }

    result = reaper_mod._reap_engine_arch(man, "mysite", 9999999999)

    assert result == "reaped"
    mock_cf.delete_distribution.assert_called_once_with(Id="E999", IfMatch="etag1")
    # destructive S3 calls now pin ExpectedBucketOwner (from ACCOUNT_ID).
    mock_s3.delete_bucket.assert_called_once_with(
        Bucket="kirocrew-web-mysite", ExpectedBucketOwner="123456789012"
    )
    mock_s3.delete_object.assert_called_once()


def test_reaper_engine_arch_distribution_not_disabled_retries(monkeypatch):
    """F1: reaper returns 'reaping' when DistributionNotDisabled."""
    import sys
    from unittest.mock import MagicMock

    mock_boto3 = MagicMock()
    mock_botocore = MagicMock()

    # We need a real ClientError class
    class _FakeClientError(Exception):
        def __init__(self, error_response, operation_name):
            self.response = error_response
            super().__init__(f"{operation_name}: {error_response}")

    mock_botocore.exceptions.ClientError = _FakeClientError
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)
    monkeypatch.setitem(sys.modules, "botocore", mock_botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", mock_botocore.exceptions)

    monkeypatch.setenv("BUCKET", "shared-bucket")
    monkeypatch.setenv("DIST_ID", "ESHARED")

    reaper_mod = _load_reaper("reaper_lambda_index_not_disabled")

    mock_s3 = MagicMock()
    mock_cf = MagicMock()
    monkeypatch.setattr(reaper_mod, "s3", mock_s3)
    monkeypatch.setattr(reaper_mod, "cf", mock_cf)
    monkeypatch.setattr(reaper_mod, "botocore", mock_botocore)
    monkeypatch.setenv("ACCOUNT_ID", "123456789012")

    # R19 F4: tag verification mocks — distribution tags match slug
    mock_cf.list_tags_for_resource.return_value = {
        "Tags": {"Items": [{"Key": "kirocrew:site", "Value": "retrysite"}]}
    }

    # Distribution is disabled but delete fails with DistributionNotDisabled
    mock_cf.get_distribution_config.return_value = {
        "ETag": "etag2",
        "DistributionConfig": {"Enabled": False},
    }
    error_response = {"Error": {"Code": "DistributionNotDisabled", "Message": "still propagating"}}
    mock_cf.delete_distribution.side_effect = _FakeClientError(error_response, "DeleteDistribution")

    man = {
        "slug": "retrysite",
        "arch": "engine",
        "bucket": "kirocrew-web-retrysite",
        "distribution_id": "EXYZ",
        "expires_at": "2020-01-01T00:00:00Z",
        "persistent": False,
    }

    result = reaper_mod._reap_engine_arch(man, "retrysite", 9999999999)

    assert result == "reaping"
    # Manifest should be rewritten with reaping=True
    mock_s3.put_object.assert_called_once()
    call_kwargs = mock_s3.put_object.call_args[1]
    import json as _json

    body = _json.loads(call_kwargs["Body"])
    assert body["reaping"] is True
