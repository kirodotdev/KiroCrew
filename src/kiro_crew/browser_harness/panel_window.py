"""A window that a panel's page opens: handed to the XR page by its address, never opened in the panel browser.

The panel browser closes every window that a page opens before it runs
(``panel_browser.py``). That stays the net. In front of it, the panel's tab
asks for no window at all: the page's ``window.open`` and a click on a link
that names another window (``target="_blank"`` and the like) are turned into
one word to the hub, ``window:<the absolute address>``, through the tab's
binding (:data:`panel_page.GUARD_BINDING`). The hub hands an address that the
panel may show to the XR page, which opens it on a panel of its own by the
same rule as an open; any other address gets the panel's refused line
(``panel_hub.PanelHub._window``).

Two scripts, because the page's own ``window.open`` lives in the page's main
world and the binding only in the panel's isolated one: :data:`OPEN_JS` (main
world) dispatches :data:`WINDOW_EVENT` on the document with the address that
the page asked for, and :data:`ASK_JS` (the panel's world) hears it, resolves
it against the page's own address and says it to the hub. A page's own script
can dispatch that event too: it then asks the hub to open a page that the
panel could show anyway, which is the same as a press on a link.
"""

from __future__ import annotations

from .panel_page import GUARD_BINDING

#: The document event that carries a window's address from the page's main world to the panel's world.
WINDOW_EVENT = "__kcPanelWindow"

#: What the panel's world hands the binding for a window: this, then the absolute address.
WINDOW_PREFIX = "window:"

#: The longest address that a window may carry to the hub, in characters.
MAX_WINDOW_URL_CHARS = 2048

#: The page's main world, top document only: ``window.open`` opens nothing and says what was asked for.
OPEN_JS = (
    "(()=>{if(window.top!==window||window.__kcPanelOpen)return;window.__kcPanelOpen=true;"
    "window.open=function(u){try{document.dispatchEvent(new CustomEvent('" + WINDOW_EVENT + "',"
    "{detail:u===undefined||u===null?'':String(u)}));}catch(_){}return null;};})()"
)

#: The panel's world, top document only: a window that the page asked for, and a link that names another window.
ASK_JS = (
    "(()=>{if(window.top!==window||globalThis.__kcPanelAsk)return;globalThis.__kcPanelAsk=true;"
    "const say=u=>{let a;try{a=new URL(u||'about:blank',location.href).href;}catch(_){return;}"
    "if(a.length>" + str(MAX_WINDOW_URL_CHARS) + ")return;"
    "try{globalThis." + GUARD_BINDING + "('" + WINDOW_PREFIX + "'+a);}catch(_){}};"
    "document.addEventListener('"
    + WINDOW_EVENT
    + "',e=>say(typeof e.detail==='string'?e.detail:''),true);"
    "document.addEventListener('click',e=>{const l=e.target&&e.target.closest?e.target.closest('a[href]'):null;"
    "if(!l)return;const t=(l.getAttribute('target')||'').trim().toLowerCase();"
    "if(!t||t==='_self'||t==='_top'||t==='_parent')return;"
    "e.preventDefault();say(l.href);},true);})()"
)


def window_url(payload: object) -> str | None:
    """The address in a binding's payload when it is a window's word, else ``None``."""
    if not isinstance(payload, str) or not payload.startswith(WINDOW_PREFIX):
        return None
    url = payload[len(WINDOW_PREFIX) :]
    return url if 0 < len(url) <= MAX_WINDOW_URL_CHARS else None


__all__ = [
    "ASK_JS",
    "MAX_WINDOW_URL_CHARS",
    "OPEN_JS",
    "WINDOW_EVENT",
    "WINDOW_PREFIX",
    "window_url",
]
