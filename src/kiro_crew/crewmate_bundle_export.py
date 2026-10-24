"""The ``.crewmate`` bundle writer (``docs/request-for-change/rfc-crewmate-share.md`` §5.2, §5.3).

Turns one crewmate's records into the zip the profile card's Export downloads, and
into the preview its confirm view lists. Both come from one :class:`ExportPlan`, so
the view and the file cannot disagree about what leaves.

Nothing private leaves. Every text runs the credential and control-character checks
``crewmate.create`` runs (``_validate_crewmate_create`` in ``guide_catalog.py``); a
part that fails is left out and named with its reason, never sent redacted. Grants,
approval mode, memory, transcripts and ``user:`` dashboard ids are never read into
the plan. No :data:`~kiro_crew.portability_archive.EXPORT_EXCLUDE` name is opened.

Until the mate-owned per-mate file exists (RFC §5.7), ``agent.json`` carries no MCP
server and no skill entry, the one part of the file that waits on it. The template's
MCP servers are still listed in the preview, with the keys a receiver would fill in,
and marked as not in the file. Rules, crons and bundled skills are written in the §5.2
format Phase 1's reader (``crewmate_bundle.py``) takes, inside its list caps, so a file
this writes never refuses on import.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from kiro_crew import change_card_catalog as catalog
from kiro_crew import crewmate_bundle as reader
from kiro_crew.change_card_catalog_kinds import _CREWMATE_NAME_MAX, CREWMATE_FORMAT_VERSIONS

# Every cap and name rule is the reader's own (RFC §5.2), so the writer cannot drift
# from what an import accepts. The names here are the ones this module and its tests use.
from kiro_crew.crewmate_bundle import AVATAR_MAX_BYTES as MAX_AVATAR_BYTES
from kiro_crew.crewmate_bundle import CARRIED_FILE_MAX_BYTES as MAX_FILE_BYTES
from kiro_crew.crewmate_bundle import CREWMATE_FORMAT
from kiro_crew.crewmate_bundle import ENTRY_MAX as MAX_ENTRIES
from kiro_crew.crewmate_bundle import FILE_MAX_BYTES as MAX_BUNDLE_BYTES
from kiro_crew.crewmate_bundle import JSON_MAX_BYTES as MAX_JSON_BYTES
from kiro_crew.crewmate_bundle import SKILL_FILE
from kiro_crew.crewmate_bundle import SKILL_FOLDER_MAX_BYTES as MAX_SKILL_BYTES
from kiro_crew.guide_catalog import _has_control_chars, _redacts
from kiro_crew.portability_archive import EXPORT_EXCLUDE, _is_excluded
from kiro_crew.portability_templates import placeholder_mcp_values
from kiro_crew.preview_text import drop_format_chars

FORMAT_VERSION = max(CREWMATE_FORMAT_VERSIONS)


#: Why a part was left out. The confirm view words each one.
REASON_SCRIPT_MISSING = "script_missing"
REASON_SCRIPT_REFUSED = "script_refused"
REASON_SCRIPT_SHARED = "script_shared"
REASON_SHELL_COMMAND = "shell_command"
REASON_CREDENTIAL = "credential"
REASON_CONTROL_CHARS = "control_chars"
REASON_TOO_LARGE = "too_large"
REASON_BAD_NAME = "bad_name"
REASON_LINK = "link"
REASON_SUBFOLDER = "subfolder"
REASON_NOT_TEXT = "not_text"
REASON_EXCLUDED = "excluded_file"
REASON_NO_SKILL_MD = "no_skill_md"
REASON_UNREADABLE = "unreadable"
REASON_BUNDLE_FULL = "bundle_full"
REASON_NAME_TAKEN = "name_taken"
REASON_CUSTOM_TEMPLATE = "custom_template"
REASON_NOT_PORTABLE = "not_portable"
REASON_FIRST_GOAL_ONLY = "first_goal_only"


class ExportRefused(Exception):
    """The crewmate cannot be exported at all: ``code`` and a sentence for the owner."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _allowed_name(name: str) -> bool:
    """Whether the reader takes *name* for a skill folder, a file in one or a cron script."""
    try:
        reader._part_name(name, "name")
    except catalog.CardCatalogError:
        return False
    return True


@dataclass(frozen=True)
class LeftOut:
    """One part the file does not carry, and why."""

    kind: str
    name: str
    reason: str

    def wire(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name, "reason": self.reason}


@dataclass
class ExportSources:
    """What core read for one crewmate. The handler fills it; nothing here reads config."""

    name: str
    description: str = ""
    avatar: dict[str, Any] = field(default_factory=dict)
    #: The uploaded picture's bytes when ``avatar`` is ``{"kind": "image"}``.
    avatar_image: bytes | None = None
    model: str = ""
    reasoning_effort: str = ""
    #: ``builtin:<id>`` when a shipped template is in force; ``""`` otherwise.
    dashboard_builtin: str = ""
    #: True when a page is in force that is not a shipped template.
    dashboard_custom: bool = False
    rules: str = ""
    goals: dict[str, Any] = field(default_factory=dict)
    #: The crons that wake this crewmate, as stored-job mappings.
    crons: list[dict[str, Any]] = field(default_factory=list)
    #: ``<config_dir>/crons``, the only folder a ``script`` cron may name; see :func:`read_cron_script`.
    crons_root: Path | None = None
    #: The crewmate's workspace ``skills/`` folder, when it has one.
    skills_root: Path | None = None
    #: The crewmate's agent template, as read; only its MCP layout is used.
    agent_spec: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExportChoices:
    """The confirm view's toggles (RFC §5.3 defaults)."""

    rules: bool = True
    goals: bool = True
    crons: bool = True
    #: Skill folder names to carry; ``None`` carries every one that passes.
    skills: frozenset[str] | None = None
    #: The lessons editor's text; ``None`` when the toggle is off.
    lessons: str | None = None


@dataclass
class ExportPlan:
    """Exactly what the file holds, plus what it leaves out."""

    name: str
    slug: str
    manifest: dict[str, Any]
    agent: dict[str, Any]
    rules: str | None
    goals: dict[str, Any] | None
    crons: list[dict[str, Any]]
    scripts: dict[str, str]
    skills: dict[str, dict[str, str]]
    avatar: tuple[str, bytes] | None
    mcp_pending: list[dict[str, Any]]
    left_out: list[LeftOut]
    lessons_name: str = ""


def text_refusal(text: str) -> str | None:
    """The reason *text* may not leave, or ``None``: a credential, or a control character.

    Tabs and newlines are text; a CRLF line end is read as a newline, so a file saved
    on Windows is judged by what it says. Invisible format characters (zero-width
    space, soft hyphen, ...) are removed before the credential check, so one of them
    cannot split a token the redactor would otherwise see.
    """
    if _has_control_chars(text.replace("\r\n", "\n"), allow_newlines=True):
        return REASON_CONTROL_CHARS
    if _redacts(text) or _redacts(drop_format_chars(text)):
        return REASON_CREDENTIAL
    return None


def _text_refusal(value: Any) -> str | None:
    """Check every string, including mapping keys, in a portable part."""
    if isinstance(value, str):
        return text_refusal(value)
    if isinstance(value, dict):
        value = [*value.keys(), *value.values()]
    if isinstance(value, (list, tuple)):
        for item in value:
            refusal = _text_refusal(item)
            if refusal:
                return refusal
    return None


def has_exfil_url(text: str) -> bool:
    """Whether the exfiltration-URL scan flags *text*: judged, never applied, so a
    flagged text is refused instead of being redacted into shape."""
    from kiro_crew.security import redact_exfiltration_urls

    cleaned, warnings = redact_exfiltration_urls(text)
    return bool(warnings) or cleaned != text


def _strip_grants(value: Any) -> Any:
    """*value* with every grant field removed at any depth (``autoApprove`` sits per server too)."""
    if isinstance(value, dict):
        return {k: _strip_grants(v) for k, v in value.items() if k not in reader._GRANT_KEYS}
    if isinstance(value, list):
        return [_strip_grants(v) for v in value]
    return value


def agent_part(spec: dict[str, Any]) -> dict[str, Any]:
    """The agent half of a bundle from a template *spec*: MCP layout only, redacted.

    Every MCP ``env``, ``headers`` and ``oauth.clientSecret`` value becomes the
    placeholder, every grant field goes, and nothing but ``mcpServers`` stays.
    """
    redacted = _strip_grants(placeholder_mcp_values(spec if isinstance(spec, dict) else {}))
    servers = redacted.get("mcpServers")
    return {"mcpServers": servers if isinstance(servers, dict) else {}}


def mcp_placeholder_rows(agent: dict[str, Any]) -> list[dict[str, Any]]:
    """Each MCP server of a redacted agent part, with the keys a receiver fills in."""
    rows: list[dict[str, Any]] = []
    for server, entry in sorted((agent.get("mcpServers") or {}).items()):
        name = str(server)[:64]
        if not isinstance(entry, dict) or text_refusal(str(server)):
            # A template is agent-writable: a name is shown only when it is not a secret.
            continue
        keys = [f"env.{k}" for k in sorted(entry.get("env") or {})]
        keys += [f"headers.{k}" for k in sorted(entry.get("headers") or {})]
        oauth = entry.get("oauth")
        if isinstance(oauth, dict) and "clientSecret" in oauth:
            keys.append("oauth.clientSecret")
        shown = [k[:96] for k in keys if not text_refusal(k)]  # judged whole, then cut
        rows.append({"server": name, "placeholders": shown})
    return rows


def _read_capped(path: Path, within: Path, cap: int, *, canonical: bool = False) -> bytes | str:
    """Bytes of *path* through the link-refusing chokepoint, or a refusal reason.

    ``canonical`` says *within* is already resolved, so the chokepoint keeps it as
    given instead of resolving it again through whatever now sits at that path.
    """
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    try:
        data = safe_read_file_bytes_nolink(
            str(path), within_root=str(within), max_bytes=cap, within_root_is_canonical=canonical
        )
    except FileTooLargeError:
        return REASON_TOO_LARGE
    except OSError:
        return REASON_UNREADABLE
    if data is None:
        # Refused by the chokepoint: a link or hardlink, a sensitive path, or a
        # real path outside *within*.
        return REASON_UNREADABLE
    if len(data) > cap:
        return REASON_TOO_LARGE
    return data


def _decoded(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_cron_script(spec: str, crons_root: Path) -> tuple[str, str, str] | LeftOut:
    """``(file name, function, source)`` for a ``script`` cron spec, or why it cannot travel.

    The spec must name a ``.py`` file directly in the cron folder, by a §5.2 name.
    """
    from kiro_crew.cron_script import resolve_script_path

    try:
        file_path, func = resolve_script_path(spec)
    except FileNotFoundError:
        return LeftOut("cron", "", REASON_SCRIPT_MISSING)
    except Exception:
        return LeftOut("cron", "", REASON_SCRIPT_REFUSED)
    path = Path(file_path)
    root = crons_root.resolve()
    if path.parent != root or not _allowed_name(path.name) or path.suffix != ".py":
        return LeftOut("cron", "", REASON_SCRIPT_REFUSED)
    if text_refusal(path.name) or text_refusal(func):
        return LeftOut("cron", "", REASON_CREDENTIAL)
    if not reader._FUNC_RE.fullmatch(func):
        return LeftOut("cron", "", REASON_SCRIPT_REFUSED)
    data = _read_capped(path, root, MAX_FILE_BYTES)
    if isinstance(data, str):
        return LeftOut(
            "cron", "", REASON_TOO_LARGE if data == REASON_TOO_LARGE else REASON_SCRIPT_REFUSED
        )
    text = _decoded(data)
    if text is None:
        return LeftOut("cron", "", REASON_NOT_TEXT)
    refusal = text_refusal(text)
    if refusal:
        return LeftOut("cron", "", refusal)
    return path.name, func, text


def _cron_expr(raw: Any) -> str | None:
    """The job's 5-field cron expression, or ``None`` when its schedule cannot travel.

    The reader (``crewmate_bundle._crons``) takes a ``cron_expr`` and nothing else, so
    an ``every`` or ``at`` schedule stays home, listed as not portable, and the
    expression meets the reader's own check here, so a file never holds one it refuses.
    """
    if not isinstance(raw, dict) or raw.get("kind") != "cron":
        return None
    expr = raw.get("cron_expr")
    if not isinstance(expr, str):
        return None
    try:
        clean, _tz = catalog._clean_cron({"cron_expr": expr}, required=True)
    except catalog.CardCatalogError:
        return None
    return clean


def portable_crons(
    jobs: list[dict[str, Any]], crons_root: Path | None
) -> tuple[list[dict[str, Any]], dict[str, str], list[LeftOut]]:
    """The crons that travel, the script files they name, and what stays home.

    Only a cron's schedule, prompt, timezone and script travel: never its
    environment, secret grants, approval mode, channel, owner or run history.
    """
    crons: list[dict[str, Any]] = []
    scripts: dict[str, str] = {}
    left_out: list[LeftOut] = []
    for job in jobs:
        name = str(job.get("name") or job.get("id") or "")
        name_refusal = text_refusal(name)
        if name_refusal:
            left_out.append(LeftOut("cron", "", name_refusal))
            continue
        name = name[:80]
        if job.get("command"):
            left_out.append(LeftOut("cron", name, REASON_SHELL_COMMAND))
            continue
        raw_schedule = job.get("schedule")
        tz = job.get("timezone")
        refusal = _text_refusal([raw_schedule, tz])
        if refusal:
            left_out.append(LeftOut("cron", "", refusal))
            continue
        expr = _cron_expr(raw_schedule)
        raw_message = job.get("message")
        message = raw_message if isinstance(raw_message, str) else ""
        # The reader needs a prompt on a cron with no script, and refuses an empty one.
        if expr is None or (not job.get("script") and not message.strip()):
            left_out.append(LeftOut("cron", name, REASON_NOT_PORTABLE))
            continue
        refusal = text_refusal(message)
        if refusal:
            left_out.append(LeftOut("cron", name, refusal))
            continue
        entry: dict[str, Any] = {"name": name, "cron_expr": expr}
        if message.strip():
            entry["message"] = message
        if isinstance(tz, str) and tz:
            entry["timezone"] = tz[:64]
        spec = job.get("script")
        if spec:
            got = (
                read_cron_script(spec, crons_root)
                if isinstance(spec, str) and crons_root is not None
                else LeftOut("cron", "", REASON_SCRIPT_REFUSED)
            )
            if isinstance(got, LeftOut):
                left_out.append(LeftOut("cron", name, got.reason))
                continue
            file_name, func, source = got
            if file_name.casefold() in {n.casefold() for n in scripts}:
                left_out.append(LeftOut("cron", name, REASON_SCRIPT_SHARED))
                continue
            # The reader pairs ``<file>.py:<function>`` with the ``crons/<file>.py`` entry.
            entry["script"] = f"{file_name}:{func}"
        # The reader's own check on this one cron (prompt length, a carriage return,
        # name), so the file never holds a cron that refuses the whole import.
        try:
            reader._crons([entry])
        except catalog.CardCatalogError:
            left_out.append(LeftOut("cron", name, REASON_NOT_PORTABLE))
            continue
        if len(crons) >= reader._MAX_CRONS:
            left_out.append(LeftOut("cron", name, REASON_BUNDLE_FULL))
            continue
        if "script" in entry:
            scripts[entry["script"].split(":", 1)[0]] = source
        crons.append(entry)
    return crons, scripts, left_out


class _TooManyEntries(Exception):
    """A folder lists more entries than any file could carry."""


def _scan(directory: Path, limit: int | None = None) -> list[os.DirEntry[str]]:
    """The folder's entries by name. With *limit*, stops listing past it and raises,
    so a folder of thousands of files is never held whole."""
    out: list[os.DirEntry[str]] = []
    with os.scandir(directory) as it:
        for entry in it:
            out.append(entry)
            if limit is not None and len(out) > limit:
                raise _TooManyEntries(str(directory))
    return sorted(out, key=lambda e: e.name)


def _entry_is_link(entry: os.DirEntry[str]) -> bool:
    from kiro_crew.portability_archive import _entry_is_link as is_link

    return is_link(entry)


def _read_skill(directory: Path) -> dict[str, str] | str:
    """Every file of one skill folder as text, or the reason the skill stays home."""
    files: dict[str, str] = {}
    seen: set[str] = set()
    total = 0
    try:
        # No file holds more than MAX_ENTRIES entries, so a folder with more stays home.
        entries = _scan(directory, MAX_ENTRIES)
    except _TooManyEntries:
        return REASON_TOO_LARGE
    except OSError:
        return REASON_UNREADABLE
    for entry in entries:
        if _entry_is_link(entry):
            return REASON_LINK
        if entry.is_dir(follow_symlinks=False):
            return REASON_SUBFOLDER
        if entry.name in EXPORT_EXCLUDE or _is_excluded(PurePosixPath(entry.name)):
            return REASON_EXCLUDED
        # The reader folds case when it checks for a name used twice.
        if not _allowed_name(entry.name) or entry.name.casefold() in seen:
            return REASON_BAD_NAME
        seen.add(entry.name.casefold())
        if text_refusal(entry.name):
            # A file NAME travels too (preview keys, zip entry names).
            return REASON_CREDENTIAL
        try:
            regular = stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode)
        except OSError:
            return REASON_UNREADABLE
        if not regular:
            return REASON_UNREADABLE
        # *directory* is already resolved: kept literal, a swap for a link cannot widen it.
        data = _read_capped(directory / entry.name, directory, MAX_FILE_BYTES, canonical=True)
        if isinstance(data, str):
            return data
        total += len(data)
        if total > MAX_SKILL_BYTES:
            return REASON_TOO_LARGE
        text = _decoded(data)
        if text is None:
            return REASON_NOT_TEXT
        refusal = text_refusal(text)
        if refusal:
            return refusal
        files[entry.name] = text
    if SKILL_FILE not in files:
        return REASON_NO_SKILL_MD
    return files


def bundled_skills(
    root: Path | None, picked: frozenset[str] | None = None
) -> tuple[dict[str, dict[str, str]], list[LeftOut]]:
    """The skill folders under *root* that can travel as files, and the ones that cannot.

    Never follows a link: a linked root, folder or file leaves that part out. The
    root is resolved once and every read is held inside that one path, so a folder
    swapped for a link mid-walk cannot move the boundary. Only what a file could
    carry is kept: a folder not in *picked*, or past the reader's skill count or the
    bundle's size cap, is never read into memory.
    """
    from kiro_crew.platform_compat import is_link_or_junction

    if root is None:
        return {}, []
    try:
        if is_link_or_junction(root):
            return {}, [LeftOut("skill", "", REASON_LINK)]
        if not root.is_dir():
            return {}, []
        canon = Path(os.path.realpath(root))
        entries = _scan(canon)
    except OSError:
        return {}, [LeftOut("skill", "", REASON_UNREADABLE)]
    skills: dict[str, dict[str, str]] = {}
    left_out: list[LeftOut] = []
    folded: set[str] = set()
    held = 0
    for entry in entries:
        shown = entry.name if _allowed_name(entry.name) and not text_refusal(entry.name) else ""
        if _entry_is_link(entry):
            left_out.append(LeftOut("skill", shown, REASON_LINK))
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        if not shown:
            left_out.append(LeftOut("skill", "", REASON_BAD_NAME))
            continue
        if entry.name.casefold() in folded:
            # A second folder whose name differs only in case: the reader refuses both.
            left_out.append(LeftOut("skill", shown, REASON_BAD_NAME))
            continue
        folded.add(entry.name.casefold())
        if picked is not None and entry.name not in picked:
            continue
        if len(skills) >= reader._MAX_SKILLS or held >= MAX_BUNDLE_BYTES:
            left_out.append(LeftOut("skill", shown, REASON_BUNDLE_FULL))
            continue
        got = _read_skill(canon / entry.name)
        if isinstance(got, str):
            left_out.append(LeftOut("skill", shown, got))
            continue
        size = sum(len(t.encode("utf-8")) for t in got.values())
        if held + size > MAX_BUNDLE_BYTES:
            left_out.append(LeftOut("skill", shown, REASON_BUNDLE_FULL))
            continue
        held += size
        skills[entry.name] = got
    return skills, left_out


def _goals_part(record: dict[str, Any]) -> tuple[dict[str, Any] | None, LeftOut | None]:
    """``goals.json`` from a goal record: the first goal and its key results, as text."""
    goals = [g for g in record.get("goals") or [] if isinstance(g, dict)]
    if not goals:
        return None, None
    first = goals[0]
    long_term = str(first.get("text") or "")
    krs = []
    dropped = len(goals) > 1
    for kr in record.get("key_results") or []:
        if not isinstance(kr, dict):
            continue
        ids = kr.get("goal_ids") or []
        if ids and first.get("id") not in ids:
            dropped = True
            continue
        krs.append({"text": str(kr.get("text") or ""), "target": str(kr.get("target") or "")})
    texts = [long_term, *(t for kr in krs for t in kr.values())]
    for text in texts:
        refusal = text_refusal(text)
        if refusal:
            return None, LeftOut("goals", "", refusal)
    # Format version 1 holds one long-term goal; the rest stay home and are named.
    left = LeftOut("goals", "", REASON_FIRST_GOAL_ONLY) if dropped else None
    return {"longTerm": long_term, "keyResults": krs}, left


def _avatar_traits(avatar: dict[str, Any]) -> dict[str, Any]:
    traits = avatar.get("traits")
    if not isinstance(traits, dict):
        return {}
    return {
        str(k)[:32]: v
        for k, v in traits.items()
        if isinstance(v, bool) or (isinstance(v, str) and len(v) <= 64 and not text_refusal(v))
    }


#: The PNG chunks a picture needs to draw. Every other chunk (tEXt, iTXt, zTXt, eXIf,
#: iCCP, time stamps, ...) can carry text or metadata and is dropped.
_PNG_KEEP = frozenset({b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND", b"sRGB", b"gAMA"})


def _png_chunk(tag: bytes, body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + tag + body + zlib.crc32(tag + body).to_bytes(4, "big")


def _zlib_stream_length(stream: bytes) -> int | None:
    """How many bytes of *stream* the zlib stream at its start uses, or ``None``.

    Inflates in bounded steps and keeps none of the output, so a small file that
    inflates hugely costs time, never memory.
    """
    inflater = zlib.decompressobj()
    data = stream
    while data and not inflater.eof:
        inflater.decompress(data, 1 << 20)
        data = inflater.unconsumed_tail
    if not inflater.eof:
        return None
    return len(stream) - len(inflater.unused_data)


def _png_pixels_only(data: bytes) -> bytes | None:
    """*data* with only the chunks a decoder needs, and its image data cut to the zlib
    stream it holds: bytes after that stream inside ``IDAT`` would otherwise travel."""
    head = bytearray(data[:8])
    idat = bytearray()
    pos = 8
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        tag = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(data):
            return None
        if tag == b"IDAT":
            idat += data[pos + 8 : pos + 8 + length]
        elif tag == b"IEND":
            used = _zlib_stream_length(bytes(idat))
            if used is None:
                return None
            return bytes(head + _png_chunk(b"IDAT", bytes(idat[:used])) + _png_chunk(b"IEND", b""))
        elif tag in _PNG_KEEP and not idat:
            head += data[pos:end]
        pos = end
    return None


def _jpeg_pixels_only(data: bytes) -> bytes | None:
    """*data* without APPn (EXIF, XMP, ICC, JFIF) and COM segments anywhere, scans
    included, and with nothing after the end-of-image marker."""
    out = bytearray(data[:2])
    pos = 2
    while pos + 2 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xD9:  # end of image
            return bytes(out + b"\xff\xd9")
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if pos + 4 > len(data):
            return None
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        end = pos + 2 + length
        if length < 2 or end > len(data):
            return None
        if not (0xE0 <= marker <= 0xEF or marker == 0xFE):
            out += data[pos:end]
        pos = end
        if marker == 0xDA:
            # Entropy-coded data runs to the next marker that is not a stuffed zero
            # or a restart marker.
            scan = pos
            while pos + 1 < len(data) and not (
                data[pos] == 0xFF and data[pos + 1] not in (0x00, *range(0xD0, 0xD8))
            ):
                pos += 1
            out += data[scan:pos]
    return None


#: The WEBP chunks a still picture needs. ICCP (profile text), EXIF, XMP and any
#: unknown chunk are dropped.
_WEBP_KEEP = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH"})
#: VP8X flags for the dropped chunks: ICC profile, EXIF, XMP.
_WEBP_DROPPED_FLAGS = 0x20 | 0x08 | 0x04


def _webp_pixels_only(data: bytes) -> bytes | None:
    """*data* with only the chunks a decoder needs, its header flags and size made to agree."""
    chunks: list[bytes] = []
    pos = 12
    while pos + 8 <= len(data):
        tag = data[pos : pos + 4]
        length = int.from_bytes(data[pos + 4 : pos + 8], "little")
        end = pos + 8 + length + (length & 1)
        if end > len(data):
            return None
        if tag in (b"ANIM", b"ANMF"):
            # A frame nests its own chunks; an animated picture stays home rather
            # than be walked chunk by chunk.
            return None
        if tag in _WEBP_KEEP:
            chunk = bytearray(data[pos:end])
            if tag == b"VP8X" and len(chunk) > 8:
                chunk[8] &= ~_WEBP_DROPPED_FLAGS & 0xFF
            chunks.append(bytes(chunk))
        pos = end
    if pos != len(data) or not chunks:
        return None
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def portable_image(ext: str, data: bytes) -> bytes | None:
    """The picture with every metadata block removed, or ``None`` when it cannot be parsed.

    An upload is stored as sent, so its PNG text chunks, JPEG EXIF (a location, a
    camera serial) or WEBP XMP would otherwise travel byte for byte.
    """
    strip = {"png": _png_pixels_only, "jpg": _jpeg_pixels_only, "webp": _webp_pixels_only}
    try:
        return strip[ext](data)
    except (KeyError, IndexError, zlib.error):
        return None


def lessons_skill_name(slug: str) -> str:
    """``<slug>-lessons``, held to the §5.2 name pattern."""
    suffix = "-lessons"
    return slug[: 64 - len(suffix)] + suffix


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def plan_export(
    src: ExportSources, slug: str, choices: ExportChoices, *, now: datetime | None = None
) -> ExportPlan:
    """Decide every part the file carries. Raises :class:`ExportRefused` only when
    the crewmate itself cannot leave (its name or the lessons text fails the checks)."""
    # Before text_refusal, whose credential pass also removes these links: named on
    # its own, the owner is told it is the link, not a credential, that stops the file.
    # A refusal of the lessons text carries a ``lessons_`` code, so the view can tell
    # it from a refusal of the crewmate itself.
    if choices.lessons and has_exfil_url(choices.lessons):
        raise ExportRefused(
            "lessons_exfil_url", "the lessons hold a link that could carry data out"
        )
    refusal = text_refusal(choices.lessons or "")
    if refusal:
        raise ExportRefused(
            f"lessons_{refusal}", "the lessons hold a credential or control character"
        )
    refusal = text_refusal(src.name)
    if refusal:
        raise ExportRefused(refusal, "the name holds a credential or control character")
    if not src.name.strip():
        raise ExportRefused("invalid_name", "the crewmate has no name")
    try:
        # The name the import card takes, so a file never carries one it refuses.
        catalog._member_name({"name": src.name}, "name", max_len=_CREWMATE_NAME_MAX)
    except catalog.CardCatalogError as exc:
        raise ExportRefused("invalid_name", f"the name cannot be imported: {exc}") from None
    left: list[LeftOut] = []

    description = src.description
    if description and text_refusal(description):
        left.append(LeftOut("description", "", text_refusal(description) or ""))
        description = ""
    if "\r" in description:
        # The reader refuses a carriage return in the description and the rules.
        left.append(LeftOut("description", "", REASON_CONTROL_CHARS))
        description = ""
    if len(description) > reader.DESCRIPTION_MAX_CHARS:
        left.append(LeftOut("description", "", REASON_TOO_LARGE))
        description = ""

    rules: str | None = None
    if choices.rules and src.rules:
        refusal = text_refusal(src.rules) or (REASON_CONTROL_CHARS if "\r" in src.rules else "")
        if refusal:
            left.append(LeftOut("rules", "", refusal))
        else:
            rules = src.rules

    goals: dict[str, Any] | None = None
    if choices.goals:
        goals, goal_left = _goals_part(src.goals)
        if goal_left:
            left.append(goal_left)

    crons: list[dict[str, Any]] = []
    scripts: dict[str, str] = {}
    if choices.crons:
        crons, scripts, cron_left = portable_crons(src.crons, src.crons_root)
        left += cron_left

    found, skill_left = bundled_skills(src.skills_root, choices.skills)
    left += skill_left
    lessons_name = ""
    if choices.lessons is not None and choices.lessons.strip():
        lessons_name = lessons_skill_name(slug)
        for taken in [n for n in found if n.casefold() == lessons_name.casefold()]:
            left.append(LeftOut("skill", taken, REASON_NAME_TAKEN))
            del found[taken]
        if len(choices.lessons.encode("utf-8")) > MAX_FILE_BYTES:
            raise ExportRefused("lessons_too_large", "the lessons text is over 256 KiB")
    skills = {
        name: files
        for name, files in found.items()
        if choices.skills is None or name in choices.skills
    }
    if lessons_name:
        skills[lessons_name] = {SKILL_FILE: choices.lessons or ""}

    avatar: tuple[str, bytes] | None = None
    traits: dict[str, Any] = {}
    kind = src.avatar.get("kind") if isinstance(src.avatar, dict) else None
    if kind == "image" and src.avatar_image:
        # The upload route's own sniffer, which the import side's check calls too.
        from kiro_crew.dashboard.handlers.agents import _sniff_image_ext

        ext = _sniff_image_ext(src.avatar_image[:16])
        pixels = portable_image(ext, src.avatar_image) if ext else None
        if pixels is None:
            left.append(LeftOut("avatar", "", REASON_NOT_PORTABLE))
        elif len(pixels) > MAX_AVATAR_BYTES:
            left.append(LeftOut("avatar", "", REASON_TOO_LARGE))
        else:
            avatar = (ext, pixels)
    elif kind == "ghost":
        traits = _avatar_traits(src.avatar)
    elif kind:
        left.append(LeftOut("avatar", "", REASON_NOT_PORTABLE))

    dashboard = ""
    if src.dashboard_builtin:
        dashboard = src.dashboard_builtin
    elif src.dashboard_custom:
        left.append(LeftOut("dashboard", "", REASON_CUSTOM_TEMPLATE))

    agent_full = agent_part(src.agent_spec)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest: dict[str, Any] = {
        "format": CREWMATE_FORMAT,
        "formatVersion": FORMAT_VERSION,
        "name": src.name,
        "description": description,
        "skills": [],
        "exportedAt": stamp,
    }
    if traits:
        manifest["avatarTraits"] = traits
    for key, value in (
        ("modelHint", src.model),
        ("reasoningEffortHint", src.reasoning_effort),
        ("dashboardTemplate", dashboard),
    ):
        if value and not text_refusal(value) and len(value) <= 72:
            manifest[key] = value
    plan = ExportPlan(
        name=src.name,
        slug=slug,
        manifest=manifest,
        # RFC §5.7: no MCP server or skill entry until the per-mate file exists.
        agent={"mcpServers": {}, "skills": []},
        rules=rules,
        goals=goals,
        crons=crons,
        scripts=scripts,
        skills={},
        avatar=avatar,
        mcp_pending=mcp_placeholder_rows(agent_full),
        left_out=left,
        lessons_name=lessons_name,
    )
    _fit_parts(plan)
    _fit_skills(plan, skills)
    refusal = _text_refusal(
        [
            plan.manifest,
            plan.agent,
            plan.rules,
            plan.goals,
            plan.crons,
            plan.scripts,
            plan.skills,
            plan.mcp_pending,
        ]
    )
    if refusal:
        raise ExportRefused(
            refusal, "a part of this crewmate holds a credential or control character"
        )
    _check_importable(plan)
    return plan


def _check_importable(plan: ExportPlan) -> None:
    """Read the planned file back through Phase 1's reader, as an import would.

    The checks above leave out each part the reader refuses, naming it. This is the
    last check: should any rule still differ, the export stops here with the
    reader's own words, rather than handing the owner a file every import refuses.
    """
    try:
        parsed = reader.read_crewmate_file(write_bundle(plan))
        reader.crewmate_import_params(parsed.bundle, parsed)
    except catalog.CardCatalogError as exc:
        raise ExportRefused("not_importable", f"the file would not import: {exc}") from None


def _over_caps(plan: ExportPlan) -> bool:
    entries = _entries(plan)
    return len(entries) > MAX_ENTRIES or sum(len(b) for _, b in entries) > MAX_BUNDLE_BYTES


def _fixed_entries(plan: ExportPlan) -> list[tuple[str, bytes]]:
    entries = [
        ("crewmate.json", _json_bytes(plan.manifest)),
        ("agent.json", _json_bytes(plan.agent)),
    ]
    if plan.avatar:
        entries.append((f"avatar.{plan.avatar[0]}", plan.avatar[1]))
    if plan.rules is not None:
        entries.append(("rules.json", _json_bytes({"rules": plan.rules})))
    if plan.goals is not None:
        entries.append(("goals.json", _json_bytes(plan.goals)))
    if plan.crons:
        entries.append(("crons.json", _json_bytes(plan.crons)))
        entries += [(f"crons/{n}", s.encode("utf-8")) for n, s in sorted(plan.scripts.items())]
    return entries


def _fit_skills(plan: ExportPlan, skills: dict[str, dict[str, str]]) -> None:
    """Add skills in name order while the file stays inside the entry and size caps.

    The lessons skill goes first, since the owner wrote it for this file.
    """
    order = sorted(skills, key=lambda n: (n != plan.lessons_name, n))
    for name in order:
        files = skills[name]
        if len(plan.skills) >= reader._MAX_SKILLS:
            # The reader names at most this many skills; one more refuses the file.
            plan.left_out.append(LeftOut("skill", name, REASON_BUNDLE_FULL))
            continue
        plan.skills[name] = files
        plan.manifest["skills"] = [{"name": n, "source": "bundled"} for n in sorted(plan.skills)]
        if _over_caps(plan):
            del plan.skills[name]
            plan.manifest["skills"] = [
                {"name": n, "source": "bundled"} for n in sorted(plan.skills)
            ]
            plan.left_out.append(LeftOut("skill", name, REASON_BUNDLE_FULL))


def _fit_parts(plan: ExportPlan) -> None:
    """Leave out a JSON part over its 64 KiB cap; refuse a manifest over it."""
    if len(_json_bytes(plan.manifest)) > MAX_JSON_BYTES:
        raise ExportRefused(REASON_TOO_LARGE, "the crewmate's description is too long to export")
    if plan.rules is not None and len(_json_bytes({"rules": plan.rules})) > MAX_JSON_BYTES:
        plan.rules = None
        plan.left_out.append(LeftOut("rules", "", REASON_TOO_LARGE))
    if plan.goals is not None and len(_json_bytes(plan.goals)) > MAX_JSON_BYTES:
        plan.goals = None
        plan.left_out.append(LeftOut("goals", "", REASON_TOO_LARGE))
    if plan.crons and len(_json_bytes(plan.crons)) > MAX_JSON_BYTES:
        for cron in plan.crons:
            plan.left_out.append(LeftOut("cron", cron["name"], REASON_TOO_LARGE))
        plan.crons, plan.scripts = [], {}
    # Schedules come before skills; the last ones stay home while the file is over
    # its entry or size cap, each with its script.
    while plan.crons and _over_caps(plan):
        cron = plan.crons.pop()
        script = cron.get("script")
        if script:
            plan.scripts.pop(script.split(":", 1)[0], None)
        plan.left_out.append(LeftOut("cron", cron["name"], REASON_BUNDLE_FULL))


def _entries(plan: ExportPlan) -> list[tuple[str, bytes]]:
    entries = _fixed_entries(plan)
    for name in sorted(plan.skills):
        for file_name, text in sorted(plan.skills[name].items()):
            entries.append((f"skills/{name}/{file_name}", text.encode("utf-8")))
    return entries


def write_bundle(plan: ExportPlan) -> bytes:
    """The ``.crewmate`` zip for *plan*. Deterministic: fixed entry order and timestamps."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in _entries(plan):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buf.getvalue()


def plan_fingerprint(plan: ExportPlan) -> str:
    """A digest of everything :func:`preview` shows. The download sends back the one it
    confirmed, and a plan rebuilt with different content is refused instead of written."""
    return str(preview(plan)["fingerprint"])


def preview(plan: ExportPlan) -> dict[str, Any]:
    """What the confirm view lists: every part that leaves, in full, and what does not."""
    view = _preview_body(plan)
    canonical = json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    view["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return view


def _preview_body(plan: ExportPlan) -> dict[str, Any]:
    avatar_bytes = (
        plan.avatar[1]
        if plan.avatar
        else (
            json.dumps(
                plan.manifest["avatarTraits"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if "avatarTraits" in plan.manifest
            else b""
        )
    )
    return {
        "name": plan.name,
        "filename": bundle_filename(plan.name),
        "description": plan.manifest.get("description", ""),
        "model": plan.manifest.get("modelHint", ""),
        "dashboard": plan.manifest.get("dashboardTemplate", ""),
        "avatar": "image" if plan.avatar else ("ghost" if "avatarTraits" in plan.manifest else ""),
        "avatar_digest": hashlib.sha256(avatar_bytes).hexdigest() if avatar_bytes else "",
        "rules": plan.rules,
        "goals": plan.goals,
        "crons": [
            (
                {**c, "source": plan.scripts.get(c["script"].split(":", 1)[0], "")}
                if "script" in c
                else c
            )
            for c in plan.crons
        ],
        "skills": [
            {"name": n, "lessons": n == plan.lessons_name, "files": plan.skills[n]}
            for n in sorted(plan.skills)
        ],
        "mcp_servers": plan.mcp_pending,
        "mcp_in_file": False,
        "left_out": [item.wire() for item in plan.left_out],
    }


def bundle_filename(name: str) -> str:
    """An ASCII-safe ``<name>.crewmate`` for the download's plain ``filename``."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "crewmate"
    return f"{stem[:64]}.crewmate"


def content_disposition(name: str) -> str:
    """``attachment`` with an ASCII ``filename`` and the exact UTF-8 name in ``filename*``."""
    exact = quote(f"{name}.crewmate", safe="")
    return f"attachment; filename=\"{bundle_filename(name)}\"; filename*=UTF-8''{exact}"
