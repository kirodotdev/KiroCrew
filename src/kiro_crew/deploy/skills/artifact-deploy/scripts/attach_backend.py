#!/usr/bin/env python3
"""Attach a per-app backend origin to the shared KiroCrew CloudFront distribution.

Idempotently adds an origin for the backend + a '<slug>/api/*' cache behavior
routing to it (no caching, all-viewer-except-host so the origin sees the right
Host). Two origin flavors:
  * API Gateway (default): plain HTTPS custom origin, no OAC. Guardrail-safe -
    the Lambda behind it is not world-accessible.
  * Lambda Function URL (--oac): adds a Lambda OAC so CloudFront SigV4-signs the
    origin request (for AWS_IAM Function URLs in unrestricted accounts).

Append-only: preserves the existing config (default S3 behavior, other apps).
CloudFront is global; region is only for CLI profile plumbing.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"  # Managed-CachingDisabled
ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"  # Managed-AllViewerExceptHostHeader
OAC_NAME = "kirocrew-deploy-lambda-oac"


def aws(profile, region, *args):
    cmd = ["aws"] + (["--profile", profile] if profile else []) + ["--region", region, *args]
    # R34 F2: every AWS spawn from these LLM-facing helpers MUST route through
    # the sandbox chokepoint — the previous ImportError fallback ran completely
    # unsandboxed when kiro_crew wasn't importable, which is exactly the
    # environment an attacker would arrange. Fail closed instead: standalone
    # operators must run via the package venv (pip install -e / the skill's
    # documented invocation), never bare python3 without kiro_crew on sys.path.
    try:
        from kiro_crew.sandbox import resource_limit_preexec, sandboxed_spawn_argv
    except ImportError:
        sys.stderr.write(
            "error: kiro_crew package not importable — refusing to spawn AWS "
            "commands unsandboxed. Run this script with the KiroCrew venv "
            "python (see skills/artifact-deploy/SKILL.md).\n"
        )
        sys.exit(1)
    wrapped_argv, env, cleanup = sandboxed_spawn_argv(cmd)
    _preexec = resource_limit_preexec()
    # Kernel RLIMIT ceiling on the child (fork bomb / FD / mem / CPU) — the
    # spawn-audit rule requires this on every sandbox-routed spawn.
    r = subprocess.run(  # noqa: S603
        wrapped_argv, capture_output=True, text=True, env=env, preexec_fn=_preexec
    )
    if cleanup:
        import os as _os

        try:
            _os.unlink(cleanup)
        except OSError:
            pass
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(r.returncode)
    return r.stdout


def ensure_lambda_oac(profile, region):
    data = json.loads(
        aws(profile, region, "cloudfront", "list-origin-access-controls", "--output", "json")
    )
    for it in (data.get("OriginAccessControlList") or {}).get("Items") or []:
        if it.get("Name") == OAC_NAME:
            return it["Id"]
    cfg = {
        "Name": OAC_NAME,
        "Description": "KiroCrew deploy Lambda OAC",
        "SigningProtocol": "sigv4",
        "SigningBehavior": "always",
        "OriginAccessControlOriginType": "lambda",
    }
    res = json.loads(
        aws(
            profile,
            region,
            "cloudfront",
            "create-origin-access-control",
            "--origin-access-control-config",
            json.dumps(cfg),
            "--output",
            "json",
        )
    )
    return res["OriginAccessControl"]["Id"]


def _validate_args(profile: str, region: str, dist_id: str, slug: str) -> None:
    """Validate all argv before any aws call. Exit 2 on mismatch."""
    _PROFILE_RE = re.compile(r"^[a-zA-Z0-9._:/-]+$")
    _REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d+$")
    _DIST_ID_RE = re.compile(r"^[A-Z0-9]{13,14}$")
    _SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

    errors = []
    if profile and not _PROFILE_RE.match(profile):
        errors.append(f"--profile: invalid format: {profile!r}")
    if not _REGION_RE.match(region):
        errors.append(f"--region: not a valid AWS region: {region!r}")
    if not _DIST_ID_RE.match(dist_id):
        errors.append(
            f"--dist-id: must match CloudFront distribution ID pattern (13-14 uppercase alphanumeric): {dist_id!r}"
        )
    if not _SLUG_RE.match(slug):
        errors.append(f"--slug: must be lowercase alphanumeric + hyphens, 1-63 chars: {slug!r}")
    if errors:
        for e in errors:
            sys.stderr.write(f"error: {e}\n")
        sys.exit(2)


# Sensitive paths that must never be read via --origin-verify-secret-file.
# Standalone list (duplicated here for standalone execution where kiro_crew
# may not be importable) — covers both the current KiroCrew data home
# (~/.kiro/crew, since the ~/.kirocrew -> ~/.kiro/crew move) and the pre-move
# legacy home, so a not-yet-migrated box is covered too.
_SENSITIVE_PREFIXES: tuple[str, ...] = (
    ".aws",
    ".ssh",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker/config.json",
    ".kube/config",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    ".kiro/crew/.env",
    ".kiro/crew/sel_hmac.key",
    ".kiro/crew/security_events.jsonl",
    ".kirocrew/.env",
    ".kirocrew/sel_hmac.key",
    ".kirocrew/security_events.jsonl",
)


def _validate_secret_file_path(path: Path) -> None:
    """Validate a secret file path before reading: must be absolute, regular,
    owned by current uid, mode 0o600, and not a sensitive path."""
    if not path.is_absolute():
        sys.stderr.write(
            f"error: --origin-verify-secret-file must be an absolute path, got: {path}\n"
        )
        sys.exit(2)

    # Check sensitive paths (relative to $HOME)
    home = Path.home()
    try:
        rel = path.resolve().relative_to(home)
        rel_str = str(rel)
        for prefix in _SENSITIVE_PREFIXES:
            if rel_str == prefix or rel_str.startswith(prefix + "/"):
                sys.stderr.write(
                    f"error: --origin-verify-secret-file points to a sensitive path: {path}\n"
                )
                sys.exit(2)
    except ValueError:
        pass  # Not under $HOME — ok

    # Must be a regular file (no symlinks, dirs, etc.)
    try:
        st = os.lstat(path)
    except OSError as e:
        sys.stderr.write(f"error: cannot stat --origin-verify-secret-file: {e}\n")
        sys.exit(2)

    import stat as stat_mod

    if stat_mod.S_ISLNK(st.st_mode):
        sys.stderr.write(f"error: --origin-verify-secret-file must not be a symlink: {path}\n")
        sys.exit(2)
    if not stat_mod.S_ISREG(st.st_mode):
        sys.stderr.write(f"error: --origin-verify-secret-file must be a regular file: {path}\n")
        sys.exit(2)

    # Must be owned by current uid
    if st.st_uid != os.getuid():
        sys.stderr.write(
            f"error: --origin-verify-secret-file must be owned by current user: {path}\n"
        )
        sys.exit(2)

    # Must have no group/other bits (mode 0o600 or stricter)
    mode_bits = stat_mod.S_IMODE(st.st_mode)
    if mode_bits & 0o077:
        sys.stderr.write(
            f"error: --origin-verify-secret-file has insecure permissions "
            f"({oct(mode_bits)}), expected 0o600 or stricter: {path}\n"
        )
        sys.exit(2)


def _validate_origin_domain(domain: str, region: str) -> None:
    """R19 F3: Validate origin_domain is a genuine API Gateway execute-api endpoint.

    The origin-verify secret header is sent to this domain — an attacker-controlled
    domain would exfiltrate the secret and open direct API GW access. Only allow
    the strict execute-api pattern for the expected region.
    """
    escaped_region = re.escape(region)
    pattern = rf"^[a-z0-9]+\.execute-api\.{escaped_region}\.amazonaws\.com$"
    if not re.fullmatch(pattern, domain):
        sys.stderr.write(
            f"error: --origin-domain must be a valid execute-api endpoint in region "
            f"{region} (pattern: <id>.execute-api.{region}.amazonaws.com), "
            f"got: {domain!r}\n"
        )
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="")
    ap.add_argument("--region", required=True)
    ap.add_argument("--dist-id", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--origin-domain", required=True)
    ap.add_argument(
        "--oac",
        action="store_true",
        help="attach a Lambda OAC (only for Lambda Function URL origins)",
    )
    ap.add_argument(
        "--origin-verify-secret",
        default="",
        help="random secret for x-kirocrew-origin-verify header (API GW authorizer)",
    )
    ap.add_argument(
        "--origin-verify-secret-file",
        default="",
        help="path to file containing origin-verify secret (preferred over argv)",
    )
    a = ap.parse_args()
    profile = a.profile or ""

    # Resolve origin-verify-secret: file takes precedence over argv
    origin_verify_secret = a.origin_verify_secret
    if a.origin_verify_secret_file:
        secret_path = Path(a.origin_verify_secret_file)
        # R13 F1: validate the path shape FIRST (absolute, regular file, owned
        # by us, mode 0600, not a sensitive path) — R10's fail-closed refactor
        # removed the fallback that was this validator's only caller, orphaning
        # it. safe_read_file below adds the atomic O_NOFOLLOW read on top.
        _validate_secret_file_path(secret_path)
        # Security: read the secret file atomically.
        # Preferred path: kiro_crew.hooks.safe_read_file does is_sensitive_path +
        # O_NOFOLLOW open in one shot, closing the TOCTOU between validation and
        # read. Fallback: standalone execution where kiro_crew is not importable
        # uses O_NOFOLLOW open directly (still race-free on the final component).
        try:
            from kiro_crew.hooks import safe_read_file

            try:
                origin_verify_secret = safe_read_file(str(secret_path)).strip()
            except PermissionError as e:
                sys.stderr.write(f"error: --origin-verify-secret-file blocked: {e}\n")
                sys.exit(2)
            except (OSError, ValueError) as e:
                sys.stderr.write(f"error: cannot read --origin-verify-secret-file: {e}\n")
                sys.exit(2)
        except ImportError:
            # R10 F2 (fail-closed): the shared sensitive-path gate lives in
            # kiro_crew.hooks.safe_read_file. Reading a user-controlled secret
            # path WITHOUT that gate (standalone local validation only) violates
            # the blocking sensitive-read rule — refuse rather than approximate.
            sys.stderr.write(
                "error: kiro_crew.hooks is unavailable — cannot read "
                "--origin-verify-secret-file without the shared sensitive-path "
                "gate. Run this script from a kiro_crew environment (fail-closed).\n"
            )
            sys.exit(2)
    # Argument validation — FieldSpec patterns with ImportError fallback for
    # standalone operator execution where kiro_crew is not on sys.path.
    _validate_args(profile, a.region, a.dist_id, a.slug)

    # R19 F3: validate origin_domain is a genuine execute-api domain in the
    # expected region — prevents exfiltration of the origin-verify secret to
    # attacker-controlled endpoints.
    _validate_origin_domain(a.origin_domain, a.region)

    # R13 F1: bounded format check — the secret is generated as secrets.token_hex(32)
    # (64 hex chars). Anything else is a wrong file / corruption — refuse rather
    # than uploading arbitrary file contents to AWS as an origin secret.
    if origin_verify_secret and not re.fullmatch(r"[0-9a-f]{64}", origin_verify_secret):
        sys.stderr.write(
            "error: origin-verify secret must be 64 lowercase hex characters "
            "(secrets.token_hex(32)) — refusing to attach a malformed secret\n"
        )
        sys.exit(2)

    origin_id = f"backend-{a.slug}"
    path_pattern = f"{a.slug}/api/*"
    oac_id = ensure_lambda_oac(profile, a.region) if a.oac else None

    gdc = json.loads(
        aws(
            profile,
            a.region,
            "cloudfront",
            "get-distribution-config",
            "--id",
            a.dist_id,
            "--output",
            "json",
        )
    )
    etag = gdc["ETag"]
    cfg = gdc["DistributionConfig"]

    origins = cfg["Origins"]
    # Build custom origin headers — includes origin-verify secret if provided
    custom_headers: dict = {"Quantity": 0}
    if origin_verify_secret:
        custom_headers = {
            "Quantity": 1,
            "Items": [
                {
                    "HeaderName": "x-kirocrew-origin-verify",
                    "HeaderValue": origin_verify_secret,
                }
            ],
        }
    existing_origin = next((o for o in origins["Items"] if o["Id"] == origin_id), None)
    if existing_origin:
        # UPDATE domain + custom headers on redeploy (new secret must propagate;
        # without this, deploy-backend.sh mints a new secret but CloudFront still
        # sends the old one → authorizer rejects → API bricked).
        existing_origin["DomainName"] = a.origin_domain
        existing_origin["CustomHeaders"] = custom_headers
        if oac_id:
            existing_origin["OriginAccessControlId"] = oac_id
    else:
        origin = {
            "Id": origin_id,
            "DomainName": a.origin_domain,
            "OriginPath": "",
            "CustomHeaders": custom_headers,
            "CustomOriginConfig": {
                "HTTPPort": 80,
                "HTTPSPort": 443,
                "OriginProtocolPolicy": "https-only",
                "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                "OriginReadTimeout": 30,
                "OriginKeepaliveTimeout": 5,
            },
            "OriginShield": {"Enabled": False},
            "ConnectionAttempts": 3,
            "ConnectionTimeout": 10,
        }
        if oac_id:
            origin["OriginAccessControlId"] = oac_id
        origins["Items"].append(origin)
        origins["Quantity"] = len(origins["Items"])

    cbs = cfg.get("CacheBehaviors") or {"Quantity": 0, "Items": []}
    cbs.setdefault("Items", [])
    if not any(b.get("PathPattern") == path_pattern for b in cbs["Items"]):
        cbs["Items"].append(
            {
                "PathPattern": path_pattern,
                "TargetOriginId": origin_id,
                "ViewerProtocolPolicy": "redirect-to-https",
                "CachePolicyId": CACHING_DISABLED,
                "OriginRequestPolicyId": ALL_VIEWER_EXCEPT_HOST,
                "Compress": True,
                "AllowedMethods": {
                    "Quantity": 7,
                    "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                    "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                },
                "SmoothStreaming": False,
                "FieldLevelEncryptionId": "",
                "LambdaFunctionAssociations": {"Quantity": 0},
                "FunctionAssociations": {"Quantity": 0},
                "TrustedSigners": {"Enabled": False, "Quantity": 0},
                "TrustedKeyGroups": {"Enabled": False, "Quantity": 0},
            }
        )
        cbs["Quantity"] = len(cbs["Items"])
    cfg["CacheBehaviors"] = cbs

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    try:
        json.dump(cfg, tmp)
        tmp.close()
        aws(
            profile,
            a.region,
            "cloudfront",
            "update-distribution",
            "--id",
            a.dist_id,
            "--distribution-config",
            f"file://{tmp.name}",
            "--if-match",
            etag,
            "--output",
            "json",
        )
    finally:
        os.unlink(tmp.name)
    tail = f" via OAC {oac_id}" if oac_id else " (no OAC)"
    print(f"attached: {path_pattern} -> {origin_id} ({a.origin_domain}){tail}")


if __name__ == "__main__":
    main()
