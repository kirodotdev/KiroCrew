"""deploy-web core engine — deterministic 6-step deploy via the ``aws`` CLI.

All AWS work shells to the ``aws`` CLI as a subprocess with ``--profile`` (never
boto3 — credential resolution stays in the external CLI process, preserving the
``sync/s3.py`` boundary and the design §6.1 non-goals). Every subprocess call
goes through :func:`run_aws`, the single chokepoint unit tests monkeypatch.

Flow (design §4):
  bucket → OAC → distribution (tagged at creation) → bucket-policy (pins the
  distribution ARN, MUST come after create-distribution) → s3 sync → invalidate
  → return the ``*.cloudfront.net`` URL.

State is stateless-by-tag (§5): re-deploy of the same logical ``site_id`` finds
the existing bucket + distribution by the ``kirocrew:site`` tag and reuses them
(idempotent), so the public URL stays stable. The physical bucket name is an
opaque ``kirocrew-web-<random>`` (no account id); idempotency never relies on
re-deriving it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import time
from typing import Any, Optional

from kiro_crew.sandbox import cgroup_scope_argv, run_limited, wrap_argv

logger = logging.getLogger(__name__)

# Indirection so tests can patch out the destroy disable→wait→delete polling sleep.
_sleep = time.sleep
_DESTROY_WAIT_POLLS = 80
_DESTROY_WAIT_INTERVAL = 15

BUCKET_PREFIX = "kirocrew-web-"
TAG_MANAGED = "kirocrew:managed"
TAG_SITE = "kirocrew:site"
DEFAULT_REGION = "us-west-2"
_BUCKET_RAND_BYTES = 6  # -> 12 hex chars

# AccessDenied → the exact IAM statement Sid the user must add (design §12 / Q3).
# Maps an aws action prefix to a human hint pointing at the policy Sid.
_ACTION_STATEMENT_HINTS: dict[str, str] = {
    "s3:CreateBucket": "S3BucketLevel",
    "s3:PutBucketPublicAccessBlock": "S3BucketLevel",
    "s3:PutEncryptionConfiguration": "S3BucketLevel",
    "s3:PutBucketOwnershipControls": "S3BucketLevel",
    "s3:PutBucketPolicy": "S3BucketLevel",
    "s3:PutBucketTagging": "S3BucketLevel",
    "s3:PutObject": "S3ObjectLevel",
    "s3:DeleteObject": "S3ObjectLevel",
    "s3:ListBucket": "S3BucketLevel",
    "cloudfront:CreateDistribution": "CloudFrontCreateList",
    "cloudfront:CreateDistributionWithTags": "CloudFrontCreateList",
    "cloudfront:CreateOriginAccessControl": "CloudFrontCreateList",
    "cloudfront:CreateInvalidation": "CloudFrontManageTagged",
    "cloudfront:GetDistribution": "CloudFrontManageTagged",
    "cloudfront:GetDistributionConfig": "CloudFrontManageTagged",
    "cloudfront:UpdateDistribution": "CloudFrontManageTagged",
    "cloudfront:DeleteDistribution": "CloudFrontManageTagged",
    "cloudfront:GetOriginAccessControl": "CloudFrontManageTagged",
    "cloudfront:DeleteOriginAccessControl": "CloudFrontManageTagged",
    "cloudfront:ListOriginAccessControls": "CloudFrontCreateList",
    "s3:DeleteBucket": "S3BucketLevel",
    "cloudfront:TagResource": "CloudFrontCreateList",
    "tag:GetResources": "DiscoveryAndIdentity",
    "sts:GetCallerIdentity": "DiscoveryAndIdentity",
}


class AWSError(Exception):
    """An ``aws`` CLI call failed. Carries a remediation hint when AccessDenied."""

    def __init__(self, message: str, *, missing_statement: Optional[str] = None) -> None:
        super().__init__(message)
        self.missing_statement = missing_statement


def random_bucket_name() -> str:
    """Opaque, collision-resistant physical bucket name. No account id (§5/Q2)."""
    return f"{BUCKET_PREFIX}{secrets.token_hex(_BUCKET_RAND_BYTES)}"


# Well-known AWS CLI install dirs, searched (after the inherited PATH) when
# resolving the binary absolutely. A Finder/Dock-launched macOS gateway inherits
# the minimal launchd PATH (``/usr/bin:/bin:/usr/sbin:/sbin``), which contains
# neither Homebrew nor the official-pkg symlink dir — so a bare ``"aws"`` fails
# ``execvp`` inside ``sandbox-exec`` with "No such file or directory", which is
# exactly why the Artifact Deploy "Verify" step failed for Homebrew users until
# the gateway was relaunched from a shell that carried the full PATH. The SSM
# session-manager-plugin installs into these same dirs, so it is resolved and
# spawn-searched through them too (#5392).
_AWS_BIN_DIRS = (
    "/opt/homebrew/bin",  # Apple Silicon Homebrew
    "/usr/local/bin",     # Intel Homebrew + official AWS CLI v2 pkg symlink
)


def resolve_aws_tool_bin(name: str) -> str:
    """Resolve an AWS-tooling binary to an absolute path, or the bare ``name``.

    Searches the inherited ``PATH`` first, then the well-known install dirs in
    :data:`_AWS_BIN_DIRS`, so the desktop-app gateway resolves the tool no matter
    how it was launched (Finder/Dock give a minimal PATH; a terminal launch does
    not). Falling back to the bare ``name`` preserves the prior behaviour — and
    its "not found" error — when the tool genuinely is not installed anywhere.

    Public and generic over ``name`` because ``aws`` is not the only AWS binary
    the gateway has to find in those dirs: ``session-manager-plugin`` installs
    into exactly the same ones (AWS's macOS ``.pkg`` symlinks it into
    ``/usr/local/bin``, the Homebrew cask into the brew prefix), and its probe hit
    the identical minimal-PATH gap (#5392). Both callers therefore share this one
    body rather than each re-deriving the dirs and the provenance rule.

    A hit found only through the fallback dirs (i.e. NOT reachable via the
    inherited ``PATH``, which the pre-existing bare-argv behaviour already
    trusted) is additionally routed through
    :func:`kiro_crew.github_runner.validate_provider_executable` — the repo's
    executable-provenance chokepoint — so an agent- or third-party-planted shim
    in a user-writable install dir is refused rather than executed inside the
    credential-bearing sandbox. On refusal we fall back to the bare name,
    preserving the prior not-found/execvp error rather than inventing a new
    failure mode.
    """
    env_path = os.environ.get("PATH", "")
    found = shutil.which(name, path=env_path) if env_path else None
    if found:
        # Same trust class as the pre-existing behaviour: execvp against the
        # inherited PATH already executed exactly this binary.
        return found
    fallback = os.pathsep.join(p for p in _AWS_BIN_DIRS if p)
    found = shutil.which(name, path=fallback) if fallback else None
    if found:
        from kiro_crew.github_runner import validate_provider_executable

        try:
            return validate_provider_executable(found)
        except ValueError:
            logger.warning("refusing %s at %s: failed provenance validation", name, found)
    return name


def resolve_aws_bin() -> str:
    """Resolve ``aws`` to an absolute path, falling back to the bare name.

    Public: this is the repo's single ``aws``-CLI resolution chokepoint, reused
    by every sibling spawn site (``cloud.aws``, ``cloud.ssm``, ``voice_reply``,
    ``dashboard.chat_voice``, the artifact-deploy skill scripts) so each one
    survives a GUI-launched gateway's minimal PATH the same way (#4770). See
    :func:`resolve_aws_tool_bin` for the search order and the provenance rule.
    """
    return resolve_aws_tool_bin("aws")


def aws_spawn_env(aws_bin: str) -> dict[str, str]:
    """This process's env with :data:`_AWS_BIN_DIRS` APPENDED to ``PATH``.

    ``aws_bin`` is the argv head the caller is about to spawn — the return value of
    :func:`resolve_aws_bin` for that same spawn. It is required, not optional: see
    the fail-closed rule below, which cannot be enforced without it.

    Resolving our own argv head absolutely is not sufficient for ``aws ssm
    start-session``: the CLI locates ``session-manager-plugin`` itself, by name,
    against the CHILD's inherited ``PATH`` at exec time. A GUI-launched gateway
    hands the child the minimal launchd ``PATH``, so the tunnel dies inside a
    correctly-resolved ``aws`` — the resolver cannot reach that lookup, only the
    child's environment can (#5392). Passing this as ``env=`` is therefore the
    other half of the same fix, not a duplicate of it.

    APPENDED, never prepended: the inherited ``PATH`` keeps first claim on every
    name, so this can only make a previously-unresolvable lookup succeed and can
    never re-point one the child already resolved. That is the whole trust
    argument for widening a credential-bearing child's ``PATH`` at all, and it is
    also why this is not :func:`kiro_crew.env.augmented_path`, which PREPENDS a
    broader set (``~/.local/bin``, npm/mise/volta shims) — the right search space
    for finding an MCP launcher, too wide and wrongly-ordered ahead of ``/usr/bin``
    for a child holding AWS credentials and a live tunnel to the user's box.

    **A non-absolute ``aws_bin`` returns the env UNWIDENED.** This is the one case
    where "can only make an unresolvable lookup succeed" is not a safety argument
    but the hazard itself: :func:`resolve_aws_tool_bin` falls back to the bare name
    precisely when it found a candidate in these dirs and
    ``validate_provider_executable`` REFUSED it, and that refusal is enforced only
    by the bare name failing ``execvp`` against a ``PATH`` those dirs are absent
    from. Widening the child's ``PATH`` would put the refused binary back on it and
    hand it AWS credentials — converting a fail-closed rejection into an execution.
    So the widening is offered only to a head that was already resolved
    absolutely, where ``execvp`` performs no ``PATH`` search at all and the dirs
    can affect nothing but the CLI's own onward lookups.

    Dirs already on ``PATH`` are not repeated, so a terminal-launched gateway
    (whose ``PATH`` carries them) gets a byte-identical env and the fix is inert
    outside the minimal-PATH case it exists for.

    Unlike the resolvers above this touches no filesystem — it is a dict copy and
    a string join over ``os.environ`` — so it is safe to call directly on the
    gateway event loop, where a PATH scan would not be.
    """
    env = dict(os.environ)
    if not os.path.isabs(aws_bin):
        # Unresolved head: the bare name IS the provenance refusal. Leave PATH
        # alone so it keeps failing execvp, as it did before this env existed.
        return env
    current = env.get("PATH", "")
    have = {p for p in current.split(os.pathsep) if p}
    extra = [d for d in _AWS_BIN_DIRS if d and d not in have]
    if extra:
        env["PATH"] = os.pathsep.join(([current] if current else []) + extra)
    return env


def _aws(args: list[str], profile: str) -> list[str]:
    """Build an ``aws`` CLI argv with an optional ``--profile`` (never a raw key)."""
    cmd = [resolve_aws_bin()] + list(args)
    if profile:
        cmd += ["--profile", profile]
    return cmd


def run_aws(
    args: list[str],
    profile: str,
    timeout: int = 30,
    *,
    extra_visible_dirs: tuple[str, ...] = (),
) -> tuple[int, str, str]:
    """Run an ``aws`` CLI command. Returns (returncode, stdout, stderr).

    Single subprocess chokepoint — unit tests monkeypatch this. Credentials are
    resolved by the CLI's own provider chain; KiroCrew passes only the profile name.

    Since deploy-web is LLM-triggerable (chat-native skill), the invocation is
    routed through the same OS-level sandbox (:func:`wrap_argv`) as every other
    agent-triggerable subprocess. ``mode="standard"`` is used because the AWS CLI
    must read ``~/.aws`` to resolve credentials; the argv is fixed (no shell, no
    user-controlled command structure — only ``--profile`` and validated args are
    appended), so ``standard`` is the correct tier (same as app subprocesses).

    ``extra_visible_dirs`` re-exposes an agent-hidden tree to THIS spawn alone: a
    transfer that must land in a directory every agent sandbox masks (so a
    same-UID agent cannot swap the destination for a link) still needs the CLI
    itself to see that directory. Naming it here lifts the mask for the one
    fixed-argv child, never for the agent.
    """
    sandboxed, cleanup = wrap_argv(
        _aws(args, profile), mode="standard", extra_visible_dirs=extra_visible_dirs
    )
    sandboxed = cgroup_scope_argv(sandboxed)  # cgroup DoS ceiling
    try:
        proc = run_limited(  # noqa: S603 — fixed argv, no shell, sandbox-wrapped
            sandboxed,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def map_access_denied(stderr: str) -> Optional[str]:
    """If stderr is an AccessDenied, return the IAM statement Sid to add (else None)."""
    if not any(tok in stderr for tok in ("AccessDenied", "not authorized", "UnauthorizedOperation")):
        return None
    for action, sid in _ACTION_STATEMENT_HINTS.items():
        if action in stderr:
            return sid
    return "the deploy-web IAM policy (see §7)"


def _trimmed_stderr(err: str, limit: int = 200) -> str:
    """CLI stderr, REDACTED and then trimmed -- in that order.

    Order is the whole point. Trimming first can cut a credential in half, and a
    half token matches no redaction pattern: the fragment then survives every
    downstream pass (a response body, a SEL audit, a log line) because by the
    time a redactor sees the string, the evidence that it WAS a credential is
    gone. Redacting the full text first means the placeholder is what gets
    trimmed, so the cutoff can only shorten already-safe text.

    Callers may redact again at their own boundary; these passes are idempotent.
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    text, _ = redact_credentials(err.strip())
    text, _ = redact_exfiltration_urls(text)
    return text[:limit]


def _checked(
    args: list[str],
    profile: str,
    *,
    action: str,
    timeout: int = 30,
    extra_visible_dirs: tuple[str, ...] = (),
) -> str:
    """Run an aws call, raising AWSError (with AccessDenied mapping) on failure."""
    # Forwarded only when set: ``run_aws`` is the chokepoint tests monkeypatch,
    # and a call without a visible-dir grant keeps the exact call shape those
    # stubs were written against.
    if extra_visible_dirs:
        rc, out, err = run_aws(
            args, profile, timeout=timeout, extra_visible_dirs=extra_visible_dirs
        )
    else:
        rc, out, err = run_aws(args, profile, timeout=timeout)
    if rc != 0:
        # Only attach an IAM-statement remediation hint when the failure is a
        # genuine authorization error. Client-side errors (NoRegion,
        # InvalidArgument, etc.) must NOT be reported as a missing permission.
        sid = map_access_denied(err)
        hint = f" — add `{action}` (policy statement `{sid}`) and re-run" if sid else ""
        raise AWSError(f"{action} failed: {_trimmed_stderr(err)}{hint}", missing_statement=sid)
    return out


# --- discovery (stateless-by-tag) -----------------------------------------

def find_site_by_tag(site_id: str, profile: str, region: str = DEFAULT_REGION) -> Optional[dict[str, str]]:
    """Resolve an existing site's bucket + distribution by the kirocrew:site tag.

    Discovery is a TRUST decision — deploy syncs with --delete, recall
    empties, destroy deletes whatever this returns. Three hardenings:
    (1) require BOTH kirocrew:site=<id> AND kirocrew:managed=true (filters
    are ANDed by get-resources), so an unrelated bucket that merely carries
    a colliding site tag is never selected; (2) the bucket must match the
    managed naming scheme (kirocrew-web-*); (3) multiple matching buckets or
    distributions are ambiguous — fail loud instead of last-match-wins,
    which could pair an arbitrary bucket with an unrelated distribution.

    Returns {"bucket": ..., "distribution_id": ..., "distribution_arn": ...} or None.
    """
    out = _checked(
        [
            "resourcegroupstaggingapi", "get-resources",
            "--tag-filters", f"Key={TAG_SITE},Values={site_id}",
            f"Key={TAG_MANAGED},Values=true",
            "--region", region or DEFAULT_REGION,
            "--output", "json",
        ],
        profile,
        action="tag:GetResources",
    )
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return None
    buckets: list[str] = []
    dists: list[str] = []
    for mapping in data.get("ResourceTagMappingList", []):
        arn = mapping.get("ResourceARN", "")
        if arn.startswith("arn:aws:s3:::"):
            candidate = arn.split(":::", 1)[1]
            if not candidate.startswith(BUCKET_PREFIX):
                # tagged but not ours by naming scheme — never trust it with
                # destructive sync/empty/delete operations
                continue
            buckets.append(candidate)
        elif ":distribution/" in arn:
            dists.append(arn)
    if len(buckets) > 1 or len(dists) > 1:
        raise AWSError(
            f"ambiguous tag discovery for site {site_id!r}: "
            f"{len(buckets)} buckets / {len(dists)} distributions carry "
            f"{TAG_SITE}={site_id} + {TAG_MANAGED}=true — refusing to guess "
            f"(destructive operations would target the wrong resource)"
        )
    bucket = buckets[0] if buckets else ""
    dist_arn = dists[0] if dists else ""
    dist_id = dist_arn.rsplit("/", 1)[-1] if dist_arn else ""
    if not bucket and not dist_id:
        return None
    return {"bucket": bucket, "distribution_id": dist_id, "distribution_arn": dist_arn}


# --- create primitives -----------------------------------------------------

def _harden_bucket(bucket: str, profile: str, tagset: str) -> None:
    """Apply every at-rest control for a deploy bucket. Idempotent puts, so this is
    safe whether the bucket was freshly created or recovered from a partial run.

    Single source of truth on purpose: this ran as two divergent copies before, and
    a control added to one silently did not apply to the deploy path that actually
    runs. ``tagset`` is the only per-caller difference.
    """
    _checked(
        ["s3api", "put-public-access-block", "--bucket", bucket,
         "--public-access-block-configuration",
         "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"],
        profile, action="s3:PutBucketPublicAccessBlock",
    )
    _checked(
        ["s3api", "put-bucket-ownership-controls", "--bucket", bucket,
         "--ownership-controls", "Rules=[{ObjectOwnership=BucketOwnerEnforced}]"],
        profile, action="s3:PutBucketOwnershipControls",
    )
    _checked(
        ["s3api", "put-bucket-encryption", "--bucket", bucket,
         "--server-side-encryption-configuration",
         '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'],
        profile, action="s3:PutEncryptionConfiguration",
    )
    _checked(
        ["s3api", "put-bucket-tagging", "--bucket", bucket, "--tagging", tagset],
        profile, action="s3:PutBucketTagging",
    )
    # Versioning is NOT enabled here, and neither is a noncurrent-version
    # lifecycle rule, even though the CloudFormation OriginBucket sets both and
    # sync_dir overwrites in place with `s3 sync --delete`.
    #
    # Teardown cannot survive it yet. `empty_bucket` runs `s3 rm --recursive`,
    # which on a versioned bucket only removes CURRENT versions (it writes delete
    # markers), so `delete-bucket` in `destroy()` would fail BucketNotEmpty --
    # after the distribution has already been deleted, leaving a half-destroyed
    # site and an orphaned bucket. The scheduled reaper takes the same path.
    #
    # Turning versioning on therefore requires, together: a version-aware purge
    # (paginated list-object-versions + batched delete-objects covering delete
    # markers) in both destroy() and the reaper, plus s3:DeleteObjectVersion and
    # s3:ListBucketVersions in the generated IAM policy -- which every existing
    # user would have to re-apply. That is its own change, not a rider on this
    # one: an unrecoverable-overwrite window is a smaller problem than a teardown
    # that strands billable infrastructure.
    #
    # No put-bucket-logging either. These buckets set ObjectOwnership
    # BucketOwnerEnforced, which disables the ACL that server access logging
    # historically relies on, so a log destination has to be granted to
    # logging.s3.amazonaws.com in the target's BUCKET POLICY instead. That grant
    # cannot live in this helper: put_oac_bucket_policy writes a complete policy
    # document after hardening and would overwrite it.


def create_private_bucket(bucket: str, region: str, profile: str) -> None:
    """Create a fully private bucket: BPA on, AES256 SSE, ACLs disabled (§3)."""
    create = ["s3api", "create-bucket", "--bucket", bucket, "--region", region]
    if region != "us-east-1":
        create += ["--create-bucket-configuration", f"LocationConstraint={region}"]
    _checked(create, profile, action="s3:CreateBucket")
    _harden_bucket(bucket, profile, f"TagSet=[{{Key={TAG_MANAGED},Value=true}}]")


def _oac_name_prefix(site_id: str) -> str:
    """Stable OAC-name prefix for a site, capped so the full name (prefix + '-' +
    6 hex) stays within CloudFront's 64-char Name limit. Used by both create and
    the delete-by-prefix cleanup so they always agree."""
    return f"deploy-web-{site_id}"[:57]


def _oac_name(site_id: str) -> str:
    """Unique OAC name ≤ 64 chars: capped prefix + random 6-hex suffix."""
    return f"{_oac_name_prefix(site_id)}-{secrets.token_hex(3)}"


def create_oac(name: str, profile: str) -> str:
    """Create an Origin Access Control (SigningBehavior=always). Returns its Id."""
    config = {
        "Name": name,
        "OriginAccessControlOriginType": "s3",
        "SigningBehavior": "always",
        "SigningProtocol": "sigv4",
    }
    out = _checked(
        ["cloudfront", "create-origin-access-control",
         "--origin-access-control-config", json.dumps(config), "--output", "json"],
        profile, action="cloudfront:CreateOriginAccessControl",
    )
    return json.loads(out)["OriginAccessControl"]["Id"]


def distribution_config(
    bucket: str, region: str, oac_id: str, response_headers_policy_id: str = ""
) -> dict[str, Any]:
    """Build the CloudFront DistributionConfig: S3 REST origin + OAC, HTTPS, index.html.

    ``response_headers_policy_id`` substitutes the managed SecurityHeadersPolicy below.
    A caller serving MUTUALLY UNTRUSTED documents from one distribution needs headers the
    managed policy does not carry, and the policy id is the only place a distribution can
    express them. Omitting it keeps the historical behaviour exactly.
    """
    origin_domain = f"{bucket}.s3.{region}.amazonaws.com"
    origin_id = f"s3-{bucket}"
    return {
        "CallerReference": secrets.token_hex(8),
        "Comment": f"deploy-web {bucket}",
        "Enabled": True,
        "DefaultRootObject": "index.html",
        "Origins": {
            "Quantity": 1,
            "Items": [{
                "Id": origin_id,
                "DomainName": origin_domain,
                "OriginAccessControlId": oac_id,
                "S3OriginConfig": {"OriginAccessIdentity": ""},
            }],
        },
        "DefaultCacheBehavior": {
            "TargetOriginId": origin_id,
            "ViewerProtocolPolicy": "redirect-to-https",
            "Compress": True,
            "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",  # Managed-CachingOptimized
            # AWS-managed SecurityHeadersPolicy: sends Strict-Transport-Security
            # (max-age=31536000; includeSubDomains), X-Content-Type-Options
            # nosniff, X-Frame-Options SAMEORIGIN, and Referrer-Policy on every
            # response — without HSTS a MITM could downgrade the first request
            # (CWE-319). Parity with base-stack.yaml's SecurityHeadersPolicy.
            "ResponseHeadersPolicyId": response_headers_policy_id
            or "67f7725c-6f97-4210-82d7-5512b31e9d03",
        },
    }


def create_distribution(bucket: str, region: str, oac_id: str, site_id: str, profile: str,
                        tags: list[dict[str, str]] | None = None,
                        response_headers_policy_id: str = "") -> dict[str, str]:
    """Create a tagged distribution (tag-on-create, §5). Returns id/arn/domain.

    ``tags`` overrides the tag set written on create. It exists because the tags below
    are the SITE surface's, and a caller needing a different set had no way to get one:
    it had to create with these and then tag/untag afterwards, which is two more calls,
    a window in which the resource is mis-tagged, and a ``cloudfront:UntagResource``
    permission it would otherwise never need. Omitting it keeps the historical behaviour
    exactly, so every existing caller is unaffected.
    """
    payload = {
        "DistributionConfig": distribution_config(
            bucket, region, oac_id, response_headers_policy_id
        ),
        "Tags": {"Items": tags if tags is not None else [
            {"Key": TAG_MANAGED, "Value": "true"},
            {"Key": TAG_SITE, "Value": site_id},
        ]},
    }
    out = _checked(
        ["cloudfront", "create-distribution-with-tags",
         "--distribution-config-with-tags", json.dumps(payload), "--output", "json"],
        profile, action="cloudfront:CreateDistributionWithTags",
    )
    dist = json.loads(out)["Distribution"]
    return {"id": dist["Id"], "arn": dist["ARN"], "domain": dist["DomainName"]}


def put_oac_bucket_policy(bucket: str, distribution_arn: str, profile: str) -> None:
    """Write the OAC bucket policy pinning AWS:SourceArn — MUST run after create_distribution."""
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowCloudFrontOAC",
                "Effect": "Allow",
                "Principal": {"Service": "cloudfront.amazonaws.com"},
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/*",
                "Condition": {"StringEquals": {"AWS:SourceArn": distribution_arn}},
            },
            # The control manifest lives beside the published site
            # content, so without this Deny it is world-readable through the
            # distribution (leaks local username + bucket/distribution/OAC
            # ids). Explicitly deny CloudFront the manifest and quarantine
            # keys — the reaper reads them via the S3 API (IAM role), which
            # this service-principal Deny does not affect.
            {
                "Sid": "DenyCloudFrontControlManifests",
                "Effect": "Deny",
                "Principal": {"Service": "cloudfront.amazonaws.com"},
                "Action": "s3:GetObject",
                "Resource": [
                    f"arn:aws:s3:::{bucket}/*/.kirocrew-deploy.json",
                    f"arn:aws:s3:::{bucket}/_quarantine/*",
                ],
            },
        ],
    }
    _checked(
        ["s3api", "put-bucket-policy", "--bucket", bucket, "--policy", json.dumps(policy)],
        profile, action="s3:PutBucketPolicy",
    )


def sync_dir(src_dir: str, bucket: str, profile: str) -> None:
    # --no-follow-symlinks: never upload a symlink's target (defense-in-depth
    # against an in-dir symlink pointing at a sensitive file slipping past the
    # handler's is_sensitive_path guard).
    _checked(["s3", "sync", src_dir, f"s3://{bucket}", "--delete", "--no-follow-symlinks"],
             profile, action="s3:PutObject", timeout=120)


def invalidate(distribution_id: str, profile: str) -> None:
    _checked(["cloudfront", "create-invalidation", "--distribution-id", distribution_id,
              "--paths", "/*"], profile, action="cloudfront:CreateInvalidation")


def distribution_status(distribution_id: str, profile: str) -> tuple[str, str]:
    """Return (status, domain) read live from the distribution (§5 live status)."""
    out = _checked(["cloudfront", "get-distribution", "--id", distribution_id, "--output", "json"],
                   profile, action="cloudfront:GetDistribution")
    dist = json.loads(out)["Distribution"]
    return dist.get("Status", ""), dist.get("DomainName", "")


# --- orchestration ---------------------------------------------------------

def deploy(site_id: str, src_dir: str, profile: str, region: str = DEFAULT_REGION) -> dict[str, Any]:
    """Idempotent deploy of ``src_dir`` to the site ``site_id``. Returns a result dict.

    First deploy: create bucket → OAC → distribution → bucket-policy → sync → invalidate.
    Re-deploy (site exists by tag): reuse bucket+distribution, just sync + invalidate.
    """
    region = region or DEFAULT_REGION
    existing = find_site_by_tag(site_id, profile, region)

    if existing and existing.get("bucket") and existing.get("distribution_id"):
        bucket = existing["bucket"]
        dist_id = existing["distribution_id"]
        sync_dir(src_dir, bucket, profile)
        invalidate(dist_id, profile)
        status, domain = distribution_status(dist_id, profile)
        return {"site_id": site_id, "bucket": bucket, "distribution_id": dist_id,
                "url": f"https://{domain}/", "status": status, "reused": True}

    # Partial-deploy recovery (§5): a prior run created + tagged the bucket but
    # failed before the distribution existed. Reuse that bucket rather than
    # allocating a new random one (which would orphan the existing tagged bucket).
    if existing and existing.get("bucket"):
        bucket = existing["bucket"]
    else:
        # First deploy — fresh infra. Retry bucket creation on a global-name 409.
        bucket = ""
        for _ in range(5):
            candidate = random_bucket_name()
            rc, _out, err = run_aws(
                ["s3api", "create-bucket", "--bucket", candidate, "--region", region]
                + ([] if region == "us-east-1"
                   else ["--create-bucket-configuration", f"LocationConstraint={region}"]),
                profile,
            )
            if rc == 0:
                bucket = candidate
                break
            if "BucketAlreadyExists" in err or "BucketAlreadyOwnedByYou" in err:
                continue
            sid = map_access_denied(err)
            raise AWSError(f"s3:CreateBucket failed: {_trimmed_stderr(err)}", missing_statement=sid)
        if not bucket:
            raise AWSError("could not allocate a unique bucket name after retries")

    # Harden the bucket (BPA / ownership / encryption / tag / versioning /
    # lifecycle / access logging) through the one shared helper.
    _harden_bucket(
        bucket, profile,
        f"TagSet=[{{Key={TAG_MANAGED},Value=true}},{{Key={TAG_SITE},Value={site_id}}}]",
    )

    oac_id = create_oac(_oac_name(site_id), profile)
    dist = create_distribution(bucket, region, oac_id, site_id, profile)
    # Ordering gotcha (§4): bucket policy MUST follow create-distribution (needs the ARN).
    put_oac_bucket_policy(bucket, dist["arn"], profile)
    sync_dir(src_dir, bucket, profile)
    invalidate(dist["id"], profile)
    return {"site_id": site_id, "bucket": bucket, "distribution_id": dist["id"],
            "oac_id": oac_id,
            "url": f"https://{dist['domain']}/", "status": "InProgress", "reused": False}


# --- recall / destroy / list ----------------------------------------------

def empty_bucket(bucket: str, profile: str) -> None:
    """Remove all objects from the bucket (s3 rm --recursive)."""
    _checked(["s3", "rm", f"s3://{bucket}", "--recursive"], profile,
             action="s3:DeleteObject", timeout=120)


def recall(site_id: str, profile: str, region: str = DEFAULT_REGION) -> dict[str, Any]:
    """Fast unpublish (§5/Q5): empty objects + invalidate /*. Infra stays, reversible.

    URL → 404 in seconds-to-minutes. NOT disable-distribution (that is slow). Edge
    caches may still serve until the invalidation completes; already-downloaded
    content cannot be recalled (surfaced by the caller).
    """
    site = find_site_by_tag(site_id, profile, region)
    if not site:
        raise AWSError(f"no site found for '{site_id}'")
    if site.get("bucket"):
        empty_bucket(site["bucket"], profile)
    if site.get("distribution_id"):
        invalidate(site["distribution_id"], profile)
    return {"site_id": site_id, "recalled": True,
            "bucket": site.get("bucket", ""), "distribution_id": site.get("distribution_id", "")}


def _get_distribution_config(dist_id: str, profile: str) -> tuple[str, dict[str, Any]]:
    out = _checked(["cloudfront", "get-distribution-config", "--id", dist_id, "--output", "json"],
                   profile, action="cloudfront:GetDistributionConfig")
    data = json.loads(out)
    return data["ETag"], data["DistributionConfig"]


def _disable_distribution(dist_id: str, profile: str) -> None:
    etag, config = _get_distribution_config(dist_id, profile)
    if config.get("Enabled", True):
        config["Enabled"] = False
        _checked(["cloudfront", "update-distribution", "--id", dist_id,
                  "--distribution-config", json.dumps(config), "--if-match", etag, "--output", "json"],
                 profile, action="cloudfront:UpdateDistribution")


def _wait_until_deployed(dist_id: str, profile: str) -> None:
    """Poll until the distribution reaches Deployed (required before delete)."""
    for _ in range(_DESTROY_WAIT_POLLS):
        status, _domain = distribution_status(dist_id, profile)
        if status == "Deployed":
            return
        _sleep(_DESTROY_WAIT_INTERVAL)
    raise AWSError(f"distribution {dist_id} did not reach Deployed in time")


def _delete_distribution(dist_id: str, profile: str) -> None:
    etag, _config = _get_distribution_config(dist_id, profile)  # latest ETag after disable
    _checked(["cloudfront", "delete-distribution", "--id", dist_id, "--if-match", etag],
             profile, action="cloudfront:DeleteDistribution")


def _delete_oac_for_site(site_id: str, profile: str) -> bool:
    """Best-effort: delete the site's OAC (named deploy-web-<site_id>-*).

    The DEPLOY policy does not grant account-wide
    cloudfront:DeleteOriginAccessControl (a compromised deploy session could
    delete every OAC in the account). Cleanup is owned by the reaper role;
    when this best-effort attempt is denied, we return False so destroy()
    reports the deferred cleanup honestly instead of claiming completeness.
    Returns True when no OAC remains for the site, False when cleanup was
    skipped/denied and is deferred to the reaper.
    """
    try:
        out = _checked(["cloudfront", "list-origin-access-controls", "--output", "json"],
                       profile, action="cloudfront:ListOriginAccessControls")
        items = (json.loads(out).get("OriginAccessControlList", {}) or {}).get("Items", [])
        # Exact-suffix match — a bare prefix cross-matches overlapping
        # site ids (see reaper_lambda). Names: <prefix>-<6 hex>.
        oac_re = re.compile(re.escape(_oac_name_prefix(site_id)) + r"-[0-9a-f]{6}$")
        for item in items:
            if oac_re.fullmatch(str(item.get("Name", ""))):
                oac_id = item["Id"]
                got = _checked(["cloudfront", "get-origin-access-control", "--id", oac_id, "--output", "json"],
                               profile, action="cloudfront:GetOriginAccessControl")
                etag = json.loads(got).get("ETag", "")
                _checked(["cloudfront", "delete-origin-access-control", "--id", oac_id, "--if-match", etag],
                         profile, action="cloudfront:DeleteOriginAccessControl")
    except AWSError:
        logger.warning("deploy-web: OAC cleanup deferred to reaper for %s", site_id)
        return False
    return True


def destroy(site_id: str, profile: str, region: str = DEFAULT_REGION) -> dict[str, Any]:
    """Full teardown (§5): disable → wait Deployed → delete distribution → delete OAC →
    empty + delete bucket. Slower than recall; removes all resources/cost.
    """
    site = find_site_by_tag(site_id, profile, region)
    if not site:
        raise AWSError(f"no site found for '{site_id}'")
    dist_id = site.get("distribution_id", "")
    bucket = site.get("bucket", "")
    if dist_id:
        _disable_distribution(dist_id, profile)
        _wait_until_deployed(dist_id, profile)
        _delete_distribution(dist_id, profile)
    oac_cleaned = _delete_oac_for_site(site_id, profile)
    if bucket:
        empty_bucket(bucket, profile)
        _checked(["s3api", "delete-bucket", "--bucket", bucket], profile, action="s3:DeleteBucket")
    return {"site_id": site_id, "destroyed": True, "bucket": bucket,
            "distribution_id": dist_id, "oac_cleanup": "done" if oac_cleaned else "deferred-to-reaper"}


def list_sites(profile: str, region: str = DEFAULT_REGION) -> list[dict[str, Any]]:
    """List all deploy-web sites live by tag (§5 stateless-by-tag — no local cache).

    Groups managed resources by the kirocrew:site tag; status/domain read live
    from each distribution's Status field.
    """
    out = _checked(
        ["resourcegroupstaggingapi", "get-resources",
         "--tag-filters", f"Key={TAG_MANAGED},Values=true",
         "--region", region or DEFAULT_REGION, "--output", "json"],
        profile, action="tag:GetResources",
    )
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return []
    sites: dict[str, dict[str, Any]] = {}
    for mapping in data.get("ResourceTagMappingList", []):
        arn = mapping.get("ResourceARN", "")
        tags = {t["Key"]: t["Value"] for t in mapping.get("Tags", [])}
        sid = tags.get(TAG_SITE, "")
        if not sid:
            continue
        entry = sites.setdefault(sid, {"site_id": sid, "bucket": "", "distribution_id": ""})
        if arn.startswith("arn:aws:s3:::"):
            entry["bucket"] = arn.split(":::", 1)[1]
        elif ":distribution/" in arn:
            entry["distribution_id"] = arn.rsplit("/", 1)[-1]
    result: list[dict[str, Any]] = []
    for sid in sorted(sites):
        entry = sites[sid]
        status, domain = "", ""
        if entry["distribution_id"]:
            try:
                status, domain = distribution_status(entry["distribution_id"], profile)
            except AWSError:
                status = "unknown"
        entry["status"] = status
        entry["url"] = f"https://{domain}/" if domain else ""
        result.append(entry)
    return result
