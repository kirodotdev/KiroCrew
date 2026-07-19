"""KiroCrew One-Click Deploy - in-account reaper Lambda.

Triggered by an EventBridge schedule (see reaper.yaml). Each run scans the
shared deploy bucket for per-slug `.kirocrew-deploy.json` manifests and, for any
NON-persistent deploy whose `expires_at` is in the past, reaps it:
  1. delete its /<slug>/ static objects (+ its _backends/<slug>/*.zip)
  2. detach its `<slug>/api/*` CloudFront behavior + origin (if any)
  3. delete its backend CloudFormation stack kirocrew-deploy-app-<slug>
     (which cascades API Gateway + Lambda + DynamoDB + role)

Runs entirely in the user's account on the schedule — no local creds, no human.
Environment: BUCKET, DIST_ID.
"""
import calendar
import json
import os
import re
import time

import boto3
import botocore.exceptions

BUCKET = os.environ["BUCKET"]
DIST_ID = os.environ["DIST_ID"]

# F7a (R9): Validate manifest-sourced resource IDs before destructive calls.
_RE_DIST_ID = re.compile(r"^[A-Z0-9]+$")
_RE_STACK_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*$")
_RE_BUCKET_NAME = re.compile(r"^[a-z0-9.\-]{3,63}$")


def _valid_dist_id(v: str) -> bool:
    return bool(v and _RE_DIST_ID.fullmatch(v))


def _valid_stack_name(v: str) -> bool:
    return bool(v and _RE_STACK_NAME.fullmatch(v))


def _valid_bucket_name(v: str) -> bool:
    return bool(v and _RE_BUCKET_NAME.fullmatch(v))


def _oac_name_ok(oac_resp, slug_val):
    """R18 F3: manifest ``oac_id`` is attacker-writable (anyone who can write
    a deploy manifest). Only delete an OAC whose Name matches THIS
    deployment's exact pattern -- otherwise skip and log."""
    name = (oac_resp.get("OriginAccessControl", {})
            .get("OriginAccessControlConfig", {}).get("Name", ""))
    pat = re.compile(re.escape(f"deploy-web-{slug_val}"[:57]) + r"-[0-9a-f]{6}$")
    return bool(pat.fullmatch(name))


s3 = boto3.client("s3")
cfn = boto3.client("cloudformation")
cf = boto3.client("cloudfront")


def _slugs():
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            p = cp["Prefix"].rstrip("/")
            if p and p not in ("_backends", "_reaper"):
                out.append(p)
    return out


def _manifest(slug):
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=f"{slug}/.kirocrew-deploy.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def _expired(man, now):
    if not man or man.get("persistent"):
        return False
    exp = man.get("expires_at")
    if not exp:
        return False
    try:
        return int(calendar.timegm(time.strptime(exp, "%Y-%m-%dT%H:%M:%SZ"))) < now
    except Exception:
        return False


def _del_prefix(prefix):
    """Delete all objects under `prefix`, batched per paginator page (<=1000 keys)."""
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})


def _detach(slug):
    pattern = f"{slug}/api/*"
    g = cf.get_distribution_config(Id=DIST_ID)
    etag, cfg = g["ETag"], g["DistributionConfig"]
    cbs = cfg.get("CacheBehaviors") or {"Quantity": 0, "Items": []}
    target, kept = None, []
    for b in cbs.get("Items", []):
        if b.get("PathPattern") == pattern:
            target = b.get("TargetOriginId")
        else:
            kept.append(b)
    if target is None:
        return
    cbs["Items"], cbs["Quantity"] = kept, len(kept)
    cfg["CacheBehaviors"] = cbs
    used = cfg["DefaultCacheBehavior"].get("TargetOriginId") == target or any(
        b.get("TargetOriginId") == target for b in kept
    )
    if not used:
        og = cfg["Origins"]
        og["Items"] = [o for o in og["Items"] if o.get("Id") != target]
        og["Quantity"] = len(og["Items"])
    cf.update_distribution(Id=DIST_ID, IfMatch=etag, DistributionConfig=cfg)


def _stack_site_tag_ok(name, slug):
    """R21 F1: a stack may only be deleted when its kirocrew:site tag equals
    the slug being reaped (deploy-backend.sh tags stacks at creation). Missing
    or mismatched tag means the stack is not provably this deployment's."""
    try:
        desc = cfn.describe_stacks(StackName=name)
        stacks = desc.get("Stacks", [])
        tags = {t.get("Key"): t.get("Value")
                for t in (stacks[0].get("Tags", []) if stacks else [])}
    except Exception as exc:  # noqa: BLE001 - fail closed on describe error
        print(json.dumps({"stack_tag_check_error": slug, "error": str(exc)}))
        return False
    if tags.get("kirocrew:site") != slug:
        print(json.dumps({"app_reap_stack_tag_mismatch": slug, "stack": name,
                          "actual": tags.get("kirocrew:site", "<missing>")}))
        return False
    return True


def _quarantine_manifest(slug):
    """R24: move an identity-mismatched (untrusted) manifest out of the sweep
    path so the reaper stops re-processing it every run. The manifest content
    is preserved under _quarantine/ for forensics; the mismatched
    infrastructure itself is left untouched."""
    key = f"{slug}/.kirocrew-deploy.json"
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        s3.put_object(
            Bucket=BUCKET,
            Key=f"_quarantine/{slug}.kirocrew-deploy.json",
            Body=obj["Body"].read(),
            ContentType="application/json",
        )
        s3.delete_object(Bucket=BUCKET, Key=key)
        print(json.dumps({"manifest_quarantined": slug}))
    except Exception as exc:  # noqa: BLE001 - best effort; retry next sweep
        print(json.dumps({"manifest_quarantine_failed": slug, "error": str(exc)}))


def _del_stack(slug):
    name = f"kirocrew-deploy-app-{slug}"
    if not _valid_stack_name(name):
        print(json.dumps({"skip_invalid_stack_name": name}))
        return
    try:
        cfn.describe_stacks(StackName=name)
    except Exception:
        return
    if not _stack_site_tag_ok(name, slug):
        return
    cfn.delete_stack(StackName=name)


def _reap_engine_arch(man, slug, now):
    """Reap an engine-arch deployment: disable+delete distribution, empty+delete per-site bucket.

    Returns: "reaped" if fully cleaned up, "reaping" if distribution still disabling (retry next sweep).
    """
    # R20 F2: the manifest body is attacker-writable; a manifest whose own
    # "slug" field disagrees with the S3 prefix it lives under is forged —
    # refuse all destructive work and stop retrying.
    man_slug = man.get("slug", "")
    if man_slug and man_slug != slug:
        print(json.dumps({"engine_reap_slug_mismatch": slug, "manifest_slug": man_slug}))
        _quarantine_manifest(slug)
        return "reaped"
    # F4: oac_pending — distribution/bucket already gone, only OAC remains.
    if man.get("oac_pending"):
        oac_id = man.get("oac_id", "")
        if oac_id:
            try:
                # F3 (R8): IfMatch ETag is REQUIRED by the CloudFront API.
                oac_resp = cf.get_origin_access_control(Id=oac_id)
                if not _oac_name_ok(oac_resp, slug):
                    print(json.dumps({"engine_reap_oac_name_mismatch": slug,
                                      "oac_id": oac_id}))
                else:
                    oac_etag = oac_resp["ETag"]
                    cf.delete_origin_access_control(Id=oac_id, IfMatch=oac_etag)
            except botocore.exceptions.ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code == "NoSuchOriginAccessControl":
                    pass  # Already gone.
                elif code == "OriginAccessControlInUse":
                    print(json.dumps({"engine_reap_oac_pending_in_use": slug, "oac_id": oac_id}))
                    return "reaping"
                else:
                    print(json.dumps({"engine_reap_oac_pending_error": slug, "error": str(e)}))
                    return "reaping"
        # OAC deleted (or already gone) — clean up manifest.
        try:
            s3.delete_object(Bucket=BUCKET, Key=f"{slug}/.kirocrew-deploy.json")
        except Exception as exc:
            print(json.dumps({"engine_reap_manifest_delete_error": slug, "error": str(exc)}))
        return "reaped"

    dist_id = man.get("distribution_id", "")
    site_bucket = man.get("bucket", "")

    # F7a (R9): Validate manifest-sourced resource IDs before destructive calls.
    if dist_id and not _valid_dist_id(dist_id):
        print(json.dumps({"engine_reap_invalid_dist_id": slug, "dist_id": dist_id}))
        return "reaping"
    if site_bucket and not _valid_bucket_name(site_bucket):
        print(json.dumps({"engine_reap_invalid_bucket": slug, "bucket": site_bucket}))
        return "reaping"

    if dist_id:
        # R19 F4: verify kirocrew:site tag matches the slug being reaped BEFORE
        # any destructive operation. The tag VALUE is the site_id (NOT "true" —
        # that's on kirocrew:managed). engine.py sets this at creation via
        # create-distribution-with-tags.
        try:
            account_id = os.environ.get("ACCOUNT_ID", "")
            if not account_id:
                sts = boto3.client("sts")
                account_id = sts.get_caller_identity()["Account"]
            dist_arn = f"arn:aws:cloudfront::{account_id}:distribution/{dist_id}"
            tag_resp = cf.list_tags_for_resource(Resource=dist_arn)
            tags = {t["Key"]: t["Value"] for t in tag_resp.get("Tags", {}).get("Items", [])}
            # R20 F2: authorize against the S3 prefix slug ONLY — the
            # manifest body is attacker-writable and must not choose
            # which deployment's resources the reaper targets.
            expected_site = slug
            if tags.get("kirocrew:site") != expected_site:
                print(json.dumps({"engine_reap_dist_tag_mismatch": slug,
                                  "dist_id": dist_id,
                                  "expected": expected_site,
                                  "actual": tags.get("kirocrew:site", "<missing>")}))
                _quarantine_manifest(slug)
                # Skip destructive ops — return reaped to stop retrying
                return "reaped"
        except Exception as exc:
            # Tag check failure is non-fatal for NoSuchDistribution (already gone)
            err_code = ""
            if hasattr(exc, "response"):
                err_code = exc.response.get("Error", {}).get("Code", "")
            if err_code != "NoSuchDistribution":
                print(json.dumps({"engine_reap_dist_tag_check_error": slug, "error": str(exc)}))
                return "reaping"

        # Two-phase distribution delete: disable first, then delete.
        try:
            g = cf.get_distribution_config(Id=dist_id)
            etag, cfg = g["ETag"], g["DistributionConfig"]
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "NoSuchDistribution":
                # Already gone — proceed to bucket cleanup.
                pass
            else:
                print(json.dumps({"engine_reap_dist_error": slug, "error": str(e)}))
                return "reaping"
        else:
            if cfg.get("Enabled", True):
                # Phase 1: disable distribution.
                cfg["Enabled"] = False
                try:
                    cf.update_distribution(Id=dist_id, IfMatch=etag, DistributionConfig=cfg)
                except Exception as exc:
                    print(json.dumps({"engine_reap_disable_error": slug, "error": str(exc)}))
                # Mark as reaping — next sweep will attempt delete.
                _reaping_manifest = json.dumps({**man, "reaping": True})
                s3.put_object(
                    Bucket=BUCKET,
                    Key=f"{slug}/.kirocrew-deploy.json",
                    Body=_reaping_manifest.encode(),
                    ContentType="application/json",
                )
                return "reaping"
            else:
                # Phase 2: distribution is disabled — delete it.
                try:
                    # Must re-fetch ETag since it may have changed.
                    g2 = cf.get_distribution_config(Id=dist_id)
                    cf.delete_distribution(Id=dist_id, IfMatch=g2["ETag"])
                except botocore.exceptions.ClientError as e:
                    code = e.response.get("Error", {}).get("Code", "")
                    if code == "DistributionNotDisabled":
                        # Still propagating — retry next sweep.
                        _reaping_manifest = json.dumps({**man, "reaping": True})
                        s3.put_object(
                            Bucket=BUCKET,
                            Key=f"{slug}/.kirocrew-deploy.json",
                            Body=_reaping_manifest.encode(),
                            ContentType="application/json",
                        )
                        return "reaping"
                    elif code == "NoSuchDistribution":
                        pass  # Already gone.
                    else:
                        print(json.dumps({"engine_reap_delete_dist_error": slug, "error": str(e)}))
                        return "reaping"

    # Empty and delete the per-site bucket.
    if site_bucket:
        # R19 F4: verify kirocrew:site tag matches the slug being reaped.
        # engine.py tags buckets at creation: kirocrew:site=<site_id>.
        try:
            bucket_tag_resp = s3.get_bucket_tagging(Bucket=site_bucket)
            bucket_tags = {t["Key"]: t["Value"] for t in bucket_tag_resp.get("TagSet", [])}
            # R20 F2: authorize against the S3 prefix slug ONLY — the
            # manifest body is attacker-writable and must not choose
            # which deployment's resources the reaper targets.
            expected_site = slug
            if bucket_tags.get("kirocrew:site") != expected_site:
                print(json.dumps({"engine_reap_bucket_tag_mismatch": slug,
                                  "bucket": site_bucket,
                                  "expected": expected_site,
                                  "actual": bucket_tags.get("kirocrew:site", "<missing>")}))
                _quarantine_manifest(slug)
                return "reaped"  # skip destructive ops
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "NoSuchBucket":
                pass  # Already gone — skip bucket cleanup
            elif code == "NoSuchTagSet":
                # R27 F1: missing tags = identity verification FAILURE, not a
                # legacy pass. A name-prefix check alone lets a forged manifest
                # point at any untagged kirocrew-web-* bucket (e.g. someone
                # else's half-created deployment) and have it emptied+deleted.
                # Fail closed: quarantine the manifest, touch nothing.
                print(json.dumps({"engine_reap_bucket_untagged": slug,
                                  "bucket": site_bucket}))
                _quarantine_manifest(slug)
                return "reaped"
            else:
                print(json.dumps({"engine_reap_bucket_tag_check_error": slug,
                                  "error": str(e)}))
                return "reaping"
        else:
            pass  # Tag matches, proceed with deletion

        try:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=site_bucket):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    s3.delete_objects(Bucket=site_bucket, Delete={"Objects": keys})
            s3.delete_bucket(Bucket=site_bucket)
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "NoSuchBucket":
                pass  # Already gone.
            else:
                print(json.dumps({"engine_reap_bucket_error": slug, "error": str(e)}))
                return "reaping"

    # F7: Delete the OAC associated with this deployment (prevents quota exhaustion).
    # The OAC id may be stored in the manifest (new deployments); for older manifests
    # without it, fall back to listing OACs and matching the engine naming pattern.
    oac_id = man.get("oac_id", "")
    if not oac_id:
        # Resolve by name prefix (engine.py names OACs: "deploy-web-<site_id>"[:57] + "-" + hex)
        # R20 F2: prefix slug only — never the manifest-supplied slug.
        site_id_val = slug
        # R13 F4: EXACT name pattern — a bare prefix match cross-matches
        # overlapping slugs (site "app" would match "app-v2"'s OAC named
        # deploy-web-app-v2-xxxxxx), deleting the wrong OAC and orphaning the
        # right one. Names are deploy-web-<site>[:57] + "-" + 6 hex (engine.py).
        oac_re = re.compile(
            re.escape(f"deploy-web-{site_id_val}"[:57]) + r"-[0-9a-f]{6}$")
        try:
            oac_resp = cf.list_origin_access_controls(MaxItems="100")
            for item in oac_resp.get("OriginAccessControlList", {}).get("Items", []):
                if oac_re.fullmatch(item.get("Name", "")):
                    oac_id = item["Id"]
                    break
        except Exception as exc:
            print(json.dumps({"engine_reap_oac_list_error": slug, "error": str(exc)}))

    if oac_id:
        try:
            # F3 (R8): IfMatch ETag is REQUIRED by the CloudFront API.
            oac_resp = cf.get_origin_access_control(Id=oac_id)
            # R18 F3: manifest oac_id is attacker-writable -- verify name.
            if not _oac_name_ok(oac_resp, slug):
                print(json.dumps({"engine_reap_oac_name_mismatch": slug,
                                  "oac_id": oac_id}))
                return "reaped"
            oac_etag = oac_resp["ETag"]
            cf.delete_origin_access_control(Id=oac_id, IfMatch=oac_etag)
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "NoSuchOriginAccessControl":
                pass  # Already gone.
            elif code == "OriginAccessControlInUse":
                # Distribution not fully deleted yet — retain manifest for next sweep.
                print(json.dumps({"engine_reap_oac_in_use": slug, "oac_id": oac_id}))
                # Write back manifest with oac_pending flag so next sweep retries
                # only the OAC delete (distribution/bucket already gone).
                try:
                    man_body = {"reaping": True, "oac_pending": True, "oac_id": oac_id,
                                "arch": "engine"}
                    s3.put_object(
                        Bucket=BUCKET,
                        Key=f"{slug}/.kirocrew-deploy.json",
                        Body=json.dumps(man_body).encode(),
                        ContentType="application/json",
                    )
                except Exception as put_exc:
                    print(json.dumps({"engine_reap_oac_manifest_rewrite_error": slug,
                                      "error": str(put_exc)}))
                return "reaping"
            else:
                # Transient error — retain manifest for next sweep.
                print(json.dumps({"engine_reap_oac_delete_error": slug, "error": str(e)}))
                try:
                    man_body = {"reaping": True, "oac_pending": True, "oac_id": oac_id,
                                "arch": "engine"}
                    s3.put_object(
                        Bucket=BUCKET,
                        Key=f"{slug}/.kirocrew-deploy.json",
                        Body=json.dumps(man_body).encode(),
                        ContentType="application/json",
                    )
                except Exception as put_exc:
                    print(json.dumps({"engine_reap_oac_manifest_rewrite_error": slug,
                                      "error": str(put_exc)}))
                return "reaping"

    # Delete the manifest from the shared bucket.
    try:
        s3.delete_object(Bucket=BUCKET, Key=f"{slug}/.kirocrew-deploy.json")
    except Exception as exc:
        print(json.dumps({"engine_reap_manifest_delete_error": slug, "error": str(exc)}))

    return "reaped"


def handler(event, context):
    now = int(time.time())
    reaped = []
    pending = []
    for slug in _slugs():
        man = _manifest(slug)

        # ── Phase B: resume reaping a deployment whose stack is being deleted ──
        if man and man.get("reaping"):
            # Engine-arch reaping (distribution disable/delete cycle)
            if man.get("arch") == "engine":
                result = _reap_engine_arch(man, slug, now)
                if result == "reaped":
                    reaped.append(slug)
                else:
                    pending.append(slug)
                continue

            stack_name = f"kirocrew-deploy-app-{slug}"
            try:
                resp = cfn.describe_stacks(StackName=stack_name)
                status = resp["Stacks"][0]["StackStatus"]
            except botocore.exceptions.ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                msg = e.response.get("Error", {}).get("Message", "")
                if code == "ValidationError" and "does not exist" in msg:
                    # Stack genuinely gone — proceed with cleanup.
                    status = "GONE"
                else:
                    # Unexpected AWS error — log, keep marker, retry next sweep.
                    print(json.dumps({"phase_b_error": slug, "error": str(e)}))
                    pending.append(slug)
                    continue
            except Exception as exc:
                # Non-ClientError (network, throttle, etc.) — keep marker, retry.
                print(json.dumps({"phase_b_error": slug, "error": str(exc)}))
                pending.append(slug)
                continue
            if status in ("GONE", "DELETE_COMPLETE"):
                _del_prefix(f"_backends/{slug}/")
                _del_prefix(f"{slug}/")
                cf.create_invalidation(
                    DistributionId=DIST_ID,
                    InvalidationBatch={
                        "Paths": {"Quantity": 1, "Items": [f"/{slug}/*"]},
                        "CallerReference": f"reaper-{slug}-{now}",
                    },
                )
                reaped.append(slug)
            elif status == "DELETE_FAILED":
                # Retry the stack deletion — CloudFormation may succeed on the
                # next attempt (e.g. after a transient resource dependency clears).
                # R21 F1: re-verify stack identity before the retry (the
                # reaping marker is manifest-writable).
                if not _stack_site_tag_ok(stack_name, slug):
                    pending.append(slug)
                    continue
                try:
                    cfn.delete_stack(StackName=stack_name)
                except Exception as retry_exc:  # noqa: BLE001
                    print(json.dumps({"phase_b_retry_delete_failed": slug, "error": str(retry_exc)}))
                # Keep the reaping marker so next sweep checks again.
                pending.append(slug)
            else:
                # Still deleting (DELETE_IN_PROGRESS) — wait for next sweep.
                pending.append(slug)
            continue

        # ── Normal expiry check ──
        if not _expired(man, now):
            continue

        # ── Engine-arch dispatch ──
        # Engine-arch deployments use per-site buckets + own CloudFront distributions
        # (tagged kirocrew:site). The existing shared-bucket reap path doesn't touch them.
        if man.get("arch") == "engine":
            result = _reap_engine_arch(man, slug, now)
            if result == "reaped":
                reaped.append(slug)
            else:
                pending.append(slug)
            continue

        # ── Phase A: initiate reap ──
        # Detach CloudFront behavior first (quick, idempotent).
        try:
            _detach(slug)
        except Exception as exc:  # noqa: BLE001 - retry next run
            print(json.dumps({"retry_next_run": slug, "error": str(exc)}))
            continue

        # Check if this deployment has a backend stack.
        stack_name = f"kirocrew-deploy-app-{slug}"
        has_stack = False
        stack_tags = {}
        try:
            desc = cfn.describe_stacks(StackName=stack_name)
            has_stack = True
            stacks = desc.get("Stacks", [])
            if stacks:
                stack_tags = {
                    t.get("Key"): t.get("Value")
                    for t in stacks[0].get("Tags", [])
                }
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            msg = e.response.get("Error", {}).get("Message", "")
            if code == "ValidationError" and "does not exist" in msg:
                has_stack = False
            else:
                # Unexpected error — skip this slug, retry next sweep.
                print(json.dumps({"stack_check_error": slug, "error": str(e)}))
                continue
        except Exception as exc:
            # Non-ClientError (network, throttle) — skip, retry next sweep.
            print(json.dumps({"stack_check_error": slug, "error": str(exc)}))
            continue

        if has_stack:
            # R21 F1: verify the stack's kirocrew:site tag matches the slug
            # being reaped BEFORE deletion — same identity gate as the
            # engine-arch path (R19 F4). deploy-backend.sh tags stacks at
            # creation (kirocrew:site=<slug>); a stack without a matching tag
            # is not provably this deployment's — skip destructive ops.
            if stack_tags.get("kirocrew:site") != slug:
                print(json.dumps({
                    "app_reap_stack_tag_mismatch": slug,
                    "stack": stack_name,
                    "actual": stack_tags.get("kirocrew:site", "<missing>"),
                }))
                # R24: quarantine the untrusted manifest so the sweep stops
                # re-processing it; the mismatched stack is left untouched.
                _quarantine_manifest(slug)
                continue
            # Two-phase: initiate stack deletion and mark manifest as reaping.
            try:
                cfn.delete_stack(StackName=stack_name)
            except Exception as exc:  # noqa: BLE001 - retry next run
                print(json.dumps({"stack_delete_failed": slug, "error": str(exc)}))
                continue
            # Rewrite manifest with reaping marker instead of deleting S3.
            reaping_manifest = json.dumps({**man, "reaping": True})
            try:
                s3.put_object(
                    Bucket=BUCKET,
                    Key=f"{slug}/.kirocrew-deploy.json",
                    Body=reaping_manifest.encode(),
                    ContentType="application/json",
                )
            except Exception as exc:  # noqa: BLE001
                print(json.dumps({"manifest_rewrite_failed": slug, "error": str(exc)}))
            pending.append(slug)
        else:
            # Static-only: single-phase reap (no stack to wait for).
            _del_prefix(f"_backends/{slug}/")
            _del_prefix(f"{slug}/")
            cf.create_invalidation(
                DistributionId=DIST_ID,
                InvalidationBatch={
                    "Paths": {"Quantity": 1, "Items": [f"/{slug}/*"]},
                    "CallerReference": f"reaper-{slug}-{now}",
                },
            )
            reaped.append(slug)

    print(json.dumps({"reaped": reaped, "pending": pending, "now": now}))
    return {"reaped": reaped, "pending": pending}
