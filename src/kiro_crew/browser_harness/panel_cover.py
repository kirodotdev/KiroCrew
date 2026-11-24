"""Secret fields are drawn covered in a panel's tab, so no picture of them leaves the browser.

A panel's pictures go to the owner's XR page, and from there onto glasses or a
phone that someone else may see. The stream names the boxes of secret fields in
each picture's header (``screencast._MASK_JS``), but a box in a header covers
nothing by itself. So the tab draws such fields covered at the source: a style
sheet in every document of the tab paints their text clear and a hatch over
them, before any picture is taken.

What is covered is what the mask looks for:

* password fields, and fields whose ``autocomplete`` names a password, a
  one-time code or a card's check number;
* the product's own asked fields (``data-kc-asked``);
* fields with a hidden-text style, and fields whose name says secret and that
  hold a value. A style sheet cannot select these, so a short script in the
  panel's own world marks them (``data-kc-cover``). It looks at the whole
  document once, and after that only at what a change added, at a field whose
  own attributes changed, and at the field that is typed into or takes the
  focus: a page that streams an answer does not pay for a walk of its whole
  tree on every frame. A field that script fills with no event is seen at its
  next focus.

The sheet is a constructed one that the document adopts, and a page's script
can take it out of ``document.adoptedStyleSheets``. It is adopted again when
it is found missing: at each sweep, and in front of each question that the
hub asks the page (``panel_page.KEEP_SHEET_JS``).

A secret that the dashboard shows as plain text outside a field (a token on a
settings page) is not a field and stays as the dashboard draws it: the owner
sees it there too.

The same sheet draws a select's list in the page. The browser draws the list
of a ``select`` by itself, in a window of its own: it is not part of the page,
so it is in no moving picture, and a press where a still shows it goes to what
lies behind it. ``appearance: base-select`` on the control and on its picker
makes the list an element of the page, in every picture, and a press on a row
is a click on that ``option``. The style also changes the closed control: it
takes the browser's own ground, border and arrow away and adds an arrow of
its own. So the script marks each select with what the page gave it
(``data-kc-list``: ``own`` when the page drew the control itself with
``appearance: none``, ``auto`` when the browser drew it), and the sheet puts
back what it took: no second arrow beside the page's own, and the browser's
ground and border for a select that the page did not style. A select with
``multiple`` or a ``size`` over one has no list to open and is left alone.
"""

from __future__ import annotations

#: The attribute the script puts on a field that only script can tell is secret.
COVER_ATTR = "data-kc-cover"

_SELECTORS = (
    "input[type=password]",
    "input[autocomplete~=current-password]",
    "input[autocomplete~=new-password]",
    "input[autocomplete~=one-time-code]",
    "input[autocomplete~=cc-csc]",
    "input[data-kc-asked]",
    "textarea[data-kc-asked]",
    f"[{COVER_ATTR}]",
)

#: The attribute the script puts on a select whose list the sheet draws in the page:
#: ``own`` (the page drew the control, with its own arrow) or ``auto``.
LIST_ATTR = "data-kc-list"

_LIST = f"select[{LIST_ATTR}]:not([multiple])"

#: A select's list as a part of the page. Only ``appearance`` and the second
#: arrow are forced: the rest is what the style took from a control that the
#: browser drew, at no weight (``:where``), so the page's own style wins.
LIST_CSS = (
    f"{_LIST},{_LIST}::picker(select){{appearance:base-select!important}}"
    f"{_LIST}{{align-items:center}}"
    f"select[{LIST_ATTR}=own]:not([multiple])::picker-icon{{display:none!important}}"
    f":where(select[{LIST_ATTR}=auto]:not([multiple])){{background-color:Field;color:FieldText;"
    "border:1px solid ButtonBorder;border-radius:2px;padding:1px 4px}"
    # A row that a finger can hit, a list that does not leave the page's view, and
    # an edge of the text's own colour, thinned: it reads in a dark theme and in a light one.
    f"{_LIST} option{{min-block-size:32px;padding-inline:8px;border-radius:4px}}"
    f"{_LIST}::picker(select){{max-block-size:60vh;overflow-y:auto;padding:4px;border-radius:8px;"
    "border:1px solid color-mix(in srgb,currentColor 28%,transparent);"
    "box-shadow:0 8px 24px rgb(0 0 0/.35)}"
)

#: The sheet: the text is not drawn at all (not as dots either, which would give
#: its length away), and a hatch says that something is there.
COVER_CSS = (
    ",".join(_SELECTORS) + "{"
    "color:transparent!important;-webkit-text-fill-color:transparent!important;"
    "text-shadow:none!important;caret-color:transparent!important;"
    "background-image:repeating-linear-gradient(135deg,#59606f 0 6px,#3b4150 6px 12px)!important;"
    "background-color:#3b4150!important}"
    + ",".join(s + "::selection" for s in _SELECTORS)
    + "{color:transparent!important;background:transparent!important}"
    + ",".join(s + "::placeholder" for s in _SELECTORS)
    + "{color:transparent!important;-webkit-text-fill-color:transparent!important}"
    + LIST_CSS
)

#: Installed in every document of the tab, in the panel's own world, before any
#: page script. The sheet is a constructed one, adopted by the document: it
#: needs no element, so it is there before the page has drawn anything.
COVER_JS = (
    "(()=>{if(globalThis.__kcCover)return;globalThis.__kcCover=true;"
    "const ATTR='" + COVER_ATTR + "',LIST='" + LIST_ATTR + "';"
    "const NAMED=/(^|[^a-z])(password|passwd|pwd|otp|totp|mfa|cvv|cvc|secret|token|apikey)([^a-z]|$)/i;"
    "let sheet=null;try{sheet=new CSSStyleSheet();sheet.replaceSync("
    + repr(COVER_CSS)
    + ");}catch(_){sheet=null;}"
    "const adopt=root=>{if(!sheet)return;try{if(root.adoptedStyleSheets.indexOf(sheet)<0)"
    "root.adoptedStyleSheets=[...root.adoptedStyleSheets,sheet];}catch(_){}};"
    "const secret=e=>{const t=getComputedStyle(e).webkitTextSecurity;if(t&&t!=='none')return true;"
    "if(!e.value)return false;"
    "const a=(e.getAttribute('name')||'')+' '+(e.getAttribute('id')||'');"
    "const c=a.replace(/([A-Z]+)([A-Z][a-z])/g,'$1 $2').replace(/([a-z0-9])([A-Z])/g,'$1 $2');"
    "return NAMED.test(a)||NAMED.test(c);};"
    "const field=e=>{if(e.hasAttribute(ATTR)){if(e.type==='password'||!secret(e))e.removeAttribute(ATTR);}"
    "else if(e.type!=='password'&&secret(e))e.setAttribute(ATTR,'');};"
    "const isField=n=>n.nodeType===1&&(n.tagName==='INPUT'||n.tagName==='TEXTAREA');"
    # A select is marked once, with what the page gave it before the sheet changed it.
    "const list=e=>{if(e.hasAttribute(LIST)||e.multiple||e.size>1)return;"
    "e.setAttribute(LIST,getComputedStyle(e).appearance==='none'?'own':'auto');};"
    # A sweep looks at one subtree: the whole document once, then only what a change added.
    "const sweep=(root,d)=>{if(d===0)adopt(document);"
    "if(isField(root))field(root);if(root.tagName==='SELECT')list(root);"
    "if(!root.querySelectorAll)return;"
    "for(const e of root.querySelectorAll('input,textarea'))field(e);"
    "for(const e of root.querySelectorAll('select'))list(e);"
    "if(d>=8)return;"
    "if(root.shadowRoot){adopt(root.shadowRoot);sweep(root.shadowRoot,d+1);}"
    "for(const h of root.querySelectorAll('*'))if(h.shadowRoot){adopt(h.shadowRoot);sweep(h.shadowRoot,d+1);}};"
    "const changed=records=>{for(const r of records){try{"
    "if(r.type==='attributes'){if(isField(r.target))field(r.target);}"
    "else for(const n of r.addedNodes)if(n.nodeType===1)sweep(n,0);}catch(_){}}};"
    "const one=e=>{try{if(e.target&&isField(e.target))field(e.target);}catch(_){}};"
    "adopt(document);"
    # A page's script can empty the document's list of adopted sheets. The sheet
    # is put back at each sweep, and whenever the hub asks the page a question.
    "globalThis.__kcCoverKeep=()=>{adopt(document);return true;};"
    "addEventListener('input',one,true);addEventListener('focusin',one,true);"
    "const start=()=>{try{sweep(document,0);}catch(_){}try{new MutationObserver(changed).observe(document,"
    "{subtree:true,childList:true,attributes:true,attributeFilter:['type','name','id','style','class']});}catch(_){}};"
    "if(document.documentElement)start();else addEventListener('DOMContentLoaded',start,{once:true});"
    "})()"
)

__all__ = ["COVER_ATTR", "COVER_CSS", "COVER_JS", "LIST_ATTR", "LIST_CSS"]
