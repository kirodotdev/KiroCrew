"""Sign-in from the Secrets vault: a ``credentials`` quick ask answered with a saved login.

The agent's side is the plain sign-in ask: the same ``ask`` op, the same
outcome words. Only the card adds:

* The vault holds login items whose URLs match the origin the ask is bound to:
  the card shows each one by its username, and the person's click on one fills its username
  and password through the same bound, re-verified write a typed answer takes
  (``quick_ask.fill`` / ``quick_ask.sign_in``).
* The person types (none matched, or "Other account" beside a match). When the
  typed username is not one the card offered, the answer route watches the tab
  after the fill (:func:`wait_signed_in`); once the sign-in landed, the same
  sheet asks "Save this login?". The typed username and password wait for that
  answer in gateway memory only (:func:`hold_offer`), never on disk, and are
  dropped on "Not now", a newer offer or :data:`SAVE_OFFER_TTL_S`. "Save"
  stores them as a NEW login item named after the host, with ``urls`` = the
  bound origin, under a free name (an existing item is never overwritten).

Matching is by BASE DOMAIN, as 1Password's and Bitwarden's defaults do: a
stored URL matches when its host and the ask's host have the same registrable
domain (eTLD+1) by the Public Suffix List, private section included, so a
login for ``appleid.apple.com`` is offered on ``developer.apple.com`` and one
for ``a.github.io`` is not offered on ``b.github.io``. The list is the
``publicsuffixlist`` wheel's own copy, read from disk, never fetched. Port is
ignored there. A host with no registrable domain (an IP address, ``localhost``,
a single label, a host that is itself a public suffix) matches only the same
host on the same port. Hosts compare in punycode; one that will not encode
matches nothing, as does a URL that does not parse. Scheme: an http page is
offered only logins stored for http (no downgrade); an http login on an
https page is fine.

The lookup, the read and the save all run in the gateway. What leaves this
module: each offered login's entry name and username to the owner's card (and
a held offer's site and typed username), and the username and password to the
page. The
answer route (``dashboard/handlers/browser_ask.py``) writes one SEL row per
vault fill and per save with the item name and the origin. The usernames reach
only the owner-only live event (the card). Never a password there, and never a
password or a username in a log, an exception, the SEL, the replay, the poll,
the tool result or anything else the agent reads.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging
import secrets
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from kiro_crew.apps.app_hosts import normalize_host
from kiro_crew.browser_harness import quick_ask as qa
from kiro_crew.secrets import SecretVault
from kiro_crew.secrets.items import ITEM_TYPE_LOGIN, ItemError, decode_item

logger = logging.getLogger(__name__)

#: The most matching logins one card carries; past it the card says how many it
#: does not show. Far above any real site's count, so every match is reachable.
MAX_OFFERED = 50
#: The longest item name the card shows (a longer one is not offered).
NAME_MAX = 120

#: SEL operations.
OP_FILL = "browser_ask_vault_fill"
OP_SAVE = "browser_ask_vault_save"

#: How long the sheet's "Signing in…" watches the tab for the sign-in to land.
LAND_WAIT_S = 30.0
#: How long the bound site must show no password box before the sign-in counts as landed.
LAND_QUIET_S = 2.0
#: How long a typed login waits in gateway memory for the person's Save or Not now.
SAVE_OFFER_TTL_S = 120.0


def _vault() -> SecretVault:
    from kiro_crew.config.loader import config_dir

    return SecretVault(config_dir())


@functools.lru_cache(maxsize=1)
def _psl() -> Any:
    """The Public Suffix List bundled in the wheel, parsed once."""
    from publicsuffixlist import PublicSuffixList

    return PublicSuffixList()


def _site(url: str) -> tuple[str, str, int] | None:
    """``(scheme, punycode host, port)`` of an http(s) URL, or ``None`` when it will not parse."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        normalized = normalize_host(host)  # punycode; None when it will not encode
        if not normalized or normalized.startswith("."):
            return None
        host = normalized
    return parts.scheme, host, port or (443 if parts.scheme == "https" else 80)


def _registrable(host: str) -> str | None:
    """The host's eTLD+1, or ``None`` for an IP, a single label or a public suffix."""
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    found = _psl().privatesuffix(host)
    return found if isinstance(found, str) and found else None


def url_matches(url: Any, origin: str) -> bool:
    """Whether a stored login URL may be offered on *origin* (base domain; see the module doc)."""
    if not isinstance(url, str) or not origin:
        return False
    stored, page = _site(url), _site(origin)
    if stored is None or page is None:
        return False
    if page[0] == "http" and stored[0] != "http":
        return False  # never hand an https login to an http page
    stored_domain, page_domain = _registrable(stored[1]), _registrable(page[1])
    if stored_domain is not None and page_domain is not None:
        return stored_domain == page_domain
    if stored_domain is None and page_domain is None:
        return stored[1:] == page[1:]
    return False


def _offerable(name: str) -> bool:
    """A name the card can show and send back as written: plain one-line text."""
    return 0 < len(name) <= NAME_MAX and qa.clean_text(name, NAME_MAX) == name


def _login_fields(vault: SecretVault, names: list[str]) -> dict[str, dict[str, Any]]:
    """The decrypted fields of each readable LOGIN among *names*; others are skipped."""
    found, _unreadable = vault.get_items_readable(names)
    out: dict[str, dict[str, Any]] = {}
    for name, (item_type, plaintext) in found.items():
        if item_type != ITEM_TYPE_LOGIN:
            continue
        try:
            out[name] = decode_item(item_type, plaintext.reveal())
        except ItemError:
            continue
    return out


def _matches(fields: dict[str, Any], origin: str) -> bool:
    urls = fields.get("urls")
    return isinstance(urls, list) and any(url_matches(u, origin) for u in urls)


def offer(origin: str) -> tuple[list[tuple[str, str]], int] | None:
    """``(offers, more)``: ``(name, username)`` of each login the card offers for
    *origin*, sorted by name, and how many more matched past :data:`MAX_OFFERED`;
    ``None`` = no vault part. The username is shown to the person on the card so
    two accounts on one site differ; the password never leaves the vault here.

    ``None`` when the vault cannot be read, so the card is the plain one (no
    list, no save question). ``([], 0)``: nothing matches, and a typed
    sign-in that lands is offered for saving. The cut is counted, never silent:
    the card says how many it does not show. Blocking.
    """
    if not origin:
        return None
    try:
        vault = _vault()
        names = [r["name"] for r in vault.list_items() if r.get("type") == ITEM_TYPE_LOGIN]
        logins = _login_fields(vault, names)
    except Exception:  # noqa: BLE001 -- an unreadable vault offers nothing; no detail
        logger.warning("vault sign-in: the vault could not be read")
        return None
    matched = sorted(
        (n, _username(f)) for n, f in logins.items() if _offerable(n) and _matches(f, origin)
    )
    return matched[:MAX_OFFERED], max(0, len(matched) - MAX_OFFERED)


def _username(fields: dict[str, Any]) -> str:
    """A login's username as the card shows it: plain one-line text, or ``""``."""
    return qa.clean_text(fields.get("username"), NAME_MAX)


def login_values(name: str, origin: str) -> dict[str, str] | None:
    """``{username, password}`` of login *name*, read now, if it still matches *origin*.

    Re-read and re-matched at answer time: an item edited or deleted since the
    card was drawn is not filled. ``None`` when it is gone, not a login, does not
    match, has no password, or the vault cannot be read. Blocking.
    """
    try:
        fields = _login_fields(_vault(), [name]).get(name)
    except Exception:  # noqa: BLE001 -- unreadable reads as gone; no detail
        logger.warning("vault sign-in: the vault could not be read")
        return None
    if fields is None or not _matches(fields, origin):
        return None
    password = fields.get("password")
    username = fields.get("username")
    if not isinstance(password, str) or not password:
        return None
    return {"username": username if isinstance(username, str) else "", "password": password}


def shape(ask: qa.QuickAsk, login: dict[str, str]) -> dict[str, str] | None:
    """*login* cut to what this ask fills, then checked like a typed answer.

    The password always; the username only where the ask has a place for it
    (a bound username field, or a sign-in walk whose page showed one).
    ``None`` when it does not fit (``quick_ask.check_values``), e.g. a sign-in
    that needs a username the item does not hold.
    """
    wants_user = (
        ask.username_mode != "none"
        if ask.spec.sign_in_takeover
        else any(b.role == "username" for b in ask.bindings)
    )
    values = {"password": login["password"]}
    if wants_user and login.get("username"):
        values["username"] = login["username"]
    return qa.check_values(ask, values)


def item_name_for(origin: str) -> str:
    """The name a saved login takes: the origin's host (``[::1]`` for IPv6)."""
    try:
        host = urlsplit(origin).hostname or ""
    except ValueError:
        host = ""
    if ":" in host:
        host = f"[{host}]"
    return host or "login"


async def save_login(origin: str, values: dict[str, str], reserved: Callable[[str], bool]) -> str:
    """Store the person's typed *values* as a new login for *origin*; the name used, or ``""``.

    Never overwrites (``SecretVault.add_item_under_free_name``) and never takes a
    name *reserved* refuses: the names the Secrets route keeps for Connections,
    WakaTime and, for a remote caller, a channel. A failure is logged without
    detail and answered ``""``: the fill has already landed.
    """
    fields = {
        "username": values.get("username", ""),
        "password": values.get("password", ""),
        "urls": [origin],
        "notes": "",
    }
    try:
        name = await _vault().add_item_under_free_name(
            item_name_for(origin), ITEM_TYPE_LOGIN, fields, reserved
        )
    except Exception:  # noqa: BLE001 -- a failed save never fails the answer; no detail
        logger.warning("vault sign-in: the login could not be saved")
        name = ""
    finally:
        fields.clear()
    return name


# ---- asking to save after the sign-in landed ----------------------------------


#: Is a password box laid out in this document (through open shadow roots)?
#: Disabled and read-only boxes count: a form that locks its fields while it
#: checks the password has not signed in yet.
_PASSWORD_PRESENT_JS = """(function () {
  var found = false;
  var walk = function (root, depth) {
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length && !found; i++) {
      var el = all[i];
      if (el.tagName === 'INPUT' && String(el.type || '').toLowerCase() === 'password') {
        var r = el.getBoundingClientRect(), cs = getComputedStyle(el);
        if (r.width >= 2 && r.height >= 2 && cs.display !== 'none' && cs.visibility !== 'hidden') found = true;
      }
      if (el.shadowRoot && depth < 4) walk(el.shadowRoot, depth + 1);
    }
  };
  walk(document, 0);
  return found;
})()"""


async def password_shown(probe: qa.PageProbe, ask: qa.QuickAsk, frames: Any = None) -> bool | None:
    """Whether a frame of the bound site lays out a password box now, locked or
    not (the save offer's landing check, ``vault_fill.wait_signed_in``). ``None``
    when the tab is on another site or mid-navigation, or a read fails. A page
    between two documents can read ``False``, so the caller trusts only an
    answer that holds."""
    try:
        top_id, _loader, now = await probe.top_frame()
        if not top_id or not now or now != ask.top_origin:
            return None
        for fprobe, frame_id in await qa._login_frames(probe, top_id, frames):
            if frame_id != top_id:
                _fid, _floader, f_origin = await qa._frame_identity(probe, fprobe, frame_id)
                if f_origin != ask.origin:
                    continue
            elif now != ask.origin:
                continue
            ctx = await fprobe.world(frame_id)
            if await fprobe.evaluate(ctx, _PASSWORD_PRESENT_JS) is True:
                return True
    except qa.AskError:
        return None
    return False


async def read_top(probe: qa.PageProbe) -> tuple[str, str, str] | None:
    try:
        return await probe.top_frame()
    except qa.AskError:
        return None


async def wait_signed_in(
    probe: qa.PageProbe,
    ask: qa.QuickAsk,
    frames: Any,
    stop: Callable[[], bool],
    folded: Callable[[], bool] = lambda: False,
    base: tuple[str, str, str] | None = None,
) -> bool:
    """Whether a typed sign-in landed, watched for up to :data:`LAND_WAIT_S`. Reads only.

    Landed: the tab left the bound site, or the bound site has shown no password
    box for :data:`LAND_QUIET_S`. Not landed: a new document shows a password box
    again (the site asked again, as after a wrong password), the time ran out,
    or *stop* says the tab or the ask moved on. When *stop* fires because the
    agent's turn ended (*folded*: the stream is gone, so the page cannot
    be read), the last read decides: a new document with no password box landed.
    *base* is the tab's document read before the fill, so a page that moved on
    during the fill already counts as a new document.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + LAND_WAIT_S
    base = base if base is not None else await read_top(probe)
    clear_since: float | None = None
    new_doc = False
    while not stop():
        top = await read_top(probe)
        now = loop.time()
        if top is not None and top[2]:
            if top[2] != ask.top_origin:
                return True
            new_doc = base is not None and top != base
            shown = await password_shown(probe, ask, frames)
            if shown is False:
                clear_since = now if clear_since is None else clear_since
                if now - clear_since >= LAND_QUIET_S:
                    return True
            elif shown:
                clear_since = None
                if new_doc:
                    return False
        if now >= end:
            return False
        await asyncio.sleep(qa.STEP_POLL_S)
    return folded() and new_doc and clear_since is not None


class SaveOffer:
    """A typed login held in memory until the person answers "Save this login?"."""

    __slots__ = ("id", "origin", "timer", "values")

    def __init__(self, origin: str, values: dict[str, str]) -> None:
        self.id = secrets.token_urlsafe(12)
        self.origin = origin
        self.values = {"username": values.get("username", ""), "password": values["password"]}
        self.timer: asyncio.TimerHandle | None = None

    def clear(self) -> None:
        self.values.clear()
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


#: One held offer per chat session, keyed by its live session key.
_offers: dict[str, SaveOffer] = {}


def drop_offer(session_key: str) -> None:
    """Forget the session's held offer, if any."""
    held = _offers.pop(session_key, None)
    if held is not None:
        held.clear()


def _expire(session_key: str, held: SaveOffer) -> None:
    if _offers.get(session_key) is held:
        drop_offer(session_key)


def hold_offer(session_key: str, origin: str, values: dict[str, str]) -> dict[str, Any]:
    """Hold a copy of *values* for :data:`SAVE_OFFER_TTL_S`; what the sheet shows:
    ``{id, site, username, expires_in_s}``. A newer offer for the session drops the older one."""
    drop_offer(session_key)
    held = SaveOffer(origin, values)
    held.timer = asyncio.get_running_loop().call_later(SAVE_OFFER_TTL_S, _expire, session_key, held)
    _offers[session_key] = held
    return {
        "id": held.id,
        "site": item_name_for(origin),
        "username": held.values["username"],
        "expires_in_s": int(SAVE_OFFER_TTL_S),
    }


def take_offer(session_key: str, offer_id: str) -> SaveOffer | None:
    """The held offer *offer_id*, removed and its timer stopped; ``None`` when it is
    gone or another one. The caller clears it when done."""
    held = _offers.get(session_key)
    if held is None or not secrets.compare_digest(held.id.encode(), offer_id.encode()):
        return None
    del _offers[session_key]
    if held.timer is not None:
        held.timer.cancel()
        held.timer = None
    return held


__all__ = [
    "LAND_QUIET_S",
    "LAND_WAIT_S",
    "OP_FILL",
    "OP_SAVE",
    "SAVE_OFFER_TTL_S",
    "SaveOffer",
    "drop_offer",
    "hold_offer",
    "item_name_for",
    "login_values",
    "offer",
    "read_top",
    "password_shown",
    "save_login",
    "shape",
    "take_offer",
    "url_matches",
    "wait_signed_in",
]
