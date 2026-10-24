"""Validation for the persisted ``session_environments`` config section.

Shared by the loader and the dashboard write gate so they cannot drift:
Coder, Kiro Cloud Sessions, Sprites and boxd profiles, never credentials (a
Sprites profile names the vault secret holding its token, never the token).
``validate_*`` raises naming the field; ``load_*`` twins drop only the invalid
parts. No config-package imports.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable
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
SECTION_OPTIONAL_KEYS: tuple[str, ...] = (
    "default_profile",
    "kiro_cloud_profiles",
    "sprites_profiles",
    "boxd_profiles",
    "stop_on_shutdown",
)
#: Days an archived session's stopped workspace is kept before it is deleted. Fixed,
#: not a setting: a stopped workspace still holds its disk.
ARCHIVED_RETENTION_DAYS = 7

_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,%d}" % (PROFILE_ID_MAX_LEN - 1))
# Control characters are never meaningful here, and OS calls refuse a NUL path.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class SessionEnvironmentsError(ValueError):
    """A ``session_environments`` value failed validation; the message names the field."""


class ProfileIdCollision(SessionEnvironmentsError):
    """One profile id names profiles of more than one provider."""


#: Every session-environment provider, once: its id (the choice row's tag and
#: the picker's ``provider``), its profile list in the section, and its display
#: name. Profile ids are unique across all of them (:func:`_refuse_shared_ids`).
PROVIDERS: tuple[tuple[str, str, str], ...] = (
    ("coder", "profiles", "Coder"),
    ("kiro_cloud", "kiro_cloud_profiles", "Kiro Cloud"),
    ("sprites", "sprites_profiles", "Sprites"),
    ("boxd", "boxd_profiles", "boxd"),
)
PROVIDER_IDS: tuple[str, ...] = tuple(p[0] for p in PROVIDERS)
#: Each VM provider's session registry, relative to the data home. Kept here, not
#: on its class, so the boot check that keeps the sweeper alive reads every
#: provider's file without importing a provider (or Coder).
VM_REGISTRY_FILES: dict[str, str] = {
    "sprites": "sprites/session-sprites.json",
    "boxd": "boxd/session-machines.json",
}
#: The providers that run on a VM Kiro Crew owns, with a session registry.
VM_PROVIDERS: tuple[tuple[str, str, str], ...] = tuple(
    p for p in PROVIDERS if p[0] in VM_REGISTRY_FILES
)
#: Each provider's profile list in the section, with the name a refusal uses.
PROFILE_LISTS: tuple[tuple[str, str], ...] = tuple((key, label) for _id, key, label in PROVIDERS)


def _refuse_shared_ids(lists: dict[str, Any]) -> set[str]:
    """Every profile id in ``lists``; raises :class:`ProfileIdCollision` on a shared one.

    A session's choice records a profile id, and that id decides the provider,
    so one id may not name profiles of two providers at once.
    """
    saved: dict[str, str] = {}
    for key, provider in PROFILE_LISTS:
        for profile in lists.get(key) or []:
            other = saved.setdefault(profile["id"], provider)
            if other != provider:
                raise ProfileIdCollision(
                    f"profile id {profile['id']!r} is used by both a {other} and a "
                    f"{provider} profile"
                )
    return set(saved)


def check_profile_ids_unique(raw: object) -> None:
    """Refuse a stored section in which one id names profiles of two providers.

    The config writer runs this on the final merged section, under the config
    write lock, for every ``session_environments`` write: a leaf write of one
    list is checked against the other lists on disk, and a whole-section save
    that omits a list is checked against the list it preserves. Each list is
    read the way the loader reads it, so an entry the loader drops cannot block
    a save. Raises :class:`ProfileIdCollision`.
    """
    if not isinstance(raw, dict):
        return
    _coder_url, coder = load_session_environments(raw)
    _refuse_shared_ids(
        {
            "profiles": coder,
            "kiro_cloud_profiles": load_kiro_cloud_profiles(raw),
            "sprites_profiles": load_sprites_profiles(raw),
            "boxd_profiles": load_boxd_profiles(raw),
        }
    )


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
    return _validate_list(value, "profiles", validate_profile)


def validate_session_environments(value: object) -> dict[str, Any]:
    """The normalized whole section; both Coder keys required, other keys refused.

    A profile id names one profile across every provider's list, because a
    session's choice resolves by id.
    """
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
    if "kiro_cloud_profiles" in value:
        result["kiro_cloud_profiles"] = validate_kiro_cloud_profiles(value["kiro_cloud_profiles"])
    if "sprites_profiles" in value:
        result["sprites_profiles"] = validate_sprites_profiles(value["sprites_profiles"])
    if "boxd_profiles" in value:
        result["boxd_profiles"] = validate_boxd_profiles(value["boxd_profiles"])
    saved_ids = _refuse_shared_ids(result)
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

    return coder_url, _load_list(raw, "profiles", validate_profile)


SPRITES_PROFILE_KEYS: tuple[str, ...] = ("id", "name", "token_secret", "workdir")
#: Default vault secret name for a Sprites API token.
SPRITES_TOKEN_SECRET_DEFAULT = "SPRITES_TOKEN"
#: A vault secret name: what Settings > Secrets accepts as a name.
SECRET_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

BOXD_PROFILE_KEYS: tuple[str, ...] = ("id", "name", "snapshot", "vcpu", "workdir")
#: 0 is the org's default size; the others are boxd's own vCPU sizes.
BOXD_VCPU_CHOICES: tuple[int, ...] = (0, 1, 2, 4, 8)
_SNAPSHOT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _exact_keys(value: object, where: str, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionEnvironmentsError(f"{where} must be an object")
    unknown = sorted(str(k) for k in value if k not in keys)
    if unknown:
        raise SessionEnvironmentsError(f"{where} has unknown key(s): {unknown}")
    missing = [k for k in keys if k not in value]
    if missing:
        raise SessionEnvironmentsError(f"{where} is missing key(s): {missing}")
    return value


def _common_fields(value: dict[str, Any], index: int, where: str, template: str) -> dict[str, Any]:
    """``id``, ``name`` and ``workdir``, checked by the Coder profile's own rules.

    A blank ``workdir`` is kept blank: the session then runs in the machine's own
    login home, read from the machine when a session starts.
    """
    workdir = value["workdir"]
    blank = isinstance(workdir, str) and not workdir.strip()
    common = validate_profile(
        {
            "id": value["id"],
            "name": value["name"],
            "template": template,
            "workdir": "/" if blank else workdir,
        },
        index,
        where=where,
    )
    if blank:
        common["workdir"] = ""
    return {key: common[key] for key in ("id", "name", "workdir")}


def validate_sprites_profile(value: object, index: int = 0) -> dict[str, Any]:
    """Validate a saved Fly.io Sprites profile. No API call, no token read.

    The token itself is never part of the profile: ``token_secret`` names the
    gateway vault entry (Settings > Secrets) that holds it.
    """
    where = f"sprites_profiles[{index}]"
    raw = _exact_keys(value, where, SPRITES_PROFILE_KEYS)
    result = _common_fields(raw, index, where, "sprites")
    token_secret = raw["token_secret"]
    if not isinstance(token_secret, str) or not SECRET_NAME_RE.fullmatch(token_secret):
        raise SessionEnvironmentsError(
            f"{where}.token_secret must be a secret name (letters, digits and '_')"
        )
    result["token_secret"] = token_secret
    return result


def validate_boxd_profile(value: object, index: int = 0) -> dict[str, Any]:
    """Validate a saved boxd profile. No CLI call, no credential.

    The gateway machine's own ``boxd auth login`` authenticates, so a profile
    names only what to create: an optional snapshot to start from, a size, and
    the working directory.
    """
    where = f"boxd_profiles[{index}]"
    raw = _exact_keys(value, where, BOXD_PROFILE_KEYS)
    result = _common_fields(raw, index, where, "boxd")
    snapshot = raw["snapshot"]
    if not isinstance(snapshot, str) or (snapshot and not _SNAPSHOT_RE.fullmatch(snapshot)):
        raise SessionEnvironmentsError(f"{where}.snapshot must be a snapshot name or id, or empty")
    vcpu = raw["vcpu"]
    if isinstance(vcpu, bool) or vcpu not in BOXD_VCPU_CHOICES:
        raise SessionEnvironmentsError(f"{where}.vcpu must be one of {list(BOXD_VCPU_CHOICES)}")
    if snapshot and vcpu:
        # boxd keeps a snapshot's own size; naming one there is an error.
        raise SessionEnvironmentsError(f"{where}: a snapshot keeps its own size; set vcpu to 0")
    result["snapshot"] = snapshot
    result["vcpu"] = vcpu
    return result


def _validate_list(
    value: object, key: str, validate: Callable[[object, int], dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SessionEnvironmentsError(f"{key} must be a list")
    if len(value) > MAX_PROFILES:
        raise SessionEnvironmentsError(f"{key} must hold at most {MAX_PROFILES} entries")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        profile = validate(entry, index)
        if profile["id"] in seen:
            raise SessionEnvironmentsError(f"{key}[{index}].id {profile['id']!r} is a duplicate")
        seen.add(profile["id"])
        out.append(profile)
    return out


def _load_list(
    raw: object, key: str, validate: Callable[[object, int], dict[str, Any]]
) -> list[dict[str, Any]]:
    """Lenient twin of :func:`_validate_list`: keep each valid entry, drop the rest."""
    if not isinstance(raw, dict):
        return []
    items = raw.get(key, [])
    if not isinstance(items, list):
        logger.warning("session_environments: %s must be a list; ignoring it", key)
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if len(out) >= MAX_PROFILES:
            logger.warning(
                "session_environments: more than %d %s; ignoring the rest", MAX_PROFILES, key
            )
            break
        try:
            profile = validate(item, index)
        except SessionEnvironmentsError as exc:
            # The message names the field, never its value.
            logger.warning("session_environments: %s; dropping that profile", exc)
            continue
        if profile["id"] in seen:
            logger.warning(
                "session_environments: %s[%d].id %r is a duplicate; dropping it",
                key,
                index,
                profile["id"],
            )
            continue
        seen.add(profile["id"])
        out.append(profile)
    return out


KIRO_CLOUD_PROFILE_KEYS: tuple[str, ...] = ("id", "name", "repositories")
#: Most repositories one Kiro Cloud session opens.
KIRO_CLOUD_REPOSITORIES_MAX = 10
#: ``name`` or ``owner/name``, the forms kiro-cli's ``--repo`` takes.
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?:/[A-Za-z0-9_.-]{1,100})?")


def validate_kiro_cloud_profile(value: object, index: int = 0) -> dict[str, Any]:
    """Validate a saved Kiro Cloud Sessions profile. No KAS call, no credential.

    The gateway's own Kiro sign-in authenticates. A profile names only the
    repositories the cloud sandbox opens; an empty list opens an empty workspace.
    """
    where = f"kiro_cloud_profiles[{index}]"
    if not isinstance(value, dict) or set(value) != set(KIRO_CLOUD_PROFILE_KEYS):
        raise SessionEnvironmentsError(f"{where} must contain exactly {KIRO_CLOUD_PROFILE_KEYS}")
    common = validate_profile(
        {"id": value["id"], "name": value["name"], "template": "kiro-cloud", "workdir": "/"},
        index,
        where=where,
    )
    repositories = value["repositories"]
    if not isinstance(repositories, list) or len(repositories) > KIRO_CLOUD_REPOSITORIES_MAX:
        raise SessionEnvironmentsError(
            f"{where}.repositories must be a list of at most {KIRO_CLOUD_REPOSITORIES_MAX} entries"
        )
    repos: list[str] = []
    for repo in repositories:
        if not isinstance(repo, str) or not _REPOSITORY_RE.fullmatch(repo.strip()):
            raise SessionEnvironmentsError(
                f"{where}.repositories entries must be a repository name or owner/name"
            )
        if repo.strip() not in repos:
            repos.append(repo.strip())
    return {"id": common["id"], "name": common["name"], "repositories": repos}


def validate_kiro_cloud_profiles(value: object) -> list[dict[str, Any]]:
    """Strict atomic list validation for the Kiro Cloud configuration editor."""
    if not isinstance(value, list) or len(value) > MAX_PROFILES:
        raise SessionEnvironmentsError(
            f"kiro_cloud_profiles must be a list of at most {MAX_PROFILES} entries"
        )
    result = [validate_kiro_cloud_profile(item, index) for index, item in enumerate(value)]
    if len({item["id"] for item in result}) != len(result):
        raise SessionEnvironmentsError("kiro_cloud_profiles contains duplicate profile IDs")
    return result


def load_kiro_cloud_profiles(raw: object) -> list[dict[str, Any]]:
    """Lenient load: keep each valid saved entry; drop invalid or duplicate ones."""
    if not isinstance(raw, dict):
        return []
    items = raw.get("kiro_cloud_profiles", [])
    if not isinstance(items, list):
        logger.warning("session_environments: kiro_cloud_profiles must be a list; ignoring it")
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if len(result) >= MAX_PROFILES:
            logger.warning(
                "session_environments: more than %d Kiro Cloud profiles; ignoring the rest",
                MAX_PROFILES,
            )
            break
        try:
            profile = validate_kiro_cloud_profile(item, index)
        except SessionEnvironmentsError as exc:
            logger.warning("session_environments: %s; dropping that profile", exc)
            continue
        if profile["id"] in seen:
            logger.warning(
                "session_environments: kiro_cloud_profiles[%d].id %r is a duplicate; dropping it",
                index,
                profile["id"],
            )
            continue
        seen.add(profile["id"])
        result.append(profile)
    return result


def validate_sprites_profiles(value: object) -> list[dict[str, Any]]:
    """Strict atomic list validation for the Sprites profile editor."""
    return _validate_list(value, "sprites_profiles", validate_sprites_profile)


def load_sprites_profiles(raw: object) -> list[dict[str, Any]]:
    return _load_list(raw, "sprites_profiles", validate_sprites_profile)


def validate_boxd_profiles(value: object) -> list[dict[str, Any]]:
    """Strict atomic list validation for the boxd profile editor."""
    return _validate_list(value, "boxd_profiles", validate_boxd_profile)


def load_boxd_profiles(raw: object) -> list[dict[str, Any]]:
    return _load_list(raw, "boxd_profiles", validate_boxd_profile)
