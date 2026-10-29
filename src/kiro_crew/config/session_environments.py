"""Validation for the persisted ``session_environments`` config section.

Shared by the loader and the dashboard write gate so they cannot drift:
``validate_*`` raises naming the field; ``load_*`` twins drop only the invalid
parts. No config-package imports.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("kiro_crew.config.loader")

#: Most profiles one config may hold.
MAX_PROFILES = 20
#: Profile id grammar: ASCII alphanumeric first, then alphanumerics, ``_`` or ``-``.
PROFILE_ID_MAX_LEN = 64
PROFILE_NAME_MAX_LEN = 128
PROFILE_TEMPLATE_MAX_LEN = 255
PROFILE_WORKDIR_MAX_LEN = 1024
CODER_URL_MAX_LEN = 2048

PROFILE_KEYS: tuple[str, ...] = ("id", "name", "template", "workdir")
#: Optional profile keys, with the value an omitted one takes.
PROFILE_OPTIONAL_KEYS: dict[str, bool] = {"compute_survives_stop": True}
SECTION_KEYS: tuple[str, ...] = ("coder_url", "profiles")
#: Optional section keys; an older dashboard's omission keeps the saved value.
SECTION_OPTIONAL_KEYS: tuple[str, ...] = ("default_profile", "stop_on_shutdown")
#: Days an archived session's stopped workspace is kept before it is deleted. Fixed,
#: not a setting: a stopped workspace still holds its disk.
ARCHIVED_RETENTION_DAYS = 7

_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,%d}" % (PROFILE_ID_MAX_LEN - 1))
# Control characters are never meaningful here, and OS calls refuse a NUL path.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class SessionEnvironmentsError(ValueError):
    """A ``session_environments`` value failed validation; the message names the field."""


def validate_coder_url(value: object) -> str:
    """The normalized Coder URL (not contacted): ``""`` (not configured), or http(s)
    with a hostname and no userinfo, query or fragment."""
    if not isinstance(value, str):
        raise SessionEnvironmentsError("coder_url must be a string")
    url = value.strip()
    if not url:
        return ""
    if len(url) > CODER_URL_MAX_LEN:
        raise SessionEnvironmentsError(f"coder_url must be at most {CODER_URL_MAX_LEN} characters")
    if _CONTROL_RE.search(url) or " " in url:
        raise SessionEnvironmentsError("coder_url must not contain spaces or control characters")
    try:
        parts = urlsplit(url)
        # ``.port`` raises ValueError on a non-numeric or out-of-range port.
        _ = parts.port
    except ValueError:
        raise SessionEnvironmentsError("coder_url must be a valid URL") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise SessionEnvironmentsError("coder_url must be an http or https URL")
    if not parts.hostname:
        raise SessionEnvironmentsError("coder_url must include a hostname")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise SessionEnvironmentsError("coder_url must not include a username or password")
    # Raw characters too: ``https://h?`` parses to an empty query.
    if "?" in url or parts.query:
        raise SessionEnvironmentsError("coder_url must not include a query string")
    if "#" in url or parts.fragment:
        raise SessionEnvironmentsError("coder_url must not include a fragment")
    return url


def _bounded_text(value: object, field: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise SessionEnvironmentsError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise SessionEnvironmentsError(f"{field} must not be empty")
    if len(text) > max_len:
        raise SessionEnvironmentsError(f"{field} must be at most {max_len} characters")
    if _CONTROL_RE.search(text):
        raise SessionEnvironmentsError(f"{field} must not contain control characters")
    return text


def validate_stop_on_shutdown(value: object) -> bool:
    """Whether the gateway stops its remote environments when it shuts down."""
    if not isinstance(value, bool):
        raise SessionEnvironmentsError("stop_on_shutdown must be true or false")
    return value


def load_stop_on_shutdown(raw: object) -> bool:
    """Lenient load: the saved choice, or True (stop them) when absent or invalid."""
    if not isinstance(raw, dict) or "stop_on_shutdown" not in raw:
        return True
    try:
        return validate_stop_on_shutdown(raw["stop_on_shutdown"])
    except SessionEnvironmentsError as exc:
        logger.warning("session_environments: %s; stopping them on shutdown", exc)
        return True


def validate_default_profile(value: object) -> str:
    """The profile id new dashboard sessions start on ("" is this machine); shape only."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _PROFILE_ID_RE.fullmatch(value):
        raise SessionEnvironmentsError("default_profile must be a profile id or empty")
    return value


def load_default_profile(raw: object) -> str:
    """Lenient load: the saved id, or "" (this machine) when absent or invalid."""
    if not isinstance(raw, dict) or "default_profile" not in raw:
        return ""
    try:
        return validate_default_profile(raw["default_profile"])
    except SessionEnvironmentsError as exc:
        logger.warning("session_environments: %s; new sessions run on this machine", exc)
        return ""


def validate_profile(value: object, index: int = 0, *, where: str = "") -> dict[str, Any]:
    """One normalized profile; an unknown key (a credential, say) is refused, not dropped."""
    where = where or f"profiles[{index}]"
    if not isinstance(value, dict):
        raise SessionEnvironmentsError(f"{where} must be an object")
    unknown = sorted(
        str(k) for k in value if k not in PROFILE_KEYS and k not in PROFILE_OPTIONAL_KEYS
    )
    if unknown:
        raise SessionEnvironmentsError(f"{where} has unknown key(s): {unknown}")
    missing = [k for k in PROFILE_KEYS if k not in value]
    if missing:
        raise SessionEnvironmentsError(f"{where} is missing key(s): {missing}")

    profile_id = value["id"]
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise SessionEnvironmentsError(
            f"{where}.id must start with an ASCII letter or digit and contain only "
            f"letters, digits, '_' or '-' (at most {PROFILE_ID_MAX_LEN} characters)"
        )
    name = _bounded_text(value["name"], f"{where}.name", PROFILE_NAME_MAX_LEN)
    template = _bounded_text(value["template"], f"{where}.template", PROFILE_TEMPLATE_MAX_LEN)
    workdir = _bounded_text(value["workdir"], f"{where}.workdir", PROFILE_WORKDIR_MAX_LEN)
    if not workdir.startswith("/"):
        raise SessionEnvironmentsError(f"{where}.workdir must be an absolute POSIX path")
    survives = value.get("compute_survives_stop", PROFILE_OPTIONAL_KEYS["compute_survives_stop"])
    if not isinstance(survives, bool):
        raise SessionEnvironmentsError(f"{where}.compute_survives_stop must be true or false")
    return {
        "id": profile_id,
        "name": name,
        "template": template,
        "workdir": workdir,
        "compute_survives_stop": survives,
    }


def validate_profiles(value: object) -> list[dict[str, Any]]:
    """Return the normalized profile list, or raise :class:`SessionEnvironmentsError`."""
    if not isinstance(value, list):
        raise SessionEnvironmentsError("profiles must be a list")
    if len(value) > MAX_PROFILES:
        raise SessionEnvironmentsError(f"profiles must hold at most {MAX_PROFILES} entries")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        profile = validate_profile(entry, index)
        if profile["id"] in seen:
            raise SessionEnvironmentsError(f"profiles[{index}].id {profile['id']!r} is a duplicate")
        seen.add(profile["id"])
        out.append(profile)
    return out


def validate_session_environments(value: object) -> dict[str, Any]:
    """The normalized whole section; both Coder keys required, other keys refused."""
    if not isinstance(value, dict):
        raise SessionEnvironmentsError("session_environments must be an object")
    unknown = sorted(str(k) for k in value if k not in (*SECTION_KEYS, *SECTION_OPTIONAL_KEYS))
    if unknown:
        raise SessionEnvironmentsError(f"session_environments has unknown key(s): {unknown}")
    missing = [k for k in SECTION_KEYS if k not in value]
    if missing:
        raise SessionEnvironmentsError(f"session_environments is missing key(s): {missing}")
    result: dict[str, Any] = {
        "coder_url": validate_coder_url(value["coder_url"]),
        "profiles": validate_profiles(value["profiles"]),
    }
    saved_ids = {profile["id"] for profile in result["profiles"]}
    if "default_profile" in value:
        default = validate_default_profile(value["default_profile"])
        # Only an id this save keeps: a removed profile cannot stay the default.
        if default and default not in saved_ids:
            raise SessionEnvironmentsError(
                f"default_profile {default!r} is not one of the saved profiles"
            )
        result["default_profile"] = default
    if "stop_on_shutdown" in value:
        result["stop_on_shutdown"] = validate_stop_on_shutdown(value["stop_on_shutdown"])
    return result


def load_session_environments(raw: object) -> tuple[str, list[dict[str, Any]]]:
    """Lenient ``(coder_url, profiles)``; each invalid part falls back with a warning."""
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("session_environments must be an object; using defaults")
        return "", []

    coder_url = ""
    if "coder_url" in raw:
        try:
            coder_url = validate_coder_url(raw["coder_url"])
        except SessionEnvironmentsError as exc:
            logger.warning("session_environments: %s; ignoring it", exc)

    profiles: list[dict[str, Any]] = []
    raw_profiles = raw.get("profiles", [])
    if not isinstance(raw_profiles, list):
        logger.warning("session_environments: profiles must be a list; ignoring it")
        raw_profiles = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_profiles):
        if len(profiles) >= MAX_PROFILES:
            logger.warning(
                "session_environments: more than %d profiles; ignoring the rest", MAX_PROFILES
            )
            break
        try:
            profile = validate_profile(entry, index)
        except SessionEnvironmentsError as exc:
            logger.warning("session_environments: %s; dropping that profile", exc)
            continue
        if profile["id"] in seen:
            logger.warning(
                "session_environments: profiles[%d].id %r is a duplicate; dropping it",
                index,
                profile["id"],
            )
            continue
        seen.add(profile["id"])
        profiles.append(profile)
    return coder_url, profiles
