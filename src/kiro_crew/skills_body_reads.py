"""Every read of a skill file the loader enumerated: the project trust key and its audit, the
enumerated-read choke point, the frontmatter cache, ``load_skill`` and the global body reader,
exact-key reads of the scoped catalog with their paging, and the shapes by which a tool call
is recognised as reading a skill body.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.

Instance fields written: ``_audited_projects``, ``_fm_cache``,
``_project_skills_unsupported_warned``.

Instance fields read: ``_dir``, ``_extra_paths``.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import NamedTuple

from kiro_crew import hooks as hooks_module
from kiro_crew import skill_trust
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink, validate_file_path
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.sel import sel
from kiro_crew.skills_discovery import _installed_package_bytes_match, _within_any

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


# A SKILL.md whose body is a saved web page is not a skill: its markup would
# land in the prompt. The bounded cache warns about each path about once.
@functools.lru_cache(maxsize=256)
def _warn_html_skill(path: str) -> bool:
    logger.warning("Skipping skill whose SKILL.md body is HTML, not markdown: %s", path)
    return True


def _html_skill_refused(meta: dict, path: object) -> bool:
    # The key holds a colon, so no front-matter line can set it.
    return bool(meta.get("_html:body")) and _warn_html_skill(str(path))


# Byte bound on any confined project skill body read on behalf of a session:
# context injection, the skill_search body grep and the /api/skills search
# route all read through this one cap, so an oversized project SKILL.md is
# skipped rather than loaded whole.
PROJECT_SKILL_BODY_CAP = 24_750
PINNED_SKILL_BODIES_CAP = 99_000
# What one exact-key read may deliver, in UTF-8 bytes. A tool response is cut at
# ``validation.MAX_RESPONSE_LEN`` characters and the cut takes the TAIL, so a body
# that does not fit under that ceiling with its framing would lose its closing
# instructions silently. A larger body is therefore refused whole and served in
# whole-line pages that each fit; the bound is a context budget, not a file limit,
# so it neither grows for a large skill nor shrinks what is on disk. The pinning
# path's ``PINNED_SKILL_BODIES_CAP`` above is the same number; if this capacity
# is ever revisited, the two move together.
SKILL_READ_CAPACITY = 99_000

# Why an exact-key read delivered no body. Three values because the caller acts
# differently on each: a key outside the scope is a lookup to correct, an
# unreadable file is the operator's to repair, and a body over the capacity is
# read again in pages. One sentence naming all three sends the reader after the
# wrong one.
SKILL_READ_OUTSIDE_SCOPE = "outside_scope"
SKILL_READ_UNREADABLE = "unreadable"
SKILL_READ_OVER_CAPACITY = "over_capacity"


class SkillBodyPage(NamedTuple):
    """Whole lines of one skill body, never more than the read's capacity in UTF-8 bytes."""

    content: str
    line_offset: int
    line_count: int
    total_lines: int
    total_bytes: int
    next_offset: int | None


class SkillReadRefusal(NamedTuple):
    """Why an exact-key read delivered nothing, with the numbers its message needs."""

    reason: str
    capacity: int
    size_bytes: int | None = None  # the whole body, when the read measured it
    confined: bool = False  # ``capacity`` is the confined project body cap
    line: int | None = None  # over_capacity: the one line that fits no page
    # outside_scope while the scope's catalog is still building: the key may yet
    # resolve once the walk finishes, so the absence is not conclusive.
    incomplete: bool = False


class _ExactRead(NamedTuple):
    """One pass of the exact-key resolution chain, with its refusal classified.

    ``refusal`` is one of the three read reasons when ``content`` is ``None`` and
    empty when a body was delivered. ``incomplete`` marks an outside-scope miss
    taken while the scope's catalog was still building.
    """

    content: str | None
    refusal: str
    confined: bool
    incomplete: bool = False


def _page_skill_body(
    body: str, *, offset: int | None, limit: int | None, capacity: int
) -> SkillBodyPage | SkillReadRefusal:
    """Deliver ``body`` whole, or the whole lines from ``offset`` that fit ``capacity``.

    Lines rather than bytes: a byte window can split a multi-byte character or a
    sentence, and the file and transcript readers this tool sits beside already
    page in lines, so an agent carries one unit across them. Every returned line
    is complete; the page stops at the last line that still fits. Only ``"\\n"``
    separates lines, because the decoder has already folded every newline form
    to it, so the count matches what a file reader shows for the same file.
    Every size here is of the body AS DELIVERED -- UTF-8 bytes of that decoded
    text -- never of the file on disk: the capacity is a budget on what one
    response carries, and a CRLF checkout is larger on disk than what is sent.

    The body may be as large as the shared file safety cap, so nothing here
    materializes it line by line: the count is a scan, the page start is a walk
    of newline positions, and only the lines of the page itself are copied out.
    """
    total_bytes = len(body.encode("utf-8"))
    if offset is None and limit is None:
        if total_bytes > capacity:
            return SkillReadRefusal(SKILL_READ_OVER_CAPACITY, capacity, size_bytes=total_bytes)
        total_lines = _skill_body_line_count(body)
        return SkillBodyPage(body, 0, total_lines, total_lines, total_bytes, None)
    total_lines = _skill_body_line_count(body)
    start = max(0, offset or 0)
    if start >= total_lines:
        # Past the last line: an empty page that still names the count, which is
        # what the renderer prints for any page with no lines.
        return SkillBodyPage("", start, 0, total_lines, total_bytes, None)
    position = 0
    for _ in range(start):
        position = body.find("\n", position) + 1
    taken: list[str] = []
    spent = 0
    most = None if limit is None else max(1, limit)
    while position < len(body) and (most is None or len(taken) < most):
        newline = body.find("\n", position)
        end = len(body) if newline < 0 else newline + 1
        line = body[position:end]
        size = len(line.encode("utf-8"))
        if spent + size > capacity:
            if not taken:
                return SkillReadRefusal(
                    SKILL_READ_OVER_CAPACITY, capacity, size_bytes=size, line=start
                )
            break
        taken.append(line)
        spent += size
        position = end
    end_line = start + len(taken)
    return SkillBodyPage(
        "".join(taken),
        start,
        len(taken),
        total_lines,
        total_bytes,
        end_line if end_line < total_lines else None,
    )


def _skill_body_line_count(body: str) -> int:
    """Lines in ``body`` as a file reader counts them: a final unterminated line counts."""
    return body.count("\n") + (1 if body and not body.endswith("\n") else 0)


#: Argument names under which file-reading tools carry a flat target. A name
#: that is absent simply yields no candidate. kiro-cli's own `read` batches its
#: targets under ``operations`` instead (see ``_tool_read_path_candidates``).
_TOOL_READ_PATH_KEYS = ("path", "file_path", "filePath", "paths", "files")

#: A whitespace/quote-delimited token ending in the skill basename — how a skill
#: read appears inside a shell command (``cat /x/SKILL.md``). Anchored on the
#: basename so it cannot match an arbitrary argument.
_SHELL_SKILL_PATH_RE = re.compile(r"""[^\s"'|;&><]+SKILL\.md""")


#: Shell commands that deliver a file's CONTENT to the model. Deliberately
#: narrow: the ledger counts bodies that reached the model, so a command that
#: merely names a path — ``rm``, ``mv``, ``wc``, ``chmod`` — earns nothing, and
#: neither does ``grep``, which emits matching lines rather than the body.
#: ``head``/``tail`` deliver a prefix, which is still a body the model read.
_SHELL_READ_VERBS = frozenset({"cat", "bat", "head", "tail", "less", "more", "view", "type"})

#: Tools whose result hands the model a file's content. ``grep``/``glob`` are
#: read-KIND but return matches and names, not bodies, so they are excluded for
#: the same reason ``grep`` is above.
_CONTENT_READ_TOOLS = frozenset({"fs_read", "read", "read_file", "readFile"})

#: Splits a shell command into independently-invoked segments, so the verb that
#: applies to a given path is the one that precedes it in ITS segment — without
#: this, ``cat a.txt && rm x/SKILL.md`` would read as a ``cat`` of the skill.
_SHELL_SEGMENT_RE = re.compile(r"(?:\|\||&&|[;|&\n]|\$\(|`)")


def _decode_skill_text(raw: bytes, *, strict: bool = True) -> str:
    """Decode SKILL.md bytes with ``read_text``'s newline handling.

    These reads take bytes rather than ``read_text`` so containment can be checked
    on the descriptor actually opened. ``read_text`` opens in TEXT mode and
    performs universal-newline translation; a bytes read does not. Git checks out
    CRLF on Windows, so without this every frontmatter key would carry a trailing
    ``\r``, nothing would match ``always`` or ``pinned``, and skill bodies would
    silently stop being injected there while Linux and macOS looked fine.

    ``utf-8-sig`` for the same reason: a UTF-8 file saved "with BOM" (Notepad and
    other Windows editors) is valid UTF-8 whose first character is U+FEFF, which
    is not content. Left in, it sits in front of the ``---`` fence, so the
    frontmatter grammar (column 0, position 0) finds no block and the skill lists
    with no metadata -- and the mark itself lands in the injected body. The codec
    strips one leading mark and is otherwise plain UTF-8: a file without one
    decodes byte-for-byte as before, and a file that is not UTF-8 at all (UTF-16,
    which opens with ``0xFF``) still raises under *strict*.

    *strict* decoding propagates invalid UTF-8, which a WRITER must hear
    (``update_auto_skill`` carries version metadata across a rewrite). Callers
    that only render text pass ``strict=False``.
    """
    text = raw.decode("utf-8-sig") if strict else raw.decode("utf-8-sig", errors="replace")
    # Universal newlines, matching TEXT-mode reads: CRLF and lone CR both fold.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _trusted_project_key(self, project_dir: str | Path | None) -> str:
    """Canonical key of *project_dir* when its skills may load, else ``""``.

    Folding the trust verdict into the cache key — rather than caching it
    alongside the results — is what makes a revoke take effect on the next
    message instead of after the TTL: withdrawing trust changes the key back
    to ``""``, which selects the project-free cache slot immediately.

    Costs one ``realpath`` plus one cached ``stat`` when a project is set,
    and nothing at all when it is not.
    """
    if project_dir is None:
        return ""
    key = skill_trust.canonical_key(project_dir)
    allowed = key is not None and skill_trust.is_key_trusted(key)
    if (
        not allowed
        and not self._project_skills_unsupported_warned
        and not skill_trust.project_skill_traversal_supported()
    ):
        # Without the no-follow directory-descriptor walk (Windows) the gate
        # refuses every project before touching its path, so the operator
        # otherwise sees no skills, no trust prompt and no reason. Name the
        # platform limit, not a project: probing whether `.kiro/skills`
        # exists would be the very path lookup the gate refuses to make.
        # Once per loader, and silent when the operator switched it off.
        self._project_skills_unsupported_warned = True
        if skill_trust.project_skills_enabled():
            logger.warning(
                "project skills (<project>/.kiro/skills) are not loaded on this "
                "platform: it lacks the no-follow directory-descriptor traversal "
                "the project-skill trust gate requires, so no project can be "
                "trusted here and no trust prompt is offered; global skills are "
                "unaffected"
            )
    self._audit_project_skill_enforcement(project_dir, key, allowed)
    if not allowed:
        return ""
    # `allowed` is only true when key is not None; assert for the type checker.
    assert key is not None
    return key


def _audit_project_skill_enforcement(
    self, project_dir: str | Path, key: str | None, allowed: bool
) -> None:
    """Record the enforcement outcome once per directory per process.

    Grant and revoke are audited where the operator acts; this records where
    that authority is USED, so "what did this session load, and on whose
    say-so" is answerable from the log rather than inferred.

    Deliberately NOT per call. This runs on every message via
    ``get_triggered_skills``, and a per-message governance event would bury the
    events that matter while adding hot-path cost to every message.
    Keyed on (canonical key, outcome) so a new directory, or the
    same directory after the feature switch is flipped, is recorded again --
    a second message about an unchanged decision is not.

    ``critical=False``: this is a record, not an audit-or-deny gate. A chat
    turn must not die because the SEL is unwritable, and the authority being
    exercised was already written synchronously when consent was given.
    """
    marker = (key or str(project_dir), allowed)
    if marker in self._audited_projects:
        return
    try:
        sel().log_governance_decision(
            session_key="",
            tool_name="skills",
            scope="project_skills",
            item=key or str(project_dir),
            outcome="allowed" if allowed else "denied",
            rule="project_skills_trust_enforced",
            reason=(
                "project skills admitted for a granted directory"
                if allowed
                else (
                    "project skills withheld: this platform lacks no-follow " "directory traversal"
                    if not skill_trust.project_skill_traversal_supported()
                    else "project skills withheld: no grant, or the feature is off"
                )
            ),
            critical=False,
        )
        self._audited_projects.add(marker)
    except Exception:  # noqa: BLE001 — an unwritable log must not fail a turn
        logger.warning("could not audit project-skills enforcement", exc_info=True)


def _read_enumerated_skill_bytes(
    self,
    path: Path,
    within: str | None,
    *,
    max_bytes: int | None = None,
    refusal_reasons: list[str] | None = None,
    canonical_root: str | None = None,
) -> bytes | None:
    """Read a file `_iter` enumerated, re-checking the root it was vetted against.

    THE single read point for enumerated skills. `_iter` is TTL-cached, so a
    path it vetted can be replaced by a link out of the granted project before
    anyone reads it; and the containment that made it acceptable is only known
    at enumeration time. This re-checks it against the recorded root, on the
    descriptor actually opened rather than on the path string.

    Returns ``None`` when the file must not be served -- escaped its root, is
    a link out, is not a regular file, is hardlinked, or exceeds the size cap.
    ``None`` is the same answer every caller already handles for "no
    metadata" / "no body", so refusing degrades a row rather than failing a
    turn.

    A path with no recorded root (global skills dir, extra paths, edition
    roots) is read UNCONFINED, which preserves the app-provider symlink that
    `_trusted_skill_roots` exists to allow. External mappings instead carry
    ``canonical_root``: the resolved root admitted during enumeration,
    including an admitted provider target. Never resolve that root again
    after an ancestor swap. Their bodies retain the global read budget.
    """
    if within is None and canonical_root is None:
        # A path this process never walked carries no admission: a row from the
        # stored snapshot came out of an agent-writable crew-home leaf, and the
        # direct read below applies no sensitive-path or UNC screen of its own,
        # so a row naming a link into a credential home would be read as a skill
        # body. The walk's own admission is therefore run on every path that is
        # not already vetted (`_vet_unconfined_path`), at the single point every
        # enumerated read goes through. Fail-closed: a path missing from the
        # vetted sets costs one check, never an unchecked read.
        if not self._vet_unconfined_path(path):
            if refusal_reasons is not None:
                refusal_reasons.append("snapshot_path_refused")
            return None
        # No project grant is involved: the global skills dir, extra paths,
        # edition roots, and the paths writers construct themselves. These
        # are operator-installed, so there is no directory to confine them
        # to -- and taxing them with the hardened reader measurably slowed
        # the per-message listing path (test_skill_listing_cost guards it)
        # and emptied frontmatter on Windows, which stopped anything looking
        # pinned and dropped skill bodies out of the context entirely.
        #
        # A direct read also keeps the failure policy intact for free: an
        # unreadable file raises OSError here, which writers must hear.
        return path.read_bytes()
    try:
        confined_max = (
            hooks_module.MAX_FILE_BYTES
            if max_bytes is None
            else min(max_bytes, hooks_module.MAX_FILE_BYTES)
        )
        raw = safe_read_file_bytes_nolink(
            str(path),
            within_root=canonical_root or within,
            max_bytes=confined_max,
            within_root_is_canonical=canonical_root is not None,
        )
    except FileTooLargeError:
        # A REFUSAL, not an error: an oversized SKILL.md must not abort a
        # chat turn, and the global path applies no cap at all today.
        if refusal_reasons is not None:
            refusal_reasons.append("size_cap")
        logger.warning("Skipping oversized confined skill file: %s", path)
        return None
    if raw is not None:
        return raw
    if refusal_reasons is not None:
        refusal_reasons.append("outside_vetted_root")
    # A confined path is read-only project/provider input. Every refusal,
    # including a file replaced or removed after enumeration, degrades to no
    # metadata/body so one checkout entry cannot abort a chat turn. Writers
    # use the unconfined branch above, where genuine read failures remain loud.
    return None


def _cached_frontmatter(
    self,
    path: Path,
    mtime: float | None = None,
    *,
    within: str | None,
    canonical_root: str | None = None,
    for_write: bool = False,
) -> dict[str, str]:
    """Parse frontmatter with mtime-based caching.

    *mtime* lets a caller that already stat()'d the file reuse that result.
    ``list_skills()`` needs the size from the same stat, and this path runs
    on a worker during context assembly — one syscall per skill, not two.

    Confined project metadata cannot stat by path: `_iter` is TTL-cached,
    so an attacker can replace the enumerated file with a link before this
    call, and statting that link can initiate a Windows UNC connection.
    Those rows are read through the descriptor-pinned reader first and use
    a digest of the admitted bytes as their cache token.

    *for_write* is set by a caller that rewrites the file from what it reads
    here. A refused unconfined read then raises ``PermissionError`` instead of
    answering "no metadata", which would have it rewrite the skill without its
    ``version``, ``pinned``, ``inject_on_trigger`` and ``created_at``.
    """
    if within is not None:
        return self._confined_frontmatter_and_size(path, within)[0]

    key = str(path)
    if mtime is None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
    cached = self._fm_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    # Failures PROPAGATE deliberately. Not every caller is a reader:
    # ``update_auto_skill`` reads this to carry ``created_at``, ``version``,
    # ``pinned`` and ``inject_on_trigger`` across a rewrite, so degrading an
    # unreadable file to "no metadata" here would make it silently drop those
    # and clobber a version snapshot. A reader that would rather show a row
    # than fail catches this at ITS call site instead.
    # Routed through the choke point rather than reading the path directly:
    # a bare read_text here has no containment, no O_NOFOLLOW, no regular-file
    # check and no size cap -- so an out-of-project `description` would reach
    # the injected skills index verbatim and attacker-set `triggers`/`always`
    # would decide what auto-loads.
    raw = self._read_enumerated_skill_bytes(path, within, canonical_root=canonical_root)
    if raw is None:
        if for_write:
            raise PermissionError(f"refusing to read skill metadata for a rewrite: {path}")
        logger.warning("Refusing metadata for a skill outside its vetted root: %s", path)
        return {}
    # A confined path is read-only project/provider metadata: malformed bytes
    # must not abort a chat turn. The unconfined path also serves writers such
    # as update_auto_skill, which must retain strict decoding so a rewrite
    # cannot silently replace undecodable metadata and lose version fields.
    meta = self._parse_frontmatter_text(_decode_skill_text(raw, strict=within is None))
    if within is None:
        meta["_content_digest"] = hashlib.sha256(raw).hexdigest()
    self._fm_cache[key] = (mtime, meta)
    return meta


def _confined_frontmatter_and_size(self, path: Path, within: str) -> tuple[dict[str, str], int]:
    """Read confined metadata before any path-following metadata probe."""
    refusal_reasons: list[str] = []
    raw = self._read_enumerated_skill_bytes(
        path,
        within,
        max_bytes=PROJECT_SKILL_BODY_CAP,
        refusal_reasons=refusal_reasons,
    )
    if raw is None:
        if "size_cap" in refusal_reasons:
            # Keep the path-derived catalog row without retaining attacker
            # metadata. The over-cap sentinel makes every body consumer skip.
            return {}, PROJECT_SKILL_BODY_CAP + 1
        logger.warning("Refusing metadata for a skill outside its vetted root: %s", path)
        return {}, 0

    key = str(path)
    token = hashlib.sha256(raw).digest()
    cached = self._fm_cache.get(key)
    if cached and cached[0] == token:
        return cached[1], len(raw)
    meta = self._parse_frontmatter_text(_decode_skill_text(raw, strict=False))
    self._fm_cache[key] = (token, meta)
    return meta, len(raw)


def load_skill(
    self,
    name: str,
    project_dir: str | Path | None = None,
    *,
    max_bytes: int | None = None,
    refusal_reasons: list[str] | None = None,
) -> str | None:
    """Load a single skill's content by name (supports nested paths).

    *project_dir* additionally allows a body to come from that project's own
    trusted ``<project>/.kiro/skills``. It is probed LAST so precedence
    matches enumeration: a repository cannot serve the body for a name the
    operator already installed globally.

    *refusal_reasons*, when given, receives why a candidate that was found
    could not be served (``size_cap``, or a reader's refusal); a ``None``
    with nothing appended means no tier holds the name at all.
    """
    if not self._safe_name(name):
        return None
    _t0 = time.monotonic()
    skill_file = self._dir / name / "SKILL.md"
    if skill_file.exists():
        content = self._read_global_skill_text(
            skill_file, max_bytes, refusal_reasons=refusal_reasons
        )
        if content is None:
            return None
        self._emit_lazy_load_metric(_t0, hit=True)
        return content
    for extra in self._extra_paths:
        skill_file = extra / name / "SKILL.md"
        if skill_file.exists():
            resolved = validate_file_path(str(skill_file))
            if resolved is None:
                logger.warning("Refusing to load skill from sensitive path: %s", skill_file)
                if refusal_reasons is not None:
                    refusal_reasons.append("sensitive_path")
                continue
            content = self._read_global_skill_text(
                Path(resolved), max_bytes, refusal_reasons=refusal_reasons
            )
            if content is None:
                return None
            self._emit_lazy_load_metric(_t0, hit=True)
            return content
    # A trusted project's own skills, last — same order as _iter_uncached.
    project_key = self._trusted_project_key(project_dir)
    if project_key:
        # Allowlist-only, like ``_resolve_path`` and ``resolve_dollar_skills``:
        # the path comes from the ENUMERATION, never built from *name*. No
        # caller-supplied string reaches a path expression, so a crafted name
        # cannot escape the trusted root.
        #
        # The containment test below is defence in depth, not the primary
        # control: ``_iter_uncached`` already refuses a skills root that
        # links out of the granted directory, so a smuggled entry cannot be
        # in this enumeration to begin with. It is kept because it also
        # states which root this branch is permitted to serve, and because
        # the primary control living in a different method is exactly the
        # kind of coupling a later refactor breaks silently.
        for candidate, skill_file, _within in self._iter(project_dir):
            if candidate != name or not _within_any(str(skill_file), (project_key,)):
                continue
            # The enumeration is TTL-cached, so the path was vetted up to a
            # minute ago: the SKILL.md it names can since have been replaced
            # by a symlink out of the project. Read through the hardened
            # reader, which opens O_NOFOLLOW and fstat()s the descriptor it
            # actually read, and which enforces containment on that same
            # inode rather than on the (now stale) path string.
            # Same choke point as the metadata read, so the two cannot
            # drift apart again -- the previous round hardened this site
            # alone and left its sibling reading the same cached paths
            # unchecked.
            if refusal_reasons is None:
                refusal_reasons = []
            confined_max = (
                PROJECT_SKILL_BODY_CAP
                if max_bytes is None
                else min(max_bytes, PROJECT_SKILL_BODY_CAP)
            )
            raw = self._read_enumerated_skill_bytes(
                skill_file,
                _within,
                max_bytes=confined_max,
                refusal_reasons=refusal_reasons,
            )
            if raw is None:
                if "outside_vetted_root" in refusal_reasons:
                    logger.warning(
                        "Refusing project skill outside its granted root: %s", skill_file
                    )
                break
            # Decoded explicitly: an implicit read would use the platform's
            # locale encoding and mangle non-ASCII bodies on Windows.
            content = _decode_skill_text(raw, strict=False)
            if _html_skill_refused(self._parse_frontmatter_text(content), skill_file):
                break
            self._emit_lazy_load_metric(_t0, hit=True)
            return content
    self._emit_lazy_load_metric(_t0, hit=False)
    return None


def _read_global_skill_text(
    self,
    path: Path,
    max_bytes: int | None,
    *,
    canonical_root: str | None = None,
    refusal_reasons: list[str] | None = None,
) -> str | None:
    """A global skill body, bounded by *max_bytes* and decode-safe.

    ``max_bytes=None`` reads up to the shared file safety cap with strict
    UTF-8, preserving the global listing path's decode behavior. Every body
    read uses the shared validated reader without project confinement:
    sensitive paths, hardlinks and identity changes are refused on the
    descriptor supplying the bytes. The one hardlink admitted is an installed
    package ``SKILL.md`` whose bytes match the installer's RECORD digest
    (:func:`_installed_package_bytes_match`), which is how a hardlinking
    installer lays out the built-in app skills this method serves.

    With a bound, refuse rather than truncate. A caller that asked for at
    most N bytes is deciding whether the body FITS, and half a skill is not
    a smaller skill: it is a body whose instructions stop mid-sentence.
    ``None`` says "this one cannot be delivered", which the caller reports
    instead of silently dropping. *refusal_reasons* records which it was:
    ``size_cap`` when the body is over the bound, ``reader_refused`` when the
    validated reader declined the file itself.

    A page is the one sanctioned partial read, and it is partial only because
    the caller asked for one: :meth:`read_scoped_skill_page` reads the body
    whole under the file safety cap and hands back the whole lines that fit
    one response together with what remains and where the next page starts,
    so the reader is told there is more. A read that asked for no page is
    still refused whole. This function itself never returns part of a body.
    """
    try:
        raw = safe_read_file_bytes_nolink(
            str(path),
            max_bytes=max_bytes,
            within_root=canonical_root,
            within_root_is_canonical=canonical_root is not None,
            admit_hardlinked=_installed_package_bytes_match,
        )
    except FileTooLargeError:
        if max_bytes is None:
            logger.debug("skill body at %s exceeds the shared file-read bound; refusing", path)
        else:
            logger.debug("skill body at %s exceeds the %d byte bound; refusing", path, max_bytes)
        if refusal_reasons is not None:
            refusal_reasons.append("size_cap")
        return None
    if raw is None:
        if refusal_reasons is not None:
            refusal_reasons.append("reader_refused")
        return None
    text = _decode_skill_text(raw, strict=max_bytes is None)
    return None if _html_skill_refused(self._parse_frontmatter_text(text), path) else text


def _emit_lazy_load_metric(t0: float, *, hit: bool) -> None:
    """Best-effort OTEL emit for on-demand skill body loads."""
    try:
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        attrs: dict[str, str | int | bool | float] = {"hit": hit}
        get_recorder().histogram(
            "kirocrew.skill.lazy_load.duration",
            elapsed_ms,
            unit="ms",
            attrs=attrs,
        )
        get_recorder().counter("kirocrew.skill.lazy_load.count", attrs=attrs)
    except Exception:  # never let telemetry break skill loading
        pass


def read_scoped_skill(
    self,
    key: str,
    *,
    only: list[str] | None = None,
    project_dir: str | Path | None = None,
    max_bytes: int = SKILL_READ_CAPACITY,
) -> str | None:
    """Read an exact catalog key, never an ambiguous leaf or caller path.

    The whole body when it fits *max_bytes*, else ``None``: the contract the
    required-skill and ``$key`` activation paths rely on, where a body that does
    not fit is a body to refuse. :meth:`read_scoped_skill_page` is the face that
    also says WHY, and that serves a larger body in pages.
    """
    return self._read_exact_key(
        key, only=only, project_dir=project_dir, max_bytes=max_bytes
    ).content


def read_scoped_skill_page(
    self,
    key: str,
    *,
    only: list[str] | None = None,
    project_dir: str | Path | None = None,
    offset: int | None = None,
    limit: int | None = None,
    capacity: int = SKILL_READ_CAPACITY,
) -> SkillBodyPage | SkillReadRefusal:
    """An exact-key read for the search tool: the whole body, or one page of it.

    *offset* and *limit* are LINES (0-based first line, most lines), the unit
    the file and transcript readers already page in. With neither given the
    body is delivered whole when it fits *capacity* and refused with its size
    when it does not; with either given, as many whole lines from *offset* as
    fit *capacity* (at most *limit*) are delivered with the offset of the next
    page. A refusal says which of three things stopped the read instead of
    naming all three, because the caller acts differently on each.

    The capacity bounds one DELIVERY, not the file. A body over it is read
    again under the bound the whole file has anyway -- the shared file safety
    cap for a global body, ``PROJECT_SKILL_BODY_CAP`` for a confined one --
    so that its size can be reported and its pages served. A confined body is
    never read past the project cap: paging never reads a checkout's file
    past what one read may, so a body over that cap is refused naming the
    project bound, whatever capacity the caller asked for, and offers no page.
    """
    read = self._read_exact_key(key, only=only, project_dir=project_dir, max_bytes=capacity)
    bound = capacity
    if read.refusal == SKILL_READ_OVER_CAPACITY:
        bound = PROJECT_SKILL_BODY_CAP if read.confined else hooks_module.MAX_FILE_BYTES
        if bound > capacity:
            read = self._read_exact_key(key, only=only, project_dir=project_dir, max_bytes=bound)
    if read.content is None:
        if read.confined and read.refusal == SKILL_READ_OVER_CAPACITY:
            return SkillReadRefusal(read.refusal, PROJECT_SKILL_BODY_CAP, confined=True)
        return SkillReadRefusal(read.refusal, bound, incomplete=read.incomplete)
    return _page_skill_body(read.content, offset=offset, limit=limit, capacity=capacity)


def _read_exact_key(
    self,
    key: str,
    *,
    only: list[str] | None,
    project_dir: str | Path | None,
    max_bytes: int,
) -> _ExactRead:
    """Resolve an exact key through the scope and classify why it was refused.

    A key the scoped enumeration does not hold, and that the building-time
    resolver does not admit, is outside the scope; so is a body whose
    ``repo_scope`` this project does not satisfy, because the catalog never
    listed it here. Once the scope holds the key, every failure to deliver its
    bytes is either the size bound (``size_cap``) or the fenced reader's own
    refusal, and never a missing key.
    """
    entry = next(
        (entry for entry in self._scoped_entries(project_dir, only) if entry[0] == key), None
    )
    confined = entry is not None and entry.project_root is not None
    reasons: list[str] = []
    if entry is None:
        content = self._exact_read_while_building(
            key, only, project_dir, max_bytes, refusal_reasons=reasons
        )
    elif entry.mapping_root is not None:
        content = self._read_global_skill_text(
            entry.path,
            max_bytes,
            canonical_root=entry.mapping_root,
            refusal_reasons=reasons,
        )
    else:
        content = self.load_skill(key, project_dir, max_bytes=max_bytes, refusal_reasons=reasons)
    if content is None:
        if "size_cap" in reasons:
            refusal = SKILL_READ_OVER_CAPACITY
        elif entry is not None or reasons:
            refusal = SKILL_READ_UNREADABLE
        else:
            # While the first walk is unfinished the building-time resolver
            # withholds the confined project tier by design, so a miss here
            # cannot tell a correct project key from an absent one.
            building = self.catalog_status(project_dir) == "building"
            return _ExactRead(None, SKILL_READ_OUTSIDE_SCOPE, confined, building)
        return _ExactRead(None, refusal, confined)
    meta = self._parse_frontmatter_text(content)
    if meta.get("repo_scope") and not self._repo_scope_satisfied(meta["repo_scope"], project_dir):
        return _ExactRead(None, SKILL_READ_OUTSIDE_SCOPE, confined)
    return _ExactRead(content, "", confined)
