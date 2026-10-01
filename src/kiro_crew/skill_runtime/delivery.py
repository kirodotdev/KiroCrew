"""What reaches the prompt: the skills directory, required bodies and trigger delivery.

``get_context`` renders the bounded startup directory, or the explicit unbudgeted
legacy reader. On the budgeted path every ``always: true`` body is charged against
``PINNED_SKILL_BODIES_CAP``, and one that cannot fit or load raises rather than
being trimmed.
``split_triggered`` and ``trigger_hint`` decide how a trigger match is
delivered: a body by default, a one-line pointer for an unconfined skill that
opted out. Matching itself (``SkillsLoader.get_triggered_skills``) stays in the
facade.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.skill_runtime import listing as _listing

if TYPE_CHECKING:
    from kiro_crew.skills import SkillsLoader

logger = logging.getLogger("kiro_crew.skills")


def _namespace_groups(skills: list[dict]) -> list[tuple[str, int]]:
    """Family label and member count for the skills the discovery entry omits.

    Eight names out of a thousand skills describe what the operator used lately,
    not what the machine can do, and the tail is reachable only by a search whose
    keywords the model has to guess. A family label plus its size is the cheapest
    thing that answers "is there anything here about X": one short line, flat in
    the number of skills, and phrased in the same vocabulary the keys use, so
    ``app-* (40)`` is already a usable query.

    The label is the key's FIRST segment — the directory for a nested key
    (``kirocrew-dev/babysit`` -> ``kirocrew-dev/``), otherwise the leading hyphen
    token (``web-verify`` -> ``web-*``). One rule, so the line cannot reorder
    itself as skills are added. A family of one is left out: its label would carry
    no more than the name already does.
    """
    counts: dict[str, int] = {}
    for skill in skills:
        key = str(skill.get("key", ""))
        if "/" in key:
            label = key.split("/", 1)[0] + "/"
        elif "-" in key:
            label = key.split("-", 1)[0] + "-*"
        else:
            continue
        counts[label] = counts.get(label, 0) + 1
    return sorted(
        ((label, count) for label, count in counts.items() if count > 1),
        key=lambda pair: (-pair[1], pair[0]),
    )


def _family_line(skills: list[dict]) -> str:
    """One short line naming the families *skills* belong to, or ``""``.

    Bounded at :data:`_FAMILY_LINE_MAX_LABELS` labels, with the rest reported as
    a count, so the cost of this line does not scale with the catalog.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    groups = _namespace_groups(skills)
    if not groups:
        return ""
    shown, rest = groups[: sk._FAMILY_LINE_MAX_LABELS], groups[sk._FAMILY_LINE_MAX_LABELS :]
    text = ", ".join(f"{label} ({count})" for label, count in shown)
    if rest:
        text += f", +{len(rest)} more"
    return text


def split_triggered(
    loader: SkillsLoader, names: list[str], project_dir: str | Path | None = None
) -> tuple[list[str], list[str]]:
    """Split matched *names* into (inject-body, pointer-only), order preserved.

    Full-body injection is the DEFAULT: a matched skill's procedure lands in
    the prompt whether or not the agent chooses to read a file. An
    unconfined skill opts out with ``inject_on_trigger: false``, which
    reduces its contribution to a single pointer line naming it and its
    path. Confined project skills always inject their body: handing the
    agent a live path would bypass the descriptor-confined reader if the
    checkout replaced ``SKILL.md`` after discovery.

    The default is deliberately the expensive one. A pointer makes delivery
    voluntary, so a skill authored to be *obeyed* on match — a mandatory
    pre-flight check, say — would be silently skipped by an agent that
    declines to read it, and a silent miss is the failure mode with no
    signal to catch it. Defaulting the other way would make forgetting the
    field fail open. Opting out is a per-skill statement that the skill is
    an offer rather than a mandate, which only its author can make.
    """
    enforced: list[str] = []
    pointer_only: list[str] = []
    for name in names:
        # project_dir must reach here: get_triggered_skills can match a
        # trusted project's own skill, and resolving project-blind would
        # return None and DROP it — no body and no pointer, so a matched
        # skill would silently contribute nothing.
        found = loader._resolve_path_and_root(name, project_dir)
        if found is None:
            continue
        skill_file, within = found
        # A reader on the per-message path: one SKILL.md that is not UTF-8
        # costs its own match, never the turn (rationale on the helper).
        meta = loader._readable_frontmatter(skill_file, within=within)
        if meta is None:
            continue
        if within is not None:
            enforced.append(name)
        elif meta.get("inject_on_trigger", "").strip().lower() == "false":
            pointer_only.append(name)
        else:
            enforced.append(name)
    return enforced, pointer_only


def trigger_hint(
    loader: SkillsLoader, names: list[str], project_dir: str | Path | None = None
) -> str:
    """Return a pointer block naming *names* and where to read each one.

    The counterpart to :meth:`get_triggered_skills` for an unconfined skill
    that opted out of full-body injection with ``inject_on_trigger: false``:
    the matcher decides which skills look relevant, and this renders that
    verdict as one line per skill instead of the skill's body. A body costs
    8k-34k chars and is charged again on every turn the match repeats; a line
    costs ~150. Confined project skills are omitted defensively because the
    agent would follow the path outside the confined reader.

    The agent reaches the procedure the same way ``get_context``'s
    ``## Available Skills`` block already directs it to — by reading the
    path. The wording deliberately does NOT ask for a re-read of a skill
    already present earlier in the conversation: ACP replays native
    history, so that content is still in the window, and a needless ``cat``
    would spend a tool round-trip only to put the body back in as tool
    output.

    Returns ``""`` for an empty *names* (no block, not an empty header).
    """
    lines: list[str] = []
    for name in names:
        # project_dir must reach here for the same reason it must reach
        # split_triggered: a trusted project's own skill can match, and
        # resolving project-blind drops it -- the pointer block would name
        # nothing and the operator would see a match that led nowhere.
        found = loader._resolve_path_and_root(name, project_dir)
        if found is None:
            continue
        skill_file, within = found
        if within is not None:
            continue
        # A reader on the per-message path: one SKILL.md that is not UTF-8
        # costs its own pointer line, never the turn (rationale on the helper).
        meta = loader._readable_frontmatter(skill_file, within=within)
        if meta is None:
            continue
        desc = loader._short_desc(meta.get("description", "") or name, suffix="…")
        lines.append(f"- **{meta.get('name', name)}**: {desc} → `{skill_file}`")
    if not lines:
        return ""
    return (
        "[Relevant skills for this message]\n"
        "These skills match this message. If one applies, read its file "
        "before acting — unless it already appears earlier in this "
        "conversation, in which case you already have its instructions.\n"
        + "\n".join(lines)
        + "\n[End of relevant skills]\n\n"
    )


def get_context(
    loader: SkillsLoader,
    budget: int | None = None,
    only: list[str] | None = None,
    project_dir: str | Path | None = None,
    project_body_budget: int | None = None,
    *,
    discovery_only: bool = False,
    required_parts_out: list[str] | None = None,
) -> str:
    """Build a bounded directory over the agent's resolved available set.

    Mapping grants availability, not eager body delivery. Ordinary global and
    confined skills load on demand through scoped search/list/read. Required
    ``always:true`` bodies share PINNED_SKILL_BODIES_CAP; exceeding it raises
    SkillContextCapacityError instead of silently dropping instructions.

    ``budget`` bounds optional discovery characters. ``discovery_only`` selects
    the shorter pointer; both variants expose complete paginated discovery.
    ``required_parts_out`` separates complete required bodies for the caller's
    protected-content admission. ``only=[]`` admits nothing. The explicit
    ``budget=None`` catalog reader retains its legacy unbudgeted rendering.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    all_skills = loader.scoped_skills(project_dir=project_dir, only=only)
    # Scope BEFORE anything is rendered. Dropping a repo-scoped skill only
    # from the injected body still leaves its summary line in the index, and
    # the index tells the agent to read the full file for anything related —
    # so an out-of-scope skill stays one `cat` away and its repo-specific
    # procedure gets applied to the wrong project. Filtering the list is the
    # single place that covers the index, both renderers, and the pinned set.
    # Collapse verified byte-identical copies of the same skill before
    # anything is rendered, for the same reason the scope filter above
    # lives here: this is the single place that covers the index, both
    # renderers, and the pinned set. Multi-root installs commonly
    # materialize one skill twice — a package tree and a flat mirror of it
    # — at different key depths, so `_iter_uncached`'s per-key shadowing
    # never sees the collision and the injected index carries N identical
    # summary lines (and, for a pinned skill, N identical full bodies).
    # Dropping a copy is only safe when the bytes are the same, and
    # `_dedupe_identical_skills` verifies exactly that: same-metadata rows
    # whose content differs are all kept.
    all_skills = _listing._dedupe_identical_skills(all_skills)
    # A first-run scope whose discovery pass has not finished enumerates to
    # nothing, and nothing is indistinguishable from a machine with no skills.
    # That matters here and nowhere else: `always: true` bodies are REQUIRED
    # instructions, so returning "" would drop them with no signal — the one
    # outcome the loader refuses everywhere else (see SkillContextCapacityError,
    # which fails loudly rather than trimming a required body). A first run
    # cannot know which skills are `always: true` without enumerating, so the
    # honest answer is neither to block the turn on the walk nor to present a
    # truncated set as complete: state that discovery is still running, so the
    # agent knows its instructions may be incomplete and can re-read them once
    # it finishes. Emitted whether or not mapped rows made `all_skills`
    # non-empty, because an incomplete catalog is incomplete either way.
    notice = (
        sk._DISCOVERY_IN_PROGRESS_NOTICE if loader.catalog_status(project_dir) == "building" else ""
    )
    if not all_skills:
        return notice
    if budget is None:
        return notice + loader._legacy_context(
            all_skills,
            restricted=only is not None,
            project_dir=project_dir,
            project_body_budget=project_body_budget,
        )
    # get_always_skills() returns the _iter() identifier — the same value
    # list_skills() exposes as "key" (the dir-relative path, e.g.
    # "team-capabilities/build-helper"), NOT the frontmatter "name". So the
    # pinned check below, _record_use() (also called with the _iter
    # identifier), and _rank_key()'s score(s["key"]) are all consistently
    # keyed by "key" — there is no key/name mismatch here.
    pinned = {str(s["key"]) for s in all_skills if s.get("always")}

    parts: list[str] = []
    pinned_spent = 0

    # Pinned global skills: full content, always injected.
    # A confined path must never be offered to the agent for a later direct
    # read, because that read would sit outside the descriptor-pinned gate.
    for s in all_skills:
        if s["key"] not in pinned:
            continue
        remaining = max(0, sk.PINNED_SKILL_BODIES_CAP - pinned_spent)
        if s.get("confine_root"):
            remaining = min(remaining, sk.PROJECT_SKILL_BODY_CAP)
        content = loader.read_scoped_skill(
            str(s["key"]), only=only, project_dir=project_dir, max_bytes=remaining
        )
        if content is None:
            detail = (
                f"the {sk.PROJECT_SKILL_BODY_CAP}-byte per-project-skill limit, "
                if s.get("confine_root")
                else ""
            )
            raise sk.SkillContextCapacityError(
                f"Required skill {s['key']!r} could not be loaded within {detail}"
                f"the {sk.PINNED_SKILL_BODIES_CAP}-byte total startup instruction "
                "capacity, or its file was unreadable. Reduce always:true skills "
                "or their bodies and verify the file before retrying."
            )
        stripped = loader.strip_frontmatter(content)
        rendered = f"### Skill: {s['key']}\n\n{stripped}"
        pinned_spent += len(rendered.encode("utf-8")) + 8
        if pinned_spent + 64 > sk.PINNED_SKILL_BODIES_CAP:
            raise sk.SkillContextCapacityError(
                f"Required skills exceed the {sk.PINNED_SKILL_BODIES_CAP}-byte "
                "startup instruction capacity; reduce always:true skills."
            )
        parts.append(rendered)

    def wrap(items: list[str]) -> str:
        if not items:
            return ""
        return "[Skills:]\n" + "\n\n---\n\n".join(items) + "\n[End of skills]\n\n"

    required = wrap(parts)
    if required_parts_out is not None:
        if required:
            required_parts_out.append(required)
        parts = []
    # Without a split consumer, required bodies still spend the total
    # allowance before optional entries. They are never clipped to fit it.
    # The discovery notice is charged here too, so an incomplete catalog cannot
    # push the assembled block past the caller's budget.
    optional_budget = max(
        0, budget - len(notice) - (len(required) if required_parts_out is None else 0)
    )
    optional: list[str] = []
    on_demand = [s for s in all_skills if s["key"] not in pinned]
    if on_demand and discovery_only:
        pointer = (
            "## Skill discovery\n\n"
            "Use skill_search(query) to search this agent’s available skills. "
            "Use skill_search(action='list', offset=0) to browse all pages and "
            "skill_search(action='read', key='full/key') for exact instructions. "
            "Search returns confined project bodies safely; never read project paths directly. "
            "Read returned instructions before use; $skillname loads an explicit skill.\n"
        )
        # Equal usage ranks fall back to key order so the eight names shown
        # do not depend on directory iteration order.
        by_key = sorted(on_demand, key=lambda s: s["key"])
        named: set[str] = set()
        for skill in sorted(by_key, key=loader._rank_key, reverse=True)[:8]:
            line = f"- {skill['key']}: {loader._short_desc(skill['description'])[:100]}\n"
            if len(wrap([pointer + line])) <= optional_budget:
                pointer += line
                named.add(str(skill["key"]))
        # Families last, and only over what was NOT named: the line exists to
        # cover what the entry hides, so repeating a family whose members are
        # all listed spends the budget saying nothing and can crowd out a
        # family that is genuinely unreachable. A name that did not fit is
        # still hidden, so only an admitted one is excluded here.
        groups = _family_line([s for s in on_demand if str(s["key"]) not in named])
        if groups:
            line = f"More families: {groups}\n"
            if len(wrap([pointer + line])) <= optional_budget:
                pointer += line
        if len(wrap([pointer])) <= optional_budget:
            optional.append(pointer)
    elif on_demand:
        ranked = sorted(on_demand, key=loader._rank_key, reverse=True)
        header = (
            "## Available Skills\n\n"
            "Search this agent's scope with skill_search(query). "
            "Browse all pages with skill_search(action='list', offset=0); "
            "load instructions with skill_search(action='read', key='full/key'). "
            "Read instructions before use; run scripts from the skill directory.\n\n"
        )

        def summary(lines: list[str], hidden: list[dict]) -> str:
            # *hidden* is passed in rather than derived from the row count: the
            # admission loop below SKIPS a row that does not fit and keeps
            # trying later ones, so the admitted rows are not a prefix of
            # `candidates` and a tail slice would name the wrong skills.
            footer: list[str] = []
            if hidden:
                footer.append(
                    f"- _...and {len(hidden)} more skill(s) not shown here. Find them "
                    "with scoped search or paginated list above._"
                )
                families = _family_line(hidden)
                if families:
                    footer.append(f"- _Families not shown: {families}._")
            return header + "\n".join(lines + footer)

        candidates = [
            f"- **{s['name']}**: {loader._short_desc(s['description'])} -> "
            + (f"skill_search('{s['key']}')" if s.get("confine_root") else f"`{s['path']}`")
            for s in ranked
        ]
        lines: list[str] = []
        admitted: list[int] = []
        hidden: list[dict] = []
        if len(wrap(optional + [summary(candidates, [])])) <= optional_budget:
            # A complete index needs no omission footer; reserve none when
            # it fits, including the exact-fit boundary.
            lines = candidates
        else:
            remaining = optional_budget - len(wrap([summary([], ranked)]))
            for index, candidate in enumerate(candidates):
                cost = len(candidate) + 1
                if cost <= remaining:
                    admitted.append(index)
                    remaining -= cost
            taken = set(admitted)
            lines = [candidates[i] for i in admitted]
            hidden = [s for i, s in enumerate(ranked) if i not in taken]
        block = summary(lines, hidden)
        # Removing rows can change which family labels win the bounded hint.
        # Reconcile that exact footer without slicing a name or instruction.
        while lines and len(wrap(optional + [block])) > optional_budget:
            admitted.pop()
            taken = set(admitted)
            lines = [candidates[i] for i in admitted]
            hidden = [row for i, row in enumerate(ranked) if i not in taken]
            block = summary(lines, hidden)
        if len(wrap(optional + [block])) <= optional_budget:
            optional.append(block)

    return notice + (required if required_parts_out is None else "") + wrap(optional)


def _legacy_context(
    loader: SkillsLoader,
    all_skills: list[dict],
    restricted: bool = False,
    project_dir: str | Path | None = None,
    project_body_budget: int | None = None,
) -> str:
    """Explicit unbudgeted reader, not the default startup path.

    Full content for unconfined pinned (``always: true``) skills, bounded
    bodies for confined project skills, and a one-line summary for every
    unconfined on-demand skill, unranked and untruncated. Project bodies
    replace their unsafe live-path summaries; unconfined skills retain the
    behavior from before lazy loading.

    *restricted* marks *all_skills* as already narrowed by an agent's
    ``skill://`` mapping, so the always-loaded set is narrowed to match: a
    pinned skill outside the mapping must NOT be force-injected, or the
    mapping would not actually bound what the agent sees.

    *project_dir* is forwarded to the ``repo_scope`` gate so this path
    scopes pinned skills exactly as the lazy-load path does — the default
    block must not be the one that leaks a repo-scoped skill.
    """
    always = loader.get_always_skills(project_dir)
    if restricted:
        allowed = {s["key"] for s in all_skills} | {s["name"] for s in all_skills}
        always = [a for a in always if a in allowed]
    parts: list[str] = []
    project_skills = [s for s in all_skills if s.get("confine_root")]
    project_keys = {s["key"] for s in project_skills}
    # Full content for unconfined always-loaded skills. Confined pinned
    # skills join every other project row in the bounded loop below.
    for name in always:
        if name in project_keys:
            continue
        content = loader.load_skill(name, project_dir)
        if content:
            stripped = loader.strip_frontmatter(content)
            parts.append(f"### Skill: {name}\n\n{stripped}")
    loader._append_project_skill_bodies(parts, project_skills, project_dir, project_body_budget)
    # Summary for on-demand skills
    on_demand = [s for s in all_skills if s["name"] not in always and not s.get("confine_root")]
    if on_demand:
        summary_lines = [
            "## Available Skills",
            "",
            "If a user request relates to any skill below, read the full "
            "skill file first with `cat <path>` before responding.",
            "To run a skill's scripts, `cd` into the directory containing its `SKILL.md`.",
            "",
        ]
        for s in on_demand:
            summary_lines.append(
                f"- **{s['name']}**: {loader._short_desc(s['description'])} → `{s['path']}`"
            )
        parts.append("\n".join(summary_lines))
    return "[Skills:]\n" + "\n\n---\n\n".join(parts) + "\n[End of skills]\n\n"


def _append_project_skill_bodies(
    loader: SkillsLoader,
    parts: list[str],
    project_skills: list[dict],
    project_dir: str | Path | None,
    budget: int | None,
) -> None:
    """Append confined bodies without reading beyond the section budget."""
    wrapper_size = len("[Skills:]\n") + len("\n[End of skills]\n\n")
    separator_size = len("\n\n---\n\n")
    used = wrapper_size + sum(len(part) for part in parts)
    if parts:
        used += separator_size * (len(parts) - 1)

    for skill in project_skills:
        prefix = f"### Skill: {skill['key']}\n\n"
        next_separator = separator_size if parts else 0
        max_bytes: int | None = None
        if budget is not None:
            max_bytes = budget - used - next_separator - len(prefix)
            if max_bytes <= 0:
                break
            # The enumeration's size is only a hint because the file can be
            # replaced afterward. It avoids opening a file that cannot fit;
            # max_bytes on the descriptor-pinned read closes the race.
            if int(skill.get("size_bytes", 0)) > max_bytes:
                continue
        content = loader.load_skill(skill["key"], project_dir, max_bytes=max_bytes)
        if not content:
            continue
        part = prefix + loader.strip_frontmatter(content)
        if budget is not None and used + next_separator + len(part) > budget:
            continue
        parts.append(part)
        used += next_separator + len(part)


def _recency_boost(loader: SkillsLoader, path_str: str, fingerprint: str = "") -> float:
    """Return the file mtime if the skill is newer than the boost window,
    else 0.0. Lets a freshly-added, never-used skill rank above stale unused
    ones (cold-start protection) without flooding the top of the list.

    The mtime comes from the row's stat *fingerprint* when it carries one, so
    ranking adds no syscall to a turn: ``list_skills`` already recorded that
    stat, or reused the one the catalog walk took. Ranking every row on the
    calling thread was the second O(N) stat pass a message paid, next to the
    metadata validation one. A row with no usable fingerprint — a confined
    project row, or a mapped row whose fingerprint carries its mapping root —
    is stat'ed exactly as before.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    mtime, _size = _listing._fingerprint_mtime_and_size(fingerprint) if fingerprint else (None, 0)
    if mtime is None:
        try:
            mtime = Path(path_str).stat().st_mtime
        except OSError:
            return 0.0
    return mtime if (time.time() - mtime) < sk._NEW_SKILL_BOOST_WINDOW_SECS else 0.0


def _rank_key(loader: SkillsLoader, s: dict) -> tuple[float, float]:
    """Sort key for on-demand skills: (usage_hits, effective_recency).
    Higher sorts first. Falls back to recency-only if the ledger is absent."""
    boost = loader._recency_boost(s["path"], str(s.get("fingerprint") or ""))
    if loader._usage is None:
        return (0.0, boost)
    return loader._usage.score(s["key"], recency_boost=boost)


def _short_desc(desc: str, suffix: str = "...") -> str:
    """Collapse whitespace and truncate a description for the summary line.

    Cuts on a word boundary when one falls in the last fifth of the budget so
    the line ends on a readable word instead of mid-token; a description with
    no such boundary (one very long token) is cut hard.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    d = " ".join((desc or "").split())
    if len(d) <= sk._SHORT_DESC_CHARS:
        return d
    cut = d[: sk._SHORT_DESC_CHARS]
    space = cut.rfind(" ")
    if space >= sk._SHORT_DESC_CHARS * 4 // 5:
        cut = cut[:space]
    return cut.rstrip() + suffix
