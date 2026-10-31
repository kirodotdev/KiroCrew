"""What a warm task brief reads: the owner's pending ask, what changed while
they were away, a bounded live read of the pull requests the asks name, the
turns since their last message, and the open work items.

Every section has its own budget and drops whole entries, so the model always
sees the ask and the changes first and never a cut JSON document (the old
40,000-character slice of the whole payload cut the transcript off). Each fact
carries its source and the time it was read, so the brief can say "not checked
since HH:MM" instead of repeating a stale claim as current.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.history_schema import transcript_sort_key
from kiro_crew.session_summary import TranscriptTurn

logger = logging.getLogger(__name__)

# Per-section budgets, in characters of each entry's compact JSON. A section
# keeps whole entries, most important first, until the next would not fit.
PENDING_ASK_CHARS = 6_000
CHANGES_CHARS = 5_000
LIVE_PRS_CHARS = 3_000
TRANSCRIPT_CHARS = 16_000
OPEN_ITEMS_CHARS = 9_000
# One entry larger than its whole section keeps both ends of its text with a
# visible omission marker: the text field is cut, never the JSON around it.
_ENTRY_TEXT_CHARS = 4_000
_ITEM_TEXT_CHARS = 400
# The live read: at most this many pull requests, each answer reused for 15
# minutes (the repo's GitHub polling rule).
MAX_LIVE_PRS = 5
LIVE_PR_TTL_SECS = 15 * 60
_MAX_LIVE_CACHE = 256
# Buttons: the pending ask's own choices first, then one per open question.
MAX_BUTTONS = 6
_MAX_BUTTON_CHARS = 80
CHECKS_NOT_READ = "not fully read"
_RECENT_OWNER_TURNS = 3
_OWNER_SAMPLE_CHARS = 300

_PR_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})/pull/(\d{1,9})\b"
)
_PR_HASH_RE = re.compile(r"(?<![\w&/])#(\d{1,9})\b")
_REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
# Characters that would end or split an [OPTIONS: ...] label.
_BUTTON_UNSAFE_RE = re.compile(r"[|\[\]\u3010\u3011\uFF3B\uFF3D\u3014\u3015\n\r]+")

_ASK_BUTTON_TEXT = {
    "en": "Tell me what {title} needs from me",
    "zh": "告诉我「{title}」需要我做什么",
    "ja": "「{title}」で私がすべきことを教えて",
    "ko": "'{title}'에 내가 할 일을 알려 줘",
    "ru": "Скажи, что нужно от меня по «{title}»",
    "hi": 'बताओ "{title}" के लिए मुझे क्या करना है',
    "bn": '"{title}"-এর জন্য আমাকে কী করতে হবে বলো',
}


def parse_epoch(raw: object) -> float | None:
    """Epoch of an ISO-8601 stamp; a naive one reads as UTC."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def clock(epoch: float | None) -> str:
    """``HH:MM`` in the owner's configured time zone (with the date when it is
    not today), or "" for no time."""
    if epoch is None:
        return ""
    try:
        # Deferred: the cron package imports the gateway's config stack, which
        # this module (loaded with the dashboard) must not pull in at import.
        from kiro_crew.cron_service.schedule import get_local_tz

        zone = get_local_tz()[1]
    except Exception:
        zone = timezone.utc
    when = datetime.fromtimestamp(epoch, zone)
    # Another day gets its date, so "since 14:05" never reads as today.
    same_day = when.date() == datetime.now(zone).date()
    return when.strftime("%H:%M" if same_day else "%Y-%m-%d %H:%M")


def _ends(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    half = max(1, (cap - 40) // 2)
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n{text[-half:]}"


def _size(entry: object) -> int:
    return len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))


def fit(entries: Iterable[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """The leading *entries* whose compact JSON fits *budget*, whole."""
    kept: list[dict[str, Any]] = []
    used = 0
    for entry in entries:
        size = _size(entry)
        if used + size > budget:
            break
        kept.append(entry)
        used += size
    return kept


# ---------------------------------------------------------------- pending ask


@dataclass(frozen=True)
class PendingAsk:
    """The thread's newest reply, when it offers choices the owner has not answered."""

    text: str
    options: tuple[str, ...]
    epoch: float | None


def pending_ask(rows: Sequence[Mapping[str, Any]]) -> PendingAsk | None:
    """The newest assistant reply ending in ``[OPTIONS: ...]``, unless answered.

    The same walk the "Waiting on you" list makes (``member_waiting``): the
    newest conversation row decides, and any later user or assistant row,
    a brief or a wake included, retires the ask. An older ask is never brought
    back (docs/decisions/2026-09-13-options-are-read-from-the-newest-reply-only.md).
    """
    # circular import: dashboard.state imports the warm brief through its
    # handlers, as member_waiting's same walk notes.
    from kiro_crew.dashboard.state import _OPTIONS_RE, _parse_options
    from kiro_crew.dashboard.system_notices import is_system_notice

    for row in reversed(rows):
        if not isinstance(row, Mapping):
            continue
        meta = row.get("meta")
        if isinstance(meta, Mapping) and meta.get(HUMAN_TURN_META_KEY) is True:
            return None
        role = row.get("role")
        content = row.get("content")
        if role not in ("user", "assistant") or is_system_notice(role, meta or {}):
            continue
        if not isinstance(content, str) or not content:
            continue
        if role == "user":
            return None
        options = _parse_options(content)
        if not options:
            return None
        text = _OPTIONS_RE.sub("", content).strip()
        return PendingAsk(
            text=_ends(text, _ENTRY_TEXT_CHARS),
            options=tuple(options),
            epoch=_row_epoch(row.get("ts")),
        )
    return None


def _row_epoch(raw: object) -> float | None:
    if not isinstance(raw, str) or not raw:
        return None
    bucket, epoch = transcript_sort_key(raw)
    return epoch if bucket == 0 else None


# ---------------------------------------------------------------- items


def _item_epochs(item: Mapping[str, Any]) -> list[float]:
    return [
        epoch
        for key in ("last_report_at", "decided_at", "closed_at")
        if (epoch := parse_epoch(item.get(key))) is not None
    ]


def changed_since(item: Mapping[str, Any], anchor: float | None) -> bool:
    """Whether a worker report, a decision or a close landed after *anchor*."""
    return anchor is not None and any(epoch > anchor for epoch in _item_epochs(item))


def is_open_ask(item: Mapping[str, Any], terminal: Callable[[object], bool]) -> bool:
    return not terminal(item.get("state")) and item.get("status") in ("question", "blocked")


def _item_entry(item: Mapping[str, Any], anchor: float | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "item_id": item.get("item_id"),
        "title": item.get("title"),
        "state": item.get("state"),
        "status": item.get("status"),
    }
    if item.get("reason"):
        entry["reason"] = item["reason"]
    # Spelled "#N", the form the brief writes and the PR-number post-check reads.
    if isinstance(item.get("pr"), int) and item["pr"] > 0:
        entry["pr"] = f"#{item['pr']}"
    for key in ("summary", "decision"):
        value = item.get(key)
        if isinstance(value, str) and value:
            entry[key] = _ends(value, _ITEM_TEXT_CHARS)
    reported = parse_epoch(item.get("last_report_at"))
    if reported is not None:
        entry["worker_report_at"] = clock(reported)
    if changed_since(item, anchor):
        entry["changed_since_away"] = True
    return entry


def open_item_entries(
    items: Sequence[Mapping[str, Any]],
    anchor: float | None,
    terminal: Callable[[object], bool],
) -> list[dict[str, Any]]:
    """Open items, and terminal ones that closed while the owner was away.

    Questions and blocked items first, then the ones that changed, then the
    rest by their newest report.
    """
    chosen = [
        item for item in items if not terminal(item.get("state")) or changed_since(item, anchor)
    ]

    def rank(item: Mapping[str, Any]) -> tuple[int, int, float]:
        epochs = _item_epochs(item)
        return (
            0 if is_open_ask(item, terminal) else 1,
            0 if changed_since(item, anchor) else 1,
            -max(epochs, default=0.0),
        )

    return fit((_item_entry(item, anchor) for item in sorted(chosen, key=rank)), OPEN_ITEMS_CHARS)


def session_changed_since(session_state: Mapping[str, Any], anchor: float | None) -> bool:
    """Whether the thread's session ledger recorded progress after *anchor*."""
    stamp = parse_epoch(session_state.get("last_progress_at"))
    return anchor is not None and stamp is not None and stamp > anchor


def change_entries(
    items: Sequence[Mapping[str, Any]],
    anchor: float | None,
    session_state: Mapping[str, Any] | None = None,
    live: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """What changed since *anchor*: item reports, decisions and closes,
    session-ledger events, and PRs the live read found merged or closed.
    Newest first."""
    rows: list[tuple[float, dict[str, Any]]] = []
    for item in items:
        if not changed_since(item, anchor):
            continue
        epoch = max(_item_epochs(item))
        closed = parse_epoch(item.get("closed_at"))
        source = "work ledger" if closed is not None and closed == epoch else "worker report"
        entry = {
            "item": item.get("title"),
            "state": item.get("state"),
            "status": item.get("status"),
            "source": source,
            "at": clock(epoch),
        }
        rows.append((epoch, {k: v for k, v in entry.items() if v}))
    events = (session_state or {}).get("events")
    for event in events if isinstance(events, list) else []:
        stamp = parse_epoch(event.get("ts")) if isinstance(event, Mapping) else None
        if anchor is None or stamp is None or stamp <= anchor:
            continue
        rows.append(
            (
                stamp,
                {
                    "event": _ends(str(event.get("text") or ""), _ITEM_TEXT_CHARS),
                    "kind": event.get("kind"),
                    "source": "session ledger",
                    "at": clock(stamp),
                },
            )
        )
    for pr in live:
        if pr.get("checked") and pr.get("state") in ("merged", "closed"):
            rows.append(
                (
                    float(pr.get("_read_epoch") or 0.0),
                    {"pr": pr["pr"], "state": pr["state"], "source": "GitHub", "at": pr["read_at"]},
                )
            )
    rows.sort(key=lambda pair: -pair[0])
    return fit((entry for _, entry in rows), CHANGES_CHARS)


# ---------------------------------------------------------------- transcript


def turns_since_owner(turns: Sequence[TranscriptTurn]) -> list[dict[str, Any]]:
    """The owner's last genuine message and every turn after it, whole.

    Over budget, the oldest turns drop first; the owner's message is kept as
    long as anything is.
    """
    start = 0
    for index in range(len(turns) - 1, -1, -1):
        turn = turns[index]
        if turn.role == "user" and not turn.injected:
            start = index
            break
    entries = [
        {
            "role": "automation" if turn.injected else turn.role,
            "at": clock(_row_epoch(turn.ts)),
            "text": _ends(turn.text, _ENTRY_TEXT_CHARS),
        }
        for turn in turns[start:]
        if turn.text.strip("\u200b \t\r\n")
    ]
    if not entries:
        return []
    head, rest = entries[0], entries[1:]
    budget = TRANSCRIPT_CHARS - _size(head)
    kept: list[dict[str, Any]] = []
    for entry in reversed(rest):
        size = _size(entry)
        if size > budget:
            break
        kept.append(entry)
        budget -= size
    kept.reverse()
    return ([head] if budget >= 0 else []) + kept


def assistant_spoke_since(turns: Sequence[TranscriptTurn], anchor: float | None) -> bool:
    """Whether the thread said anything visible after *anchor*."""
    if anchor is None:
        return False
    for turn in turns:
        epoch = _row_epoch(turn.ts)
        if (
            turn.role == "assistant"
            and epoch is not None
            and epoch > anchor
            and turn.text.strip("\u200b \t\r\n")
        ):
            return True
    return False


def owner_samples(turns: Sequence[TranscriptTurn]) -> list[str]:
    """The owner's last three genuine messages, for the reply language."""
    genuine = [t.text for t in turns if t.role == "user" and not t.injected and t.text.strip()]
    return [text[:_OWNER_SAMPLE_CHARS] for text in genuine[-_RECENT_OWNER_TURNS:]]


# ---------------------------------------------------------------- live PRs


@dataclass(frozen=True)
class PrRef:
    repo: str
    number: int

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.number}"


def default_repo(items: Sequence[Mapping[str, Any]], texts: Iterable[str]) -> str | None:
    """The one repository the thread's work names, or ``None`` when it names
    none or several: a bare ``#123`` is read only against an unambiguous repo."""
    repos = {
        repo
        for item in items
        if isinstance(item.get("acceptance"), Mapping)
        and isinstance(repo := item["acceptance"].get("repo"), str)
        and _REPO_RE.match(repo)
    }
    for text in texts:
        repos.update(f"{m.group(1)}/{m.group(2)}" for m in _PR_URL_RE.finditer(text))
    return repos.pop() if len(repos) == 1 else None


def pr_refs(texts: Iterable[str], repo: str | None, limit: int = MAX_LIVE_PRS) -> list[PrRef]:
    """Pull requests the *texts* name, in order, without repeats, at most *limit*."""
    refs: list[PrRef] = []
    for text in texts:
        found: list[tuple[int, PrRef]] = [
            (m.start(), PrRef(f"{m.group(1)}/{m.group(2)}", int(m.group(3))))
            for m in _PR_URL_RE.finditer(text)
        ]
        if repo:
            found.extend(
                (m.start(), PrRef(repo, int(m.group(1)))) for m in _PR_HASH_RE.finditer(text)
            )
        for _, ref in sorted(found, key=lambda pair: pair[0]):
            if ref not in refs:
                refs.append(ref)
            if len(refs) >= limit:
                return refs
    return refs


def pr_numbers(text: str) -> set[int]:
    """Every pull-request number *text* names, as ``#N`` or a PR URL."""
    numbers = {int(m.group(3)) for m in _PR_URL_RE.finditer(text)}
    numbers.update(int(m.group(1)) for m in _PR_HASH_RE.finditer(text))
    return numbers


def facts_of(response: object) -> dict[str, Any] | None:
    """One probe result's ``response`` reduced to what the brief states, or
    ``None`` for an error result (it carries no response)."""
    # Deferred: the monitoring stack is only needed once a brief reads a PR.
    from kiro_crew.monitoring.github_pull_request import GitHubPullRequestResponse
    from kiro_crew.monitoring.pull_request import PullRequestFacts

    # The GitHub provider hands back its own response type; the shared facts
    # type carries the same fields, so either is read the same way.
    if not isinstance(response, (GitHubPullRequestResponse, PullRequestFacts)):
        return None
    checks = {"failed": 0, "pending": 0, "passed": 0, "unknown": 0}
    for check in response.checks:
        if check.state in checks:
            checks[check.state] += 1
    # A check read that did not finish says nothing about green or red.
    complete = bool(getattr(response, "checks_complete", True))
    return {
        "state": "draft" if response.state == "open" and response.draft else response.state,
        "review": response.review_decision or "",
        "mergeable": response.mergeability or "",
        # A closed or merged PR's checks are not reported; an open one whose
        # check read did not finish says so in words, which survive the
        # None-dropping in read_live_prs.
        "checks": ((checks if complete else CHECKS_NOT_READ) if response.state == "open" else None),
    }


def _probe_github(urls: list[str]) -> dict[str, dict[str, Any] | None]:
    """Read each PR's state through the structured monitor's GitHub probe."""
    from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProvider

    results = GitHubPullRequestProvider().probe(urls)
    return {url: facts_of(getattr(results.get(url), "response", None)) for url in urls}


# Module seam: tests replace it, so no unit test ever runs gh.
probe_github: Callable[[list[str]], dict[str, dict[str, Any] | None]] = _probe_github
# url -> (read epoch, facts). Shared by every thread in this process.
_live_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def read_live_prs(refs: Sequence[PrRef], now: float | None = None) -> list[dict[str, Any]]:
    """One entry per ref: its live state and read time, or ``checked: false``
    with the time of the last good read. Never raises."""
    now = time.time() if now is None else now
    stale = [
        ref.url
        for ref in refs
        if ref.url not in _live_cache or now - _live_cache[ref.url][0] >= LIVE_PR_TTL_SECS
    ]
    if stale:
        try:
            fresh = await asyncio.to_thread(probe_github, stale)
        except Exception:
            logger.debug("warm task brief live PR read failed", exc_info=True)
            fresh = {}
        for url, facts in fresh.items():
            if isinstance(facts, dict):
                _live_cache.pop(url, None)
                _live_cache[url] = (now, facts)
        while len(_live_cache) > _MAX_LIVE_CACHE:
            _live_cache.pop(next(iter(_live_cache)))
        failed = {url for url in stale if not isinstance(fresh.get(url), dict)}
    else:
        failed = set()
    entries: list[dict[str, Any]] = []
    for ref in refs:
        cached = _live_cache.get(ref.url)
        entry: dict[str, Any] = {"pr": f"#{ref.number}", "repo": ref.repo, "url": ref.url}
        if cached is None or ref.url in failed:
            entry["checked"] = False
            entry["not_checked_since"] = clock(cached[0]) if cached else "never"
        else:
            entry.update(cached[1])
            entry["checked"] = True
            entry["source"] = "GitHub"
            entry["read_at"] = clock(cached[0])
            entry["_read_epoch"] = cached[0]
        entries.append({k: v for k, v in entry.items() if v is not None})
    return entries


def public_live(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """*entries* without the private read epoch, within the section budget."""
    return fit(
        ({k: v for k, v in e.items() if not k.startswith("_")} for e in entries), LIVE_PRS_CHARS
    )


# ---------------------------------------------------------------- buttons


def button_label(label: str) -> str:
    text = " ".join(_BUTTON_UNSAFE_RE.sub(" ", label).split())
    return text if len(text) <= _MAX_BUTTON_CHARS else text[: _MAX_BUTTON_CHARS - 1].rstrip() + "…"


def buttons(
    pending: PendingAsk | None,
    ask_items: Sequence[Mapping[str, Any]],
    language: str,
) -> list[str]:
    """The choices the brief offers, from parsed data only, never model text:
    the pending ask's options verbatim, then one per open question or block."""
    labels: list[str] = []
    for option in pending.options if pending else ():
        label = button_label(option)
        if label and label not in labels:
            labels.append(label)
    template = _ASK_BUTTON_TEXT.get(language, _ASK_BUTTON_TEXT["en"])
    for item in ask_items:
        title = button_label(str(item.get("title") or ""))[:40].rstrip()
        label = button_label(template.format(title=title)) if title else ""
        if label and label not in labels:
            labels.append(label)
    return labels[:MAX_BUTTONS]
