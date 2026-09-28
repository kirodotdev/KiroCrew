"""Candidate enumeration -- everything starts excluded.

Ported from ``crew_export/candidates.py`` (skills + mcp only: the app's four-entry layout has
no workspace/ or knowledge/, so those categories, and the sqlite knowledge walk behind them,
are deliberately not ported). A candidate carries the content pin a review records, or the
reason it can never be included.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import hashing as _hashing
from . import pinned as _pinned
from . import scan as _scan
from . import sensitive as _sensitive
from .contract import _MAX_PROMPT_BYTES, ExportRefused
from .crew import ResolvedCrew

# MCP servers Kiro Crew resolves to an absolute path to a local binary; copying
# the definition ships a path that does not exist in the container. Ported from
# ``crew_export/candidates.py:_CONTAINER_OWNED_MCP``.
_CONTAINER_OWNED_MCP = frozenset(
    {"kirocrew-core", "kirocrew-cron", "kirocrew-computer", "kirocrew-dashboard"}
)


@dataclass
class Candidate:
    kind: str  # "skills" | "mcp"
    id: str
    #: sha256 of the candidate's content; the pin the review records and the
    #: build re-checks. Empty only for a blocked candidate that was never read.
    content_hash: str
    note: str = ""
    #: Set when structurally ineligible (a credential store); refused if selected.
    blocked: str = ""


def skill_candidates(skills_root: Path) -> list[Candidate]:
    """Skill directories (each dir holding a ``SKILL.md``), deny-by-default.

    Skills are global on the owner's machine and many drive ``gh``, an AWS
    profile, Playwright or the loopback gateway -- none of which exist in a
    customer-facing container -- so selection is a deployment judgement and every
    skill starts excluded.
    """
    if _pinned._is_redirecting_entry(skills_root):
        # Judged BEFORE ``is_dir()``, which follows the link: a symlinked or
        # junctioned ``skills`` root makes ``rglob("SKILL.md")`` below enumerate a
        # tree OUTSIDE ``--source``, and every match's ``relative_to(skills_root)``
        # still reads in-bounds, so files sourced elsewhere are selectable and ship
        # in the bundle. This is the redirect class the per-entry guard (below) and
        # ``_refuse_redirects_in_chain`` already block at the SKILL.md and the
        # out/staging/previous paths; the root itself was the uncovered variant.
        # Refused, not skipped: a silently empty skills list looks like a deliberate
        # persona-only choice, which is exactly the omission a redirected root hides.
        raise ExportRefused(
            f"the skills root {skills_root} is a link or junction. Enumerating skills "
            f"through it would walk a tree outside --source while every id still reads "
            f"in-bounds, so files sourced elsewhere would ship in the bundle. Refusing "
            f"to traverse a redirected skills root; point --source at a real directory."
        )
    if not skills_root.is_dir():
        # ``not is_dir()`` conflates two cases that must not share an answer, because a
        # directory's SHAPE is author-supplied input (via --source / the crew home) just as
        # much as a spec field is. A genuinely ABSENT root is the ordinary persona-only crew;
        # a root that EXISTS but is not a directory -- a plain file, a FIFO, a device where the
        # ``skills`` directory should be -- is a MALFORMED structure, and shipping an empty
        # bundle for it is the same silent-omission trap as a dropped skill asset: the operator
        # gets a plausible-looking persona-only bundle instead of being told their layout is
        # wrong. So the wrong-TYPE case is REFUSED (the author-supplied-structure rule:
        # absent -> empty, wrong-type -> refuse, unreadable -> fail closed), and only the
        # absent case warns.
        if _pinned._is_redirecting_entry(skills_root) or skills_root.exists():
            raise ExportRefused(
                f"the skills root {skills_root} exists but is not a directory. It is derived "
                f"from the crew home (--source / KIROCREW_HOME), and a non-directory there is "
                f"a malformed layout, not an empty skill set; refusing rather than ship a "
                f"bundle that silently omits every skill. Point --source at a real crew home."
            )
        # A missing skills root is the silent-omission trap fix #3 addresses: the
        # curation scans a directory that does not exist, finds nothing, and
        # produces a bundle with no skills that looks like a deliberate choice. A
        # crew with genuinely zero skills is legitimate (many crews ship persona
        # only), so this is a warning, not a refusal -- but it is LOUD, on stderr,
        # naming the path, so an operator who expected skills sees the cause
        # (usually a wrong home or an unset KIROCREW_HOME) rather than a
        # plausible-looking empty bundle.
        print(
            f"WARNING: skills root {skills_root} does not exist; the bundle will "
            f"contain NO skills. If this crew is meant to have skills, check the "
            f"crew home (KIROCREW_HOME / --source). If it is persona-only, ignore "
            f"this.",
            file=sys.stderr,
        )
        return []
    out: list[Candidate] = []
    for skill_md in _pinned._walk_no_reparse(skills_root, match="SKILL.md"):
        skill_dir = skill_md.parent
        rel = skill_dir.relative_to(skills_root).as_posix()
        # A component between the skills root and this SKILL.md that redirects (a symlink or a
        # Windows junction) is refused BEFORE ``is_file()``/``_read_text`` below, because those
        # resolve the path and on Windows resolving a junction to a UNC share is an outbound
        # SMB/NTLM probe. The root-junction guard covers only the skills root; a NESTED junction
        # is reached here, so ``_redirect_between`` walks each component and blocks the skill if
        # any redirects. (``rglob`` has already listed the name; this stops the resolving read.)
        crossed = _pinned._redirect_between(skills_root, skill_md)
        if crossed is not None:
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=(
                        f"reached through a link or junction at "
                        f"{crossed.relative_to(skills_root).as_posix()}; its location is "
                        f"outside the crew source, so it is not shipped"
                    ),
                )
            )
            continue
        # The SKILL.md must be a readable regular file of UTF-8 text, judged HERE, because
        # ``rglob("SKILL.md")`` matches the NAME and everything after it assumed content.
        #
        # A FIFO, a device node, a directory called SKILL.md, or a file that is not UTF-8 all
        # reached this list. The credential scan then skipped them -- ``_read_text`` returns
        # None for content it cannot decode and the loop below does ``continue`` -- so the
        # skill passed unblocked, was selectable, and shipped a bundle whose skill has no
        # usable instructions. Worse for the FIFO: the scan's own read blocks forever on a
        # pipe with no writer, so the build hangs instead of finishing.
        #
        # Blocked rather than dropped, so the notes name it. A skill silently missing from
        # the plan looks like a skill that was never there.
        if _pinned._is_redirecting_entry(skill_md) or not skill_md.is_file():
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=(
                        "SKILL.md is not a regular file (it is a link, a directory or a "
                        "special file), so there is nothing to ship for this skill"
                    ),
                )
            )
            continue
        # The UTF-8 probe reads SKILL.md through the SAME authority the scan and copy use --
        # ``safe_read_file_bytes_nolink`` -- not a bare descriptor read. ``_read_text_openat``
        # opens ``O_NOFOLLOW`` but does not fstat ``st_nlink``, so a credential hard-linked to
        # a second innocent name at ``SKILL.md`` would be decoded here through its second name.
        # Nothing downstream ships those bytes (the scan at the guard below and the copy both
        # refuse ``st_nlink > 1`` before anything is emitted), but reading the candidate
        # through the authority closes the read itself rather than relying on a later gate:
        # ``None`` means the guard rejected it (hard link, sensitive, not a regular file,
        # unreadable), and a file above the ceiling or one that is not UTF-8 is unscannable
        # text. Any of these blocks the skill with a reason rather than passing it selectable.
        try:
            from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
        except ImportError as exc:
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=(
                        f"cannot be certified clean because kiro_crew.hooks is not importable "
                        f"here ({exc}); that module holds the sensitive-path and hard-link "
                        f"rules this read has to satisfy, and a local approximation is not the "
                        f"same check"
                    ),
                )
            )
            continue
        try:
            _probe = safe_read_file_bytes_nolink(
                str(skill_md), str(skills_root), max_bytes=_MAX_PROMPT_BYTES
            )
        except FileTooLargeError:
            _probe = None
        _readable = _probe is not None
        if _probe is not None:
            try:
                _probe.decode("utf-8")
            except UnicodeDecodeError:
                _readable = False
        if not _readable:
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=(
                        "SKILL.md is not UTF-8 text the guard can certify (it is unreadable, "
                        "too large, sensitive, or hard-linked to another name), so the "
                        "container could not read it and the credential scan could not either"
                    ),
                )
            )
            continue
        # Credential store inside the skill => blocked, never includable. Both
        # halves apply, mirroring _copy_skill and _resolve_prompt_path: a file
        # NAMED like a credential (refused_by_name) and a file LOCATED inside a
        # credential directory (refused_by_location, e.g. a nested .aws/config
        # whose basename is innocent). Catching the location half here reports
        # the skill as blocked in the curation plan rather than letting it look
        # selectable and only failing at copy time.
        #
        # A directory junction inside the skill is checked FIRST: ``rglob`` descends into it
        # and the files under it report ``is_symlink()`` False, so both credential scans below
        # would read (or fail to read) the junction target's files as if in-tree. Blocking the
        # skill on any redirecting component keeps content whose true location is outside the
        # source from being scanned-as-clean and later copied.
        redirect = next(
            (p for p in _pinned._walk_no_reparse(skill_dir) if _pinned._is_redirecting_entry(p)),
            None,
        )
        if redirect is not None:
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=f"reaches outside the source through a link or junction: "
                    f"{redirect.relative_to(skill_dir).as_posix()}",
                )
            )
            continue
        cred_file = next(
            (
                p
                for p in _pinned._walk_no_reparse(skill_dir)
                if p.is_file()
                and _pinned._redirect_between(skill_dir, p) is None
                and (_sensitive.refused_by_name(p) or _sensitive.refused_by_location(p))
            ),
            None,
        )
        if cred_file is not None:
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=f"contains a credential store: "
                    f"{cred_file.relative_to(skill_dir).as_posix()}",
                )
            )
            continue
        # A hard credential in any readable file blocks the skill too. The scan reads each
        # candidate file through the shared file-read guard, the one authority that owns the
        # sensitive-path, descriptor-fstat and hard-link refusals. The name and location
        # checks above clear a file by its PATH, and a hard link gives a credential file a
        # second innocent name inside the skill: skill_dir/notes.md hard-linked to
        # ~/.aws/credentials clears the path check while its bytes are the credential, and its
        # content need not match any scan pattern. ``safe_read_file_bytes_nolink`` opens the
        # leaf ``O_NOFOLLOW`` and fstats the descriptor it opened -- ``st_nlink > 1`` is the
        # identity a name check and ``scan_text`` cannot see -- and confirms the opened inode
        # resolves inside ``skill_dir`` and is not sensitive. A file it refuses blocks the
        # candidate HERE, in the curation plan, rather than letting the skill look selectable
        # and only failing at copy time, which mirrors the credential-store checks above.
        try:
            from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
        except ImportError as exc:
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=(
                        f"cannot be certified clean because kiro_crew.hooks is not importable "
                        f"here ({exc}); that module holds the sensitive-path and hard-link "
                        f"rules the credential scan has to satisfy, and a local approximation "
                        f"of them is not the same check"
                    ),
                )
            )
            continue
        hard_hit = ""
        guard_refused = ""
        for p in _pinned._walk_no_reparse(skill_dir):
            if not p.is_file() or p.is_symlink():
                continue
            # TWO refusal channels that mean different things: None is "the guard rejected
            # this", while the size cap RAISES. A file above the ceiling is an asset, not
            # scannable text, so it cannot be certified clean and blocks the skill rather
            # than shipping past an unread file.
            try:
                scanned = safe_read_file_bytes_nolink(
                    str(p), str(skill_dir), max_bytes=_MAX_PROMPT_BYTES
                )
            except FileTooLargeError:
                guard_refused = (
                    f"contains a file above the {_MAX_PROMPT_BYTES} byte scan ceiling, which "
                    f"cannot be certified clean: {p.relative_to(skill_dir).as_posix()}"
                )
                break
            if scanned is None:
                # The guard rejected the read: the file is hard-linked to another name,
                # sensitive, not a regular file, outside the skill, or unreadable. A hard
                # link is the case a name check cannot see, so a credential given a second
                # innocent name inside the skill is caught here rather than shipped.
                guard_refused = (
                    f"contains a file the file-read guard refuses (hard-linked to another "
                    f"name, sensitive, or not a readable regular file): "
                    f"{p.relative_to(skill_dir).as_posix()}"
                )
                break
            # Decode the guarded bytes exactly as they sit on disk. A file that is not UTF-8
            # is unscannable text, not a credential the scan can read: skip it here as the
            # by-name reader did, leaving the copy-time guard to refuse a non-UTF-8 member.
            try:
                text = scanned.decode("utf-8")
            except UnicodeDecodeError:
                continue
            leaks = _scan.scan_text(text, f"skills/{rel}/{p.relative_to(skill_dir).as_posix()}")
            if leaks:
                hard_hit = f"contains a credential -- {leaks[0].render()}"
                break
        if guard_refused:
            out.append(Candidate(kind="skills", id=rel, content_hash="", blocked=guard_refused))
            continue
        if hard_hit:
            out.append(Candidate(kind="skills", id=rel, content_hash="", blocked=hard_hit))
            continue
        out.append(Candidate(kind="skills", id=rel, content_hash=_hashing._tree_hash(skill_dir)))
    return out


def _canonical_server(spec: dict) -> str:
    return json.dumps(spec, sort_keys=True, ensure_ascii=False)


def mcp_candidates(agent_spec: dict) -> list[Candidate]:
    """MCP servers declared by the crew's agent spec, deny-by-default.

    Ported from ``crew_export/candidates.py:mcp_candidates``: a server reasonable
    on the owner's laptop may be a customer-reachable side effect in production,
    so tool surface is a deployment decision and an empty ``mcp.json`` is the
    expected outcome, not a degraded one.
    """
    servers = agent_spec.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    out: list[Candidate] = []
    for name, spec in sorted(servers.items()):
        if not isinstance(spec, dict):
            continue
        canonical = _canonical_server(spec)
        if name in _CONTAINER_OWNED_MCP:
            out.append(
                Candidate(
                    kind="mcp",
                    id=name,
                    content_hash=_hashing._sha(canonical.encode("utf-8")),
                    blocked="a Kiro Crew-managed server that resolves to an absolute "
                    "path on this machine; the container composes its own",
                )
            )
            continue
        leaks = _scan.scan_text(canonical, f"mcp/{name}")
        blocked = f"contains a credential -- {leaks[0].render()}" if leaks else ""
        out.append(
            Candidate(
                kind="mcp",
                id=name,
                content_hash=_hashing._sha(canonical.encode("utf-8")),
                blocked=blocked,
            )
        )
    return out


def enumerate_all(crew: ResolvedCrew, agent_spec: dict) -> dict[str, list[Candidate]]:
    return {
        "skills": skill_candidates(crew.skills_root),
        "mcp": mcp_candidates(agent_spec),
    }
