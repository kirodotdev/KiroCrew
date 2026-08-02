#!/usr/bin/env python3
"""Fail-closed helpers for release-feed emergency controls.

The public CDN is the read plane because the production publisher role is
intentionally write-only for ``feed/*``.  Every mutable pointer writer calls
``guard`` immediately before changing a feed.  Missing, oversized, malformed,
or frozen control state therefore blocks the pointer mutation while leaving
immutable release artifacts untouched.

This file is stdlib-only so GitHub-hosted runners can use it without installing
packages after assuming the production publishing role.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Sequence

CONTROL_SCHEMA_VERSION = 1
ALLOWED_CHANNELS = frozenset({"nightly", "insider", "stable"})
CONTROL_KEYS = frozenset(
    {
        "schema_version",
        "channel",
        "frozen",
        "minimum_supported_version",
        "withdrawn_versions",
        "generation",
        "updated_at",
        "reason",
    }
)
FEED_NAMES = frozenset(
    {"latest-mac.yml", "latest-linux.yml", "latest-mac.json", "latest-cli.json"}
)
MAX_CONTROL_BYTES = 32 * 1024
MAX_FEED_BYTES = 128 * 1024
MAX_REASON_CHARS = 500
MAX_WITHDRAWN_VERSIONS = 100
MAX_FEED_FILES = 10
MAX_VERSION_CHARS = 128
_HTTP_TIMEOUT_SECS = 15

_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BASE64_SHA512_RE = re.compile(r"^[A-Za-z0-9+/]{86}==$")
_OPAQUE_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.!+_-]*$")


class ReleaseControlError(ValueError):
    """A release-control or feed contract violation."""


def _require_channel(channel: Any) -> str:
    if not isinstance(channel, str) or channel not in ALLOWED_CHANNELS:
        raise ReleaseControlError(
            f"channel must be one of {sorted(ALLOWED_CHANNELS)}, got {channel!r}"
        )
    return channel


def _parse_semver(value: Any, *, field: str, allow_empty: bool = False) -> tuple:
    if allow_empty and value == "":
        return ()
    if not isinstance(value, str):
        raise ReleaseControlError(f"{field} must be a string")
    if len(value) > MAX_VERSION_CHARS:
        # Also bounds every numeric component BEFORE int(): Python 3.12 raises
        # a plain ValueError for >4300-digit conversions, which would escape
        # the fail-closed ReleaseControlError contract as a raw traceback.
        raise ReleaseControlError(
            f"{field} must contain at most {MAX_VERSION_CHARS} characters"
        )
    match = _SEMVER_RE.fullmatch(value)
    if not match:
        raise ReleaseControlError(f"{field} must be a canonical SemVer value, got {value!r}")
    prerelease = match.group(4)
    identifiers: tuple[tuple[int, Any], ...] = ()
    if prerelease is not None:
        parsed: list[tuple[int, Any]] = []
        for identifier in prerelease.split("."):
            if identifier.isdigit():
                if len(identifier) > 1 and identifier.startswith("0"):
                    raise ReleaseControlError(
                        f"{field} has a numeric prerelease identifier with a leading zero"
                    )
                parsed.append((0, int(identifier)))
            else:
                parsed.append((1, identifier))
        identifiers = tuple(parsed)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), identifiers)


def _semver_lt(left: str, right: str) -> bool:
    l_major, l_minor, l_patch, l_pre = _parse_semver(left, field="version")
    r_major, r_minor, r_patch, r_pre = _parse_semver(right, field="version")
    core_left = (l_major, l_minor, l_patch)
    core_right = (r_major, r_minor, r_patch)
    if core_left != core_right:
        return core_left < core_right
    if not l_pre:
        return False  # a release is newer than any prerelease of the same core
    if not r_pre:
        return True
    for left_id, right_id in zip(l_pre, r_pre):
        if left_id == right_id:
            continue
        # Numeric identifiers have lower precedence than non-numeric ones.
        if left_id[0] != right_id[0]:
            return left_id[0] < right_id[0]
        return left_id[1] < right_id[1]
    return len(l_pre) < len(r_pre)


def _timestamp(value: Any, *, field: str = "updated_at") -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReleaseControlError(
            f"{field} must be an RFC 3339 UTC timestamp ending in Z"
        )
    try:
        dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ReleaseControlError(f"{field} must be a valid RFC 3339 timestamp") from exc
    return value


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _reason(value: Any) -> str:
    if not isinstance(value, str):
        raise ReleaseControlError("reason must be a string")
    value = value.strip()
    if (
        not value
        or len(value) > MAX_REASON_CHARS
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ReleaseControlError(
            f"reason must contain 1-{MAX_REASON_CHARS} characters without controls"
        )
    return value


def _opaque_version(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_VERSION_CHARS
        or not _OPAQUE_VERSION_RE.fullmatch(value)
    ):
        raise ReleaseControlError(
            f"{field} must be a non-empty, bounded release version"
        )
    return value


def _feed_version(value: Any, feed_name: str, *, field: str) -> str:
    if feed_name == "latest-cli.json":
        return _opaque_version(value, field=field)
    _parse_semver(value, field=field)
    return value


def validate_control(raw: Any, *, expected_channel: str | None = None) -> dict[str, Any]:
    """Validate and normalize one release-control document."""
    if not isinstance(raw, dict):
        raise ReleaseControlError("release control must be a JSON object")
    keys = frozenset(raw)
    if keys != CONTROL_KEYS:
        missing = sorted(CONTROL_KEYS - keys)
        unknown = sorted(keys - CONTROL_KEYS)
        raise ReleaseControlError(
            f"release control keys do not match schema (missing={missing}, unknown={unknown})"
        )
    if raw["schema_version"] != CONTROL_SCHEMA_VERSION:
        raise ReleaseControlError(
            f"unsupported release-control schema_version {raw['schema_version']!r}"
        )
    channel = _require_channel(raw["channel"])
    if expected_channel is not None and channel != _require_channel(expected_channel):
        raise ReleaseControlError(
            f"control channel {channel!r} does not match requested channel {expected_channel!r}"
        )
    if type(raw["frozen"]) is not bool:  # bool only; integers are not accepted
        raise ReleaseControlError("frozen must be a JSON boolean")
    minimum = raw["minimum_supported_version"]
    _parse_semver(minimum, field="minimum_supported_version", allow_empty=True)
    withdrawn = raw["withdrawn_versions"]
    if not isinstance(withdrawn, list) or len(withdrawn) > MAX_WITHDRAWN_VERSIONS:
        raise ReleaseControlError(
            f"withdrawn_versions must be a JSON array with at most {MAX_WITHDRAWN_VERSIONS} entries"
        )
    normalized_withdrawn: list[str] = []
    for index, version in enumerate(withdrawn):
        _parse_semver(version, field=f"withdrawn_versions[{index}]")
        if version in normalized_withdrawn:
            raise ReleaseControlError(f"withdrawn_versions contains duplicate {version!r}")
        normalized_withdrawn.append(version)
    generation = raw["generation"]
    if type(generation) is not int or generation < 1:
        raise ReleaseControlError("generation must be an integer >= 1")
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "channel": channel,
        "frozen": raw["frozen"],
        "minimum_supported_version": minimum,
        "withdrawn_versions": normalized_withdrawn,
        "generation": generation,
        "updated_at": _timestamp(raw["updated_at"]),
        "reason": _reason(raw["reason"]),
    }


def bootstrap_control(channel: str, reason: str, *, now: str | None = None) -> dict[str, Any]:
    """Create the first control document, frozen by default."""
    return validate_control(
        {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "channel": _require_channel(channel),
            "frozen": True,
            "minimum_supported_version": "",
            "withdrawn_versions": [],
            "generation": 1,
            "updated_at": now or _now(),
            "reason": _reason(reason),
        }
    )


def mutate_control(
    control: dict[str, Any],
    operation: str,
    *,
    reason: str,
    version: str = "",
    now: str | None = None,
) -> dict[str, Any]:
    """Apply one audited emergency operation to a valid control document."""
    updated = validate_control(control)
    updated["generation"] += 1
    updated["updated_at"] = now or _now()
    updated["reason"] = _reason(reason)

    if operation == "freeze":
        updated["frozen"] = True
    elif operation == "unfreeze":
        updated["frozen"] = False
    elif operation == "restore":
        # A restore is deliberately sticky: a normal publisher cannot race the
        # recovered pointers until an operator separately unfreezes the channel.
        updated["frozen"] = True
    elif operation == "withdraw":
        _parse_semver(version, field="withdraw version")
        updated["frozen"] = True
        if version not in updated["withdrawn_versions"]:
            updated["withdrawn_versions"].append(version)
    elif operation == "set-minimum":
        _parse_semver(version, field="minimum supported version")
        updated["minimum_supported_version"] = version
    elif operation == "clear-minimum":
        updated["minimum_supported_version"] = ""
    else:
        raise ReleaseControlError(f"unsupported operation {operation!r}")
    return validate_control(updated)


def _decode_json(data: bytes, *, source: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if key in parsed:
                raise ReleaseControlError(
                    f"{source} repeats JSON object key {key!r}"
                )
            parsed[key] = value
        return parsed

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except ReleaseControlError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        # ValueError (not just JSONDecodeError): a >4300-digit number inside
        # otherwise-bounded JSON raises Python's int-conversion limit as a
        # plain ValueError, which must fail closed, not traceback.
        raise ReleaseControlError(f"{source} is not valid UTF-8 JSON") from exc


def _load_json(path: Path, *, limit: int) -> Any:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReleaseControlError(f"cannot read {path}: {exc}") from exc
    if len(data) > limit:
        raise ReleaseControlError(f"{path} exceeds the {limit}-byte limit")
    return _decode_json(data, source=str(path))


def _safe_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ReleaseControlError("control URL is invalid") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ReleaseControlError("URLs containing credentials are not permitted")
    try:
        parsed.port
    except ValueError as exc:
        raise ReleaseControlError("URL port is invalid") from exc
    if parsed.fragment or not parsed.hostname:
        raise ReleaseControlError("URL must have a host and no fragment")
    if parsed.scheme == "https":
        return url
    if parsed.scheme != "http":
        raise ReleaseControlError("URL must use HTTPS (HTTP is test-only on loopback)")
    host = parsed.hostname
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        raise ReleaseControlError("plain HTTP is permitted only for loopback tests")
    return url


def _url_origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(_safe_url(url))
    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname.lower(), parsed.port or default_port


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow redirects only within the original public origin."""

    def __init__(self, origin: tuple[str, str, int]) -> None:
        super().__init__()
        self._origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _url_origin(newurl) != self._origin:
            raise ReleaseControlError("cross-origin release-feed redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_bytes(url: str, *, limit: int) -> bytes:
    """Fetch a bounded public document, rejecting unsafe redirects and failures."""
    safe_url = _safe_url(url)
    origin = _url_origin(safe_url)
    request = urllib.request.Request(
        safe_url,
        headers={"Accept": "application/json, text/yaml", "Cache-Control": "no-cache"},
    )
    opener = urllib.request.build_opener(_SameOriginRedirectHandler(origin))
    try:
        with opener.open(request, timeout=_HTTP_TIMEOUT_SECS) as response:
            if _url_origin(response.geturl()) != origin:
                raise ReleaseControlError("cross-origin release-feed response refused")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) > limit:
                        raise ReleaseControlError(
                            f"response exceeds the {limit}-byte limit"
                        )
                except ValueError as exc:
                    raise ReleaseControlError("response Content-Length is invalid") from exc
            data = response.read(limit + 1)
    except ReleaseControlError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseControlError(f"cannot fetch release control/feed: {exc}") from exc
    if len(data) > limit:
        raise ReleaseControlError(f"response exceeds the {limit}-byte limit")
    return data


def load_control_url(url: str, *, expected_channel: str) -> dict[str, Any]:
    raw = _decode_json(
        fetch_bytes(url, limit=MAX_CONTROL_BYTES),
        source="release-control response",
    )
    return validate_control(raw, expected_channel=expected_channel)


def _validate_artifact_url(url: Any, artifact_base: str) -> str:
    if not isinstance(url, str):
        raise ReleaseControlError("feed artifact URL must be a string")
    safe_url = _safe_url(url)
    base = _safe_url(artifact_base.rstrip("/")) if artifact_base else ""
    if not base:
        raise ReleaseControlError("artifact_base must name the public byte host")
    base_parts = urllib.parse.urlsplit(base)
    url_parts = urllib.parse.urlsplit(safe_url)
    if base_parts.query or url_parts.query:
        raise ReleaseControlError("artifact URLs must not contain a query string")
    base_path = base_parts.path.rstrip("/")
    if (
        _url_origin(safe_url) != _url_origin(base)
        or not url_parts.path.startswith(base_path + "/")
    ):
        raise ReleaseControlError(
            f"feed artifact URL {url!r} is outside the configured byte host"
        )
    return url


def _validate_channel_urls(
    urls: list[str],
    *,
    feed_name: str,
    expected_channel: str,
    artifact_base: str,
    feed_version: str,
) -> None:
    channel = _require_channel(expected_channel)
    version = _feed_version(feed_version, feed_name, field="feed version")
    base_path = urllib.parse.urlsplit(
        _safe_url(artifact_base.rstrip("/"))
    ).path.rstrip("/")
    lane = "cli" if feed_name == "latest-cli.json" else "desktop"
    expected_prefix = f"{base_path}/{lane}/{channel}/{version}/"
    for url in urls:
        path = urllib.parse.urlsplit(url).path
        decoded = urllib.parse.unquote(path)
        # Electron and CDNs normalize dot-segments and (on Windows)
        # backslashes AFTER this validation, so a raw prefix match is not
        # enough: a URL like .../<version>/../<other>/x.dmg passes
        # startswith() yet downloads outside the declared channel/version.
        # Reject the traversal artifacts before trusting the prefix.
        if "\\" in decoded or any(
            segment in (".", "..") for segment in decoded.split("/")
        ):
            raise ReleaseControlError(
                f"{feed_name} artifact URL contains a path traversal artifact"
            )
        if not path.startswith(expected_prefix):
            raise ReleaseControlError(
                f"{feed_name} artifact URL does not belong to version "
                f"{version!r} in channel {channel!r}"
            )


def _metadata_fields(raw: Any) -> tuple[str, list[str]]:
    minimum = ""
    withdrawn: list[str] = []
    if isinstance(raw, dict):
        minimum = raw.get("minimumSupportedVersion", "")
        withdrawn_raw = raw.get("withdrawnVersions", [])
        if (
            not isinstance(withdrawn_raw, list)
            or len(withdrawn_raw) > MAX_WITHDRAWN_VERSIONS
        ):
            raise ReleaseControlError(
                f"withdrawnVersions must be an array with at most "
                f"{MAX_WITHDRAWN_VERSIONS} entries"
            )
        withdrawn = withdrawn_raw
    _parse_semver(minimum, field="minimumSupportedVersion", allow_empty=True)
    for index, version in enumerate(withdrawn):
        _parse_semver(version, field=f"withdrawnVersions[{index}]")
    if len(withdrawn) != len(set(withdrawn)):
        raise ReleaseControlError("withdrawnVersions contains duplicates")
    return minimum, withdrawn


def _yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_yaml_feed(text: str, *, artifact_base: str) -> dict[str, Any]:
    if "\t" in text:
        raise ReleaseControlError("feed YAML must not contain tabs")
    top: dict[str, str] = {}
    files: list[dict[str, Any]] = []
    in_files = False
    for line in text.splitlines():
        if line and not line.startswith(" "):
            match = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*):(?:\s*(.*))?", line)
            if match:
                key = match.group(1)
                if key in top:
                    raise ReleaseControlError(f"feed YAML repeats top-level key {key!r}")
                top[key] = match.group(2) or ""
                in_files = key == "files"
                continue
        if not in_files or not line:
            continue
        url_match = re.fullmatch(r"  - url:\s*(.+)", line)
        if url_match:
            if len(files) >= MAX_FEED_FILES:
                raise ReleaseControlError(
                    f"desktop feed files must have at most {MAX_FEED_FILES} entries"
                )
            files.append(
                {
                    "url": _validate_artifact_url(
                        _yaml_scalar(url_match.group(1)), artifact_base
                    )
                }
            )
            continue
        sha_match = re.fullmatch(r"    sha512:\s*(.+)", line)
        if sha_match and files:
            if "sha512" in files[-1]:
                raise ReleaseControlError("desktop feed file repeats sha512")
            files[-1]["sha512"] = _yaml_scalar(sha_match.group(1))
            continue
        size_match = re.fullmatch(r"    size:\s*([0-9]+)", line)
        if size_match and files:
            if "size" in files[-1]:
                raise ReleaseControlError("desktop feed file repeats size")
            size_text = size_match.group(1)
            if len(size_text) > 20:  # bounds int() below any uint64 (and the
                # 4300-digit ValueError limit), keeping parsing fail-closed
                raise ReleaseControlError("desktop feed file size is too large")
            files[-1]["size"] = int(size_text)
            continue
        raise ReleaseControlError(f"desktop feed has malformed files entry {line!r}")
    required = {"version", "files", "path", "sha512", "releaseDate"}
    if not required.issubset(top):
        raise ReleaseControlError(
            f"desktop feed lacks required top-level keys {sorted(required - set(top))}"
        )
    version = _yaml_scalar(top["version"])
    _parse_semver(version, field="feed version")
    if top["files"]:
        raise ReleaseControlError("desktop feed files must be a block sequence")
    if not files:
        raise ReleaseControlError("desktop feed files must not be empty")
    urls: list[str] = []
    digest_by_url: dict[str, str] = {}
    for entry in files:
        if set(entry) != {"url", "sha512", "size"}:
            raise ReleaseControlError("each desktop feed file needs url, sha512, and size")
        if not _BASE64_SHA512_RE.fullmatch(entry["sha512"]):
            raise ReleaseControlError(
                "desktop feed sha512 values must be base64 raw digests"
            )
        if entry["size"] < 1:
            raise ReleaseControlError("desktop feed file sizes must be positive")
        if entry["url"] in digest_by_url:
            raise ReleaseControlError("desktop feed files contain duplicate URLs")
        urls.append(entry["url"])
        digest_by_url[entry["url"]] = entry["sha512"]
    path_url = _validate_artifact_url(_yaml_scalar(top["path"]), artifact_base)
    if path_url not in urls:
        raise ReleaseControlError("desktop feed path must name one of files[].url")
    top_digest = _yaml_scalar(top["sha512"])
    if not _BASE64_SHA512_RE.fullmatch(top_digest):
        raise ReleaseControlError(
            "desktop feed top-level sha512 must be a base64 raw digest"
        )
    if digest_by_url[path_url] != top_digest:
        raise ReleaseControlError(
            "desktop feed path digest does not match files[].sha512"
        )
    _timestamp(_yaml_scalar(top["releaseDate"]), field="releaseDate")
    minimum = _yaml_scalar(top.get("minimumSupportedVersion", ""))
    withdrawn_text = top.get("withdrawnVersions", "[]")
    try:
        withdrawn = json.loads(withdrawn_text)
    except ValueError as exc:
        # Includes JSONDecodeError and the >4300-digit int-conversion limit.
        raise ReleaseControlError(
            "withdrawnVersions must use a JSON-compatible flow array"
        ) from exc
    _metadata_fields(
        {"minimumSupportedVersion": minimum, "withdrawnVersions": withdrawn}
    )
    return {
        "version": version,
        "urls": urls,
        "minimumSupportedVersion": minimum,
        "withdrawnVersions": withdrawn,
    }


def feed_info(
    path: Path,
    feed_name: str,
    *,
    artifact_base: str,
    expected_channel: str,
) -> dict[str, Any]:
    """Validate a known feed and return its version, URLs, and controls."""
    if feed_name not in FEED_NAMES:
        raise ReleaseControlError(f"unknown feed name {feed_name!r}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReleaseControlError(f"cannot read {path}: {exc}") from exc
    if len(data) > MAX_FEED_BYTES:
        raise ReleaseControlError(f"feed exceeds the {MAX_FEED_BYTES}-byte limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseControlError("feed is not UTF-8") from exc
    if feed_name.endswith(".yml"):
        info = _parse_yaml_feed(text, artifact_base=artifact_base)
        _validate_channel_urls(
            info["urls"],
            feed_name=feed_name,
            expected_channel=expected_channel,
            artifact_base=artifact_base,
            feed_version=info["version"],
        )
        return info
    raw = _decode_json(data, source="feed")
    if not isinstance(raw, dict):
        raise ReleaseControlError("feed JSON must be an object")
    minimum, withdrawn = _metadata_fields(raw)
    version = raw.get("version")
    if feed_name == "latest-mac.json":
        _parse_semver(version, field="feed version")
        required = {"version", "url", "dmg", "name", "pub_date"}
        if not required.issubset(raw):
            raise ReleaseControlError(
                f"legacy mac feed lacks keys {sorted(required - set(raw))}"
            )
        urls = [
            _validate_artifact_url(raw["url"], artifact_base),
            _validate_artifact_url(raw["dmg"], artifact_base),
        ]
    else:
        _feed_version(version, feed_name, field="CLI feed version")
        required = {
            "channel",
            "version",
            "wheel_url",
            "sha256",
            "python_requires",
            "pub_date",
        }
        if not required.issubset(raw):
            raise ReleaseControlError(
                f"CLI feed lacks keys {sorted(required - set(raw))}"
            )
        channel = _require_channel(raw["channel"])
        if channel != _require_channel(expected_channel):
            raise ReleaseControlError(
                f"CLI feed channel {channel!r} does not match {expected_channel!r}"
            )
        if not isinstance(raw["sha256"], str) or not _SHA256_RE.fullmatch(raw["sha256"]):
            raise ReleaseControlError(
                "CLI feed sha256 must be 64 lowercase hex characters"
            )
        urls = [_validate_artifact_url(raw["wheel_url"], artifact_base)]
    _validate_channel_urls(
        urls,
        feed_name=feed_name,
        expected_channel=expected_channel,
        artifact_base=artifact_base,
        feed_version=version,
    )
    return {
        "version": version,
        "urls": urls,
        "minimumSupportedVersion": minimum,
        "withdrawnVersions": withdrawn,
    }


def rewrite_feed(
    source: Path,
    destination: Path,
    feed_name: str,
    control: dict[str, Any],
    *,
    artifact_base: str = "",
) -> dict[str, Any]:
    """Apply current control metadata to an existing valid feed."""
    normalized = validate_control(control)
    info = feed_info(
        source,
        feed_name,
        artifact_base=artifact_base,
        expected_channel=normalized["channel"],
    )
    if info["version"] in normalized["withdrawn_versions"]:
        raise ReleaseControlError(
            f"refusing to publish withdrawn version {info['version']!r} as a live feed"
        )
    minimum = normalized["minimum_supported_version"]
    if minimum:
        try:
            below_floor = _semver_lt(info["version"], minimum)
        except ReleaseControlError as exc:
            # A version that cannot be compared to the floor (e.g. a PEP 440
            # CLI dev stamp like 1.1.0.dev1) must FAIL CLOSED: treating it as
            # above-floor would let `rewrite` republish exactly the build an
            # emergency floor was set to retire. The operator can clear the
            # floor or publish a SemVer-comparable version instead.
            raise ReleaseControlError(
                f"cannot compare {info['version']!r} to minimum {minimum!r}; "
                "refusing to publish a version the floor cannot vet"
            ) from exc
        if below_floor:
            raise ReleaseControlError(
                f"refusing to publish {info['version']!r} below minimum {minimum!r}"
            )

    if feed_name.endswith(".json"):
        raw = _load_json(source, limit=MAX_FEED_BYTES)
        raw["minimumSupportedVersion"] = minimum
        raw["withdrawnVersions"] = normalized["withdrawn_versions"]
        body = json.dumps(raw, indent=2, sort_keys=False) + "\n"
    else:
        text = source.read_text(encoding="utf-8")
        lines = [
            line
            for line in text.splitlines()
            if not line.startswith("minimumSupportedVersion:")
            and not line.startswith("withdrawnVersions:")
        ]
        lines.extend(
            [
                f"minimumSupportedVersion: {json.dumps(minimum)}",
                "withdrawnVersions: "
                + json.dumps(normalized["withdrawn_versions"], separators=(",", ":")),
            ]
        )
        body = "\n".join(lines) + "\n"
    destination.write_text(body, encoding="utf-8", newline="\n")
    return feed_info(
        destination,
        feed_name,
        artifact_base=artifact_base,
        expected_channel=normalized["channel"],
    )


def snapshot_feed(
    url: str,
    destination: Path,
    feed_name: str,
    new_version: str,
    *,
    artifact_base: str,
    expected_channel: str,
) -> bool:
    """Capture a valid live feed unless this is an idempotent same-version retry."""
    _feed_version(new_version, feed_name, field="new version")
    data = fetch_bytes(url, limit=MAX_FEED_BYTES)
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as handle:
            handle.write(data)
            candidate = Path(handle.name)
        info = feed_info(
            candidate,
            feed_name,
            artifact_base=artifact_base,
            expected_channel=expected_channel,
        )
        if info["version"] == new_version:
            return False
        candidate.replace(destination)
        candidate = None
        return True
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)


def _dump_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")


def _append_env(path: Path, control: dict[str, Any]) -> None:
    normalized = validate_control(control)
    values = {
        "MINIMUM_SUPPORTED_VERSION": normalized["minimum_supported_version"],
        "WITHDRAWN_VERSIONS": json.dumps(
            normalized["withdrawn_versions"], separators=(",", ":")
        ),
        "RELEASE_CONTROL_GENERATION": str(normalized["generation"]),
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ReleaseControlError(f"unsafe newline in {key}")
            handle.write(f"{key}={value}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--channel", required=True)
    bootstrap.add_argument("--reason", required=True)
    bootstrap.add_argument("--output", type=Path, required=True)

    mutate = sub.add_parser("mutate")
    mutate.add_argument("--input", type=Path, required=True)
    mutate.add_argument("--channel", required=True)
    mutate.add_argument(
        "--operation",
        choices=("freeze", "unfreeze", "restore", "withdraw", "set-minimum", "clear-minimum"),
        required=True,
    )
    mutate.add_argument("--version", default="")
    mutate.add_argument("--reason", required=True)
    mutate.add_argument("--output", type=Path, required=True)

    fetch = sub.add_parser("fetch")
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--channel", required=True)
    fetch.add_argument("--output", type=Path, required=True)

    guard = sub.add_parser("guard")
    guard.add_argument("--url", required=True)
    guard.add_argument("--channel", required=True)
    guard.add_argument("--candidate-version", required=True)
    guard.add_argument("--save", type=Path, required=True)
    guard.add_argument("--github-env", type=Path, required=True)

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--url", required=True)
    snapshot.add_argument("--channel", required=True)
    snapshot.add_argument("--feed-name", choices=sorted(FEED_NAMES), required=True)
    snapshot.add_argument("--new-version", required=True)
    snapshot.add_argument("--artifact-base", required=True)
    snapshot.add_argument("--output", type=Path, required=True)

    rewrite = sub.add_parser("rewrite")
    rewrite.add_argument("--input", type=Path, required=True)
    rewrite.add_argument("--output", type=Path, required=True)
    rewrite.add_argument("--feed-name", choices=sorted(FEED_NAMES), required=True)
    rewrite.add_argument("--control", type=Path, required=True)
    rewrite.add_argument("--artifact-base", required=True)

    info = sub.add_parser("feed-info")
    info.add_argument("--input", type=Path, required=True)
    info.add_argument("--channel", required=True)
    info.add_argument("--feed-name", choices=sorted(FEED_NAMES), required=True)
    info.add_argument("--artifact-base", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "bootstrap":
            _dump_json(args.output, bootstrap_control(args.channel, args.reason))
        elif args.command == "mutate":
            control = validate_control(
                _load_json(args.input, limit=MAX_CONTROL_BYTES),
                expected_channel=args.channel,
            )
            _dump_json(
                args.output,
                mutate_control(
                    control,
                    args.operation,
                    reason=args.reason,
                    version=args.version,
                ),
            )
        elif args.command == "fetch":
            control = load_control_url(args.url, expected_channel=args.channel)
            _dump_json(args.output, control)
        elif args.command == "guard":
            control = load_control_url(args.url, expected_channel=args.channel)
            _dump_json(args.save, control)
            if control["frozen"]:
                raise ReleaseControlError(
                    f"release feed for {args.channel} is frozen at generation "
                    f"{control['generation']}: {control['reason']}"
                )
            candidate = args.candidate_version
            _parse_semver(candidate, field="candidate version")
            if candidate in control["withdrawn_versions"]:
                raise ReleaseControlError(
                    f"candidate version {candidate!r} has been withdrawn"
                )
            minimum = control["minimum_supported_version"]
            if minimum and _semver_lt(candidate, minimum):
                raise ReleaseControlError(
                    f"candidate version {candidate!r} is below minimum {minimum!r}"
                )
            _append_env(args.github_env, control)
        elif args.command == "snapshot":
            snapshot_feed(
                args.url,
                args.output,
                args.feed_name,
                args.new_version,
                artifact_base=args.artifact_base,
                expected_channel=args.channel,
            )
        elif args.command == "rewrite":
            control = validate_control(_load_json(args.control, limit=MAX_CONTROL_BYTES))
            info = rewrite_feed(
                args.input,
                args.output,
                args.feed_name,
                control,
                artifact_base=args.artifact_base,
            )
            print(json.dumps(info, separators=(",", ":")))
        elif args.command == "feed-info":
            print(
                json.dumps(
                    feed_info(
                        args.input,
                        args.feed_name,
                        artifact_base=args.artifact_base,
                        expected_channel=args.channel,
                    ),
                    separators=(",", ":"),
                )
            )
        return 0
    except ReleaseControlError as exc:
        print(f"release-feed-control: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
