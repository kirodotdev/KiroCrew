"""Bounded live observation of the owner's dashboard tab, for the agent's guides.

The agent's ``find_ui`` answers from the packaged index, which says what exists in
this build. Whether the person's tab shows a control RIGHT NOW is a different
question, and only that tab can answer it. This module is the gateway's half of
that question: it holds one in-flight request per ask, delivers it to ONE tab
over the owner socket (``guide_observe`` frame), and accepts that tab's reply
on the owner-only ``POST /api/guide/observe`` route.

What may cross, and nothing else:

* The REQUEST names manifest-validated curated location ids (at most
  :data:`MAX_TARGETS`), the reveal scopes their plans use and the runtime
  predicates their steps carry (at most :data:`MAX_SCOPES` /
  :data:`MAX_PREDICATES`). No route, selector, text or entity id is ever asked
  for.
* The REPLY is ``{tab_id, request_id, build_digest, document_epoch, sequence,
  targets: [{id, status}], scopes: [{id, state}], predicates: [{id, state}]}``
  with exactly the requested ids, each status from :data:`TARGET_STATUSES`,
  each scope state from :data:`SCOPE_STATES` (``unknown``: no owner of it is
  mounted), each predicate state from :data:`PREDICATE_STATES`. Any other
  field, a missing or extra id, or a non-enum value refuses the whole reply: a
  tab has no way to send page text through this channel.

Which tab: the guide's owner tab once a guide is running in the caller's slot;
before Start, the tab that sent that slot's latest chat message (recorded from
the ``X-Guide-Tab`` header of the owner's own ``POST /api/chat``). With neither,
the answer is ``not_observed`` -- replies from several tabs are never merged.

Freshness: a reply must answer the pending ``request_id`` from the tab it was
asked of, within :data:`OBSERVE_WAIT_SECONDS`; a tab's ``document_epoch`` is
fixed for its page load and its ``sequence`` only rises, so a late, replayed or
foreign reply is dropped. Everything here lives in memory, expires, and is never
persisted or logged with its content.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from kiro_crew.dashboard.guide_runs import GuideError

#: The browser waits for nothing; the agent's ``find_ui`` waits at most this
#: long for the tab's reply (its own HTTP budget is 1.5 s).
OBSERVE_WAIT_SECONDS = 1.2
#: A slot's latest-sender tab is forgotten after this long.
SENDER_TTL_SECONDS = 30 * 60
#: Ceiling on target ids per request, and on reveal scopes.
MAX_TARGETS = 16
MAX_SCOPES = 8
MAX_PREDICATES = 8
#: Ceiling on concurrently pending requests, remembered senders and tab epochs.
MAX_PENDING = 32
MAX_SENDERS = 256
MAX_TABS = 256

STATUS_POINTABLE = "pointable"
STATUS_OFFSCREEN = "offscreen"
STATUS_HIDDEN = "hidden"
STATUS_UNMOUNTED = "unmounted"
STATUS_DISABLED = "disabled"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNKNOWN = "unknown"
TARGET_STATUSES = frozenset(
    {
        STATUS_POINTABLE,
        STATUS_OFFSCREEN,
        STATUS_HIDDEN,
        STATUS_UNMOUNTED,
        STATUS_DISABLED,
        STATUS_AMBIGUOUS,
        STATUS_UNKNOWN,
    }
)

SCOPE_OPEN = "open"
SCOPE_CLOSED = "closed"
SCOPE_UNKNOWN = "unknown"
SCOPE_STATES = frozenset({SCOPE_OPEN, SCOPE_CLOSED, SCOPE_UNKNOWN})
PREDICATE_MET = "met"
PREDICATE_UNMET = "unmet"
PREDICATE_UNKNOWN = "unknown"
PREDICATE_STATES = frozenset({PREDICATE_MET, PREDICATE_UNMET, PREDICATE_UNKNOWN})

#: ``live.status`` when no fresh reply arrived (or none could be asked for).
NOT_OBSERVED = "not_observed"

REASON_NO_TAB = "no_tab"
REASON_STALE_TAB = "stale_tab"
REASON_BUILD_MISMATCH = "build_mismatch"

_REPLY_KEYS = frozenset(
    {
        "tab_id",
        "request_id",
        "build_digest",
        "document_epoch",
        "sequence",
        "targets",
        "scopes",
        "predicates",
    }
)
_TAB_MAX = 128
_EPOCH_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_DIGEST_MAX = 80
_SEQUENCE_MAX = 2**53


def _clean_tab(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _TAB_MAX:
        return None
    if any(ord(ch) < 0x21 or ch == "\x7f" for ch in value):
        return None
    return value


@dataclass
class _Pending:
    request_id: str
    tab_id: str
    targets: tuple[str, ...]
    scopes: tuple[str, ...]
    build_digest: str
    future: asyncio.Future[dict[str, Any]]
    predicates: tuple[str, ...] = ()


@dataclass
class ObservationHub:
    """Every in-flight observation and the little it needs to route one, in memory."""

    clock: Callable[[], float] = time.time
    _pending: dict[str, _Pending] = field(default_factory=dict)
    _senders: OrderedDict[str, tuple[str, float]] = field(default_factory=OrderedDict)
    _tabs: OrderedDict[str, tuple[str, int]] = field(default_factory=OrderedDict)
    _ui_langs: OrderedDict[str, tuple[str, float]] = field(default_factory=OrderedDict)

    # ── which tab ──

    def note_sender(self, slot_key: str, tab_id: object) -> None:
        """Remember the tab that sent *slot_key*'s latest message (owner sends only)."""
        tab = _clean_tab(tab_id)
        if not tab or not slot_key:
            return
        self._senders.pop(slot_key, None)
        self._senders[slot_key] = (tab, self.clock())
        while len(self._senders) > MAX_SENDERS:
            self._senders.popitem(last=False)

    def sender_for(self, slot_key: str) -> str | None:
        held = self._senders.get(slot_key)
        if held is None:
            return None
        tab, at = held
        if self.clock() - at >= SENDER_TTL_SECONDS:
            self._senders.pop(slot_key, None)
            return None
        return tab

    # ── which language that tab shows ──

    def note_ui_lang(self, slot_key: str, tag: str) -> None:
        """Remember the UI language *slot_key*'s latest sending tab renders.

        *tag* must already be a validated shipped-catalog tag
        (``context.normalize_ui_language_tag``); a blank one is ignored, so a
        send without the header keeps the last known language.
        """
        if not slot_key or not tag:
            return
        self._ui_langs.pop(slot_key, None)
        self._ui_langs[slot_key] = (tag, self.clock())
        while len(self._ui_langs) > MAX_SENDERS:
            self._ui_langs.popitem(last=False)

    def ui_lang_for(self, slot_key: str) -> str | None:
        held = self._ui_langs.get(slot_key)
        if held is None:
            return None
        tag, at = held
        if self.clock() - at >= SENDER_TTL_SECONDS:
            self._ui_langs.pop(slot_key, None)
            return None
        return tag

    # ── one request ──

    def begin(
        self,
        *,
        tab_id: str,
        targets: tuple[str, ...],
        scopes: tuple[str, ...],
        build_digest: str,
        predicates: tuple[str, ...] = (),
    ) -> _Pending:
        if len(self._pending) >= MAX_PENDING:
            raise GuideError(429, "too_many_observations", "too many observations are in flight")
        loop = asyncio.get_running_loop()
        p = _Pending(
            request_id=f"o_{secrets.token_urlsafe(12)}",
            tab_id=tab_id,
            targets=targets,
            scopes=scopes,
            build_digest=build_digest,
            future=loop.create_future(),
            predicates=predicates,
        )
        self._pending[p.request_id] = p
        return p

    def end(self, request_id: str) -> None:
        """Forget a request: answered, timed out or abandoned. Nothing is kept."""
        p = self._pending.pop(request_id, None)
        if p is not None and not p.future.done():
            p.future.cancel()

    def pending_count(self) -> int:
        return len(self._pending)

    # ── the tab's reply ──

    def deliver(self, body: dict[str, Any]) -> None:
        """Validate one reply and hand it to the request waiting on it.

        Raises :class:`GuideError` for any malformed, foreign, late or replayed
        reply; nothing of a refused reply is kept.
        """
        extra = set(body) - _REPLY_KEYS
        missing = _REPLY_KEYS - set(body)
        if extra or missing:
            raise GuideError(400, "invalid_observation", "unexpected observation fields")
        tab = _clean_tab(body.get("tab_id"))
        request_id = body.get("request_id")
        if tab is None or not isinstance(request_id, str) or len(request_id) > 64:
            raise GuideError(400, "invalid_observation", "tab_id and request_id are required")
        p = self._pending.get(request_id)
        if p is None or p.future.done():
            raise GuideError(409, "observation_expired", "no such observation is waiting")
        if p.tab_id != tab:
            raise GuideError(
                409, "observation_wrong_tab", "this observation was asked of another tab"
            )
        digest = body.get("build_digest")
        epoch = body.get("document_epoch")
        seq = body.get("sequence")
        if not isinstance(digest, str) or len(digest) > _DIGEST_MAX:
            raise GuideError(400, "invalid_observation", "build_digest must be text")
        if not isinstance(epoch, str) or not _EPOCH_RE.match(epoch):
            raise GuideError(400, "invalid_observation", "document_epoch is malformed")
        if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq < _SEQUENCE_MAX:
            raise GuideError(400, "invalid_observation", "sequence must be a non-negative integer")
        targets = _exact_entries(body.get("targets"), p.targets, "status", _is_status)
        scopes = _exact_entries(body.get("scopes"), p.scopes, "state", _is_scope_state)
        predicates = _exact_entries(
            body.get("predicates"), p.predicates, "state", _is_predicate_state
        )
        held = self._tabs.get(tab)
        if held is not None:
            held_epoch, held_seq = held
            if held_epoch != epoch or seq <= held_seq:
                raise GuideError(409, "observation_stale", "a newer reply from this tab was seen")
        self._tabs.pop(tab, None)
        self._tabs[tab] = (epoch, seq)
        while len(self._tabs) > MAX_TABS:
            self._tabs.popitem(last=False)
        if digest != p.build_digest:
            # The tab runs another build: its ids may not mean ours. Nothing it
            # said about them is used.
            p.future.set_result({"status": NOT_OBSERVED, "reason": REASON_BUILD_MISMATCH})
            return
        p.future.set_result(
            {
                "status": "observed",
                "observed_at": self.clock(),
                "targets": [{"id": i, "status": targets[i]} for i in p.targets],
                "scopes": [{"id": i, "state": scopes[i]} for i in p.scopes],
                "predicates": [{"id": i, "state": predicates[i]} for i in p.predicates],
            }
        )


def _is_status(value: object) -> bool:
    return isinstance(value, str) and value in TARGET_STATUSES


def _is_scope_state(value: object) -> bool:
    return isinstance(value, str) and value in SCOPE_STATES


def _is_predicate_state(value: object) -> bool:
    return isinstance(value, str) and value in PREDICATE_STATES


def _exact_entries(
    raw: object, wanted: tuple[str, ...], key: str, ok: Callable[[object], bool]
) -> dict[str, Any]:
    """``[{id, <key>}]`` naming each of *wanted* exactly once, with valid values."""
    if not isinstance(raw, list) or len(raw) != len(wanted):
        raise GuideError(400, "invalid_observation", "the reply must answer exactly the ids asked")
    out: dict[str, Any] = {}
    allowed = set(wanted)
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"id", key}:
            raise GuideError(400, "invalid_observation", "an entry carries unexpected fields")
        ident, value = item.get("id"), item.get(key)
        if not isinstance(ident, str) or ident not in allowed or ident in out or not ok(value):
            raise GuideError(400, "invalid_observation", "an entry is not one that was asked")
        out[ident] = value
    return out


def observation_hub_for(state: Any) -> ObservationHub:
    """The one hub attached to this gateway's dashboard state."""
    hub = getattr(state, "_guide_observation_hub", None)
    if not isinstance(hub, ObservationHub):
        hub = ObservationHub()
        state._guide_observation_hub = hub
    return hub


def note_chat_sender(state: Any, slot_key: str, tab_id: object, ui_lang: object = None) -> None:
    """Record the owner tab that just sent a message to *slot_key*. Never raises.

    *ui_lang* is that tab's ``X-UI-Lang`` header: the language its dashboard
    renders. Kept only when it names a shipped catalog
    (``context.normalize_ui_language_tag``), so ``find_ui`` can quote labels
    as that screen shows them.
    """
    try:
        hub = observation_hub_for(state)
        if tab_id:
            hub.note_sender(slot_key, tab_id)
        if ui_lang:
            from kiro_crew.context import normalize_ui_language_tag

            hub.note_ui_lang(slot_key, normalize_ui_language_tag(ui_lang, source="X-UI-Lang"))
    except Exception:  # pragma: no cover - a hint must never break a send
        pass
