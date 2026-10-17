"""What a crew turn is told.

The brief and its presence check, the labels and vocabulary a crew may use, the
snapshot, the ``Never`` block, the nudge and the assembled turn prompt. Callers
reach every name here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.crew_runtime`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from . import crew_model, crew_store, provider

logger = logging.getLogger("kirocrew.app.issue-radar")


# ── the brief ───────────────────────────────────────────────────────────────

#: First line of ``crew_brief.md``. The injection rule keys on this string.
BRIEF_SENTINEL = "<!-- kirocrew-crew-brief v1 -->"

_BRIEF_PATH = Path(__file__).with_name("crew_brief.md")
_brief_cache: str | None = None


def brief_text() -> str:
    """The brief, read once per process.

    A missing file is returned as an empty string rather than raising: a crew with
    no brief is a bad crew, but a crash in the always-on poll loop is worse.
    """
    global _brief_cache
    if _brief_cache is None:
        try:
            _brief_cache = _BRIEF_PATH.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - defensive
            logger.warning("crew brief unreadable at %s", _BRIEF_PATH, exc_info=True)
            _brief_cache = ""
    return _brief_cache


def brief_is_present(slot: Any) -> bool:
    """Whether this session still carries the brief.

    TWO conditions, and the second one is the whole point: a message must contain
    the sentinel AND be at least as long as the brief itself. A compaction summary
    routinely quotes a marker it saw ("the session opened with
    ``<!-- kirocrew-crew-brief v1 -->`` and a work list…"), and a sentinel-only
    check would read that as a hit and leave the crew running for the rest of the
    day on a paraphrase of its own instructions. The carrying message is always
    brief + nudge, so it is strictly longer than the brief; nothing that merely
    mentions the sentinel can be.

    One rule, four situations: session start, post-compaction, gateway restart,
    and whatever truncates a window next. No heuristic and no schedule to keep in
    sync with the truncation mechanism.

    COST, measured on this install: context runs ~0.154 credits per 1k tokens on
    claude-opus-5 and the brief is ~4.2k tokens, i.e. ~0.6 credits to inject.
    Anything appended to ``slot.messages`` ACCUMULATES — it is re-sent as context
    on every later turn — so re-sending the brief on all ~80 turns of a crew's day
    costs ~2100 credits/day/crew (80 turns × the growing prefix), against a handful
    of injections for the presence check. That is the entire reason this is a
    presence check and not "every turn" or "every N turns".
    """
    brief = brief_text()
    if not brief:
        return True  # nothing to inject; never loop on a missing file
    for msg in getattr(slot, "messages", None) or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str) or BRIEF_SENTINEL not in content:
            continue
        if len(content) >= len(brief):
            return True
    return False


# ── the nudge ───────────────────────────────────────────────────────────────

#: The claim label, identical in every install. It is what one crew reads to know
#: another is already working an issue, so it is deliberately NOT configurable: an
#: install that renamed it would be invisible to every crew that did not, and both
#: would work the same issue.
CLAIM_LABEL = "crew: in progress"


def writable_labels(settings: dict[str, Any] | None = None) -> tuple[str, ...]:
    """Every label a crew in this repository may write — resolved, not a constant.

    Two of them: the claim, and the one that says a human has to look at this. The
    second is the repo's ``needs_human_label`` setting, which is the whole reason
    this is a function. Both directions of getting it wrong are load-bearing, and a
    module constant can express neither: a crew told it may NOT write the label the
    settings name has no way to hand an issue to a human, and a crew told it MAY
    write a label the settings do not name puts a label of its own invention on a
    stranger's issue — the one prohibition in the nudge with a public blast radius.

    Falls back to the store's own default, and to the claim label alone if even
    that is absent, because naming an empty label reads as permission to write one.
    """
    raw = (settings or {}).get("needs_human_label")
    needs_human = raw.strip() if isinstance(raw, str) else ""
    if not needs_human:
        fallback = crew_store.DEFAULT_SETTINGS.get("needs_human_label")
        needs_human = fallback.strip() if isinstance(fallback, str) else ""
    return (CLAIM_LABEL, needs_human) if needs_human else (CLAIM_LABEL,)


def vocabulary(key: provider.RepoKey | None = None) -> Mapping[str, str]:
    """The provider's display vocabulary, defaulting to GitHub's.

    One place resolves it, so every prompt path either takes a key or falls back
    identically. ``None`` means GitHub for the same reason
    ``provider.normalize_provider`` defaults there: it is what an install that
    predates multi-provider support is, and what a hand-built snapshot in a test
    should read as.
    """
    return provider.terms(key or provider.RepoKey())


def never_block(labels: Sequence[str] = (), terms: Mapping[str, str] | None = None) -> str:
    """The brief's ``Never`` list, compressed, naming this repo's writable labels.

    Repeated on EVERY turn, unlike the brief. It is cheap (~80 words) and it buys
    back authority the brief does not have: an injected brief arrives as a USER
    message, not a system prompt, so it is the weakest kind of instruction in the
    window and the furthest from the turn's actual work. Prohibitions kept adjacent
    to the instruction are the version that holds.

    The label clause names the resolved set rather than the ``crew:`` prefix,
    because a prefix rule cannot express this permission: ``needs_human_label`` is
    free text and need not carry the prefix at all, while any other
    ``crew:``-prefixed label — one left behind on an issue, or one a differently
    configured install writes — does carry it and is still not this crew's to write.

    The CI clause names NO PATH. ``.github/`` is wrong on GitLab and names a
    directory that does not exist on an Azure DevOps repo — and
    a per-provider path would be no better, because the prohibition is not about a
    location: a GitHub repo can be gated by a CircleCI or Jenkins config outside
    ``.github/``, and an Azure pipeline definition can live anywhere under any
    filename. The crew is reading the repository and can see where its gates run
    from; what it needs told is that those files are off limits. That is also why
    the location is deliberately NOT a ``provider.terms`` key: terms is display
    vocabulary, and a path is neither display nor reliably per-provider.

    The merge verb takes the provider's own noun (``PR`` / ``MR``), because this is
    the prohibition a crew is most likely to reason around, and one phrased in a
    vocabulary its forge does not use reads as being about something else.
    """
    allowed = ", ".join(f"`{lab}`" for lab in (labels or writable_labels()))
    vocab = terms or vocabulary()
    return (
        "Never: modify CI or gate configuration — the workflow or pipeline "
        "definitions this repo's gates run from, wherever they live, plus any rule "
        "file those gates read. They judge you. "
        f"Never write any label other than {allowed}. "
        "Never push to main or whichever branch this repo defaults to, and never "
        f"merge a {vocab['change_request_short']} yourself. Never edit another crew's "
        "claim comment, except appending a takeover to proved-dead claims. Never hold "
        "uncommitted changes in two worktrees. Never end a "
        "turn without writing the ledger. Never put an absolute path, a host name or "
        "anything else about this machine into a progress line — progress lines go "
        "public. Never report a gate as passing when you have not seen its exit code."
    )


def build_snapshot(
    owner: str,
    repo: str,
    crew: dict[str, Any],
    root: Path | None = None,
    key: provider.RepoKey | None = None,
) -> dict[str, Any]:
    """The volatile facts a crew must never guess — read from the store, no API calls.

    ``key`` carries the repo's PROVIDER into the rendered prompt. It is optional and
    defaults to GitHub, which keeps every existing caller and the legacy layout
    exactly as they were; ``sweep_repo`` — the only production path that composes a
    prompt — always has a real key and passes it.
    """
    crew_id = str(crew.get("id") or "")
    items = crew_store.list_work_items(owner, repo, crew_id, root, open_only=True)
    # The writable set belongs with everything else volatile rather than in a
    # constant: an operator can rename ``needs_human_label`` between two turns of
    # the same session, and the nudge is the only place the crew is told which
    # labels it may write.
    settings = crew_store.read_settings(owner, repo, root)
    return {
        "name": crew.get("name") or crew_id,
        "id": crew_id,
        "owner": owner,
        "repo": repo,
        # Part of the crew's identity, exactly like owner/repo, and the one field
        # that decides what the rest of the prompt CALLS things. Carried as the
        # provider name rather than a pre-rendered vocabulary so the snapshot stays
        # a set of facts and ``compose_nudge`` stays the only renderer.
        "provider": (key or provider.RepoKey()).provider,
        "labels": list(crew.get("labels") or []),
        "writable_labels": list(writable_labels(settings)),
        "open_count": crew_store.open_slot_count(owner, repo, crew_id, root),
        "max_open": int(crew.get("max_open") or 0),
        "triage_model": crew_model.triage_model(crew) if crew_model.triage_in_effect(crew) else "",
        "items": [
            {
                "number": it.get("number"),
                "phase": it.get("phase") or "",
                "next": (it.get("next") or "").strip(),
                "pr_number": it.get("pr_number"),
            }
            for it in items
        ],
    }


def compose_nudge(snapshot: dict[str, Any]) -> str:
    """The per-turn message: a volatile snapshot (~120 words) then the Never block.

    Everything in the first part can change between two turns of the same session —
    the crew may have been renamed, re-scoped, re-limited, or had an item picked up
    by a human — which is why it is re-sent every turn instead of living in the
    brief. The brief says "your name, your repository, your label scope and your
    limits arrive in the nudge"; this is that promise.

    Every noun and every sigil comes from ``provider.terms`` via the snapshot's
    ``provider`` field. The rendering an unwired version produced — ``(PR #12)`` on
    a GitLab project, "every open issue" on a board that only has work items — is
    not cosmetic: a crew told to work "issues" on Azure DevOps will look for a
    primitive that does not exist, and ``#12`` addresses a DIFFERENT item than
    ``!12`` on both GitLab and Azure, so a crew quoting the nudge back into a
    comment points at an unrelated item.
    """
    vocab = vocabulary(provider.RepoKey(provider=str(snapshot.get("provider") or "")))
    # An empty label list means EVERY open tracked item, not none. The editor
    # defaults a new crew to no labels and does not require any, so the opposite
    # reading would tell every default-configured crew to pick up nothing, and such
    # a crew would idle for its whole life without an error
    # anywhere. No backend code filters on this list; it is advisory text the crew
    # self-applies from the brief, so the wording here IS the contract, and the
    # brief's filter step is conditioned to match.
    scope = ", ".join(snapshot.get("labels") or []) or (
        f"(no label filter — every open {vocab['tracked_item']})"
    )
    allowed = [str(lab) for lab in (snapshot.get("writable_labels") or ()) if str(lab).strip()]
    if not allowed:
        # A snapshot assembled without a resolved set still has to be told something
        # true, and the store's defaults are what a repo with no settings file
        # resolves to anyway. Naming nothing here would leave the one prohibition
        # with a public blast radius unstated.
        allowed = list(writable_labels())
    lines = [
        f"[crew turn] {snapshot.get('name')} · {snapshot.get('owner')}/"
        f"{snapshot.get('repo')} · id {snapshot.get('id')}",
        f"Label scope: {scope}.",
        "Writable labels: " + ", ".join(f"`{lab}`" for lab in allowed) + " — no others, ever.",
        f"Open {snapshot.get('open_count')}/{snapshot.get('max_open')}",
    ]
    items = snapshot.get("items") or []
    if items:
        # "Work item" here is the LEDGER's own word for a crew's tracked slot (see
        # ``crew_store``), not Azure's noun for a board item, so this header stays
        # provider-independent. What varies is what each line POINTS AT.
        lines.append("Open work items:")
        for it in items:
            # The change request takes the provider's noun AND its sigil; the
            # tracked item keeps ``#``, which is correct on all three providers
            # (see ``provider._TERMS`` — only the change-request sequence diverges).
            pr = (
                f" ({vocab['change_request_short']} "
                f"{vocab['change_request_sigil']}{it['pr_number']})"
                if it.get("pr_number")
                else ""
            )
            nxt = it.get("next") or "no next step recorded — decide one and record it"
            lines.append(f"- #{it.get('number')} {it.get('phase')}{pr} — next: {nxt}")
    else:
        lines.append("Open work items: none. Pick up new work if you are under your limit.")
    triage = str(snapshot.get("triage_model") or "")
    if triage:
        # Carried only while the crew really switches (``triage_in_effect``): a
        # held, withheld or same-as-work triage model leaves every turn on the
        # session's model, and a turn told to stop short there would leave the
        # work for the idle timer. While it is in effect the rule is true on every
        # turn whichever model this one is on: the open items above tell the crew
        # which kind of turn it is.
        acting = ", ".join(sorted(crew_model.ACTING_PHASES))
        lines.append(
            f"Triage model `{triage}`: a turn with no item in an acting phase "
            f"({acting}) runs on it. On such a turn only decide what needs action; "
            "if something does, record that item's move into an acting phase and "
            "end the turn. The next turn runs on your work model."
        )
    lines.append(
        "Read the ledger first, reconcile every open item against the six unblock "
        "signals, advance ONE item, and write the ledger before the turn ends."
    )
    return "\n".join(lines) + "\n\n" + never_block(allowed, vocab)


def compose_turn_prompt(
    slot: Any,
    owner: str,
    repo: str,
    crew: dict[str, Any],
    root: Path | None = None,
    key: provider.RepoKey | None = None,
) -> str:
    """The full prompt for the next turn: the brief when it is missing, then the nudge.

    Injection rides on the prompt rather than being appended to ``slot.messages``
    on its own, because an appended message is transcript only — the agent process
    holds its own context and sees a prompt. One consequence worth knowing: the
    message that carries the brief IS the nudge message, which is what makes the
    length guard in :func:`brief_is_present` sound.

    BLOCKING — :func:`build_snapshot` globs the crew's item dir and parses every
    open item. Async callers use :func:`compose_turn_prompt_async`.
    """
    return _assemble_turn_prompt(slot, build_snapshot(owner, repo, crew, root, key))


async def compose_turn_prompt_async(
    slot: Any,
    owner: str,
    repo: str,
    crew: dict[str, Any],
    root: Path | None = None,
    key: provider.RepoKey | None = None,
) -> str:
    """:func:`compose_turn_prompt` with the store reads off the event loop.

    Every launch and every wake composes a prompt, and the work behind it grows
    with the crew: one directory glob plus a JSON parse per open work item. A crew
    that has been running for a while would stall the gateway — and with it the
    always-on poll loop this app lives in, the one thing that must never block,
    since the sweep is what wakes a crew when CI turns red.

    Only the snapshot moves. The presence check stays on the loop because it walks
    ``slot.messages``, which a running turn appends to; that is the same split
    ``rehydrate_slot_from_history_async`` documents for slot state.
    """
    snapshot = await asyncio.to_thread(partial(build_snapshot, owner, repo, crew, root, key))
    return _assemble_turn_prompt(slot, snapshot)


def _assemble_turn_prompt(slot: Any, snapshot: dict[str, Any]) -> str:
    """Nudge from an already-read snapshot, with the brief in front when it is gone."""
    nudge = compose_nudge(snapshot)
    if brief_is_present(slot):
        return nudge
    return brief_text() + "\n\n---\n\n" + nudge
