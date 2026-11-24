"""What a panel tab may open, and the scripts the gateway runs inside it.

A panel tab shows one of the dashboard's own pages to the owner's XR page
(``panel_hub.py``). Two things about that page are decided here, and both are pure:

* **The address rule.** A panel opens this gateway's own origin only, and only
  a path of one of the fixed shapes in :data:`PATH_SHAPES`. :func:`allowed_path`
  answers for a path, :func:`allowed_url` for a full address (a redirect, a link
  the page followed).
* **The in-page scripts.** They run in an isolated world (:data:`PANEL_WORLD`),
  so page script can neither replace the DOM functions they call nor remove
  their listeners. :data:`GUARD_JS` is installed before any page script: it
  stops a trusted press or key on a control that grants an agent permission, at
  the event itself, and answers the three fixed questions a panel asks.

Both are written here as they hold with the guard on
(``browser.xr_pages_guarded``). The guard is off by default, and each function
that decides takes ``guarded``: with it false any path of the origin opens but
the ones in :data:`SHUT_SHAPES`, no page is for looking at only, the grant mark
is not looked at, and of the acting parameters only ``token`` is refused.

The grant mark is the dashboard's own: ``data-agent-grant`` on a control
(``no`` on a control inside a trust region that grants nothing) and
``data-guide-trust-root`` on a whole region of the agent's own ceiling. The
nearest mark wins.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Collection, Mapping
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

#: The isolated world every panel script runs in.
PANEL_WORLD = "kirocrew_panel"

#: The binding the guard calls when it stops a press; it exists in
#: :data:`PANEL_WORLD` only, so page script cannot call it.
GUARD_BINDING = "__kcPanelGuarded"

#: What the move guard hands that binding for a move it stopped.
MOVE_STOPPED = "move"

#: The dashboard's mark on a control that grants an agent permission.
GRANT_ATTR = "data-agent-grant"
#: The XR page's own mark on a question card's Dismiss and Submit
#: (`QuestionCard.tsx`): controls that answer an ask and carry no grant mark.
#: Nothing but :data:`GUARD_JS`'s `grants()` reads it; the guard does not.
ANSWER_ATTR = "data-xr-answer"
#: The dashboard's mark on a region of the agent's own ceiling.
TRUST_ROOT_ATTR = "data-guide-trust-root"

#: The pages a panel may show, as whole-path patterns: the pages a person works
#: with. Nothing else opens, and a route that is added later is refused until
#: its shape is put here. Left out on purpose: the XR page itself, the pages
#: that publish (``/deploy``, ``/artifacts/deploy``, ``/artifacts/remote/...``),
#: every app's page, the pages that set up who or what reaches the gateway, and
#: ``/knowledge``, which only leads on to one of those. Three Settings tabs
#: are left out. ``people`` and ``connections`` act as they open: the one
#: admits the people who accepted an invite, the other starts the enabled MCP
#: servers and a sign-in helper. A page whose opening gives an agent more, or
#: widens who may reach the gateway, is not a panel's to open. ``instances``
#: and ``connections`` are also where the routes ``/instances`` and
#: ``/connections`` lead, and those are not on the list.
#: ``streamPath.ts`` holds the same patterns for the XR page, and a test of
#: the page reads them from here.
PATH_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"/chat(/[A-Za-z0-9_.:-]{1,128})?"),
    re.compile(r"/members"),
    re.compile(r"/notifications"),
    re.compile(r"/sessions"),
    re.compile(r"/session-dashboards"),
    re.compile(r"/overview"),
    re.compile(r"/crew-board"),
    re.compile(r"/schedule"),
    re.compile(r"/projects(/[A-Za-z0-9_.:~-]{1,128}){0,6}"),
    re.compile(r"/tasks"),
    re.compile(r"/artifacts(/(?!deploy$)[A-Za-z0-9_.-]{1,128})?"),
    re.compile(r"/logs"),
    re.compile(
        r"/settings(/(?!(people|connections|instances)(/|$))[a-z0-9-]{1,64}(/[a-z0-9-]{1,64}){0,2})?"
    ),
)

#: The pages a panel shows for looking at only: the Task Runner and Schedule.
#: Their own buttons approve a held step, execute a planned run, tick
#: "auto-approve tool calls" and run a job now, each at one plain press, and
#: they carry no grant mark: a mark is read by the dashboard's own guide too,
#: and would close those pages to it. While a panel's tab stands at one of
#: these paths the hub sends it no press, no key and no text
#: (:func:`look_only`); the wheel and a pointer's move pass, so the person
#: reads and scrolls.
LOOK_ONLY_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"/projects(/.*)?"),
    re.compile(r"/tasks(/.*)?"),
    re.compile(r"/schedule(/.*)?"),
)

#: What stays out while the guard is off (``browser.xr_pages_guarded``): every
#: other path of the gateway's own origin then opens on a panel. The XR page
#: itself (a room inside a room), the pages that publish, and the pages that
#: admit people or link other machines: what they do reaches other people or
#: other machines. Matched against the path as the dashboard's router reads it
#: (:func:`_as_routed`). ``streamPath.ts`` holds the same patterns.
SHUT_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"/xr(/.*)?"),
    re.compile(r"/deploy(/.*)?"),
    re.compile(r"/artifacts/(deploy|remote)(/.*)?"),
    re.compile(r"/settings/(people|instances)(/.*)?"),
    re.compile(r"/instances(/.*)?"),
)

#: An address a panel is asked to open while the guard is off: printable
#: characters with no space, as a browser writes an address.
_PLAIN_ADDRESS = re.compile(r"/[\x21-\x7e]*")

#: The one parameter that stays refused while the guard is off: ``token``
#: sends words that nobody typed, and no button of the dashboard needs it.
UNGUARDED_LAUNCH_PARAMS = frozenset({"token"})

#: The parameters of an address that make the dashboard act with no press:
#: ``token`` opens a session and sends the words the token carries,
#: ``autoSend`` sends what waits in the message box, and ``autoRun`` (with
#: ``applied``) executes a planned run of the Task Runner. A note that an
#: agent raises carries buttons whose address the agent chooses, so one tap
#: on any words would otherwise be that act. ``applied`` alone only selects a
#: run, and passes. Compared in lower case, after the browser's own decoding;
#: the pages read these from the query alone, so the part after ``#`` is not
#: looked at.
LAUNCH_PARAMS = frozenset({"token", "autosend", "autorun"})

#: The Settings page, and the parameter it takes its tab from when its path
#: names none. It draws that tab's pane at once, before it rewrites its address
#: to ``/settings/<tab>``, so a tab that the list leaves out would be drawn, and
#: would have done what it does as it opens, by ``/settings?tab=<tab>``. On the
#: bare path a ``tab`` passes only when ``/settings/<its value>`` is on the
#: list; given twice, every value must pass. The name is compared in lower
#: case, which is stricter than the page, which reads ``tab`` alone.
SETTINGS_PATH = "/settings"
TAB_PARAM = "tab"

#: Longest path a panel may name.
MAX_PATH_CHARS = 300

#: The Bots page, and the one parameter a panel may open it with: the name of
#: the Bot whose page it is. ``streamPath.ts`` builds the same address.
MEMBERS_PATH = "/members"
MEMBER_PARAM = "member"
#: Longest address a panel may be opened at: a path, or the Bots page with a
#: Bot's name, whose every byte may be three characters once encoded.
MAX_ADDRESS_CHARS = 2000

#: Most preference bytes (keys plus values) the XR page may hand its tab.
MAX_PREF_BYTES = 200_000
MAX_PREF_KEYS = 400
MAX_PREF_VALUE_CHARS = 16_000


def _as_routed(path: str) -> str:
    """*path* as the dashboard's router reads it.

    The router decodes a path once, takes no notice of case, of a slash at the
    end or of two slashes in a row. So ``/XR``, ``/xr/`` and ``/%78r`` are all
    the XR page, and a pattern is matched against this one spelling. An
    encoded slash is read as a slash here, which the router does not do: that
    only shuts more.
    """
    plain = re.sub(r"/+", "/", unquote(path).lower())
    return plain.rstrip("/") or "/"


def allowed_path(path: Any, *, guarded: bool = True) -> str | None:
    """*path* when a panel may show it, else ``None``.

    With the guard on, one of the fixed shapes. With it off, any path but the
    ones in :data:`SHUT_SHAPES`.
    """
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
        return None
    # A dot segment is a path the browser rewrites into another one.
    if any(segment in (".", "..") for segment in path.split("/")):
        return None
    if guarded:
        return path if any(shape.fullmatch(path) for shape in PATH_SHAPES) else None
    if not path.startswith("/"):
        return None
    routed = _as_routed(path)
    if any(segment in (".", "..") for segment in routed.split("/")):
        return None
    return None if any(shape.fullmatch(routed) for shape in SHUT_SHAPES) else path


def _is_member_name(name: str) -> bool:
    """Whether *name* has the shape the product gives a Bot's name."""
    from kiro_crew.members_parts.member_space import is_valid_member_name

    return is_valid_member_name(name)


def _member_address(name: str) -> str:
    """The Bots page with one Bot named, in the gateway's own spelling: every reserved character encoded."""
    return f"{MEMBERS_PATH}?{MEMBER_PARAM}={quote(name, safe='')}"


def _unguarded_open(address: str, known: Collection[str] | None) -> str | None:
    """The address a panel may be opened at while the guard is off, else ``None``.

    A path that :func:`allowed_path` lets through with the guard off, with any
    query that :func:`query_refused` does not refuse. One rule of the guarded
    way holds here too, because it is about which page comes up and not about
    what a press may do: a Bot that the Bots page is asked for by name is one
    of the owner's, so a panel never shows another Bot than the one it names.
    """
    if not _PLAIN_ADDRESS.fullmatch(address):
        return None
    path, mark, query = address.partition("?")
    if allowed_path(path, guarded=False) is None:
        return None
    if not mark:
        return address
    if query_refused(address, guarded=False):
        return None
    if _as_routed(path) == MEMBERS_PATH:
        names = [value for key, value in parse_qsl(query, True) if key == MEMBER_PARAM]
        if names and (known is None or any(name not in known for name in names)):
            return None
    return address


def allowed_open(
    address: Any, known: Collection[str] | None = None, *, guarded: bool = True
) -> str | None:
    """The address a panel may be opened at, in the gateway's own spelling, else ``None``.

    With the guard off (*guarded* false) an address that passes the rule below
    is spelled as below, and any other is taken by :func:`_unguarded_open`.
    With it on:


    A path of the list with nothing after it, as :func:`allowed_path` has it.
    Or the Bots page with exactly one query, ``member=<name>``: the page of one
    Bot. Without a name the Bots page shows whichever Bot that browser showed
    last, so a panel that is opened for a Bot names it.

    *known* is the owner's Bots as the gateway holds them: the name must be one
    of them. With ``None`` (they could not be read) no name passes: the shape
    of a name is nearly any one line of text, and for a name that is nobody's
    the Bots page opens the Bot it showed last. Any other key, a second pair, a
    fragment or a name that cannot be read is refused: the Bots page acts on
    other keys of its query as it opens. The address that comes back is built
    here from the decoded name, never copied from the caller.
    """
    if not isinstance(address, str) or not address or len(address) > MAX_ADDRESS_CHARS:
        return None
    if "#" in address:
        return None
    listed = _guarded_open(address, known)
    if listed is not None or guarded:
        return listed
    return _unguarded_open(address, known)


def _guarded_open(address: str, known: Collection[str] | None) -> str | None:
    """:func:`allowed_open` with the guard on, for an address of a fit length with no fragment."""
    path, mark, query = address.partition("?")
    clean = allowed_path(path)
    if clean is None or not mark:
        return clean
    if clean != MEMBERS_PATH:
        return None
    try:
        pairs = parse_qsl(query, True, True, errors="strict", max_num_fields=1)
    except ValueError:
        return None
    if len(pairs) != 1 or pairs[0][0] != MEMBER_PARAM:
        return None
    name = pairs[0][1]
    if known is None or name not in known or not _is_member_name(name):
        return None
    return _member_address(name)


def address_of(url: Any) -> str:
    """What a panel is told that its tab shows: the path, and on the Bots page the one Bot it names.

    Nothing else of a query is told. A Bots page whose query names no Bot, or
    names two, is told as the bare path.
    """
    path = path_of(url)
    if path != MEMBERS_PATH:
        return path
    try:
        pairs = parse_qsl(urlsplit(url).query, True, errors="strict")
    except ValueError:
        return path
    names = [value for key, value in pairs if key == MEMBER_PARAM]
    if len(names) == 1 and _is_member_name(names[0]):
        return _member_address(names[0])
    return path


def look_only(path: Any, *, guarded: bool = True) -> bool:
    """Whether a panel whose tab stands at *path* takes no press, key or text.

    A path that is not known, or is not one a panel may show, counts as one:
    where the hub cannot say that the tab shows a page that a panel may show,
    nothing is pressed on it. With the guard off no other page is one.
    """
    if allowed_path(path, guarded=guarded) is None:
        return True
    return guarded and any(shape.fullmatch(path) for shape in LOOK_ONLY_SHAPES)


def origin_of(url: Any) -> str | None:
    """``scheme://host:port`` of *url* (an IPv6 host in brackets), or ``None``."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if not host or port is None or parts.username or parts.password:
        return None
    return f"{parts.scheme}://{'[' + host + ']' if ':' in host else host}:{port}"


def loopback_origin(host: Any, port: Any) -> str | None:
    """The gateway's own origin on the loopback address a request was accepted on.

    A panel's tab reaches the gateway from this host, so it is a loopback caller.
    The sign-in gate pins a session to the address it was opened from: the tab
    presents the owner's session from the same loopback address the owner's own
    socket came in on, and from nowhere else. A socket accepted on any other
    address gets no panel.
    """
    if not isinstance(host, str) or isinstance(port, bool) or not isinstance(port, int):
        return None
    if not 0 < port < 65536:
        return None
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if not address.is_loopback:
        return None
    return f"http://{'[' + str(address) + ']' if address.version == 6 else address}:{port}"


def allowed_url(url: Any, origin: str, *, guarded: bool = True) -> bool:
    """Whether *url* is a page a panel may show on *origin* (``scheme://host:port``)."""
    if not origin or origin_of(url) != origin:
        return False
    return allowed_path(urlsplit(url).path or "/", guarded=guarded) is not None


def carries_launch(url: Any, *, guarded: bool = True) -> bool:
    """Whether *url* carries a parameter that makes the dashboard act with no press.

    A panel's tab goes to no such address: not by a load, in the tab or in a
    frame, and not by a move inside the page. Nobody pressed anything for what
    it would start. Any other query passes. An address that cannot be read
    counts as one that carries it. With the guard off the dashboard's own
    sends pass, and only :data:`UNGUARDED_LAUNCH_PARAMS` is refused.
    """
    if not isinstance(url, str):
        return True
    try:
        query = urlsplit(url).query
    except ValueError:
        return True
    refused = LAUNCH_PARAMS if guarded else UNGUARDED_LAUNCH_PARAMS
    return any(name.strip().lower() in refused for name, _value in parse_qsl(query, True))


def names_unlisted_tab(url: Any, *, guarded: bool = True) -> bool:
    """Whether *url* is the bare Settings path with a ``tab`` that a panel may not show.

    An address that cannot be read counts as one that does. With the guard off
    the Settings path is compared as the router reads it, since any spelling
    of it is then a path that opens.
    """
    if not isinstance(url, str):
        return True
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    if (parts.path if guarded else _as_routed(parts.path)) != SETTINGS_PATH:
        return False
    return any(
        name.strip().lower() == TAB_PARAM
        and allowed_path(f"{SETTINGS_PATH}/{value}", guarded=guarded) is None
        for name, value in parse_qsl(parts.query, True)
    )


def query_refused(url: Any, *, guarded: bool = True) -> bool:
    """Whether the query of *url* asks for what a panel's tab does not open.

    The one question for a load in the tab, a load in a frame and a move
    inside the page: :func:`carries_launch` or :func:`names_unlisted_tab`.
    """
    return carries_launch(url, guarded=guarded) or names_unlisted_tab(url, guarded=guarded)


def path_of(url: Any) -> str:
    """The path of *url* and nothing else of it: what a panel says that its tab shows."""
    try:
        return urlsplit(url).path or "/" if isinstance(url, str) else ""
    except ValueError:
        return ""


def clean_prefs(raw: Any) -> dict[str, str]:
    """The XR page's own stored preferences, bounded, for its tab to start with."""
    out: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return out
    budget = MAX_PREF_BYTES
    for key, value in raw.items():
        if len(out) >= MAX_PREF_KEYS:
            break
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if not key or len(key) > 200 or len(value) > MAX_PREF_VALUE_CHARS:
            continue
        budget -= len(key) + len(value)
        if budget < 0:
            break
        out[key] = value
    return out


def prefs_script(prefs: Mapping[str, str]) -> str:
    """Page-world script that puts *prefs* into an empty ``localStorage``.

    The tab's browser starts with nothing, so without the owner's theme and the
    notes already read it would paint the light theme and its first-run dialogs.
    """
    data = json.dumps(dict(prefs)).replace("<", "\\u003c")
    return (
        "(()=>{try{const p=" + data + ";"
        "for(const k of Object.keys(p)){if(localStorage.getItem(k)===null)localStorage.setItem(k,p[k]);}"
        "}catch(_){}})()"
    )


#: The guard and the three readers. One object, ``__kcPanel``, in the isolated world.
#:
#: ``guarded(el)`` walks up from *el* through shadow hosts: the first element
#: that carries the grant mark decides (``no`` passes), else a trust region
#: decides (guarded). ``focus()`` says the same of the element that holds the
#: focus, so the hub can hold text and keys back from a guarded surface without
#: a second question. Text is stopped at the event too: ``Input.insertText``
#: fires no key event, but the field's ``beforeinput`` is trusted and can be
#: cancelled, and a paste or a drop onto a guarded field ends the same way.
#: The capture listeners sit on ``window`` and are added
#: before any page script, so they run first; only trusted events are stopped,
#: which is every event the gateway dispatches for a hand.
GUARD_JS = (
    "(()=>{if(globalThis.__kcPanel)return;"
    "const GRANT='" + GRANT_ATTR + "',ROOT='" + TRUST_ROOT_ATTR + "',ANSWER='" + ANSWER_ATTR + "';"
    "const up=n=>n.parentElement||(n.getRootNode&&n.getRootNode().host)||null;"
    "const OFF=globalThis.__kcPanelUnguarded===true;"
    "const guarded=el=>{if(OFF)return false;for(let n=el;n;n=up(n)){if(n.nodeType!==1)continue;"
    "if(n.hasAttribute(GRANT))return n.getAttribute(GRANT)!=='no';"
    "if(n.hasAttribute(ROOT))return true;}return false;};"
    "const at=e=>{const p=e.composedPath?e.composedPath():[];let t=p.length?p[0]:e.target;"
    "if(t&&t.nodeType!==1)t=t.parentElement;return t&&t.nodeType===1?t:null;};"
    "const stop=e=>{if(!e.isTrusted)return;const t=at(e);if(!t||!guarded(t))return;"
    "e.stopImmediatePropagation();e.preventDefault();"
    "try{globalThis." + GUARD_BINDING + "(e.type);}catch(_){}};"
    "for(const type of ['pointerdown','mousedown','pointerup','mouseup','click','auxclick',"
    "'dblclick','contextmenu','keydown','keypress','keyup','touchstart','touchend',"
    "'beforeinput','paste','drop'])"
    "window.addEventListener(type,stop,true);"
    "const TAKE='a[href],button,summary,select,input,textarea,label,[role=button],[role=tab],"
    "[role=switch],[role=checkbox],[role=radio],[role=menuitem],[role=menuitemradio],"
    '[role=menuitemcheckbox],[role=option],[role=link],[role=combobox],[contenteditable=""],'
    '[contenteditable=true],[tabindex]:not([tabindex="-1"])\';'
    "const TEXT='input:not([type=button]):not([type=submit]):not([type=checkbox]):not([type=radio])"
    ':not([type=range]),textarea,[contenteditable=""],[contenteditable=true]\';'
    "const box=r=>[Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)];"
    "const deep=(x,y)=>{let el=document.elementFromPoint(x,y);"
    "for(let i=0;i<8&&el&&el.shadowRoot;i++){const inner=el.shadowRoot.elementFromPoint(x,y);"
    "if(!inner||inner===el)break;el=inner;}return el;};"
    # A list or a picker that the browser draws by itself, open now: it is not in
    # the page, so a press where a picture shows it would go to what lies behind
    # it. A select whose list the panel's sheet draws in the page is not one:
    # the control and its picker both have the sheet's ``appearance``, and a
    # page that takes it from either gets the browser's own list back. A
    # browser that does not know ``:open`` throws here, and the answer is no.
    "const inPage=(e,part)=>getComputedStyle(e,part).appearance==='base-select';"
    "const drawnOutside=e=>e.tagName==='INPUT'||!inPage(e)||!inPage(e,'::picker(select)');"
    "const picker=()=>{try{let e=document.activeElement;"
    "for(let i=0;i<8&&e&&e.shadowRoot&&e.shadowRoot.activeElement;i++)e=e.shadowRoot.activeElement;"
    "if(e&&e.matches&&e.matches('select:open,input:open')&&drawnOutside(e))return true;"
    "for(const o of document.querySelectorAll('select:open,input:open'))if(drawnOutside(o))return true;"
    "return false;}catch(_){return false;}};"
    "const target=(x,y)=>{if(picker())return {rect:[0,0,0,0],kind:'',label:'',guarded:false,picker:true};"
    "const el=deep(x,y);if(!el)return null;"
    "const hit=el.closest?el.closest(TAKE):null;const g=guarded(el);"
    "if(!hit)return g?{rect:box(el.getBoundingClientRect()),kind:'',label:'',guarded:true}:null;"
    "const r=hit.getBoundingClientRect();if(r.width<2||r.height<2)return null;"
    "const label=(hit.getAttribute('aria-label')||hit.getAttribute('title')||"
    "hit.getAttribute('placeholder')||hit.innerText||'').trim().replace(/\\s+/g,' ').slice(0,60);"
    "return {rect:box(r),kind:hit.matches(TEXT)?'text':'button',label:label,guarded:g||guarded(hit)};};"
    "const focus=()=>{let e=document.activeElement;"
    "for(let i=0;i<8&&e&&e.shadowRoot&&e.shadowRoot.activeElement;i++)e=e.shadowRoot.activeElement;"
    "if(!e||e===document.body)return {editable:false,kind:'',rect:null,guarded:false};"
    "const editable=e.isContentEditable||((e.tagName==='TEXTAREA'||e.tagName==='INPUT')&&!e.readOnly&&!e.disabled&&e.matches(TEXT));"
    "const kind=e.tagName==='IFRAME'?'frame':(e.tagName==='INPUT'?(e.type||'text').toLowerCase():"
    "(e.tagName==='TEXTAREA'?'textarea':(e.isContentEditable?'text':'')));"
    "return {editable:!!editable||kind==='frame',kind:kind,rect:box(e.getBoundingClientRect()),"
    "guarded:guarded(e)};};"
    # Where the controls that answer an ask are drawn in the window now: those
    # that carry the grant mark, whatever it says (an approval's card), and a
    # question card's Dismiss and Submit, which carry the XR page's own mark.
    # The same with the guard on and off. The XR page reads from it that such a
    # card came, went or moved. shortcut: 40 at most, and none inside a shadow root;
    # widen when a page draws a marked control there.
    "const grants=()=>{const out=[];for(const el of document.querySelectorAll('['+GRANT+'],['+ANSWER+']')){"
    "const r=el.getBoundingClientRect();"
    "if(r.width<2||r.height<2||r.bottom<=0||r.right<=0||r.top>=innerHeight||r.left>=innerWidth)continue;"
    "if(el.checkVisibility&&!el.checkVisibility({visibilityProperty:true}))continue;"
    "out.push(box(r));if(out.length>=40)break;}return out;};"
    "const state=()=>{let s=document.scrollingElement,best=0;"
    "for(const el of document.querySelectorAll('body *')){if(el.scrollHeight<=el.clientHeight+8)continue;"
    "const o=getComputedStyle(el).overflowY;if(o!=='auto'&&o!=='scroll')continue;"
    "const a=el.clientWidth*el.clientHeight;if(a>best){best=a;s=el;}}"
    "return {path:location.pathname,title:document.title.slice(0,120),"
    "theme:document.documentElement.dataset.theme||'',"
    "scrollTop:Math.round(s?s.scrollTop:0),scrollMax:Math.round(s?s.scrollHeight-s.clientHeight:0),"
    "words:(document.body?document.body.innerText||'':'').replace(/\\s+/g,' ').length,"
    "grants:grants()};};"
    "let still=null;"
    "const caret=on=>{if(!still){still=document.createElement('style');"
    "still.textContent='*{caret-animation:manual !important}';}"
    "const root=document.head||document.documentElement;"
    "if(on&&!still.isConnected&&root)root.appendChild(still);if(!on&&still.isConnected)still.remove();"
    "return on;};"
    "globalThis.__kcPanel={target:target,focus:focus,state:state,caret:caret};})()"
)


def guard_script(*, guarded: bool = True) -> str:
    """:data:`GUARD_JS` as a panel's tab gets it.

    With the guard off one word stands in front of it, in the isolated world
    that page script cannot reach: the walk for a grant mark then answers no
    for every element, so no press, key or text is stopped and the readers say
    ``guarded: false`` of everything.
    """
    return GUARD_JS if guarded else "globalThis.__kcPanelUnguarded=true;" + GUARD_JS


#: In front of every question: the panel's sheet is adopted again when the page
#: took it out of ``document.adoptedStyleSheets`` (``panel_cover.COVER_JS``).
#: Without it a page's script that empties that list uncovers every secret
#: field, and nothing notices.
KEEP_SHEET_JS = "globalThis.__kcCoverKeep&&globalThis.__kcCoverKeep()"


def question_js(question: str, x: float = 0.0, y: float = 0.0) -> str | None:
    """The expression that answers one of the three fixed questions, or ``None``.

    It answers ``false`` when the reader is not in the page, which is not the
    same as ``null`` ("nothing takes a press here"): the hub sends no press, key
    or text on ``false``. Each question first puts the panel's sheet back
    (:data:`KEEP_SHEET_JS`).
    """
    if question == "target":
        ask = f"target({float(x)!r},{float(y)!r})"
    elif question in ("focus", "state"):
        ask = f"{question}()"
    else:
        return None
    return f"({KEEP_SHEET_JS},globalThis.__kcPanel?globalThis.__kcPanel.{ask}:false)"


#: The move guard: an in-page move of the tab's own page to a path off the
#: list is stopped before it happens.
#:
#: The dashboard is one document, and a press on a link or a tab moves it with
#: no load, so the hub's pause never sees that move. Seen afterwards
#: (``Page.navigatedWithinDocument``) the page is already drawn, and what it
#: does as it opens is done. The browser tells every such move to
#: ``navigation`` first, where it can still be cancelled; the dashboard's
#: router then reads the address, finds it unchanged, and stays. A load is the
#: hub's pause's to decide, a step in the history leads to a page that was let
#: through before, and a frame's own moves are its own. A move to a listed
#: path whose query carries one of :data:`LAUNCH_PARAMS` is stopped the same
#: way: the names are read as the page would read them (``URLSearchParams``).
#: So is a move to the bare Settings path with a ``tab`` that the list leaves
#: out (:data:`TAB_PARAM`). With no list in front of it the script lets no
#: move through.
MOVE_JS = (
    "(()=>{const nav=globalThis.navigation;"
    "if(!nav||globalThis.__kcPanelMoves||window.top!==window)return;"
    "globalThis.__kcPanelMoves=true;"
    "const SHAPES=(globalThis.__kcPanelShapes||[]).map(s=>new RegExp('^(?:'+s+')$'));"
    "const SHUT=Array.isArray(globalThis.__kcPanelShut)"
    "?globalThis.__kcPanelShut.map(s=>new RegExp('^(?:'+s+')$')):null;"
    "const LAUNCH=new Set(globalThis.__kcPanelLaunch||[]);"
    "const dots=p=>p.split('/').some(s=>s==='.'||s==='..');"
    "const routed=p=>{let d;try{d=decodeURIComponent(p);}catch(_){return null;}"
    "return d.toLowerCase().replace(/\\/+/g,'/').replace(/\\/+$/,'')||'/';};"
    "const open=p=>{const r=routed(p);return r!==null&&!dots(r)&&!SHUT.some(x=>x.test(r));};"
    "const listed=p=>p.length<=300&&!dots(p)&&(SHUT?open(p):SHAPES.some(r=>r.test(p)));"
    "const launches=u=>{for(const k of u.searchParams.keys())"
    "if(LAUNCH.has(k.trim().toLowerCase()))return true;return false;};"
    "const tab=u=>{if(u.pathname!=='" + SETTINGS_PATH + "'"
    "&&!(SHUT&&routed(u.pathname)==='" + SETTINGS_PATH + "'))return false;"
    "for(const [k,v] of u.searchParams)"
    "if(k.trim().toLowerCase()==='" + TAB_PARAM + "'&&!listed('" + SETTINGS_PATH + "/'+v))"
    "return true;return false;};"
    "nav.addEventListener('navigate',e=>{"
    "if(!e.cancelable||(e.navigationType!=='push'&&e.navigationType!=='replace'))return;"
    "if(!e.destination||!e.destination.sameDocument)return;"
    "let u;try{u=new URL(e.destination.url);}catch(_){return;}"
    "if(u.origin!==location.origin)return;"
    "if(listed(u.pathname)&&!launches(u)&&!tab(u))return;"
    "e.preventDefault();"
    "try{globalThis." + GUARD_BINDING + "('" + MOVE_STOPPED + "');}catch(_){}"
    "});})()"
)


def move_guard_script(*, guarded: bool = True) -> str:
    """:data:`MOVE_JS` with what it refuses in front of it.

    With the guard on, the list of shapes and the acting parameters. With it
    off, the paths that stay shut (:data:`SHUT_SHAPES`) and the one parameter
    that stays refused.
    """
    if guarded:
        shapes = json.dumps([shape.pattern for shape in PATH_SHAPES])
        launch = json.dumps(sorted(LAUNCH_PARAMS))
        head = "globalThis.__kcPanelShapes=" + shapes + ";"
    else:
        shut = json.dumps([shape.pattern for shape in SHUT_SHAPES])
        launch = json.dumps(sorted(UNGUARDED_LAUNCH_PARAMS))
        head = "globalThis.__kcPanelShut=" + shut + ";"
    return head + "globalThis.__kcPanelLaunch=" + launch + ";" + MOVE_JS


def caret_js(still: bool) -> str:
    """Hold the text caret still (no blink) or let it blink again."""
    return f"globalThis.__kcPanel?globalThis.__kcPanel.caret({'true' if still else 'false'}):null"


__all__ = [
    "ANSWER_ATTR",
    "GRANT_ATTR",
    "GUARD_BINDING",
    "GUARD_JS",
    "LAUNCH_PARAMS",
    "LOOK_ONLY_SHAPES",
    "MAX_PATH_CHARS",
    "MOVE_JS",
    "MOVE_STOPPED",
    "PANEL_WORLD",
    "PATH_SHAPES",
    "SETTINGS_PATH",
    "SHUT_SHAPES",
    "TAB_PARAM",
    "TRUST_ROOT_ATTR",
    "UNGUARDED_LAUNCH_PARAMS",
    "allowed_path",
    "allowed_url",
    "caret_js",
    "carries_launch",
    "clean_prefs",
    "guard_script",
    "look_only",
    "loopback_origin",
    "move_guard_script",
    "names_unlisted_tab",
    "origin_of",
    "path_of",
    "prefs_script",
    "query_refused",
    "question_js",
]
