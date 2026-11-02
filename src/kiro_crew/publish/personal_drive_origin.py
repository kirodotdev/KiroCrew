"""Checks that the personal drive's distribution serves published objects from the drive.

Split out of :mod:`kiro_crew.publish.personal_drive` to keep that module under the
repository's line cap. These functions read a distribution configuration that the
caller already fetched and make no AWS call. ``personal_drive`` re-exports every name,
so ``personal_drive.<name>`` keeps working for callers and tests.
"""

from __future__ import annotations

from typing import Any

#: The selected CloudFront origin may serve bytes other than the uploaded object.
_ORIGIN_MISMATCH_NOTICE_CODE = "origin_mismatch"


def _path_pattern_matches(pattern: str, object_key: str) -> bool:
    """Whether a CloudFront cache-behavior pattern selects ``object_key``."""
    pattern = pattern.lstrip("/")
    object_key = object_key.lstrip("/")
    pattern_pos = 0
    key_pos = 0
    star_pos = -1
    retry_key_pos = -1

    while key_pos < len(object_key):
        if (
            pattern_pos < len(pattern)
            and pattern[pattern_pos] != "*"
            and pattern[pattern_pos] in ("?", object_key[key_pos])
        ):
            pattern_pos += 1
            key_pos += 1
        elif pattern_pos < len(pattern) and pattern[pattern_pos] == "*":
            star_pos = pattern_pos
            retry_key_pos = key_pos
            pattern_pos += 1
        elif star_pos >= 0:
            retry_key_pos += 1
            key_pos = retry_key_pos
            pattern_pos = star_pos + 1
        else:
            return False

    while pattern_pos < len(pattern) and pattern[pattern_pos] == "*":
        pattern_pos += 1
    return pattern_pos == len(pattern)


def _origin_names_bucket(domain: str, bucket: str) -> bool:
    """Whether an S3 REST origin domain names ``bucket`` in a supported form."""
    host = domain.lower()
    if ".s3-website-" in host or ".s3-website." in host:
        return False
    prefix = f"{bucket}.s3"
    suffix = ".amazonaws.com"
    if not host.startswith(prefix) or not host.endswith(suffix):
        return False
    middle = host[len(prefix) : -len(suffix)]
    return not middle or (middle[0] in ".-" and len(middle) > 1 and "." not in middle[1:])


def _origin_warning_for(
    config: dict[str, Any], dist_id: str, bucket: str, object_key: str, public_prefix: str
) -> str:
    """Describe a behavior selected for ``object_key`` that may serve different bytes.

    ``config`` is the distribution's ``DistributionConfig``. Returns "" when the selected
    behavior reads ``bucket`` directly with no origin path.
    """
    behavior = config.get("DefaultCacheBehavior") or {}
    for candidate in (config.get("CacheBehaviors") or {}).get("Items") or []:
        if _path_pattern_matches(str(candidate.get("PathPattern", "")), object_key):
            behavior = candidate
            break

    target_id = str(behavior.get("TargetOriginId", ""))
    origins = (config.get("Origins") or {}).get("Items") or []
    selected: dict[str, Any] = next(
        (origin for origin in origins if str(origin.get("Id", "")) == target_id), {}
    )
    domain = str(selected.get("DomainName", ""))
    if _origin_names_bucket(domain, bucket) and selected.get("OriginPath", "") == "":
        return ""
    return (
        f"The artifact was published to the drive bucket ({bucket}), but this account's "
        f"personal drive distribution ({dist_id}) does not serve published objects "
        "directly from that bucket with no origin path. The link may show something "
        f"other than what was uploaded. Point the behavior for {public_prefix} objects "
        "at that bucket with no origin path in the AWS console."
    )
