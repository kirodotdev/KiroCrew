"""Shared fixtures and fakes for the personal drive publish suite
(``test_publish_personal_drive.py`` and its ``test_publish_personal_drive_<topic>.py``
parts): the bucket, policy, account and distribution names, the registry isolation
every part runs under, a payload file, stubbed profiles, the deploy engine spy and a
publish shortcut.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kiro_crew import publish_provider as pp
from kiro_crew.publish import personal_drive as pd

BUCKET = f"{pd.BUCKET_PREFIX}abc123def456"
POLICY_NEW = "RHP-NEW"
POLICY_EXISTING = "RHP-EXISTING"
#: The account the spy's profiles resolve to. Twelve digits, because that is the shape the
#: handle parser requires before it will read a field as an account.
ACCOUNT_ID = "123456789012"
DIST = "E123"
DOMAIN = "d1.cloudfront.net"


@pytest.fixture(autouse=True)
def _clean_registry():
    """Full isolation, and the registry is put BACK.

    ``reset_providers()`` clears cached INSTANCES only, so without also clearing the
    factory map a registration would leak into the next test. Clearing alone is not
    enough either: the map is process-wide, so a bare clear on teardown leaves every
    later test in the worker looking at an empty registry -- the leak inverted.
    """
    saved = dict(pp._FACTORIES)
    pp._FACTORIES.clear()
    pp.reset_providers()
    yield
    pp._FACTORIES.clear()
    pp._FACTORIES.update(saved)
    pp.reset_providers()


@pytest.fixture(name="payload")
def payload_fixture(tmp_path: Path) -> str:
    f = tmp_path / "index.html"
    f.write_text("<h1>hi</h1>", encoding="utf-8")
    return str(f)


def _registry(names: list[str], default: str = "") -> dict:
    return {
        "version": 2,
        "profiles": [{"name": n, "region": "us-west-2"} for n in names],
        "default": default or (names[0] if names else ""),
    }


def _stub_profiles(monkeypatch, names: list[str], default: str = "") -> dict:
    reg = _registry(names, default)
    monkeypatch.setattr(pd.profiles, "load_registry", lambda: reg)

    def _resolve(requested: str = ""):
        name = requested or reg["default"]
        if not name:
            return None
        for entry in reg["profiles"]:
            if entry["name"] == name:
                return entry["name"], entry["region"]
        return None

    monkeypatch.setattr(pd.profiles, "resolve_profile", _resolve)
    return reg


class _EngineSpy:
    """Records every engine call the provider makes.

    ``existing_keys=None`` means "every object exists"; a set makes ``head-object``
    key-aware so a test can put the drive in a specific state. ``drive=False`` makes the
    drive not exist yet, exercising the create branch. ``head_error`` makes
    ``head-object`` fail with a NON-not-found error.
    """

    def __init__(
        self,
        *,
        status: str = "Deployed",
        enabled: bool = True,
        existing_keys: set[str] | None = None,
        drive: bool = True,
        head_error: str = "",
        stored_digest: str = "",
        content_type: str = "text/html",
        existing_policy: bool = False,
        # The CSP directive / Override the pre-existing same-named policy carries. Default
        # to the module's exact sandbox values so existing_policy=True models a correct
        # policy that should be REUSED; a test overrides these to model a pre-created
        # policy WITHOUT the sandbox (wrong/absent CSP, or Override false) that must fail
        # closed. ``None`` drops the ContentSecurityPolicy block entirely (absent CSP).
        existing_policy_csp: str | None = pd._SANDBOX_CSP,
        existing_policy_override: bool = True,
        # Which response-headers policy the EXISTING distribution reports as attached to
        # its default behaviour. ``None`` means "whatever the resolver would hand out",
        # i.e. a correctly wired drive. A test sets it to model a distribution whose
        # sandbox header was detached or swapped after creation, which the reuse path must
        # refuse; set it to "" to model the header being removed outright.
        attached_policy_id: str | None = None,
        # The account `sts get-caller-identity` reports for this profile. A handle pins the
        # account it was published under, so a test sets this to a DIFFERENT id to model a
        # profile repointed at another account, or to "" to model credentials that do not
        # resolve at all.
        account: str = ACCOUNT_ID,
        buckets: list[str] | None = None,
        # Does the TAGGING API report the distribution (only ever in us-east-1)?
        cf_tags_visible: bool = True,
        # Does CloudFront's OWN tag API report the drive tag on it?
        cf_own_tags: bool = True,
        # An AWSError message to raise from the CloudFront-own-tags FALLBACK: the discovery
        # path that must propagate "could not look" instead of reading a throttle as "no
        # drive" and rebuilding it. ``cf_list_error`` fails list-distributions (the outer
        # swallow site); ``cf_tags_error`` fails list-tags-for-resource (the per-
        # distribution one). Reached only with cf_tags_visible=False, which sends
        # discovery down this fallback in the first place.
        cf_list_error: str = "",
        cf_tags_error: str = "",
    ):
        self.status = status
        self.enabled = enabled
        self.existing_keys = existing_keys
        self.drive = drive
        self.cf_tags_visible = cf_tags_visible
        self.cf_own_tags = cf_own_tags
        self.cf_list_error = cf_list_error
        self.cf_tags_error = cf_tags_error
        self.head_error = head_error
        self.stored_digest = stored_digest
        self.content_type = content_type
        self.existing_policy = existing_policy
        self.existing_policy_csp = existing_policy_csp
        self.existing_policy_override = existing_policy_override
        self.attached_policy_id = attached_policy_id
        self.account = account
        self.policy_ids: list[str] = []
        self.buckets = buckets
        self.checked: list[list[str]] = []
        self.invalidated: list[str] = []
        self.created_buckets: list[str] = []
        self.created_distributions: list[str] = []
        self.created_site_ids: list[str] = []
        self.created_tags: list[object] = []
        self.tag_queries = 0

    def _tag_payload(self, region: str = "") -> str:
        """Mimic the real tagging API's REGION scoping.

        `resourcegroupstaggingapi` returns only resources "located in the specified AWS
        Region", and CloudFront is not a regional resource -- its own docs say Tag Editor
        and Resource Groups do not cover it. A spy that answered the same rows for every
        region hid the defect that made a healthy drive look half-built outside us-east-1,
        so the distribution row is served only for us-east-1, and `cf_tags_visible=False`
        models the stricter reading where the tagging API never reports it at all.
        """
        if not self.drive:
            return json.dumps({"ResourceTagMappingList": []})
        rows: list[dict] = []
        if region != "us-east-1":
            names = self.buckets if self.buckets is not None else [BUCKET]
            rows += [{"ResourceARN": f"arn:aws:s3:::{n}"} for n in names]
        if region == "us-east-1" and self.cf_tags_visible:
            rows.append({"ResourceARN": f"arn:aws:cloudfront::1:distribution/{DIST}"})
        return json.dumps({"ResourceTagMappingList": rows})

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(pd.engine, "distribution_status", lambda d, p: (self.status, DOMAIN))
        monkeypatch.setattr(pd.engine, "invalidate", lambda d, p: self.invalidated.append(d))
        monkeypatch.setattr(pd.engine, "_harden_bucket", lambda b, p, tags: None)
        monkeypatch.setattr(pd.engine, "_oac_name", lambda site_id: f"oac-{site_id}"[:64])
        monkeypatch.setattr(pd.engine, "_trimmed_stderr", lambda err, limit=200: err[:limit])

        def _create_oac(name, profile):
            return "OAC1"

        def _create_distribution(
            bucket, region, oac_id, site_id, profile, tags=None, response_headers_policy_id=""
        ):
            self.created_distributions.append(bucket)
            self.created_site_ids.append(site_id)
            self.created_tags.append(tags)
            self.policy_ids.append(response_headers_policy_id)
            return {"id": "E999", "arn": "arn:aws:cloudfront::1:distribution/E999"}

        monkeypatch.setattr(pd.engine, "create_oac", _create_oac)
        monkeypatch.setattr(pd.engine, "create_distribution", _create_distribution)

        def _checked(args, profile, action="", timeout=0):
            self.checked.append(list(args))
            if args[:2] == ["resourcegroupstaggingapi", "get-resources"]:
                self.tag_queries += 1
                asked = args[args.index("--region") + 1] if "--region" in args else ""
                return self._tag_payload(asked)
            if args[:2] == ["cloudfront", "list-distributions"]:
                if self.cf_list_error:
                    raise pd.engine.AWSError(self.cf_list_error)
                if not self.drive:
                    return json.dumps({"DistributionList": {"Items": []}})
                return json.dumps(
                    {
                        "DistributionList": {
                            "Items": [
                                {"Id": DIST, "ARN": f"arn:aws:cloudfront::1:distribution/{DIST}"}
                            ]
                        }
                    }
                )
            if args[:2] == ["cloudfront", "list-tags-for-resource"]:
                if self.cf_tags_error:
                    raise pd.engine.AWSError(self.cf_tags_error)
                if not self.cf_own_tags:
                    return json.dumps({"Tags": {"Items": []}})
                return json.dumps(
                    {"Tags": {"Items": [{"Key": pd.TAG_DRIVE, "Value": pd.TAG_DRIVE_VALUE}]}}
                )
            if args[:2] == ["sts", "get-caller-identity"]:
                if not self.account:
                    raise pd.engine.AWSError("could not resolve credentials")
                return json.dumps({"Account": self.account})
            if args[:2] == ["cloudfront", "list-response-headers-policies"]:
                if not self.existing_policy:
                    return json.dumps({"ResponseHeadersPolicyList": {"Items": []}})
                security_headers: dict = {}
                if self.existing_policy_csp is not None:
                    security_headers["ContentSecurityPolicy"] = {
                        "ContentSecurityPolicy": self.existing_policy_csp,
                        "Override": self.existing_policy_override,
                    }
                return json.dumps(
                    {
                        "ResponseHeadersPolicyList": {
                            "Items": [
                                {
                                    "ResponseHeadersPolicy": {
                                        "Id": POLICY_EXISTING,
                                        "ResponseHeadersPolicyConfig": {
                                            "Name": pd._SANDBOX_POLICY_NAME,
                                            "SecurityHeadersConfig": security_headers,
                                        },
                                    }
                                }
                            ]
                        }
                    }
                )
            if args[:2] == ["cloudfront", "create-response-headers-policy"]:
                return json.dumps({"ResponseHeadersPolicy": {"Id": POLICY_NEW}})
            if args[:2] == ["cloudfront", "get-distribution-config"]:
                attached = self.attached_policy_id
                if attached is None:
                    # A correctly wired drive: the id the resolver would hand out.
                    attached = POLICY_EXISTING if self.existing_policy else POLICY_NEW
                return json.dumps(
                    {
                        "DistributionConfig": {
                            "Enabled": self.enabled,
                            "DefaultCacheBehavior": {"ResponseHeadersPolicyId": attached},
                        }
                    }
                )
            return "{}"

        monkeypatch.setattr(pd.engine, "_checked", _checked)

        def _run_aws(args, profile):
            if args[:2] == ["s3api", "head-object"]:
                key = args[args.index("--key") + 1]
                if self.head_error:
                    return 1, "", self.head_error
                if self.existing_keys is not None and key not in self.existing_keys:
                    return 1, "", "An error occurred (404) ... Not Found"
                meta = {"sha256": self.stored_digest} if self.stored_digest else {}
                return 0, json.dumps({"ContentType": self.content_type, "Metadata": meta}), ""
            if args[:2] == ["s3api", "create-bucket"]:
                self.created_buckets.append(args[args.index("--bucket") + 1])
                return 0, "{}", ""
            return 0, "{}", ""

        monkeypatch.setattr(pd.engine, "run_aws", _run_aws)

    # helpers a test can read

    def arg(self, verb: str, flag: str) -> str:
        for a in self.checked:
            if a[:2] == ["s3api", verb]:
                return a[a.index(flag) + 1]
        raise AssertionError(f"no {verb} recorded")

    def put_keys(self) -> list[str]:
        return [a[a.index("--key") + 1] for a in self.checked if a[:2] == ["s3api", "put-object"]]

    def keys_deleted(self) -> list[str]:
        return [
            a[a.index("--key") + 1] for a in self.checked if a[:2] == ["s3api", "delete-object"]
        ]

    def keys_copied_to(self) -> list[str]:
        return [a[a.index("--key") + 1] for a in self.checked if a[:2] == ["s3api", "copy-object"]]

    def bucket_policy(self) -> dict:
        return json.loads(self.arg("put-bucket-policy", "--policy"))

    def harden_tagsets(self) -> list[str]:
        return [a for a in self.checked if a[:1] == ["resourcegroupstaggingapi"]]

    def created_policies(self) -> list[list[str]]:
        """Every create-response-headers-policy call recorded."""
        return [
            a for a in self.checked if a[:2] == ["cloudfront", "create-response-headers-policy"]
        ]


def _publish(provider, payload, visibility="public", shared_with=None):
    return asyncio.run(
        provider.publish(
            file_path=payload,
            content_type="text/html",
            title="t",
            summary="",
            tags=[],
            visibility=visibility,
            shared_with=shared_with or [],
        )
    )
