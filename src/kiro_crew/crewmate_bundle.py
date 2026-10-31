"""The ``.crewmate`` bundle reader (``docs/request-for-change/rfc-crewmate-share.md`` §5.2).

Two halves, both fail closed:

* :func:`read_crewmate_file` turns the raw bytes of one uploaded ``.crewmate``
  zip into a :class:`CrewmateFile`. The zip inventory is vetted
  (:func:`kiro_crew.zip_vet.vet_zip_inventory_bytes`) before ``zipfile`` reads
  anything, and every entry name, type and size is checked before its bytes
  are read. Nothing touches the disk.
* :func:`crewmate_import_params` turns the parsed parts into the
  ``crewmate.import`` change-card params, re-running every §5.2 rule.

The files a bundle carries (bundled skills, cron scripts, an avatar picture)
are listed on the card by :mod:`kiro_crew.crewmate_carried`; this module only
proves they are well formed and paired with the manifest.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Any

from kiro_crew import change_card_catalog as catalog
from kiro_crew import crewmate_carried

#: The format an export writes. Its manifest entry is :data:`MANIFEST_ENTRY`.
CREWMATE_FORMAT = "kirocrew.crewjam"
MANIFEST_ENTRY = "crewjam.json"
#: The format and manifest entry of a file made before the Crew Jam rename. The
#: reader still takes them, through the same checks, so old files keep importing.
LEGACY_CREWMATE_FORMAT = "kirocrew.crewmate"
LEGACY_MANIFEST_ENTRY = "crewmate.json"
CREWMATE_FORMATS = (CREWMATE_FORMAT, LEGACY_CREWMATE_FORMAT)
#: The file extension an export writes (an import also takes ``.crewmate``).
FILE_SUFFIX = ".crewjam"

#: §5.2 caps.
FILE_MAX_BYTES = 8 * 1024 * 1024
ENTRY_MAX = 64
AVATAR_MAX_BYTES = 512 * 1024
JSON_MAX_BYTES = 64 * 1024
CARRIED_FILE_MAX_BYTES = 256 * 1024
SKILL_FOLDER_MAX_BYTES = 1024 * 1024
#: Bounds a bundle's lists so a card stays small; not a §5.2 cap.
_MAX_SKILLS = 32
_MAX_CRONS = 16
#: The longest ``description`` an import takes (the export writer reads it too).
DESCRIPTION_MAX_CHARS = 1000

#: ``<name>`` and ``<file>`` of the two folders (§5.2): no slash, no drive letter.
PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_FUNC_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_APP_SOURCE_RE = re.compile(r"^app:[a-z0-9][a-z0-9_-]{0,63}$")
_TEMPLATE_ID_RE = re.compile(r"^builtin:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

SKILL_FILE = "SKILL.md"
_JSON_ENTRIES = {
    MANIFEST_ENTRY: "crewmate",
    LEGACY_MANIFEST_ENTRY: "crewmate",
    "agent.json": "agent",
    "rules.json": "rules",
    "goals.json": "goals",
    "crons.json": "crons",
}
_AVATAR_ENTRIES = {"avatar.png": "png", "avatar.jpg": "jpg", "avatar.webp": "webp"}
_BUNDLE_KEYS = {"crewmate", "agent", "rules", "goals", "crons", "avatar"}
_MANIFEST_HINTS = ("modelHint", "reasoningEffortHint", "dashboardTemplate")
_MANIFEST_KEYS = {
    *"format formatVersion name description avatarTraits skills sender exportedAt".split(),
    *_MANIFEST_HINTS,
}
_CRON_KEYS = {"name", "message", "cron_expr", "timezone", "script"}
_GRANT_KEYS = ("allowedTools", "autoApprove", "permissions")


def _refuse(code: str, message: str) -> catalog.CardCatalogError:
    return catalog._fail(code, message)


@dataclass(frozen=True)
class CrewmateFile:
    """One vetted ``.crewmate`` file: its JSON parts and the files it carries."""

    bundle: dict[str, Any]
    sha256: str
    #: ``skills/<name>/<file>`` bytes, by skill name then file name.
    skills: dict[str, dict[str, bytes]] = field(default_factory=dict)
    #: ``crons/<file>.py`` bytes, by file name.
    scripts: dict[str, bytes] = field(default_factory=dict)
    #: ``(ext, bytes)`` of ``avatar.<ext>``.
    avatar: tuple[str, bytes] | None = None


#: Names Windows opens as a device whatever the extension (``nul.py`` is NUL).
_WINDOWS_DEVICE_RE = re.compile(r"^(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?$", re.IGNORECASE)


def _part_name(value: str, what: str) -> str:
    if not PART_RE.fullmatch(value) or ".." in value or _WINDOWS_DEVICE_RE.fullmatch(value):
        raise _refuse("invalid_path", f"{what} '{value[:64]}' is not an allowed name")
    return value


def _entry_kind(name: str) -> tuple[str, tuple[str, ...]]:
    """Where entry *name* belongs, or a refusal. Only two folders may hold a ``/``."""
    if "\\" in name or name.startswith("/") or ":" in name:
        raise _refuse("invalid_path", "the file holds a path outside the bundle")
    parts = name.split("/")
    if len(parts) == 1:
        if name in _JSON_ENTRIES:
            return "json", (name,)
        if name in _AVATAR_ENTRIES:
            return "avatar", (name,)
        raise _refuse("unknown_entry", f"unknown entry '{name[:64]}'")
    if len(parts) == 3 and parts[0] == "skills":
        return "skill", (_part_name(parts[1], "skill"), _part_name(parts[2], "skill file"))
    if len(parts) == 2 and parts[0] == "crons":
        script = _part_name(parts[1], "cron script")
        if not script.endswith(".py"):
            raise _refuse("invalid_path", "a cron script must be a .py file")
        return "script", (script,)
    raise _refuse("invalid_path", "only skills/<name>/<file> and crons/<file>.py may hold a folder")


def _is_plain_file(info: zipfile.ZipInfo) -> bool:
    if info.is_dir() or info.flag_bits & 0x1:  # a folder entry, or encrypted
        return False
    mode = info.external_attr >> 16
    # A Unix-made entry names its type; a link (or device, fifo) is refused.
    return not stat.S_IFMT(mode) or stat.S_ISREG(mode)


def _read_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo, cap: int) -> bytes:
    # The header's size is the sender's word; the read itself is what is bounded.
    if info.file_size > cap:
        raise _refuse("too_large", f"'{info.filename[:64]}' is over its size limit")
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        # The export writes only these two; bzip2 and LZMA decoders raise their
        # own error types, so they are refused before any decoder runs.
        raise _refuse("invalid_bundle", f"'{info.filename[:64]}' uses an unsupported compression")
    try:
        with zf.open(info) as fh:
            blob = fh.read(cap + 1)
    except (zipfile.BadZipFile, zlib.error, ValueError, OSError, NotImplementedError, EOFError):
        # A bad CRC, a header that disagrees with the directory, an unknown method.
        raise _refuse("invalid_bundle", f"'{info.filename[:64]}' cannot be read") from None
    if len(blob) > cap:
        raise _refuse("too_large", f"'{info.filename[:64]}' is over its size limit")
    return blob


def _json_part(name: str, blob: bytes) -> Any:
    try:
        return json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        # RecursionError: JSON nested deeper than the parser's stack.
        raise _refuse("invalid_bundle", f"'{name}' is not UTF-8 JSON") from None


def read_crewmate_file(data: bytes) -> CrewmateFile:
    """Vet and parse one ``.crewmate`` file. Raises :class:`catalog.CardCatalogError`."""
    from kiro_crew.zip_vet import ZipInventoryRejected, vet_zip_inventory_bytes

    if not isinstance(data, (bytes, bytearray)) or not data:
        raise _refuse("invalid_bundle", "this is not a .crewmate file")
    if len(data) > FILE_MAX_BYTES:
        raise _refuse("too_large", "the file is over 8 MiB")
    try:
        vet_zip_inventory_bytes(bytes(data), max_members=ENTRY_MAX)
    except ZipInventoryRejected as exc:
        code = "too_many_entries" if exc.reason == "too_many_members" else "invalid_bundle"
        raise _refuse(code, "this is not a .crewmate file") from None
    try:
        zf = zipfile.ZipFile(io.BytesIO(bytes(data)))
    except (zipfile.BadZipFile, ValueError, OSError):
        raise _refuse("invalid_bundle", "this is not a .crewmate file") from None
    parts: dict[str, Any] = {}
    skills: dict[str, dict[str, bytes]] = {}
    scripts: dict[str, bytes] = {}
    avatar: tuple[str, bytes] | None = None
    with zf:
        infos = zf.infolist()
        if len(infos) > ENTRY_MAX:
            raise _refuse("too_many_entries", "the file has more than 64 entries")
        names = [i.filename for i in infos]
        if len(set(names)) != len(names) or len({n.casefold() for n in names}) != len(names):
            raise _refuse("invalid_bundle", "the file names one entry twice")
        total = 0
        for info in infos:
            kind, where = _entry_kind(info.filename)
            if not _is_plain_file(info):
                raise _refuse("invalid_entry", f"'{info.filename[:64]}' is not a plain file")
            if kind == "json":
                if _JSON_ENTRIES[where[0]] in parts:
                    # crewjam.json and crewmate.json are one part; a file holds one.
                    raise _refuse("invalid_bundle", "the file holds more than one manifest")
                parts[_JSON_ENTRIES[where[0]]] = _json_part(
                    where[0], _read_entry(zf, info, JSON_MAX_BYTES)
                )
                blob_len = info.file_size
            elif kind == "avatar":
                if avatar is not None:
                    raise _refuse("invalid_bundle", "the file holds more than one avatar")
                blob = _read_entry(zf, info, AVATAR_MAX_BYTES)
                avatar, blob_len = (_AVATAR_ENTRIES[where[0]], blob), len(blob)
            elif kind == "skill":
                folder = skills.setdefault(where[0], {})
                blob = _read_entry(zf, info, CARRIED_FILE_MAX_BYTES)
                folder[where[1]] = blob
                if sum(len(b) for b in folder.values()) > SKILL_FOLDER_MAX_BYTES:
                    raise _refuse("too_large", f"skill '{where[0]}' is over 1 MiB")
                blob_len = len(blob)
            else:
                blob = _read_entry(zf, info, CARRIED_FILE_MAX_BYTES)
                scripts[where[0]], blob_len = blob, len(blob)
            total += blob_len
            if total > FILE_MAX_BYTES:
                raise _refuse("too_large", "the file unpacks to over 8 MiB")
    for required in (MANIFEST_ENTRY, "agent.json"):
        if _JSON_ENTRIES[required] not in parts:
            raise _refuse("invalid_bundle", f"the file has no {required}")
    if avatar is not None:
        parts["avatar"] = {"type": avatar[0], "size": len(avatar[1])}
    return CrewmateFile(
        bundle=parts,
        sha256=hashlib.sha256(bytes(data)).hexdigest(),
        skills=skills,
        scripts=scripts,
        avatar=avatar,
    )


def _bundle_part(bundle: dict[str, Any], key: str, kind: type, *, required: bool = False) -> Any:
    value = bundle.get(key)
    if value is None and not required:
        return kind()
    if not isinstance(value, kind):
        raise _refuse(
            "invalid_bundle", f"'{key}' must be a{'n object' if kind is dict else ' list'}"
        )
    return value


def _text(value: Any, where: str, *, max_len: int, allow_cr: bool = False) -> str:
    """Text the crewmate reads: the credential and control-character checks of a card.

    A file may end its lines with CRLF (a Windows editor's); a card field may not.
    """
    from kiro_crew.guide_catalog import needs_redaction

    if not isinstance(value, str) or len(value) > max_len:
        raise _refuse("invalid_bundle", f"'{where}' must be text of at most {max_len} characters")
    allowed = "\n\r\t" if allow_cr else "\n\t"
    if any(ord(c) < 32 and c not in allowed or ord(c) == 127 for c in value):
        raise _refuse("invalid_text", f"'{where}' holds a control character")
    if needs_redaction(value):
        raise _refuse("invalid_text", f"'{where}' looks like it holds a credential")
    return value


def file_text(blob: bytes, where: str) -> str:
    """A carried skill or script file as the card shows it: UTF-8 text, checked like a card field."""
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        raise _refuse("invalid_text", f"'{where}' is not UTF-8 text") from None
    return _text(text, where, max_len=CARRIED_FILE_MAX_BYTES, allow_cr=True)


def _skill_refs(m: dict[str, Any]) -> list[dict[str, str]]:
    items = _bundle_part(m, "skills", list)
    if len(items) > _MAX_SKILLS:
        raise _refuse("invalid_bundle", f"a bundle names at most {_MAX_SKILLS} skills")
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise _refuse("invalid_bundle", "each skill must be an object")
        catalog._only(item, {"name", "source"}, "skills")
        name, source = item.get("name"), item.get("source")
        if not isinstance(name, str) or not isinstance(source, str):
            raise _refuse("invalid_bundle", "each skill needs a name and a source")
        _part_name(name, "skill")
        if source != "bundled" and not _APP_SOURCE_RE.fullmatch(source):
            raise _refuse("invalid_bundle", "a skill's source is 'bundled' or 'app:<name>'")
        out.append({"name": name, "source": source})
    if len({r["name"] for r in out}) != len(out):
        raise _refuse("invalid_bundle", "the bundle names one skill twice")
    return out


def _crons(raw: list[Any]) -> list[dict[str, str]]:
    if len(raw) > _MAX_CRONS:
        raise _refuse("invalid_bundle", f"a bundle carries at most {_MAX_CRONS} crons")
    out: list[dict[str, str]] = []
    for job in raw:
        if not isinstance(job, dict):
            raise _refuse("invalid_bundle", "each cron must be an object")
        catalog._only(job, _CRON_KEYS, "crons.json")
        clean = {"name": _text(job.get("name"), "crons.name", max_len=catalog._NAME_MAX)}
        if not clean["name"].strip():
            raise _refuse("invalid_bundle", "each cron needs a name")
        expr, tz = catalog._clean_cron(job, required=True)
        clean["cron_expr"] = expr or ""
        if tz:
            clean["timezone"] = tz
        script = job.get("script")
        if script is not None:
            file, _, func = str(script).partition(":") if isinstance(script, str) else ("", "", "")
            if not file.endswith(".py") or not _FUNC_RE.fullmatch(func):
                raise _refuse("invalid_bundle", "a cron's script is '<file>.py:<function>'")
            clean["script"] = f"{_part_name(file, 'cron script')}:{func}"
        message = job.get("message")
        if message is not None or script is None:
            clean["message"] = _text(message, "crons.message", max_len=catalog._TEXT_MAX)
            if not clean["message"].strip():
                raise _refuse("invalid_bundle", "a cron without a script needs a message")
        out.append(clean)
    return out


def crewmate_import_params(bundle: Any, files: CrewmateFile | None = None) -> dict[str, Any]:
    """The ``crewmate.import`` params for one parsed ``.crewmate`` bundle.

    *bundle* is :attr:`CrewmateFile.bundle` (or the same shape an app hands
    ``ctx.crewmate``); *files* is the :class:`CrewmateFile` it came from, whose
    skill folders, cron scripts and avatar picture are paired with the manifest.
    Fails closed on anything §5.2 does not allow: an unknown key, another format
    or a newer version, a grant, a ``user:`` or HTML dashboard, a ``bundled``
    skill without its folder (or a folder no skill names), a ``script`` cron
    without its file (or a file no cron names), text that looks like a
    credential, and, until the mate-owned agent file exists (§5.7), any
    ``agent.json`` MCP server or skill.
    """
    if not isinstance(bundle, dict):
        raise _refuse("invalid_bundle", "the bundle must be an object")
    catalog._only(bundle, _BUNDLE_KEYS, "bundle")
    m = _bundle_part(bundle, "crewmate", dict, required=True)
    catalog._only(m, _MANIFEST_KEYS, "crewmate.json")
    if m.get("format") not in CREWMATE_FORMATS:
        raise _refuse("invalid_bundle", "this is not a .crewmate file")
    version = m.get("formatVersion")
    if type(version) is not int or version < 1:
        raise _refuse("invalid_bundle", "'formatVersion' must be a whole number")
    if version not in catalog.CREWMATE_FORMAT_VERSIONS:
        raise _refuse("newer_format", "this file needs a newer Kiro Crew")
    agent = _bundle_part(bundle, "agent", dict, required=True)
    grant = sorted(set(agent) & set(_GRANT_KEYS))
    if grant:
        raise _refuse("grant_in_bundle", f"a bundle never carries '{grant[0]}'")
    catalog._only(agent, {"mcpServers", "skills"}, "agent.json")
    if agent.get("mcpServers") or agent.get("skills"):
        raise _refuse("needs_mate_config", "this version cannot import MCP servers yet")
    rules = _bundle_part(bundle, "rules", dict)
    catalog._only(rules, {"rules"}, "rules.json")
    goals = _bundle_part(bundle, "goals", dict)
    catalog._only(goals, {"longTerm", "keyResults"}, "goals.json")
    marker = _bundle_part(bundle, "avatar", dict)
    catalog._only(marker, {"type", "size"}, "avatar")
    sender = _bundle_part(m, "sender", dict)
    catalog._only(sender, {"label"}, "sender")
    for key in (*_MANIFEST_HINTS, "exportedAt"):
        catalog._clean_str(m, key, required=False, max_len=72, allow_empty=True)
    template = (m.get("dashboardTemplate") or "").strip()
    if template.startswith("user:"):
        raise _refuse("user_template", "a custom dashboard stays with its sender")
    if template and not _TEMPLATE_ID_RE.fullmatch(template):
        raise _refuse("invalid_bundle", "'dashboardTemplate' must be a builtin: id")

    refs = _skill_refs(m)
    skills = files.skills if files else {}
    scripts = files.scripts if files else {}
    bundled = {r["name"] for r in refs if r["source"] == "bundled"}
    if bundled != set(skills):
        raise _refuse(
            "skill_mismatch", "each bundled skill needs its folder, and each folder a skill"
        )
    for name, folder in skills.items():
        if SKILL_FILE not in folder:
            raise _refuse("skill_mismatch", f"skill '{name}' has no {SKILL_FILE}")
        for file, blob in folder.items():
            file_text(blob, f"skills/{name}/{file}")
    crons = _crons(_bundle_part(bundle, "crons", list))
    named = [c["script"].partition(":")[0] for c in crons if "script" in c]
    if sorted(named) != sorted(scripts) or len(set(named)) != len(named):
        raise _refuse("script_mismatch", "each script cron needs its file, and each file one cron")
    for file, blob in scripts.items():
        file_text(blob, f"crons/{file}")
    if marker and (files is None or files.avatar is None):
        raise _refuse("invalid_bundle", "the bundle names an avatar it does not carry")
    if files is not None and files.avatar is not None:
        crewmate_carried.check_avatar(*files.avatar)

    params: dict[str, Any] = {
        "name": m.get("name"),
        "format_version": version,
        "description": _text(
            m.get("description") or "", "description", max_len=DESCRIPTION_MAX_CHARS
        ),
        "goal": goals.get("longTerm") or None,
        "sender": sender.get("label"),
    }
    if rules.get("rules"):
        params["rules"] = _text(rules["rules"], "rules", max_len=_rules_max())
    if crons:
        params["crons"] = crons
    if refs:
        params["skills"] = refs
    traits = _bundle_part(m, "avatarTraits", dict)
    if traits:
        params["avatar_traits"] = traits
    for key, param in (
        ("modelHint", "model_hint"),
        ("reasoningEffortHint", "reasoning_effort_hint"),
    ):
        if (m.get(key) or "").strip():
            params[param] = m[key].strip()
    if template:
        params["dashboard_template"] = template
    if files is not None:
        params["bundle_sha256"] = files.sha256
    left_out_parts = {
        "keyResults": _bundle_part(goals, "keyResults", list),
        # The model hint has its own row (shown, not applied); only an effort
        # hint with no model row is named as left out.
        "model": not params.get("model_hint") and params.get("reasoning_effort_hint"),
        "dashboardTemplate": template,
    }
    left_out = [code for code, part in left_out_parts.items() if part]
    if left_out:
        params["not_imported"] = left_out
    if files is not None:
        carried = crewmate_carried.manifest(files)
        if carried:
            params["carried"] = carried
    return catalog.validate_params(catalog.KIND_CREWMATE_IMPORT, params)


def _rules_max() -> int:
    from kiro_crew.members import MEMBER_RULES_MAX_CHARS

    return int(MEMBER_RULES_MAX_CHARS)
