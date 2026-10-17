"""What an operator reviews before skill text nobody vetted goes live or loads: the pending
auto-skill queue (list, detail, script verdict, update preview and both approve paths) and the
consent picker's catalog of a project's skills. Every string these surfaces serve is redacted
here, which makes this module the redaction sink ``security_posture`` registers for them.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.

Instance fields read: ``_dir``.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import shutil
import stat
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from kiro_crew import pinned_fs, skill_trust
from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import pinned_directory
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.skills_auto_model import (
    _AUTO_NAME_PATTERN,
    _PENDING_DOCUMENT_MAX_BYTES,
    AUTO_SKILL_NAMESPACE,
    _emit_pending_consumed,
)
from kiro_crew.skills_body_reads import PROJECT_SKILL_BODY_CAP, _decode_skill_text
from kiro_crew.skills_candidate_tree import _PENDING_SCRIPT_MAX_DEPTH, _PENDING_SCRIPT_MAX_ENTRIES
from kiro_crew.skills_script_validator import MAX_SCRIPT_BYTES, validate_scripts

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


_VALIDATION_REPORT_MAX_FINDINGS = 16
_VALIDATION_REPORT_MAX_STRING_CHARS = 1024
_VALIDATION_REPORT_TRUNCATION_KEY = "<truncated>"


class PendingApprovalRefused(Exception):
    """A pending-candidate approval was refused, with a machine-readable reason.

    ``reason`` is one of: ``not_found`` (no such candidate), ``live_exists``
    (a live skill already holds the name), ``kind_mismatch`` (an update
    candidate reached the new-skill approve variant, which would promote it
    fresh while its live target stays unchanged), ``script_validation_failed``
    (``report`` carries the redacted ``{filename: [findings]}`` map from
    ``validate_scripts``), ``target_missing`` (an update candidate whose live
    target is gone), ``stale_base`` (an update merged against an older live
    version), ``invalid_layout`` (symlink / unexpected candidate entry),
    ``redaction_failed``, or ``promotion_failed`` (an OS-level I/O failure —
    a read or write, a refused read of the live skill's metadata included).
    Raised by the ``*_checked`` approve variants so
    the dashboard can tell the user WHY the click did nothing.
    """

    def __init__(self, reason: str, report: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.report = report


# Named by the path callers import it from: a traceback renders a raised class as
# ``module.qualname``, so the owner module must not leak into it.
PendingApprovalRefused.__module__ = "kiro_crew.skills"


def catalog_project_skills(self, project_dir: str | Path) -> list[dict]:
    """Return confined project rows without requiring or exercising trust.

    The consent picker must describe a project skill before the operator
    grants it. Project rows therefore cannot use the legacy Kiro workspace
    scanner, which resolves and reads link targets before the loader can
    reject them. This path enumerates through the loader's confined walker
    and reads each row through the descriptor-pinned no-link reader.
    """
    key = skill_trust.canonical_key(project_dir)
    if key is None:
        return []
    skills: list[dict] = []
    for name, skill_file, confined_root in self._iter_uncached(key):
        if confined_root != key:
            continue
        raw = self._read_enumerated_skill_bytes(
            skill_file, confined_root, max_bytes=PROJECT_SKILL_BODY_CAP
        )
        if raw is None:
            continue
        meta = self._parse_frontmatter_text(_decode_skill_text(raw, strict=False))
        description = self._redact_text(meta.get("description", name))
        repo_scope = self._redact_text(meta.get("repo_scope", ""))
        skills.append(
            {
                "confine_root": confined_root,
                "key": name,
                # Preserve the open-standard catalog identity: its display
                # name is the relative directory name, not a frontmatter
                # alias that would expand to a different path.
                "name": name,
                "description": description,
                "path": str(skill_file),
                "dir": str(skill_file.parent),
                "always": meta.get("always", "").strip().lower() == "true",
                "repo_scope": repo_scope,
                # Project paths cannot safely offer a live pointer to the
                # agent, so report the effective forced-body behavior.
                "inject_on_trigger": True,
                "size_bytes": len(raw),
                **self._usage_fields(name),
                "owned": False,
            }
        )
    return skills


def _read_pending_meta(self, slug: str) -> dict:
    mf = self._pending_root() / slug / ".meta.json"
    # Never follow an LLM-planted symlink (could point at a sensitive file).
    if mf.is_symlink():
        return {}
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Recursively redact secrets from LLM-produced metadata before it can
    # surface via the pending list/detail API. The crystallize skill writes
    # .meta.json directly, bypassing the consolidation redaction path, so a
    # credential in ANY (incl. nested) value must be scrubbed here.
    redacted = self._redact_deep(data)
    return redacted if isinstance(redacted, dict) else {}


def list_pending_skills(self) -> list[dict]:
    """Return ``{slug, name, description, triggers, has_scripts, created_at, path}``
    for every staged candidate."""
    root = self._pending_root()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / "SKILL.md").exists():
            continue
        # Only surface canonical slugs. A crystallize direct-write could name
        # the pending dir with credential-shaped text; anything that isn't a
        # canonical single-segment slug is skipped so it can't be serialized
        # to the dashboard as a "slug" (and can't be approved/dismissed by
        # the slug-keyed handlers, which apply the same guard).
        if not _AUTO_NAME_PATTERN.match(child.name):
            continue
        meta = self._read_pending_meta(child.name)
        # Same verdict the approve path will reach, so the card can carry a
        # warning badge WITHOUT the user expanding the row first. The
        # verdict walk is self-defending — descriptor-pinned, budgeted,
        # fail-closed (see _pending_scripts_verdict) — so no candidate-wide
        # pre-walk runs here: an unbudgeted os.walk before the budgeted one
        # would itself be the unbounded per-poll traversal the budgets
        # exist to prevent. ``None`` means the platform cannot compute a
        # trustworthy verdict; the field is omitted rather than serving a
        # false all-clear.
        verdict = self._pending_scripts_verdict(child)
        entry = {
            "slug": child.name,
            "name": meta.get("name", f"{AUTO_SKILL_NAMESPACE}/{child.name}"),
            "description": meta.get("description", ""),
            "triggers": meta.get("triggers", ""),
            "has_scripts": bool(meta.get("has_scripts")),
            "created_at": meta.get("created_at", ""),
            "source": meta.get("source", ""),
            "kind": meta.get("kind", "new"),
            "target": meta.get("target"),
            "base_version": meta.get("base_version"),
            # NB: no on-disk ``path`` — this dict is API-facing (feeds
            # /api/skills/-/pending) and must not leak the server's home
            # / directory layout to dashboard clients.
        }
        if verdict is not None:
            v_ok, v_report = verdict
            entry["script_validation"] = {
                "ok": v_ok,
                "report": self._redact_validation_report(v_report),
            }
        out.append(entry)
    return out


def _redact_text(text: object) -> str:
    """Two-pass redaction for untrusted skill text.

    Project catalog metadata and pending skill detail/approval both reach
    the dashboard from files an untrusted producer can write. Apply the same
    exfiltration-URL and credential passes at those read points so neither
    surface can return secrets or promote them live.
    """
    if not isinstance(text, str):
        return ""
    safe, _ = redact_exfiltration_urls(text)
    safe, _ = redact_credentials(safe)
    return safe


def _redact_deep(self, obj: object) -> object:
    """Recursively redact every string in a nested dict/list structure so a
    credential hidden in a nested ``.meta.json`` value can't reach the
    dashboard unredacted (top-level-only redaction missed those). String
    dict KEYS are redacted too — a prompt-injected key can carry a secret."""
    if isinstance(obj, str):
        return self._redact_text(obj)
    if isinstance(obj, dict):
        return {
            (self._redact_text(k) if isinstance(k, str) else k): self._redact_deep(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [self._redact_deep(v) for v in obj]
    return obj


def _redact_file_in_place(self, fp: Path) -> bool:
    """Redact secrets from a file in place. Returns False if the file could
    not be read or a required rewrite failed — the caller MUST abort
    promotion so an unredacted secret never reaches a live skill."""
    try:
        original = fp.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    safe = self._redact_text(original)
    if safe == original:
        return True
    try:
        fp.write_text(safe, encoding="utf-8")
    except OSError:
        return False
    return True


def _read_candidate_pinned(self, pdir: Path) -> tuple[str, dict, list[dict]] | None:
    """Read a candidate's ``SKILL.md``, ``.meta.json`` and scripts through ONE pin chain.

    The traversal both judges and reads. The screen-then-read shape this replaces
    refused a link that was PRESENT at the check, so a candidate tree the LLM can
    write needed only to have the link absent at the screen and present by the
    ``read_text`` a few statements later, and the contents of a file of its
    choosing were served -- the redaction passes cover credential and exfil-URL
    shapes only, so anything else came back intact. Here every open refuses a link
    at the name itself, so there is no gap between the judgement and the read.

    Returns None when the candidate cannot be read as a plain tree: a link
    anywhere, a non-regular entry, or a directory replaced underneath. "Anywhere"
    is the whole top level, not only the names this reads: the approve path
    refuses an unexpected top-level entry outright, so a detail read that served
    one would be MORE permissive than the approve it exists to inform.

    Two byte caps, one per kind of file. ``SKILL.md`` and ``.meta.json`` are
    documents and read under ``_PENDING_DOCUMENT_MAX_BYTES``, derived from what
    the generator may write; every file under ``scripts/`` is an executable and
    read under ``MAX_SCRIPT_BYTES``, the verdict walk's own per-file cap. Either
    cap refuses the candidate whole: the read is bounded, never partial.
    """
    try:
        pinned = pinned_directory(pdir)
    except OSError:
        return None
    with pinned:
        # Screen the whole top level BEFORE a byte is read, so the refusal this
        # docstring promises is structural rather than a side effect of which
        # names happen to be read. `_collect_scripts_pinned` refuses a link
        # anywhere under `scripts/`; this covers every OTHER top-level name --
        # `.meta.json`, and an unexpected entry the approve path refuses outright,
        # which nothing on this path opens and so nothing else would check. It
        # does not REPLACE the per-name refusals below: it cannot see a swap that
        # happens after it, which is what those catch.
        #
        # Enumeration is BOUNDED by the same entry budget the verdict walk spends,
        # because this runs per request against a tree an agent writes directly: a
        # planted crowd of names would otherwise be materialized in one allocation
        # here, before any later check could refuse it. Over budget refuses the
        # candidate rather than serving a partial view of its directory.
        listed = pinned.names_bounded(_PENDING_SCRIPT_MAX_ENTRIES)
        if listed is None:
            return None
        names = set(listed)
        if any(pinned.is_link(name) for name in names):
            return None
        try:
            # The DOCUMENT cap, not the script cap: a generated procedure may run
            # to AUTO_SKILL_MAX_PROCEDURE_CHARS characters, several times what a
            # bundled script may hold, and a valid candidate must open for review.
            body = pinned.read_text("SKILL.md", max_bytes=_PENDING_DOCUMENT_MAX_BYTES)
        except (OSError, UnicodeDecodeError):
            return None
        meta: dict = {}
        if ".meta.json" in names:
            try:
                raw = pinned.read_text(".meta.json", max_bytes=_PENDING_DOCUMENT_MAX_BYTES)
            except (OSError, UnicodeDecodeError):
                # Unreadable THROUGH THE PIN is a fence signal, not bad content:
                # the name is not the plain file it screened as, which is the same
                # class as SKILL.md failing above, and over the document cap is
                # refused the same way. Refuse the candidate rather than serve it
                # with empty metadata.
                return None
            try:
                parsed = json.loads(raw)
            except ValueError:
                # Malformed JSON is the agent writing nonsense, not a swap. The
                # reviewer still sees SKILL.md and the scripts.
                parsed = None
            if isinstance(parsed, dict):
                # Recursively redact secrets from LLM-produced metadata before it
                # can surface via the pending detail API: the crystallize skill
                # writes ``.meta.json`` directly, bypassing the consolidation
                # redaction path, so a credential in ANY nested value is scrubbed.
                scrubbed = self._redact_deep(parsed)
                meta = scrubbed if isinstance(scrubbed, dict) else {}
        scripts: list[dict] = []
        if "scripts" in names:
            if pinned.is_link("scripts"):
                # NOT redundant with the screen above, and the difference is
                # timing: that screen refuses a link PRESENT when the traversal
                # started, this one refuses a link swapped in while the reads
                # above were running. A tree written by an agent can change
                # between the two.
                return None
            if pinned.is_dir("scripts"):
                sub = pinned.child_if_real_dir("scripts")
                if sub is None:
                    return None
                # ONE budget for the whole subtree, spent the way the verdict walk
                # spends it: entries and aggregate bytes, same constants. Per-call
                # rather than per-directory, or a planted tree of many small
                # directories would each get a fresh allowance.
                budget = {"entries": 0}
                with sub:
                    if not self._collect_scripts_pinned(sub, (), scripts, budget):
                        return None
        return body, meta, scripts


def _pending_scripts_verdict(self, pdir: Path) -> tuple[bool, dict] | None:
    """Cheap pre-approval validation verdict for the pending LIST path.

    Takes the CANDIDATE ROOT: the top-level layout is prechecked against
    the same rules the approve path enforces (see
    :meth:`_candidate_layout_findings_at`) before the ``scripts`` walk, so a
    symlinked ``SKILL.md`` or a stray top-level file fails the verdict
    here exactly as approve would refuse it.

    The candidate root is resolved EXACTLY ONCE: one
    :func:`pinned_fs.open_dir_pinned` call pins it (``O_NOFOLLOW`` on the
    root and every ancestor), and that descriptor is retained for the
    whole verdict — the top-level layout scan reads through it, and the
    ``scripts`` directory is opened RELATIVE to it (``dir_fd``), with an
    identity check that the opened directory is the same inode the
    layout scan stat-ed. No name under the candidate is ever re-resolved
    from a path, so a candidate root swapped between any two steps —
    whether for a symlink (refused at the pinned open) or for a
    different REAL directory renamed over it (unreachable, because no
    second resolution exists to land on it) — cannot redirect any part
    of the verdict into another tree. Every entry below ``scripts`` is
    likewise stat-ed, opened and read relative to the walk's own
    descriptors.

    The verdict fails CLOSED on everything the approve path would refuse:
    a symlink entry is an invalid layout, an oversized script is flagged
    from its size alone (its bytes are never loaded — this runs on every
    dashboard poll, and a crystallize direct-write can plant files the
    staging cap never saw), an unreadable or undecodable script is a
    finding rather than a silent omission (approve refuses such a
    candidate at redaction, so ``ok: true`` would be a false all-clear),
    and an unexpected walk error degrades to a failing
    verdict-unavailable finding rather than blanking the caller's whole
    list. Small, decodable scripts get the real ``validate_scripts`` run,
    matching the approve path's verdict.

    Returns ``None`` on a platform without descriptor-relative opens,
    BEFORE the candidate is touched at all. A link/junction check
    followed by a by-name scan races with replacement of the candidate
    root: even a refusal can expose the target's filenames. On Windows
    the pre-click "fails validation" badge disappears entirely, because
    no trustworthy verdict can be computed without descriptor-relative
    opens. The caller omits the field; approve remains the authority at
    click time.
    """
    if not pinned_fs.supports_pinned_walk():
        return None
    try:
        root_fd = pinned_fs.open_dir_pinned(pdir, what="pending candidate root")
    except pinned_fs.PinnedPathRefusal:
        return False, {"<candidate>": ["invalid layout: candidate root is not a real directory"]}
    except OSError:
        return False, {"<candidate>": ["verdict unavailable: candidate unreadable"]}
    try:
        return self._pending_scripts_verdict_at(root_fd)
    finally:
        os.close(root_fd)


def _pending_scripts_verdict_at(self, root_fd: int) -> tuple[bool, dict] | None:
    """The verdict body, entirely relative to the retained root descriptor."""
    layout = self._candidate_layout_findings_at(root_fd)
    if layout:
        return False, {"<candidate>": layout}
    sst = pinned_fs.stat_at(root_fd, "scripts")
    if sst is None:
        # No scripts directory at all — nothing to validate.
        return True, {}
    if stat.S_ISLNK(sst.st_mode) or not stat.S_ISDIR(sst.st_mode):
        return False, {"<candidate>": ["invalid layout: 'scripts' is not a real directory"]}
    if not pinned_fs.supports_pinned_tree_walk():
        return None
    try:
        scripts_fd = os.open("scripts", pinned_fs.dir_flags(), dir_fd=root_fd)
    except OSError:
        return False, {"<candidate>": ["invalid layout: contains a symlink"]}
    try:
        # The OPENED directory must be the same inode the stat above
        # described — a 'scripts' swapped between the stat and the open
        # (even for another real directory; both live under the pinned
        # root) fails the verdict closed instead of being walked as if
        # it were the audited one.
        ost = os.fstat(scripts_fd)
        if (ost.st_ino, ost.st_dev) != (sst.st_ino, sst.st_dev):
            os.close(scripts_fd)
            return False, {"<candidate>": ["invalid layout: entry changed during scan"]}
    except OSError:
        os.close(scripts_fd)
        return False, {"<candidate>": ["verdict unavailable: candidate unreadable"]}
    scripts: list[dict] = []
    extra: dict[str, list[str]] = {}
    # Hard budgets for the whole walk: this runs on every dashboard poll,
    # and a crystallize direct-write can plant MANY small files — or many
    # nested directories — that the per-file size cap never bounds.
    # Directories count against the same entry budget and recursion is
    # depth-capped, so a planted tree can neither grow the traversal
    # without limit nor raise RecursionError into the caller's degraded
    # ok-true fallback. Exceeding any budget stops the walk immediately
    # and OMITS the verdict (see the breach return below) — never a false
    # refusal claim; the approve path remains the authority on the full
    # set.
    max_files = _PENDING_SCRIPT_MAX_ENTRIES
    max_depth = _PENDING_SCRIPT_MAX_DEPTH
    budget = {"files": 0, "bytes": 0, "breached": False}
    max_total_bytes = max_files * MAX_SCRIPT_BYTES

    def _breach(reason: str) -> None:
        budget["breached"] = True
        extra.setdefault("<candidate>", []).append(reason)

    def _walk(fd: int, prefix: str, depth: int) -> None:
        # Collect names LAZILY with a cap before sorting: an eager
        # sorted(os.listdir(fd)) materializes an attacker-sized directory
        # in one allocation before any budget applies, which is the exact
        # per-poll exhaustion the budgets exist to prevent. Scanning stops
        # at the entry budget, so at most max_files+1 names are ever held.
        names: list[str] = []
        with os.scandir(fd) as scanner:
            for entry in scanner:
                names.append(entry.name)
                if len(names) > max_files:
                    _breach(f"too many scripts: over {max_files} entries")
                    return
        for nm in sorted(names):
            if budget["breached"]:
                return
            rel = f"{prefix}{nm}"
            try:
                est = os.stat(nm, dir_fd=fd, follow_symlinks=False)
            except OSError:
                extra.setdefault(rel, []).append("unreadable script: stat failed")
                continue
            if stat.S_ISLNK(est.st_mode):
                extra.setdefault(rel, []).append("invalid layout: entry is a symlink")
            elif stat.S_ISDIR(est.st_mode):
                # Directories spend the same entry budget as files, and
                # recursion is depth-capped — a planted tree of many or
                # deeply nested dirs must not reintroduce the unbounded
                # per-poll traversal (or a RecursionError that the
                # caller's fallback would degrade to a false all-clear).
                budget["files"] += 1
                if budget["files"] > max_files:
                    _breach(f"too many scripts: over {max_files} entries")
                    return
                if depth >= max_depth:
                    _breach(f"scripts tree too deep: over {max_depth} levels")
                    return
                try:
                    sub = os.open(nm, pinned_fs.dir_flags(), dir_fd=fd)
                except OSError:
                    extra.setdefault(rel, []).append("invalid layout: entry is a symlink")
                    continue
                try:
                    _walk(sub, f"{rel}/", depth + 1)
                finally:
                    os.close(sub)
            elif stat.S_ISREG(est.st_mode):
                if est.st_size > MAX_SCRIPT_BYTES:
                    extra.setdefault(rel, []).append(
                        f"too large: {est.st_size} bytes > {MAX_SCRIPT_BYTES} cap"
                    )
                    continue
                budget["files"] += 1
                budget["bytes"] += est.st_size
                if budget["files"] > max_files or budget["bytes"] > max_total_bytes:
                    _breach(
                        f"too many scripts: over {max_files} entries or "
                        f"{max_total_bytes} aggregate bytes"
                    )
                    return
                try:
                    # O_NONBLOCK: O_NOFOLLOW rejects a swapped symlink but
                    # NOT a swapped FIFO, and a blocking O_RDONLY open of a
                    # writerless FIFO wedges this poll thread forever. For
                    # a regular file O_NONBLOCK is a no-op on open and
                    # read, so the flag costs nothing on the honest path.
                    rfd = os.open(
                        nm,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                        dir_fd=fd,
                    )
                except OSError:
                    extra.setdefault(rel, []).append("unreadable script: open failed")
                    continue
                try:
                    # The OPENED descriptor must be the same regular inode
                    # the stat above described: a name swapped between the
                    # stat and the open (FIFO, device, replaced file) fails
                    # the verdict closed instead of being read as if it
                    # were the audited entry.
                    ost = os.fstat(rfd)
                    if not stat.S_ISREG(ost.st_mode) or (ost.st_ino, ost.st_dev) != (
                        est.st_ino,
                        est.st_dev,
                    ):
                        extra.setdefault(rel, []).append(
                            "invalid layout: entry changed during scan"
                        )
                        continue
                    raw = os.read(rfd, MAX_SCRIPT_BYTES + 1)
                except OSError:
                    extra.setdefault(rel, []).append("unreadable script: read failed")
                    continue
                finally:
                    os.close(rfd)
                try:
                    scripts.append({"filename": rel, "content": raw.decode("utf-8")})
                except UnicodeDecodeError:
                    extra.setdefault(rel, []).append("unreadable script: not valid UTF-8")

    try:
        try:
            _walk(scripts_fd, "", 0)
        finally:
            os.close(scripts_fd)
        if budget["breached"]:
            # A breached budget means the LIST path declined to do the
            # work, not that approve will refuse: approve's collector is
            # unbudgeted, so a candidate with 65 small clean scripts
            # approves fine. Claiming `ok: false` here makes the badge and
            # hint over-promise a refusal that never comes — the honest
            # verdict is NO verdict (the caller omits the field, exactly
            # like a platform that cannot pin the walk). The exhaustion
            # defense is unchanged: the walk already STOPPED at its budget.
            return None
        v_ok, v_report = validate_scripts(scripts)
        if v_ok:
            # Approve validates TWICE: raw, then again after redacting in
            # place (a credential-shaped token redacts into broken
            # syntax and is refused). Mirror the second stage on in-memory
            # copies so a script that is clean raw but breaks under
            # redaction does not read `ok: true` for a click approve
            # refuses. Verdict-side only — nothing on disk is touched.
            redacted = [
                {"filename": s["filename"], "content": self._redact_text(s["content"])}
                for s in scripts
            ]
            v_ok, v_report = validate_scripts(redacted)
    except Exception:
        # One broken candidate must not blank the caller's whole pending
        # list, and an unvalidated candidate must not read clean: degrade
        # to a failing verdict-unavailable finding.
        return False, {"<candidate>": ["verdict unavailable: unexpected walk error"]}
    for fn, findings in extra.items():
        v_report.setdefault(fn, []).extend(findings)
    return (v_ok and not extra), v_report


def get_pending_skill(self, slug: str) -> dict | None:
    """Return full pending-candidate detail incl. SKILL.md body + script bodies."""
    if not self._is_pending_slug_safe(slug):
        return None
    pdir = self._pending_root() / slug
    if not (pdir / "SKILL.md").exists():
        # The ordinary "no such candidate" answer. Not a security check -- the
        # pinned read below is -- so probing by name here costs nothing.
        return None
    # ONE descriptor-pinned traversal both validates and reads: the body, the
    # metadata and every script come back from opens that refuse a link AT THE
    # NAME, so there is no screen-then-read gap for a candidate to flip a name
    # through and no way to point the detail API at a file outside the candidate.
    read = self._read_candidate_pinned(pdir)
    if read is None:
        logger.warning("Refusing to read pending %s: candidate is not a plain tree of files", slug)
        return None
    body, meta, scripts = read
    # Same hardened verdict as the pending LIST (descriptor-pinned walk,
    # fail-closed on unreadable/oversized entries) — deriving it from the
    # display collection instead would let a silently omitted unreadable
    # script present a clean detail verdict while approve refuses. The
    # display ``scripts`` list below is unchanged. ``None`` (platform
    # cannot compute a trustworthy verdict) omits the field rather than
    # serving a false all-clear.
    verdict = self._pending_scripts_verdict(pdir)
    for s in scripts:
        s["filename"] = self._redact_text(s.get("filename", ""))
        s["content"] = self._redact_text(s.get("content", ""))
    detail = {
        "slug": slug,
        "name": meta.get("name", f"{AUTO_SKILL_NAMESPACE}/{slug}"),
        "meta": meta,
        "kind": meta.get("kind", "new"),
        "target": meta.get("target"),
        "base_version": meta.get("base_version"),
        "content": self._redact_text(body),
        "scripts": scripts,
    }
    if verdict is not None:
        v_ok, v_report = verdict
        detail["script_validation"] = {
            "ok": v_ok,
            "report": self._redact_validation_report(v_report),
        }
    return detail


def _restore_redacted(backup: dict[Path, bytes]) -> None:
    """Write each file in ``backup`` back to its snapshotted bytes. A write that fails is
    skipped, so one unwritable file does not keep the others from being restored."""
    for fp, original in backup.items():
        try:
            fp.write_bytes(original)
        except OSError:
            pass


def _validate_and_redact_candidate(self, src: Path, name: str) -> dict[Path, bytes]:
    """Re-validate + redact a candidate's SKILL.md and scripts IN PLACE.

    Shared by ``approve_pending_skill_checked`` and ``approve_pending_update_checked`` so
    both enforce the identical discipline: validate every script (covers
    crystallize direct-writes), snapshot each target's ORIGINAL bytes, redact
    in place, then re-validate scripts (redacting a credential-shaped token
    can break syntax). On ANY failure the originals are restored and a
    ``PendingApprovalRefused`` is raised naming the reason — a validation
    failure carries the redacted ``{filename: [findings]}`` report so the
    refusal can tell the reviewer WHAT was flagged — and a rejected
    candidate is never left corrupted. On success returns the
    ``{path: original_bytes}`` snapshot so the caller can restore on a
    LATER failure (e.g. a failed move / snapshot).
    """
    sdir_src = src / "scripts"
    # Pre-redaction script validation.
    if sdir_src.is_dir():
        ok, report = validate_scripts(self._collect_scripts(sdir_src))
        if not ok:
            logger.warning("Refusing to approve %s: script validation failed: %s", name, report)
            raise PendingApprovalRefused(
                "script_validation_failed", report=self._redact_validation_report(report)
            )
    # Snapshot each target FIRST so an abort after partial in-place redaction
    # restores the candidate's ORIGINAL bytes.
    redact_targets = [src / "SKILL.md"]
    if sdir_src.is_dir():
        for root, _dirs, files in os.walk(sdir_src):
            for nm in files:
                fp = Path(root) / nm
                if fp.is_file() and not fp.is_symlink():
                    redact_targets.append(fp)
    redact_backup: dict[Path, bytes] = {}
    for fp in redact_targets:
        try:
            redact_backup[fp] = fp.read_bytes()
        except OSError:
            pass

    for fp in redact_targets:
        if not self._redact_file_in_place(fp):
            _restore_redacted(redact_backup)
            logger.warning(
                "Refusing to approve %s: could not redact %s before promotion", name, fp.name
            )
            raise PendingApprovalRefused("redaction_failed")
    # Re-validate scripts AFTER redaction so a broken/altered helper never
    # goes live and the pending draft is not corrupted.
    if sdir_src.is_dir():
        ok, report = validate_scripts(self._collect_scripts(sdir_src))
        if not ok:
            _restore_redacted(redact_backup)
            logger.warning(
                "Refusing to approve %s: scripts invalid after redaction: %s", name, report
            )
            raise PendingApprovalRefused(
                "script_validation_failed", report=self._redact_validation_report(report)
            )
    return redact_backup


def _redact_validation_report(self, report: dict) -> dict:
    """Bound and redact reports at retention for every pending HTTP surface.

    Keep at most the verdict's script-entry budget, with independently
    bounded finding lists and strings. Redact BEFORE shortening strings:
    cutting first could turn a credential into an unrecognised fragment.
    One fixed summary reports omitted entries (including redacted-key
    collisions), findings within retained entries, and characters within
    retained strings. Entries beyond the population cap are never redacted
    or copied.
    """
    safe: dict[str, list[str]] = {}
    omitted_entries = max(0, len(report) - _PENDING_SCRIPT_MAX_ENTRIES)
    omitted_findings = 0
    omitted_chars = 0
    for fn, findings in islice(report.items(), _PENDING_SCRIPT_MAX_ENTRIES):
        redacted_key = self._redact_text(str(fn))
        key = redacted_key[:_VALIDATION_REPORT_MAX_STRING_CHARS]
        # Redaction and shortening can collapse distinct filenames. Never
        # overwrite a prior entry or let a filename steal the summary slot.
        if key in safe or key == _VALIDATION_REPORT_TRUNCATION_KEY:
            omitted_entries += 1
            continue
        omitted_chars += len(redacted_key) - len(key)
        values: list[str] = []
        omitted_findings += max(0, len(findings) - _VALIDATION_REPORT_MAX_FINDINGS)
        for finding in islice(findings, _VALIDATION_REPORT_MAX_FINDINGS):
            redacted = self._redact_text(str(finding))
            value = redacted[:_VALIDATION_REPORT_MAX_STRING_CHARS]
            omitted_chars += len(redacted) - len(value)
            values.append(value)
        safe[key] = values
    if omitted_entries or omitted_findings or omitted_chars:
        safe[_VALIDATION_REPORT_TRUNCATION_KEY] = [
            "too large: validation report truncated; "
            f"omitted {omitted_entries} script entries, "
            f"{omitted_findings} findings from retained entries, "
            f"{omitted_chars} characters from retained strings"
        ]
    return safe


def preview_pending_update(self, slug: str) -> dict | None:
    """Return an approval preview for a pending UPDATE candidate.

    Produces ``{live_body, proposed_body, diff, from_version, to_version,
    base_version, stale_base}`` where ``proposed_body`` is the EXACT content
    ``approve_pending_update_checked`` would write (same frontmatter rewrite), so the
    reviewer's diff is what approval actually does — not raw candidate text
    whose ``name`` / ``created_at`` / ``version`` lines are rewritten anyway.

    Returns ``None`` when the slug is unsafe, the candidate is missing or is
    not an update, or its target is not a live auto-skill whose metadata
    can be read (a refused rewrite read included). Read-only:
    never mutates the candidate or the live skill.
    """
    if not self._is_pending_slug_safe(slug):
        return None
    src = self._pending_root() / slug
    cand_file = src / "SKILL.md"
    if not cand_file.exists() or cand_file.is_symlink():
        return None
    meta = self._read_pending_meta(slug)
    if meta.get("kind") != "update":
        return None
    target = meta.get("target")
    if not isinstance(target, str) or not target:
        return None
    target_slug = self._auto_slug_from_name(target)
    if not self._is_pending_slug_safe(target_slug):
        return None
    live_file = self._dir / AUTO_SKILL_NAMESPACE / target_slug / "SKILL.md"
    if not live_file.exists():
        return None
    target_name = f"{AUTO_SKILL_NAMESPACE}/{target_slug}"
    # Read the live body through the guarded reader (symlink + sensitive-path
    # + inside-tree checks) rather than touching the file directly — this
    # feeds the dashboard API.
    live_body = self.read_auto_skill_body(target_name)
    if live_body is None:
        return None
    try:
        cand_body = cand_file.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        current_version = self.get_auto_skill_version(target_name)
        _live_fm = self._cached_frontmatter(live_file, within=None, for_write=True)
    except OSError:
        return None
    proposed_body = self._rewrite_update_frontmatter(
        cand_body,
        target_name=target_name,
        created_at=_live_fm.get("created_at", ""),
        version=current_version + 1,
        pinned=str(_live_fm.get("pinned", "")).strip().lower() in ("true", "1", "yes"),
        pointer_only=str(_live_fm.get("inject_on_trigger", "")).strip().lower() == "false",
    )
    # Redact both sides: this feeds the dashboard API, and the candidate is
    # only redacted in place at approve time (so an un-approved draft may
    # still hold a credential-shaped token).
    live_safe = self._redact_text(live_body)
    proposed_safe = self._redact_text(proposed_body)
    diff = "".join(
        difflib.unified_diff(
            live_safe.splitlines(keepends=True),
            proposed_safe.splitlines(keepends=True),
            fromfile=f"{target_name} (v{current_version}, live)",
            tofile=f"{target_name} (v{current_version + 1}, proposed)",
            n=3,
        )
    )
    raw_base = meta.get("base_version")
    return {
        "live_body": live_safe,
        "proposed_body": proposed_safe,
        "diff": diff,
        "from_version": current_version,
        "to_version": current_version + 1,
        "base_version": raw_base,
        "stale_base": isinstance(raw_base, int) and raw_base != current_version,
    }


def approve_pending_update_checked(self, slug: str) -> str:
    """Promote a pending UPDATE candidate over its live target auto-skill.

    Preconditions (all checked BEFORE any live mutation; a failure here
    leaves BOTH the live skill and the candidate untouched and raises
    ``PendingApprovalRefused`` with the reason): the slug is safe, the
    candidate has a ``SKILL.md``, its ``.meta.json`` has
    ``kind == "update"``, and ``target`` names an EXISTING live auto
    skill. Then: the shared symlink/unexpected-entry guard runs, scripts are
    re-validated, and SKILL.md + scripts are redacted in place (originals
    restored on failure).

    Promotion: snapshot the current live ``SKILL.md`` to
    ``auto/<target>/.versions/v<N>-SKILL.md`` (N = current live version),
    write the candidate over live with frontmatter rewritten (preserve live
    ``created_at``, ``name`` = ``auto/<target>``, ``version`` = N+1), move the
    candidate scripts into the live ``scripts/`` (exec bit set on POSIX),
    prune ``.versions`` to the newest ``MAX_SKILL_VERSIONS``, delete the
    pending dir, and SEL-audit. Returns ``auto/<target>`` on success.
    """
    if not self._is_pending_slug_safe(slug):
        raise PendingApprovalRefused("not_found")
    src = self._pending_root() / slug
    if not (src / "SKILL.md").exists():
        raise PendingApprovalRefused("not_found")
    meta = self._read_pending_meta(slug)
    if meta.get("kind") != "update":
        raise PendingApprovalRefused("not_found")
    target = meta.get("target")
    if not isinstance(target, str) or not target:
        raise PendingApprovalRefused("target_missing")
    target_slug = self._auto_slug_from_name(target)
    if not self._is_pending_slug_safe(target_slug):
        raise PendingApprovalRefused("target_missing")
    live_dir = self._dir / AUTO_SKILL_NAMESPACE / target_slug
    live_skill = live_dir / "SKILL.md"
    if not live_skill.exists():
        logger.warning(
            "Refusing to approve update %s: target %r is not a live auto skill", slug, target
        )
        raise PendingApprovalRefused("target_missing")
    target_name = f"{AUTO_SKILL_NAMESPACE}/{target_slug}"
    # The LIVE side is a write target here (unlike approve_pending_skill_checked, which
    # moves into a fresh dest), so it needs its own symlink guard: a symlinked
    # ``scripts/`` (or any symlinked entry) would let ``mkdir``/``copy2`` follow
    # the link and write candidate content OUTSIDE the skill directory.
    if self._candidate_has_symlink(live_dir):
        logger.warning(
            "Refusing to approve update %s: live skill directory contains a symlink",
            target_name,
        )
        raise PendingApprovalRefused("invalid_layout")
    # Shared symlink + unexpected-entry rejection.
    if not self._candidate_layout_ok(src, target_name):
        raise PendingApprovalRefused("invalid_layout")
    # One verdict, two surfaces: same consult as
    # ``approve_pending_skill_checked`` — a computable failing verdict
    # refuses with the badge's own report, so the update click can never
    # disagree with the badge it stood behind.
    _fold = self._pending_scripts_verdict(src)
    if _fold is not None:
        _fold_ok, _fold_report = _fold
        if not _fold_ok:
            logger.warning(
                "Refusing to approve update %s: the pending verdict this candidate's "
                "badge serves reports failing findings",
                target_name,
            )
            raise PendingApprovalRefused(
                "script_validation_failed",
                report=self._redact_validation_report(_fold_report),
            )
    # Read the live version and frontmatter BEFORE the candidate is redacted
    # in place. These are rewrite reads, so a live path that does not vet
    # raises rather than answering "no metadata", and that refusal must leave
    # the candidate byte-identical to what was staged.
    try:
        current_version = self.get_auto_skill_version(target_name)
        live_fm = self._cached_frontmatter(live_skill, within=None, for_write=True)
    except OSError:
        logger.warning(
            "Refusing to approve update %s: could not read the live skill's metadata",
            target_name,
        )
        raise PendingApprovalRefused("promotion_failed")
    # Re-validate + redact the candidate in place (restores originals on
    # fail, raising PendingApprovalRefused with the reason).
    redact_backup = self._validate_and_redact_candidate(src, target_name)

    # Compute the new live content from the redacted candidate BEFORE any
    # live mutation — a read failure aborts with live + candidate intact.
    try:
        candidate_body = (src / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        _restore_redacted(redact_backup)
        raise PendingApprovalRefused("promotion_failed")
    # Snapshot under a number that is guaranteed free, so an earlier snapshot
    # can never be destroyed by drifted numbering.
    versions_dir = self._versions_root(target_slug)
    snapshot_version = (
        self._resolve_snapshot_version(versions_dir, current_version)
        if versions_dir.is_dir()
        else current_version
    )
    new_version = snapshot_version + 1
    # ``base_version`` records the live version the merge was computed
    # against. If the live skill advanced since staging, this candidate's body
    # was merged from an OLDER base, so writing it would replace whatever the
    # intervening approval added. REFUSE rather than warn: the reviewer cannot
    # be relied on to notice, because an already-open sibling candidate's diff
    # is served from the frontend query cache and may still be the v1-based
    # one. The candidate stays pending so it can be dismissed (a fresh
    # proposal will be merged against the new base).
    raw_base = meta.get("base_version")
    if isinstance(raw_base, int) and raw_base != current_version:
        # The candidate stays pending so the reviewer can dismiss it, which
        # means it stays VISIBLE — so it must also stay byte-identical to what
        # was staged. Redaction already ran in place above; undo it, or the
        # rejected draft is left permanently altered and the diff the reviewer
        # re-opens is not the one they staged.
        _restore_redacted(redact_backup)
        logger.warning(
            "Refusing to approve stale update for %s: candidate based on v%s, live is v%d",
            target_name,
            raw_base,
            current_version,
        )
        sel().log_tool_invocation(
            session_key="skills",
            tool_name="auto_skill_update_approve",
            tool_kind="permission",
            outcome="rejected",
            metadata={
                "target": target_name,
                "base_version": raw_base,
                "live_version": current_version,
                "reason": "stale_base",
            },
        )
        raise PendingApprovalRefused("stale_base")
    live_created_at = live_fm.get("created_at", "")
    # Carry the live skill's pin forward: a pinned skill is exempt from the
    # lifecycle's inactivity / max-N archival, and silently dropping the flag
    # here would expose a user-pinned skill to being archived.
    live_pinned = str(live_fm.get("pinned", "")).strip().lower() in ("true", "1", "yes")
    # Same for the injection opt-out: the candidate never carries it, so
    # writing it over live without this would silently turn full-body
    # injection back on for a skill the user had made pointer-only.
    live_pointer_only = str(live_fm.get("inject_on_trigger", "")).strip().lower() == "false"
    new_live_content = self._rewrite_update_frontmatter(
        candidate_body,
        target_name=target_name,
        created_at=live_created_at,
        version=new_version,
        pinned=live_pinned,
        pointer_only=live_pointer_only,
    )
    # Snapshot the current live SKILL.md into .versions/ (point-of-no-return
    # is the live overwrite below; if the snapshot fails, live is untouched).
    versions_dir = self._versions_root(target_slug)
    snapshot = versions_dir / f"v{snapshot_version}-SKILL.md"
    try:
        versions_dir.mkdir(parents=True, exist_ok=True)
        live_prev = live_skill.read_text(encoding="utf-8")
        atomic_write(snapshot, live_prev)
    except OSError:
        _restore_redacted(redact_backup)
        logger.warning(
            "Refusing to approve update %s: could not snapshot live version", target_name
        )
        raise PendingApprovalRefused("promotion_failed")
    # (f) Write candidate over live.
    try:
        atomic_write(live_skill, new_live_content)
    except OSError:
        # atomic_write renames into place, so a failure leaves the live
        # SKILL.md untouched; drop the snapshot we just wrote and restore.
        try:
            snapshot.unlink()
        except OSError:
            pass
        _restore_redacted(redact_backup)
        logger.warning("Refusing to approve update %s: could not write live SKILL.md", target_name)
        raise PendingApprovalRefused("promotion_failed")
    # (g) Promote candidate scripts into the live scripts/ dir (exec bit on
    # POSIX). COPY rather than move: the pending dir is deleted in (i), so a
    # move that fails partway would leave the approved script in neither
    # place. Copying keeps the candidate intact as the rollback source, and
    # any failure aborts the whole approval — restoring the live SKILL.md
    # from the snapshot we just wrote and leaving the candidate reviewable.
    src_scripts = src / "scripts"
    copied: list[Path] = []
    # Pre-existing destinations we OVERWRITE: keep their original bytes+mode so
    # a rollback restores them. Without this, replacing an existing live script
    # and then failing on a later file would roll SKILL.md back while leaving
    # the replacement script live — an internally inconsistent skill.
    overwritten: dict[Path, tuple[bytes, int]] = {}
    if src_scripts.is_dir():
        live_scripts = live_dir / "scripts"
        try:
            live_scripts.mkdir(parents=True, exist_ok=True)
            for root, _dirs, files in os.walk(src_scripts):
                rel_root = Path(root).relative_to(src_scripts)
                for nm in files:
                    sfp = Path(root) / nm
                    if not sfp.is_file() or sfp.is_symlink():
                        continue
                    dest_dir = live_scripts / rel_root
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dfp = dest_dir / nm
                    if dfp.exists():
                        # Snapshot BEFORE the overwrite; a read failure here
                        # aborts rather than clobbering un-restorable content.
                        _st = dfp.stat()
                        overwritten[dfp] = (dfp.read_bytes(), _st.st_mode)
                    else:
                        # Only track files WE created, so a rollback never
                        # deletes a script the live skill already shipped.
                        copied.append(dfp)
                    shutil.copy2(str(sfp), str(dfp))
                    dfp.chmod(dfp.stat().st_mode | 0o111)
        except OSError:
            for _p in copied:
                try:
                    _p.unlink()
                except OSError:
                    pass
            for _p, (_b, _mode) in overwritten.items():
                try:
                    _p.write_bytes(_b)
                    _p.chmod(_mode)
                except OSError:
                    logger.error(
                        "Update %s rollback could not restore live script %s",
                        target_name,
                        _p.name,
                    )
            try:
                atomic_write(live_skill, live_prev)
            except OSError:
                logger.error(
                    "Update %s failed mid-promotion AND the live SKILL.md could "
                    "not be restored; the snapshot remains at %s",
                    target_name,
                    snapshot,
                )
            else:
                try:
                    snapshot.unlink()
                except OSError:
                    pass
            _restore_redacted(redact_backup)
            logger.warning(
                "Refusing to approve update %s: could not promote candidate scripts",
                target_name,
            )
            raise PendingApprovalRefused("promotion_failed")
    # (h) Prune version history to the cap.
    self._prune_versions(versions_dir)
    # (i) Remove the pending candidate.
    # Captured BEFORE the removal so a same-slug replacement staged after
    # this instant keeps its notification (see approve_pending_skill_checked).
    consumed_at = datetime.now(tz=timezone.utc).isoformat()
    shutil.rmtree(src, ignore_errors=True)
    # (j) Audit the approved update.
    sel().log_tool_invocation(
        session_key="skills",
        tool_name="auto_skill_update_approve",
        tool_kind="permission",
        outcome="invoked",
        metadata={
            "target": target_name,
            "from_version": current_version,
            "to_version": new_version,
            "base_version": raw_base,
            "stale_base": False,
        },
    )
    # (8) Make the updated live skill visible to trigger matching now.
    self._invalidate_iter_cache()
    logger.info(
        "Approved pending update: %s (v%d -> v%d)", target_name, current_version, new_version
    )
    # The candidate cleanup above ignores rmtree errors (e.g. a Windows
    # file lock), so the candidate can survive in the pending queue even
    # though the update went live. Only report it consumed when the
    # directory is really gone — otherwise the queue still shows an
    # actionable review and its notification must stay unread.
    if not src.exists():
        _emit_pending_consumed(
            {
                "slug": slug,
                "outcome": "approved",
                "name": target_name,
                "consumed_at": consumed_at,
            }
        )
    return target_name


def approve_pending_skill_checked(self, slug: str) -> str:
    """Promote a pending candidate to a live auto-skill.

    Re-validates + redacts the candidate, then moves ``auto/.pending/<slug>``
    → ``auto/<slug>`` and marks any bundled scripts executable. Returns the
    live name; raises ``PendingApprovalRefused`` (with the reason, and the
    redacted findings report for a validation failure) if the candidate is
    missing, a live skill of that name already exists, it contains a
    symlink, script validation fails, or redaction fails. Every check runs
    BEFORE the move, so a rejected candidate is left untouched in the
    pending queue.
    """
    if not self._is_pending_slug_safe(slug):
        raise PendingApprovalRefused("not_found")
    src = self._pending_root() / slug
    if not (src / "SKILL.md").exists():
        raise PendingApprovalRefused("not_found")
    # An UPDATE candidate must never be consumed down this path: promoting
    # it fresh would create ``auto/<candidate-slug>`` while its live target
    # stays unchanged — the update silently never lands. The HTTP handler
    # routes on the candidate detail's ``kind``, but a raising detail read
    # drops that to ``None`` and defaults HERE, so the guard has to live at
    # the consumption point. ``kind_mismatch`` (not ``not_found``): the
    # candidate exists and is approvable via its own path, and the
    # not-found recovery copy ("approved or dismissed elsewhere") would be
    # a lie for a still-pending candidate.
    if self._read_pending_meta(slug).get("kind") == "update":
        logger.warning(
            "Refusing to approve %s as a NEW skill: candidate metadata marks it an update",
            slug,
        )
        raise PendingApprovalRefused("kind_mismatch")
    name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
    dest = self._dir / name
    if dest.exists():
        logger.warning("Cannot approve %s: a live skill already exists", name)
        raise PendingApprovalRefused("live_exists")
    # Reject any symlink in the candidate + any unexpected top-level entry
    # (defense-in-depth on top of the mandatory human review); promotion +
    # chmod must only touch known, real files. Factored into a shared helper
    # so the update-approve path enforces the identical layout guard.
    if not self._candidate_layout_ok(src, name):
        raise PendingApprovalRefused("invalid_layout")
    # One verdict, two surfaces: approve consults the SAME verdict function
    # the pre-click badge serves. Where a verdict is computable, a failing
    # one refuses HERE with the verdict's own report, so the badge and the
    # click cannot disagree on any candidate the badge judged — an
    # ``ok: false`` badge is a refusal by construction, not by parallel
    # re-derivation. ``None`` (no pinned opens, or a budget breach) keeps
    # this path's own machinery as the sole authority, exactly the
    # authority split the verdict's contract documents.
    _fold = self._pending_scripts_verdict(src)
    if _fold is not None:
        _fold_ok, _fold_report = _fold
        if not _fold_ok:
            logger.warning(
                "Refusing to approve %s: the pending verdict this candidate's badge "
                "serves reports failing findings",
                name,
            )
            raise PendingApprovalRefused(
                "script_validation_failed",
                report=self._redact_validation_report(_fold_report),
            )
    # Re-validate every script + redact the body + scripts before going live;
    # snapshots each file first so a failure restores the ORIGINAL bytes and
    # never leaves a corrupted pending draft (raising PendingApprovalRefused
    # with the reason). Shared with the update path.
    redact_backup = self._validate_and_redact_candidate(src, name)

    # Drop pending-only bookkeeping ONLY after every check + redaction has
    # passed and immediately before the move, so a failed approval leaves the
    # candidate — including its .meta.json (description/triggers) — intact in
    # the pending queue for re-review. A removal FAILURE (non-writable dir,
    # etc.) must ABORT: otherwise the raw, possibly secret-bearing .meta.json
    # would ride into the live skill dir and be exposed by the browser. Only
    # an already-absent file (FileNotFoundError) is benign. We stash the meta
    # bytes first so a subsequent MOVE failure can restore them (otherwise the
    # candidate would be left stranded in pending without its metadata).
    meta_path = src / ".meta.json"
    meta_backup: bytes | None = None
    try:
        meta_backup = meta_path.read_bytes()
    except FileNotFoundError:
        meta_backup = None
    except OSError:
        _restore_redacted(redact_backup)
        logger.warning(
            "Refusing to approve %s: could not read pending .meta.json before promotion", name
        )
        raise PendingApprovalRefused("promotion_failed")
    try:
        meta_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        _restore_redacted(redact_backup)
        logger.warning(
            "Refusing to approve %s: could not remove pending .meta.json before promotion",
            name,
        )
        raise PendingApprovalRefused("promotion_failed")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Cutoff for notification resolution, captured BEFORE the candidate
    # leaves the pending queue: staging refuses to overwrite an existing
    # candidate, so a same-slug replacement can only be staged after this
    # instant — its notification carries a strictly later ``ts`` and must
    # survive the resolve.
    consumed_at = datetime.now(tz=timezone.utc).isoformat()
    try:
        shutil.move(str(src), str(dest))
    except OSError:
        # Promotion failed after we deleted the pending bookkeeping — restore
        # .meta.json AND the redacted files so the candidate stays intact in
        # the pending queue for re-review instead of being left corrupted.
        if meta_backup is not None and src.is_dir():
            try:
                meta_path.write_bytes(meta_backup)
            except OSError:
                pass
        _restore_redacted(redact_backup)
        logger.warning("Refusing to approve %s: could not move candidate live", name)
        raise PendingApprovalRefused("promotion_failed")
    # Mark scripts executable now that a human approved them (recursively).
    sdir = dest / "scripts"
    if sdir.is_dir():
        for root, _dirs, files in os.walk(sdir):
            for nm in files:
                sf = Path(root) / nm
                if sf.is_file() and not sf.is_symlink():
                    try:
                        sf.chmod(sf.stat().st_mode | 0o111)
                    except OSError:
                        pass
    self._invalidate_iter_cache()
    logger.info("Approved pending skill: %s", name)
    _emit_pending_consumed(
        {"slug": slug, "outcome": "approved", "name": name, "consumed_at": consumed_at}
    )
    return name
