"""``PagePort``: an app's handle on the browser page of one of its own chat slots.

An app's handle on its own slot's page, handed out by ``ctx.browser.page(slot)``
(``apps/browser_sdk.py``), behind the ``permissions.browser`` grant and the
``app_browser_sdk`` preview, both re-read at every call. RFC
``rfc-app-sdk-browser.md`` sections 2 and 4.

One door. Every page operation goes through
:func:`kiro_crew.dashboard.handlers.browser_ops.run_browser_op`, the function the
``/api/browser/op`` route (the Bot's ``browser`` tool) calls too, with the slot's
own runtime session key (``dashboard:<slot>``), the key the Bot in that slot
browses under. So the on/off switch (``capabilities.browse``, read for that key,
never for the app's name), the navigate gate on the requested address, an app's
``browserSites``, the takeover hold, the gateway-wide lock, the tab binding and the
per-caller checks apply to an app exactly as they apply to the Bot, from one copy.
An app op queues on the same lock as the Bot's.

Which tabs. A slot is the app's only when its key is spelled ``<app>-<id>`` AND the
live slot under it carries the app's stamp (``slot._app``): an app name can extend
another's (``foo`` and ``foo-bar``), so a prefix alone is not enough. The check is
:class:`~kiro_crew.apps.chat_slot_sdk.ChatSlotSDK`'s own, and runs on every call,
so a slot archived since the port was handed out answers ``not_your_slot``. Tabs
come from the slot's own ledger (``LiveHub.targets_of``); a user's tab, another
app's or another chat's never appears.

Trust. Not a boundary against an app's own in-gateway Python, which runs trusted
(RFC section 3, the wording of ``chat_slot_sdk.py``): it is the supported surface
and the place the identity checks live.

Answers are values, never exceptions: ``{"ok": True, ...}``, or ``{"ok": False,
"code", "error"}`` with a fixed ``code`` (``policy_denied``, ``url_blocked``,
``password_field``, ``user_holds``, ``not_your_slot``, ``permission_denied``,
``preview_off``, plus the op's own refusals). A verb the grant does not hold answers
``permission_denied``, never a silent no-op.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: The verbs ``permissions.browser`` grants (RFC section 1).
VERB_VIEW = "view"
VERB_READ = "read"
VERB_ACT = "act"
VERBS = (VERB_VIEW, VERB_READ, VERB_ACT)

#: Default and largest ``outline`` budget, in characters.
OUTLINE_BUDGET = 6000
#: Largest image ``screenshot`` hands back, in bytes.
SCREENSHOT_MAX_BYTES = 8 * 1024 * 1024

_NOT_YOUR_SLOT = "not_your_slot"
_PERMISSION_DENIED = "permission_denied"


def _refusal(code: str, error: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error}


def _read_screenshot(path: str) -> bytes | None:
    """The image at *path*, or ``None``. Blocking: run it off the loop.

    The image sits in a leaf an agent's sandbox can write, so it is read with no
    link followed, pinned to that leaf, the cap enforced on the bytes read (over it
    raises ``FileTooLargeError``).
    """
    from kiro_crew.apps.backend_sdk.files import read_bytes_nofollow
    from kiro_crew.browser_harness.ops import screenshot_dir
    from kiro_crew.config.paths import config_dir

    return read_bytes_nofollow(
        path,
        within_root=os.path.realpath(screenshot_dir(config_dir())),
        max_bytes=SCREENSHOT_MAX_BYTES,
        within_root_is_canonical=True,
    )


def owns_slot(state: Any, app: str, key: Any) -> bool:
    """Whether *key* is one of *app*'s live slots: spelled ``<app>-<id>`` and stamped."""
    from kiro_crew.apps.chat_slot_sdk import ChatSlotSDK, _live

    if not isinstance(key, str) or not key:
        return False
    slot = _live(state, key)
    return ChatSlotSDK(app).owns(slot)


#: Keys whose values are identifiers the gateway minted, never page text: kept as sent.
_ID_KEYS = frozenset({"ok", "code", "ref", "target_id", "returned_to", "op"})


def _redacted(value: Any, key: str = "") -> Any:
    """*value* with every page string redacted whole, as the Bot's tool redacts its text.

    Every answer an app gets passes here before any cut, so no credential is ever
    split by a cut into a head the redactor does not match.
    """
    if isinstance(value, str) and key not in _ID_KEYS:
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        value, _ = redact_exfiltration_urls(value)
        value, _ = redact_credentials(value)
        return value
    if isinstance(value, dict):
        return {k: _redacted(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v, key) for v in value]
    return value


def _flat(answer: dict[str, Any]) -> dict[str, Any]:
    """One shape for the route's bodies: the op's result, or a refusal with ``ok: False``.

    The route's ``user_in_control`` hold (which the Bot's tool parks on) reads
    ``user_holds`` here: an app is told the user has the page, and waits on nothing.
    """
    if answer.get("ok") is True and isinstance(answer.get("result"), dict):
        return dict(answer["result"])
    if answer.get("ok") is True:
        return dict(answer)
    out = {k: v for k, v in answer.items() if k != "hold"}
    out["ok"] = False
    if out.get("code") == "user_in_control":
        out["code"] = "user_holds"
        out["error"] = "the user has taken over this page; try again once they hand it back"
    out.setdefault("code", "browser_error")
    out.setdefault("error", "")
    return out


class PagePort:
    """The page of one app slot: view it, read it, act on it, through the Bot's own door."""

    __slots__ = ("_app", "_key", "_verbs", "_verb_gate", "_state")

    def __init__(
        self,
        app: str,
        slot_key: str,
        *,
        verbs: Any = (),
        state: Any,
        verb_gate: Callable[[str], tuple[str, str] | None] | None = None,
    ) -> None:
        """*verbs* is the set the owner approved (``app-grants``); *state* the dashboard's.

        *verb_gate*, when given, replaces *verbs*: a blocking call, run off the loop at
        every call, that answers ``(code, error)`` for a verb the app may not use now,
        or ``None``. ``ctx.browser.page`` passes one, so a revoke, the preview turned
        off or the app turned off applies to a port the app already holds.
        """
        if not isinstance(app, str) or not app:
            raise ValueError("a page port needs the app's name")
        self._app = app
        self._key = slot_key
        self._verbs = frozenset(v for v in (verbs or ()) if v in VERBS)
        self._verb_gate = verb_gate
        self._state = state

    @property
    def slot(self) -> str:
        return self._key

    @property
    def session_key(self) -> str:
        """The slot's runtime session key: the one the Bot in it browses under."""
        return f"dashboard:{self._key}"

    # ── gates ───────────────────────────────────────────────────────────────

    def _caller(self) -> Any:
        from kiro_crew.dashboard.handlers.browser_ops import GatewayCaller

        return GatewayCaller(f"app:{self._app}")

    def _audit(self, outcome: str, error: str = "") -> None:
        """One row in the gateway's audit log, as ``run_browser_op`` writes for an op."""
        from kiro_crew.dashboard.handlers import browser_ops

        browser_ops._audit(self._caller(), outcome, error)

    async def _not_allowed(self, verb: str) -> dict[str, Any] | None:
        if not owns_slot(self._state, self._app, self._key):
            self._audit("denied", _NOT_YOUR_SLOT)
            return _refusal(_NOT_YOUR_SLOT, f"{self._key!r} is not one of {self._app!r}'s slots")
        if self._verb_gate is not None:
            # Off the loop: the gate reads the config and the app's grant files.
            refused = await asyncio.to_thread(self._verb_gate, verb)
        elif verb not in self._verbs:
            refused = (
                _PERMISSION_DENIED,
                f"the owner has not approved browser {verb!r} for {self._app!r}",
            )
        else:
            refused = None
        if refused is not None:
            code, error = refused
            self._audit("denied", code)
            return _refusal(code, error)
        return None

    async def _gate(self, verb: str) -> dict[str, Any] | None:
        """For the methods that do not run an op: slot, verb, and the on/off switch.

        Audited here, since no op runs: a refusal and an allowed call each write a row.
        """
        refused = await self._not_allowed(verb)
        if refused is not None:
            return refused
        from kiro_crew.dashboard.handlers.browser_policy import browse_refusal

        # Asked for the slot's own session key, and a deny stops the engine, as
        # ``run_browser_op`` does for every op.
        denied = await browse_refusal(session_key=self.session_key, audit=False)
        if denied is not None:
            from kiro_crew.browser_harness.governance import DENIED_CODE, DENIED_TEXT

            self._audit("denied", DENIED_CODE)
            return _refusal(DENIED_CODE, DENIED_TEXT)
        self._audit("allowed")
        return None

    async def _op(self, verb: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """The op's answer, every page string in it redacted."""
        return dict(_redacted(await self._op_raw(verb, op, args)))

    async def _op_raw(self, verb: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """The op's answer as the route returns it; the caller redacts before handing it out."""
        refused = await self._not_allowed(verb)
        if refused is not None:
            return refused
        from kiro_crew.dashboard.handlers.browser_ops import run_browser_op

        answer, _status = await run_browser_op(
            self.session_key,
            op,
            args,
            state=self._state,
            caller=self._caller(),
        )
        return _flat(answer)

    @staticmethod
    def _hub() -> Any:
        from kiro_crew.browser_harness.live_hub import get_live_hub

        return get_live_hub()

    def _viewport(self) -> dict[str, float] | None:
        """The streamed viewport's size in CSS px, ``{w, h}``, or ``None`` before a frame.

        An app page has only frame pixels, whose width differs from the viewport's
        and changes with the card: ``x * w / frame_width`` turns one into the other.
        """
        sess = self._hub().get(self._key)
        stream = getattr(sess, "stream", None)
        w = getattr(stream, "device_width", 0.0)
        h = getattr(stream, "device_height", 0.0)
        ok = all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
            for v in (w, h)
        )
        return {"w": float(w), "h": float(h)} if ok else None

    # ── view ────────────────────────────────────────────────────────────────

    async def aim(self, x: float, y: float, label: str = "") -> dict[str, Any]:
        """Point the cursor drawn over the live view at (*x*, *y*), viewport CSS px.

        Drawn over the frame by the card, never inside the page. ``shown`` is false
        when no live view streams the slot now.
        """
        refused = await self._gate(VERB_VIEW)
        if refused is not None:
            return refused
        return {"ok": True, "shown": self._hub().aim(self._key, x, y, label)}

    async def view_state(self) -> dict[str, Any]:
        """The live view's state, address and title, whether the user holds the page, and
        ``viewport``: the viewport's ``{w, h}`` in CSS px, ``None`` before a frame."""
        refused = await self._gate(VERB_VIEW)
        if refused is not None:
            return refused
        hub = self._hub()
        sess = hub.get(self._key)
        if sess is None:
            return {
                "ok": True,
                "state": "",
                "url": "",
                "title": "",
                "user_holds": False,
                "viewport": None,
            }
        return {
            "ok": True,
            "state": sess.state,
            "url": _redacted(sess.url),
            "title": _redacted(sess.title),
            "user_holds": bool(hub.in_control(self._key)),
            "viewport": self._viewport(),
        }

    # ── read ────────────────────────────────────────────────────────────────

    async def tabs(self) -> dict[str, Any]:
        """This slot's own tabs: ``{"ok": True, "tabs": [{target_id, url, title}]}``."""
        return await self._op(VERB_READ, "tabs", {"action": "list"})

    async def outline(self, *, budget: int = OUTLINE_BUDGET) -> dict[str, Any]:
        """The page outline the Bot reads (``snapshot``), cut to *budget* characters."""
        size = budget if isinstance(budget, int) and not isinstance(budget, bool) else 0
        size = max(1, min(size, OUTLINE_BUDGET)) if size else OUTLINE_BUDGET
        from kiro_crew.browser_harness.ops import TEXT_CAP

        out = await self._op_raw(VERB_READ, "snapshot", {})
        raw = out.get("snapshot")
        if isinstance(raw, str) and len(raw) >= TEXT_CAP and raw.strip():
            # The op cut it, maybe mid-credential: that last word is withheld.
            kept = raw.rstrip()
            out["snapshot"] = kept[: len(kept) - len(kept.split()[-1])]
        # Redacted whole, and only then cut to the budget.
        out = dict(_redacted(out))
        if out.get("ok") and isinstance(out.get("snapshot"), str):
            text = out["snapshot"]
            out["snapshot"] = text[:size]
            if len(text) > size:
                out["truncated"] = True
        return out

    async def screenshot(self, *, region: Any = None) -> dict[str, Any]:
        """A PNG of the page: ``{"ok": True, "png": bytes}``. *region* as the op takes it."""
        args: dict[str, Any] = {} if region is None else {"region": region}
        out = await self._op_raw(VERB_READ, "screenshot", args)
        if not out.get("ok"):
            return dict(_redacted(out))
        path = str(out.pop("path", ""))
        out = dict(_redacted(out))
        from kiro_crew.apps.backend_sdk.files import FileTooLargeError

        try:
            data = await asyncio.to_thread(_read_screenshot, path)
        except FileTooLargeError:
            self._audit("error", "screenshot_too_large")
            return _refusal(
                "screenshot_too_large", f"the image is over {SCREENSHOT_MAX_BYTES} bytes"
            )
        if data is None:
            self._audit("error", "screenshot_error")
            return _refusal("screenshot_error", "the browser wrote no readable image")
        out["png"] = data
        return out

    async def pick(self, x: float, y: float) -> dict[str, Any]:
        """The element drawn at view point (*x*, *y*), viewport CSS px.

        ``{"ok": True, "ref", "tag", "text", "box": {x, y, w, h}, "viewport"}``; the
        ref is one ``click`` and ``type`` take. A protected field answers
        ``password_field``.
        """
        return self._with_viewport(await self._op(VERB_READ, "pick", {"x": x, "y": y}))

    async def locate(self, texts: list[str]) -> dict[str, Any]:
        """Where each of *texts* is drawn: ``{"ok": True, "found": [{text, boxes}]}``.

        ``boxes`` are viewport CSS px, one per visible occurrence, in page order;
        ``viewport`` is as :meth:`view_state` answers it.
        """
        return self._with_viewport(await self._op(VERB_READ, "locate", {"texts": texts}))

    def _with_viewport(self, out: dict[str, Any]) -> dict[str, Any]:
        if out.get("ok"):
            out["viewport"] = self._viewport()
        return out

    # ── act ─────────────────────────────────────────────────────────────────

    async def navigate(self, url: str) -> dict[str, Any]:
        return await self._op(VERB_ACT, "navigate", {"url": url})

    async def click(self, ref: str) -> dict[str, Any]:
        return await self._op(VERB_ACT, "click", {"ref": ref})

    async def type(self, ref: str, text: str) -> dict[str, Any]:
        return await self._op(VERB_ACT, "type", {"ref": ref, "text": text})


__all__ = ["VERBS", "PagePort", "owns_slot"]
