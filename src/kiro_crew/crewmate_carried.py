"""The files a ``.crewmate`` bundle carries, as a ``crewmate.import`` card lists them.

A bundle can carry three kinds of file beside its JSON parts (rfc-crewmate-share.md
§5.2): bundled skill folders (``skills/<name>/<file>``), cron scripts
(``crons/<file>.py``) and an avatar picture. A card holds at most 64 KiB, so it
never holds their bytes: it holds this module's :func:`manifest` (each file's
name, size and sha256) and the sha256 of the whole file, which the local backend
keeps as an owner-only staged copy (:mod:`kiro_crew.crewmate_stage`). The card
shows each file in full by reading that copy; the press writes them through
``POST /api/members/{slug}/import-files``, which re-reads the copy and refuses
on any mismatch.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from kiro_crew.crewmate_bundle import CrewmateFile

SKILL_FILE = "SKILL.md"
_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_AVATAR_TYPES = ("png", "jpg", "webp")
_MAX_SKILLS = 32
_MAX_FILES = 64


def _fail(code: str, message: str) -> Exception:
    from kiro_crew import change_card_catalog as catalog

    return catalog._fail(code, message)


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def avatar_token(blob: bytes) -> str:
    """The staging token the avatar commit checks: the upload route's own digest prefix."""
    return digest(blob)[:16]


def check_avatar(ext: str, blob: bytes) -> None:
    """Refuse a picture that is not the PNG, JPEG or WEBP its name says, or is cut off."""
    from kiro_crew.dashboard.handlers.agents import _image_body_complete, _sniff_image_ext

    if _sniff_image_ext(blob[:16]) != ext or not _image_body_complete(ext, blob):
        raise _fail("invalid_avatar", "the picture is not a complete PNG, JPEG or WEBP image")


def manifest(files: "CrewmateFile") -> dict[str, Any]:
    """The card's record of every carried file: names, sizes and digests, never bytes."""
    out: dict[str, Any] = {}
    if files.skills:
        out["skills"] = [
            {
                "name": name,
                "files": [
                    {"path": path, "size": len(blob), "sha256": digest(blob)}
                    for path, blob in sorted(folder.items())
                ],
            }
            for name, folder in sorted(files.skills.items())
        ]
    if files.scripts:
        out["scripts"] = [
            {"file": file, "size": len(blob), "sha256": digest(blob)}
            for file, blob in sorted(files.scripts.items())
        ]
    if files.avatar is not None:
        ext, blob = files.avatar
        out["avatar"] = {"type": ext, "size": len(blob), "sha256": digest(blob)}
    return out


def _entry(item: Any, key: str) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != {key, "size", "sha256"}:
        raise _fail("invalid_param", "'carried' lists a file in an unknown shape")
    name, size, sha = item[key], item["size"], item["sha256"]
    if not isinstance(name, str) or not _PART_RE.fullmatch(name) or ".." in name:
        raise _fail("invalid_param", "'carried' names a file outside the bundle's rules")
    if type(size) is not int or not 0 <= size <= 1024 * 1024:
        raise _fail("invalid_param", "'carried' gives a file a size it cannot have")
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise _fail("invalid_param", "'carried' gives a file a digest it cannot have")
    return {key: name, "size": size, "sha256": sha}


def validate(value: Any) -> dict[str, Any]:
    """A ``carried`` card param exactly as :func:`manifest` writes it, or a refusal."""
    if value in (None, {}):
        return {}
    if not isinstance(value, dict) or not set(value) <= {"skills", "scripts", "avatar"}:
        raise _fail("invalid_param", "'carried' must list skills, scripts or an avatar")
    out: dict[str, Any] = {}
    count = 0
    skills = value.get("skills")
    if skills is not None:
        if not isinstance(skills, list) or not 0 < len(skills) <= _MAX_SKILLS:
            raise _fail("invalid_param", "'carried.skills' must be a short list")
        clean = []
        for skill in skills:
            if not isinstance(skill, dict) or set(skill) != {"name", "files"}:
                raise _fail("invalid_param", "'carried.skills' items are {name, files}")
            name = skill["name"]
            if not isinstance(name, str) or not _PART_RE.fullmatch(name) or ".." in name:
                raise _fail("invalid_param", "'carried.skills' names a skill outside the rules")
            entries = skill["files"]
            if not isinstance(entries, list) or not entries:
                raise _fail("invalid_param", "each carried skill lists its files")
            listed = [_entry(f, "path") for f in entries]
            if SKILL_FILE not in {f["path"] for f in listed}:
                raise _fail("invalid_param", f"each carried skill has a {SKILL_FILE}")
            count += len(listed)
            clean.append({"name": name, "files": listed})
        out["skills"] = clean
    scripts = value.get("scripts")
    if scripts is not None:
        if not isinstance(scripts, list) or not scripts:
            raise _fail("invalid_param", "'carried.scripts' must be a list")
        listed = [_entry(s, "file") for s in scripts]
        if any(not s["file"].endswith(".py") for s in listed):
            raise _fail("invalid_param", "a carried script is a .py file")
        count += len(listed)
        out["scripts"] = listed
    avatar = value.get("avatar")
    if avatar is not None:
        clean_avatar = _entry(avatar, "type")
        if clean_avatar["type"] not in _AVATAR_TYPES:
            raise _fail("invalid_param", "a carried picture is a png, jpg or webp")
        out["avatar"] = clean_avatar
    if count > _MAX_FILES:
        raise _fail("invalid_param", "'carried' lists more files than a bundle holds")
    return out


def code_exec(carried: dict[str, Any]) -> bool:
    """Whether the carried files run code here: a cron script, or a skill file besides SKILL.md."""
    if carried.get("scripts"):
        return True
    return any(
        f.get("path") != SKILL_FILE
        for skill in carried.get("skills") or ()
        for f in skill.get("files") or ()
    )
