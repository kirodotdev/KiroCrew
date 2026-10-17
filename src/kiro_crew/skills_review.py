"""What an operator reviews before skill text nobody vetted goes live or loads: the pending
auto-skill queue (list, detail, script verdict, update preview and both approve paths) and the
consent picker's catalog of a project's skills. Every string these surfaces serve is redacted
here, which makes this module the redaction sink ``security_posture`` registers for them. The
redacting steps of a claimed promotion live here for the same reason: the validation and
redaction of a captured candidate generation, the claimed update and new-skill promotions
that publish it, and the restore of a claim a promotion could not complete.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.

Instance fields read: ``_dir``.
"""

from __future__ import annotations

import difflib
import errno
import json
import logging
import os
import secrets
import stat
from itertools import islice
from pathlib import Path
from typing import cast

from kiro_crew import pinned_fs, platform_compat, skill_trust
from kiro_crew.platform_compat import is_link_or_junction, pinned_directory
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.skills_auto_authority import auto_skill_promotion_disabled_reason
from kiro_crew.skills_auto_model import (
    _AUTO_NAME_PATTERN,
    _PENDING_DOCUMENT_MAX_BYTES,
    AUTO_SKILL_NAMESPACE,
    _ClaimSnapshot,
    _emit_pending_consumed,
    _PinnedPrivateState,
    _SkillTreeSnapshot,
    _ValidatedCandidateSnapshot,
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
    ``redaction_failed``, ``promotion_disabled`` (this host cannot stage or
    promote auto-skills: see :func:`auto_skill_promotion_disabled_reason`), or
    ``promotion_failed`` (an OS-level I/O failure — a read or write, a refused
    read of the live skill's metadata included — or a claimed generation that
    changed or could not be published).
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


def _note_refusal(
    refusal: list[PendingApprovalRefused] | None,
    reason: PendingApprovalRefused,
) -> None:
    """Record why a claimed promotion refused; the first reason recorded wins.

    The claim protocol returns ``None`` for every refusal so a failure can never
    be mistaken for a partial success. The ``*_checked`` approve variants still
    owe the dashboard the reason, so the refusal sites that have one note it
    here and the variant raises the first one noted. Each site constructs the
    exception with a literal reason, which the dashboard's refusal-vocabulary pin
    reads from source.
    """
    if refusal is not None and not refusal:
        refusal.append(reason)


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


def _candidate_metadata_from_bytes(self, raw: bytes | None, *, redact: bool) -> dict:
    """Parse metadata already captured from an authenticated tree snapshot."""
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if not redact:
        return data
    redacted = self._redact_deep(data)
    return redacted if isinstance(redacted, dict) else {}


def _read_pending_meta(self, slug: str) -> dict:
    """The pending candidate's metadata, for display and routing only.

    Advisory: a writer can still change the public candidate, so nothing that
    authorizes a promotion reads it. Promotion parses the metadata captured in
    the claimed generation instead (``_candidate_metadata_from_bytes``).
    """
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


def _restore_claimed_update(
    self,
    claim: Path,
    claim_fd: int,
    slug: str,
    claim_snapshot: _ClaimSnapshot | None = None,
) -> Path | None:
    """Requeue the exact quarantined public inode without replacement."""
    notify = False
    restored: Path | None = None
    captured = claim_snapshot or _ClaimSnapshot(None, None)
    restored_meta = self._candidate_metadata_from_bytes(
        captured.metadata_bytes,
        redact=False,
    )
    lock_state: list[_PinnedPrivateState] = []
    with self._file_lock("pending.lock", state_out=lock_state) as acquired:
        if not acquired or not lock_state:
            logger.error("Could not restore claimed update %s", claim)
            return None
        try:
            private_state = lock_state[0]
            pending_parent = private_state.pending
            if self._pinned_parent_matches(pending_parent):
                quarantined = self._stat_pinned_child(
                    private_state.quarantine,
                    claim.name,
                )
                quarantine_linked = (
                    stat.S_ISLNK(quarantined.st_mode)
                    or bool(getattr(quarantined, "st_reparse_tag", 0))
                    or is_link_or_junction(private_state.quarantine.path / claim.name)
                )
                quarantine_identity = self._pinned_child_identity(
                    private_state.quarantine,
                    claim.name,
                )
                if quarantine_identity is None:
                    raise OSError("public quarantine identity is unavailable")
                if captured.quarantine_identity is not None:
                    if quarantine_identity != captured.quarantine_identity or (
                        not quarantine_linked and not stat.S_ISDIR(quarantined.st_mode)
                    ):
                        raise OSError("public quarantine changed identity")
                elif not quarantine_linked:
                    raise OSError("public quarantine has no authenticated identity")
                has_authenticated_snapshot = (
                    self._authenticated_claim_snapshot_state(
                        claim_fd,
                        self._claim_lock_path(claim.name),
                        claim.name,
                    )
                    is not None
                    or self._authenticated_claim_publication(
                        claim_fd,
                        self._claim_lock_path(claim.name),
                        claim.name,
                    )
                    is not None
                )
                for number in [None, *range(2, 51)]:
                    if number is None:
                        candidate_slug = slug
                    else:
                        suffix = f"-{number}"
                        candidate_slug = f"{slug[: 64 - len(suffix)].rstrip('-')}{suffix}"
                    notify = (
                        restored_meta.get("notify_suppressed") is True or candidate_slug != slug
                    )
                    receipt_written = False
                    if has_authenticated_snapshot:
                        if quarantine_identity is None:
                            raise OSError("authenticated restore has no native identity")
                        receipt_written = self._write_claim_restore_state(
                            claim_fd,
                            self._claim_lock_path(claim.name),
                            claim.name,
                            restore_slug=candidate_slug,
                            restore_identity=quarantine_identity,
                        )
                        if not receipt_written:
                            raise OSError("claim restore receipt failed")
                    try:
                        if quarantine_linked:
                            self._rename_untrusted_link_no_replace(
                                private_state.quarantine,
                                claim.name,
                                pending_parent,
                                candidate_slug,
                                expected_identity=quarantine_identity,
                            )
                        else:
                            assert quarantine_identity is not None
                            self._rename_skill_child_no_replace(
                                private_state.quarantine,
                                claim.name,
                                pending_parent,
                                candidate_slug,
                                expected_identity=quarantine_identity,
                            )
                    except OSError as exc:
                        if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                            raise
                        if receipt_written and not self._clear_claim_restore_state(
                            claim_fd,
                            self._claim_lock_path(claim.name),
                            claim.name,
                        ):
                            raise OSError("could not clear refused restore receipt") from exc
                        continue
                    self._sync_pinned_rename_parents(
                        private_state.quarantine,
                        pending_parent,
                    )
                    restored = cast(Path, self._pending_root()) / candidate_slug
                    restored_info = self._stat_pinned_child(
                        pending_parent,
                        candidate_slug,
                    )
                    if quarantine_linked:
                        if (
                            not (
                                stat.S_ISLNK(restored_info.st_mode)
                                or bool(getattr(restored_info, "st_reparse_tag", 0))
                                or is_link_or_junction(restored)
                            )
                            or self._pinned_child_identity(
                                pending_parent,
                                candidate_slug,
                            )
                            != quarantine_identity
                        ):
                            raise OSError("restored pending link changed identity")
                    else:
                        restored_identity = self._pinned_child_identity(
                            pending_parent,
                            candidate_slug,
                        )
                        if (
                            not stat.S_ISDIR(restored_info.st_mode)
                            or restored_identity != quarantine_identity
                        ):
                            raise OSError("restored pending inode changed identity")
                    if not self._discard_trusted_claim(claim):
                        logger.error(
                            "Could not remove restored trusted materialization %s",
                            claim,
                        )
                    break
        except OSError:
            logger.error("Could not restore claimed update %s", claim, exc_info=True)
            return None
        if restored is None:
            logger.error("No pending slot available to restore %s", claim)
            return None
    if notify:
        notification_meta = dict(restored_meta)
        notification_meta["slug"] = restored.name
        notification_meta["name"] = f"{AUTO_SKILL_NAMESPACE}/{restored.name}"
        safe_meta = self._redact_deep(notification_meta)
        self._emit_pending_staged_metadata(
            restored.name,
            safe_meta if isinstance(safe_meta, dict) else {},
        )
    return restored


def list_pending_skills(self) -> list[dict]:
    """Return ``{slug, name, description, triggers, has_scripts, created_at, path}``
    for every staged candidate."""
    # A claim whose owning process died mid-transaction is restored or
    # retired before the queue is read, so the list never omits a candidate
    # that only an interrupted promotion is holding.
    self._recover_abandoned_claims()
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
            "name": f"{AUTO_SKILL_NAMESPACE}/{child.name}",
            "description": meta.get("description", ""),
            "triggers": meta.get("triggers", ""),
            "has_scripts": meta.get("has_scripts") is True,
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
        # Display-only newline folding: stored bytes, generation hashes and
        # promotion authority keep the candidate's exact spelling.
        content = str(s.get("content", "")).replace("\r\n", "\n").replace("\r", "\n")
        s["content"] = self._redact_text(content)
    detail = {
        "slug": slug,
        # The directory name is authoritative: a refusal restore can land a
        # candidate under a sibling slot without rewriting its metadata.
        "name": f"{AUTO_SKILL_NAMESPACE}/{slug}",
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


def _validate_and_redact_candidate(
    self,
    src: Path,
    name: str,
    refusal: list[PendingApprovalRefused] | None = None,
) -> _ValidatedCandidateSnapshot | None:
    """Validate and redact one descriptor-authenticated candidate generation.

    Never mutates the candidate: redaction is applied to the captured
    snapshot, so a refused candidate stays byte-identical to what was staged.
    ``refusal`` receives the reason (and a script report) for a refusal.
    """
    if not self._candidate_layout_ok(src, name):
        _note_refusal(refusal, PendingApprovalRefused("invalid_layout"))
        return None
    tree = self._skill_tree_snapshot(src)
    if tree is None:
        logger.warning("Refusing to approve %s: candidate snapshot is unreadable", name)
        _note_refusal(refusal, PendingApprovalRefused("invalid_layout"))
        return None
    return self._validate_and_redact_snapshot(tree, name, refusal=refusal)


def _validate_and_redact_snapshot(
    self,
    tree: _SkillTreeSnapshot,
    name: str,
    *,
    refusal: list[PendingApprovalRefused] | None = None,
) -> _ValidatedCandidateSnapshot | None:
    """Validate and redact one already-captured immutable candidate tree."""
    allowed_files = {Path("SKILL.md"), Path(".meta.json")}
    if any(
        relative != Path(".") and (not relative.parts or relative.parts[0] != "scripts")
        for relative in tree.dir_modes
    ) or any(
        (len(relative.parts) == 1 and relative not in allowed_files)
        or (len(relative.parts) > 1 and relative.parts[0] != "scripts")
        for relative in tree.files
    ):
        logger.warning("Refusing to approve %s: unexpected candidate entry", name)
        _note_refusal(refusal, PendingApprovalRefused("invalid_layout"))
        return None
    source_files = tree.files
    redacted_files: dict[Path, bytes] = {}
    metadata: dict[str, object] = {}
    try:
        for relative, raw in source_files.items():
            if relative == Path(".meta.json"):
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    parsed = {}
                if isinstance(parsed, dict):
                    redacted_meta = self._redact_deep(parsed)
                    if isinstance(redacted_meta, dict):
                        metadata = redacted_meta
                continue
            redacted_files[relative] = self._redact_text(raw.decode("utf-8")).encode("utf-8")
    except UnicodeDecodeError:
        logger.warning("Refusing to approve %s: candidate snapshot is unreadable", name)
        _note_refusal(refusal, PendingApprovalRefused("redaction_failed"))
        return None

    skill_path = Path("SKILL.md")
    if skill_path not in redacted_files:
        _note_refusal(refusal, PendingApprovalRefused("invalid_layout"))
        return None

    def _scripts(files: dict[Path, bytes]) -> list[dict[str, str]]:
        scripts: list[dict[str, str]] = []
        for relative, payload in sorted(files.items(), key=lambda item: str(item[0])):
            if not relative.parts or relative.parts[0] != "scripts":
                continue
            scripts.append(
                {
                    "filename": str(relative.relative_to("scripts")),
                    "content": payload.decode("utf-8"),
                }
            )
        return scripts

    before_scripts = _scripts(source_files)
    if before_scripts:
        ok, report = validate_scripts(before_scripts)
        if not ok:
            logger.warning("Refusing to approve %s: script validation failed: %s", name, report)
            _note_refusal(
                refusal,
                PendingApprovalRefused(
                    "script_validation_failed", report=self._redact_validation_report(report)
                ),
            )
            return None
    after_scripts = _scripts(redacted_files)
    if after_scripts:
        ok, report = validate_scripts(after_scripts)
        if not ok:
            logger.warning(
                "Refusing to approve %s: scripts invalid after redaction: %s",
                name,
                report,
            )
            _note_refusal(
                refusal,
                PendingApprovalRefused(
                    "script_validation_failed", report=self._redact_validation_report(report)
                ),
            )
            return None
    return _ValidatedCandidateSnapshot(
        source_files=source_files,
        files=redacted_files,
        modes=tree.file_modes,
        metadata=metadata,
        generation_hash=tree.generation_hash,
    )


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
    # One snapshot supplies body and metadata, so a retained writer cannot
    # make the preview describe two different candidate generations.
    candidate_tree = self._skill_tree_snapshot(src)
    if candidate_tree is None:
        return None
    meta = self._candidate_metadata_from_bytes(
        candidate_tree.files.get(Path(".meta.json")),
        redact=True,
    )
    if meta.get("kind") != "update":
        return None
    try:
        cand_body = candidate_tree.files[Path("SKILL.md")].decode("utf-8")
    except (KeyError, UnicodeDecodeError):
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


def _promote_pending_update(
    self,
    slug: str,
    *,
    refuse_scripts: bool = False,
    expected_candidate_binding: str | None = None,
    claimed_out: list[bool] | None = None,
    committed_out: list[tuple[str, int]] | None = None,
    refusal: list[PendingApprovalRefused] | None = None,
) -> tuple[str, int] | None:
    """Claim and promote an update, returning its lock-authoritative version."""
    claimed = self._claim_pending_update(slug)
    if claimed is None:
        return None
    if claimed_out is not None:
        claimed_out[:] = [True]
    claim, claim_fd, consumed_at, claim_snapshot = claimed
    result: tuple[str, int] | None = None
    live_published = False
    try:
        if is_link_or_junction(claim):
            return None
        observed = self._validate_and_redact_candidate(claim, slug, refusal=refusal)
        if (
            observed is None
            or claim_snapshot.generation_hash is None
            or not secrets.compare_digest(
                observed.generation_hash,
                claim_snapshot.generation_hash,
            )
        ):
            logger.warning("Refusing promotion of %s: claimed generation changed", slug)
            return None
        if claim_snapshot.tree is None:
            logger.warning("Refusing promotion of %s: claim has no immutable authority", slug)
            return None
        snapshot = self._validate_and_redact_snapshot(claim_snapshot.tree, slug, refusal=refusal)
        if snapshot is None:
            logger.warning("Refusing promotion of %s: snapshot validation failed", slug)
            return None
        meta = snapshot.metadata
        target = meta.get("target")
        if meta.get("kind") != "update":
            _note_refusal(refusal, PendingApprovalRefused("not_found"))
            return None
        if not isinstance(target, str) or not target:
            _note_refusal(refusal, PendingApprovalRefused("target_missing"))
            return None
        target_slug = self._auto_slug_from_name(target)
        if not self._is_pending_slug_safe(target_slug):
            _note_refusal(refusal, PendingApprovalRefused("target_missing"))
            return None
        with self._promotion_lock(target_slug) as acquired:
            if not acquired:
                logger.warning("Promotion lock unavailable for %s", target)
                return None
            try:
                with self._pin_private_state(
                    create=False,
                    require_sensitive=True,
                ) as private_state:
                    result = self._approve_claimed_update_locked(
                        claim,
                        claim_fd=claim_fd,
                        slug=slug,
                        meta=meta,
                        snapshot=snapshot,
                        refuse_scripts=refuse_scripts,
                        expected_candidate_binding=expected_candidate_binding,
                        private_state=private_state,
                        refusal=refusal,
                    )
                    if result is None:
                        return None
                    name, _new_version = result
                    live_published = True
                    if committed_out is not None:
                        committed_out[:] = [result]
                    if not self._commit_claim_consumption(claim, claim_fd):
                        result = None
                        return None
                    if not self._pinned_parent_matches(
                        private_state.auto
                    ) or not self._pinned_parent_matches(private_state.private):
                        result = None
                        return None
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                logger.warning("Promotion authority became unavailable for %s", target)
                return None
        if os.path.lexists(claim):
            logger.warning(
                "Published update evidence remains active for restart retention: %s",
                claim,
            )
        _emit_pending_consumed(
            {"slug": slug, "outcome": "approved", "name": name, "consumed_at": consumed_at}
        )
        return result
    finally:
        cleanup_claim_lock = True
        try:
            if result is None and not live_published:
                cleanup_claim_lock = self._restore_failed_promotion_claim(
                    claim,
                    claim_fd,
                    slug,
                    claim_snapshot,
                )
        except OSError:
            cleanup_claim_lock = False
            logger.error("Could not restore claimed update %s", claim, exc_info=True)
        finally:
            platform_compat.release_lock(claim_fd)
            os.close(claim_fd)
            if cleanup_claim_lock:
                self._cleanup_claim_lock(claim.name)


def _approve_claimed_skill_locked(
    self,
    src: Path,
    claim_fd: int,
    slug: str,
    claim_snapshot: _ClaimSnapshot,
    private_state: _PinnedPrivateState,
    *,
    refusal: list[PendingApprovalRefused] | None = None,
) -> str | None:
    """Publish one immutable claimed snapshot while its target lock is held."""
    name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
    dest = private_state.auto.path / slug
    if is_link_or_junction(src):
        return None
    if claim_snapshot.tree is None:
        logger.warning("Refusing to approve %s: claim has no immutable authority", name)
        return None
    snapshot = self._validate_and_redact_snapshot(claim_snapshot.tree, name, refusal=refusal)
    if snapshot is None:
        logger.warning("Refusing to approve %s: snapshot validation failed", name)
        return None
    if snapshot.metadata.get("kind") == "update":
        logger.warning("Refusing to approve %s as new: claimed candidate is an update", name)
        _note_refusal(refusal, PendingApprovalRefused("kind_mismatch"))
        return None
    if self._pinned_child_exists(private_state.auto, slug):
        logger.warning("Cannot approve %s: a live skill already exists", name)
        _note_refusal(refusal, PendingApprovalRefused("live_exists"))
        return None

    stage, _backup = self._publication_paths(src.name)
    if stage.parent != private_state.private.path:
        return None
    if self._pinned_child_exists(private_state.private, stage.name):
        logger.error("Refusing to reuse skill publication stage for %s", name)
        return None
    try:
        self._materialize_candidate_snapshot(snapshot, stage)
        self._sync_skill_tree(stage)
        after_hash = self._skill_tree_hash_child(private_state.private, stage.name)
        if after_hash is None:
            raise OSError("staged skill generation changed")
        if not self._prepare_claim_publication(
            claim_fd,
            self._claim_lock_path(src.name),
            src.name,
            kind="new",
            target_slug=slug,
            before_hash=None,
            after_hash=after_hash,
            claim_snapshot=_ClaimSnapshot(
                snapshot.generation_hash,
                snapshot.source_files.get(Path(".meta.json")),
            ),
            live_backup_identity=None,
            snapshot_version=None,
            new_version=1,
        ):
            raise OSError("claim journal failed")
        if (
            self._publish_prepared_skill_tree(
                state=private_state,
                live_dir=dest,
                stage=stage,
                backup=None,
                backup_identity=None,
                before_hash=None,
                after_hash=after_hash,
            )
            != "published"
        ):
            logger.warning("Refusing to approve %s: whole-tree publication did not complete", name)
            return None
    except (KeyError, OSError):
        if (
            self._authenticated_claim_publication(
                claim_fd, self._claim_lock_path(src.name), src.name
            )
            is None
        ):
            self._cleanup_publication_artifacts(src.name)
        logger.warning("Refusing to approve %s: could not publish validated snapshot", name)
        return None

    self._invalidate_iter_cache()
    logger.info("Approved pending skill: %s", name)
    return name


def _refuse_failing_pending_scripts(self, src: Path, name: str) -> None:
    """Raise ``script_validation_failed`` when the candidate's scripts fail.

    One verdict, two surfaces: approve consults the SAME verdict function the
    pre-click badge serves, so a computable failing verdict refuses with the
    badge's own report and the click cannot disagree with the badge. Where no
    verdict is computable (no pinned walk), the by-name scripts are validated
    instead so the refusal still names the reason. Advisory either way: it
    reads the public candidate and can only refuse; the claimed snapshot is
    validated again before anything is published.
    """
    _fold = self._pending_scripts_verdict(src)
    if _fold is None:
        sdir = src / "scripts"
        scripts = self._collect_scripts(sdir) if sdir.is_dir() else []
        _fold = validate_scripts(scripts) if scripts else (True, {})
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


def approve_pending_update_checked(self, slug: str) -> str:
    """Atomically claim and promote a pending UPDATE candidate over its live target.

    Raises ``PendingApprovalRefused`` with the reason; a refusal leaves both
    the live skill and the candidate as they were. The advisory checks below
    read the PUBLIC candidate, which a writer can still change, so they can
    only refuse earlier with a precise reason and never authorize anything.
    Promotion then captures an immutable generation before the public
    directory is renamed into the claim, and publishes only from that
    capture: the live tree is moved aside under ``auto/.live-quarantine/``
    and a fresh private stage holding the rewritten body, the version
    snapshot and the validated scripts is renamed live in its place. A
    refusal restores the claimed candidate to review without replacing a
    newer public candidate. Returns ``auto/<target>`` on success.
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
    if not (self._dir / AUTO_SKILL_NAMESPACE / target_slug / "SKILL.md").exists():
        logger.warning(
            "Refusing to approve update %s: target %r is not a live auto skill", slug, target
        )
        raise PendingApprovalRefused("target_missing")
    disabled = auto_skill_promotion_disabled_reason()
    if disabled is not None:
        logger.warning("Refusing to approve update %s: %s", slug, disabled)
        raise PendingApprovalRefused("promotion_disabled")
    target_name = f"{AUTO_SKILL_NAMESPACE}/{target_slug}"
    # The live version and frontmatter are rewrite reads, so a live path
    # that does not vet raises rather than answering "no metadata"; refuse
    # with the I/O reason instead of letting the claim run.
    try:
        self.get_auto_skill_version(target_name)
        self._cached_frontmatter(
            self._dir / AUTO_SKILL_NAMESPACE / target_slug / "SKILL.md",
            within=None,
            for_write=True,
        )
    except OSError:
        logger.warning(
            "Refusing to approve update %s: could not read the live skill's metadata",
            target_name,
        )
        raise PendingApprovalRefused("promotion_failed")
    if not self._candidate_layout_ok(src, target_name):
        raise PendingApprovalRefused("invalid_layout")
    self._refuse_failing_pending_scripts(src, target_name)
    refusal: list[PendingApprovalRefused] = []
    result = self._promote_pending_update(slug, refusal=refusal)
    if result is None:
        raise refusal[0] if refusal else PendingApprovalRefused("promotion_failed")
    return result[0]


def approve_pending_skill_checked(self, slug: str) -> str:
    """Atomically claim and publish a pending candidate as a live auto-skill.

    Returns the live name; raises ``PendingApprovalRefused`` (with the reason,
    and the redacted findings report for a validation failure) if the
    candidate is missing, a live skill of that name already exists, it holds
    a link or an unexpected entry, script validation fails, or publication
    does not complete. The advisory checks read the PUBLIC candidate and can
    only refuse; publication uses only the generation captured at the claim
    rename, materialized into a fresh private stage that is renamed live
    without replacement. A refusal restores the claimed candidate to review.
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
    if (self._dir / name).exists():
        logger.warning("Cannot approve %s: a live skill already exists", name)
        raise PendingApprovalRefused("live_exists")
    disabled = auto_skill_promotion_disabled_reason()
    if disabled is not None:
        logger.warning("Refusing to approve %s: %s", name, disabled)
        raise PendingApprovalRefused("promotion_disabled")
    if not self._candidate_layout_ok(src, name):
        raise PendingApprovalRefused("invalid_layout")
    self._refuse_failing_pending_scripts(src, name)
    refusal: list[PendingApprovalRefused] = []
    result = self._promote_pending_skill(slug, refusal=refusal)
    if result is None:
        raise refusal[0] if refusal else PendingApprovalRefused("promotion_failed")
    return result
